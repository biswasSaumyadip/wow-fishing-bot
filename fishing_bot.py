"""
WoW Fishing Bot -- YOLO-based bobber/splash detection with a debug GUI.

Run:  python fishing_bot.py
Needs: best.pt (your trained YOLO model) in the same folder as this file.
"""
import os
import sys
import math
import time
import ctypes
import subprocess
import traceback
import importlib
from collections import deque
from multiprocessing import Process, Queue, Lock, freeze_support

# ====================== DEPENDENCIES ======================
# pip package name -> module name that is actually imported. The old code did
# __import__("opencv_python") / __import__("pywin32"), which can never succeed,
# so every launch re-ran pip install for those packages.
REQUIRED = {
    "opencv-python": "cv2",
    "numpy": "numpy",
    "ultralytics": "ultralytics",
    "pywin32": "win32gui",
    "mss": "mss",
    "customtkinter": "customtkinter",
    "PyAudioWPatch": "pyaudiowpatch",  # WASAPI loopback capture for splash-sound detection
}


def ensure_dependencies():
    missing = []
    for pkg, module in REQUIRED.items():
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(pkg)
    if "PyAudioWPatch" in missing:
        # Optional: only needed for sound mode; the bot falls back to vision if absent.
        missing.remove("PyAudioWPatch")
        if sys.platform == "win32":
            try:
                print("Installing PyAudioWPatch (splash-sound detection)...")
                subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "PyAudioWPatch"])
            except Exception as e:
                print(f"Could not install PyAudioWPatch ({e}); sound mode will be unavailable.")
    if missing:
        print(f"Installing missing packages: {', '.join(missing)}")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", *missing])
        if "pywin32" in missing:
            # pywin32 needs its post-install step before win32gui can be imported.
            try:
                subprocess.check_call([sys.executable, "-m", "pywin32_postinstall", "-install"])
            except Exception:
                pass


ensure_dependencies()

import cv2 as cv  # noqa: E402
import numpy as np  # noqa: E402
import win32gui  # noqa: E402
import win32con  # noqa: E402
import win32api  # noqa: E402
import win32process  # noqa: E402
from mss import mss  # noqa: E402

# Make the process DPI-aware so screen/client coordinate math isn't thrown off
# by Windows display scaling. Must run before any window is created, and in
# every process (the bot workers are spawned as fresh interpreters).
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

# ====================== TUNABLES ======================
IMGSZ = 1280          # inference size; lower (960/640) if "inference ms" in the log is > ~400 ms
FLOOR_CONF = 0.10     # minimum confidence to even report a box (so near-misses are visible in the log)
BASELINE_MARGIN = 0.15  # a splash must beat the idle-bobber confidence by at least this much
VERIFY_DELAY = 1.0    # seconds to wait after a click before checking whether the bobber vanished
VERIFY_RADIUS = 90    # px: a box this close to the click position means "bobber still there"
VERIFY_MIN_CONF = 0.25  # ignore very weak boxes during that check (e.g. the loot window popping up)
BOBBER_MIN_CONF = 0.30  # minimum 'Bobber' confidence to trust a position fix
TRACK_INTERVAL = 0.4  # seconds between bobber position updates in sound mode
AUDIO_CHUNK = 1024    # loopback frames per level sample (~21 ms at 48 kHz)
SILENCE_DB = -90.0

DEFAULTS = {
    "hours": 6.0,
    "mode": "sound",      # "sound" (splash sound + bobber tracking) or "vision" (CatchClick class)
    "sound_db": 12.0,     # splash must be this many dB louder than the ambient measured after the cast
    "conf": 0.5,
    "watch_seconds": 25.0,
    "min_bite_delay": 4.0,
    "required_hits": 2,
    "cast_key": "1",
    "shift_click": False,
    "adaptive": True,
}


class bcolors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    ENDC = '\033[0m'


# ====================== REAL INPUT (SendInput) ======================
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
KEYEVENTF_KEYUP = 0x0002
VK_SHIFT = 0x10
VK_MENU = 0x12
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_ulong),
        ("wParamL", ctypes.c_ushort),
        ("wParamH", ctypes.c_ushort),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]


def _send_inputs(*inputs):
    arr = (INPUT * len(inputs))(*inputs)
    sent = ctypes.windll.user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
    if sent != len(inputs):
        raise OSError(f"SendInput sent {sent}/{len(inputs)} events (error {ctypes.GetLastError()})")


def mouse_input(flags, dx=0, dy=0):
    inp = INPUT(type=INPUT_MOUSE)
    inp.mi = MOUSEINPUT(dx, dy, 0, flags, 0, None)
    return inp


def key_input(vk, up=False):
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.ki = KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP if up else 0, 0, None)
    return inp


def move_mouse_absolute(screen_x, screen_y):
    """
    SetCursorPos alone just teleports the pointer; WoW only refreshes what is
    under the cursor (the "Fishing Bobber" hover) when it receives a real
    mouse-move event, so we also push an absolute move through SendInput.
    Virtual-desktop coordinates make this correct on multi-monitor setups.
    """
    win32api.SetCursorPos((int(screen_x), int(screen_y)))
    vx = win32api.GetSystemMetrics(SM_XVIRTUALSCREEN)
    vy = win32api.GetSystemMetrics(SM_YVIRTUALSCREEN)
    vw = win32api.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    vh = win32api.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    nx = int((screen_x - vx) * 65535 / max(vw - 1, 1))
    ny = int((screen_y - vy) * 65535 / max(vh - 1, 1))
    _send_inputs(mouse_input(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, nx, ny))


def right_click(hold_shift=False):
    """
    Genuine OS-level right-click at the current cursor position. Posting
    WM_RBUTTONDOWN/UP to the window is not enough: WoW reads the mouse via
    raw input for 3D-world interaction, so a posted message never "clicks"
    the bobber. Shift+right-click triggers WoW's auto-loot.
    """
    if hold_shift:
        _send_inputs(key_input(VK_SHIFT))
        time.sleep(0.05)
    try:
        _send_inputs(mouse_input(MOUSEEVENTF_RIGHTDOWN))
        time.sleep(0.10)
        _send_inputs(mouse_input(MOUSEEVENTF_RIGHTUP))
    finally:
        if hold_shift:
            time.sleep(0.05)
            _send_inputs(key_input(VK_SHIFT, up=True))


def force_foreground(hwnd, timeout=1.0):
    """
    SetForegroundWindow is refused by Windows when the caller isn't the active
    process. Tapping Alt first is the long-standing workaround: it makes the
    system treat the next foreground change as user-initiated. Falls back to
    attaching to the foreground thread's input queue.
    """
    if win32gui.GetForegroundWindow() == hwnd:
        return True
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        time.sleep(0.3)

    try:
        _send_inputs(key_input(VK_MENU), key_input(VK_MENU, up=True))
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass

    deadline = time.time() + timeout
    while time.time() < deadline:
        if win32gui.GetForegroundWindow() == hwnd:
            return True
        time.sleep(0.05)

    try:
        fg = win32gui.GetForegroundWindow()
        fg_thread, _ = win32process.GetWindowThreadProcessId(fg)
        my_thread = win32api.GetCurrentThreadId()
        ctypes.windll.user32.AttachThreadInput(my_thread, fg_thread, True)
        try:
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)
        finally:
            ctypes.windll.user32.AttachThreadInput(my_thread, fg_thread, False)
        time.sleep(0.1)
    except Exception:
        pass
    return win32gui.GetForegroundWindow() == hwnd


# ====================== SPLASH SOUND MONITOR ======================
class AudioMonitor:
    """
    Captures whatever is playing on the default speakers (WASAPI loopback)
    and keeps a short history of loudness samples in dBFS. The bite splash in
    WoW is a sharp, loud transient, so "level jumps N dB above the ambient
    measured right after the cast" is a far more reliable bite signal than
    anything the vision model produces.
    """

    def __init__(self, log=print):
        import pyaudiowpatch as pyaudio
        self._pa_mod = pyaudio
        self.pa = pyaudio.PyAudio()
        try:
            wasapi = self.pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        except OSError as e:
            raise RuntimeError("WASAPI is not available on this system") from e

        device = self.pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        if not device.get("isLoopbackDevice", False):
            for loopback in self.pa.get_loopback_device_info_generator():
                if device["name"] in loopback["name"]:
                    device = loopback
                    break
            else:
                raise RuntimeError(f"No loopback device found for default speakers '{device['name']}'")

        self.device_name = device["name"]
        self.rate = int(device["defaultSampleRate"])
        self.channels = max(1, int(device["maxInputChannels"]))
        self.levels = deque(maxlen=6000)  # (timestamp, dBFS); ~2 min at 48 kHz / 1024-frame chunks
        self.stream = self.pa.open(
            format=pyaudio.paInt16,
            channels=self.channels,
            rate=self.rate,
            input=True,
            input_device_index=device["index"],
            frames_per_buffer=AUDIO_CHUNK,
            stream_callback=self._on_audio,
        )
        self.stream.start_stream()
        log(f"Listening to '{self.device_name}' ({self.rate} Hz, {self.channels} ch) for the splash sound.")

    def _on_audio(self, in_data, frame_count, time_info, status):
        samples = np.frombuffer(in_data, dtype=np.int16).astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0
        self.levels.append((time.time(), 20.0 * math.log10(max(rms, 1e-5))))
        return (None, self._pa_mod.paContinue)

    def _snapshot(self):
        # list(deque) copies atomically under the GIL; iterating the live deque
        # while the audio callback appends would raise "mutated during iteration".
        return list(self.levels)

    def peak_since(self, ts):
        """Loudest sample recorded at/after ts, or None if nothing arrived."""
        peak = None
        for t, db in reversed(self._snapshot()):
            if t < ts:
                break
            peak = db if peak is None else max(peak, db)
        return peak

    def floor_between(self, t0, t1):
        """Median level in [t0, t1] -- robust ambient estimate."""
        vals = sorted(db for t, db in self._snapshot() if t0 <= t <= t1)
        if not vals:
            return None
        return vals[len(vals) // 2]

    def close(self):
        try:
            self.stream.stop_stream()
            self.stream.close()
        finally:
            self.pa.terminate()


# ====================== BOT CORE ======================
class FishingBot:
    def __init__(self, instance_id, hwnd, run_duration, debug_queue, click_lock, settings):
        from ultralytics import YOLO  # imported here so the GUI starts fast

        self.instance_id = instance_id
        self.hwnd = hwnd
        self.run_duration = run_duration
        self.debug_queue = debug_queue
        self.click_lock = click_lock
        self.settings = {**DEFAULTS, **(settings or {})}

        self.catchclick_conf = float(self.settings["conf"])
        self.watch_seconds = float(self.settings["watch_seconds"])
        self.min_bite_delay = float(self.settings["min_bite_delay"])
        self.required_hits = max(1, int(self.settings["required_hits"]))
        self.cast_key = str(self.settings["cast_key"])[:1] or "1"
        self.shift_click = bool(self.settings["shift_click"])
        self.adaptive = bool(self.settings["adaptive"])
        self.mode = str(self.settings["mode"]).lower()
        self.sound_db = float(self.settings["sound_db"])

        self.inference_times = []
        self.audio = None

        model_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "best.pt")
        if not os.path.exists(model_path):
            raise FileNotFoundError(
                f"Model not found: {model_path}\nPlace best.pt in the same folder as fishing_bot.py"
            )
        self.model = YOLO(model_path)
        names = list(self.model.names.values()) if isinstance(self.model.names, dict) else list(self.model.names)
        self.log(f"Model loaded. Classes: {names}")
        lower_names = [n.lower() for n in names]
        self.has_bobber_class = "bobber" in lower_names
        if "catchclick" not in lower_names and self.mode == "vision":
            self.log("WARNING: model has no 'catchclick' class -- vision mode will never detect a bite!")
        if not self.has_bobber_class:
            self.log("Note: model has no 'Bobber' class; falling back to 'CatchClick' boxes to locate the bobber.")

        # First inference includes several seconds of warm-up; do it now rather
        # than during the first cast's grace window.
        w, h = self.get_window_size()
        self.model(np.zeros((max(h, 32), max(w, 32), 3), dtype=np.uint8), imgsz=IMGSZ, conf=FLOOR_CONF, verbose=False)
        self.log("Warm-up inference done.")

        if self.mode == "sound":
            try:
                self.audio = AudioMonitor(log=self.log)
                self.log(
                    "Sound mode: in WoW turn ON 'Enable Sound in Background' and Sound Effects, "
                    "turn Music/Ambience down, and make sure WoW outputs to the default playback device."
                )
            except Exception as e:
                self.mode = "vision"
                self.log(f"Could not start splash-sound capture ({e}). Falling back to vision mode.")

    def close(self):
        if self.audio is not None:
            try:
                self.audio.close()
            except Exception:
                pass

    # ---------- helpers ----------
    def log(self, msg):
        try:
            self.debug_queue.put(f"Bot {self.instance_id} | {msg}")
        except Exception:
            print(f"[Bot {self.instance_id}] {msg}")

    def window_alive(self):
        return win32gui.IsWindow(self.hwnd)

    def get_window_size(self):
        rect = win32gui.GetClientRect(self.hwnd)
        return rect[2] - rect[0], rect[3] - rect[1]

    def capture_window(self, sct):
        w, h = self.get_window_size()
        if w < 32 or h < 32:
            return None  # minimized or being resized
        left, top = win32gui.ClientToScreen(self.hwnd, (0, 0))
        img = np.array(sct.grab({"top": top, "left": left, "width": w, "height": h}))
        return cv.cvtColor(img, cv.COLOR_BGRA2BGR)

    def send_key(self, key):
        vk = win32api.VkKeyScan(key) & 0xFF
        if vk in (0, 0xFF):
            return
        scan = win32api.MapVirtualKey(vk, 0)
        down_lparam = 1 | (scan << 16)
        up_lparam = down_lparam | (1 << 30) | (1 << 31)
        win32api.PostMessage(self.hwnd, win32con.WM_KEYDOWN, vk, down_lparam)
        time.sleep(0.08)
        win32api.PostMessage(self.hwnd, win32con.WM_KEYUP, vk, up_lparam)
        time.sleep(0.08)

    def send_mouse_click(self, x, y):
        """(x, y) are client-area coordinates from the captured frame."""
        screen_x, screen_y = win32gui.ClientToScreen(self.hwnd, (int(x), int(y)))
        # One click at a time across all bot processes: otherwise two clients
        # fight over the cursor and the foreground window and both miss.
        with self.click_lock:
            if not force_foreground(self.hwnd):
                self.log("Warning: could not bring WoW to the foreground; click may land on another window.")
            move_mouse_absolute(screen_x, screen_y)
            time.sleep(0.2)  # let WoW register the hover before clicking
            right_click(hold_shift=self.shift_click)
            time.sleep(0.1)

    # ---------- detection ----------
    def detect(self, screenshot):
        """
        Runs the model once and returns the best box per class:
            {"bobber": (pos, conf) | None, "catchclick": (pos, conf) | None}
        at or above FLOOR_CONF. Thresholding happens in the cast loop so
        near-misses stay visible in the log.
        """
        t0 = time.perf_counter()
        results = self.model(screenshot, imgsz=IMGSZ, conf=FLOOR_CONF, verbose=False)
        self.inference_times.append(time.perf_counter() - t0)
        if len(self.inference_times) > 50:
            self.inference_times.pop(0)

        best = {"bobber": None, "catchclick": None}
        for result in results:
            for box in result.boxes:
                name = result.names[int(box.cls)].lower()
                if name not in best:
                    continue
                conf = float(box.conf.item())
                if best[name] is None or conf > best[name][1]:
                    x1, y1, x2, y2 = map(int, box.xyxy[0])
                    best[name] = (((x1 + x2) // 2, (y1 + y2) // 2), conf)
        return best

    def detect_objects(self, screenshot):
        """(pos, conf) of the best 'catchclick' box, or (None, 0.0)."""
        hit = self.detect(screenshot)["catchclick"]
        return hit if hit is not None else (None, 0.0)

    @staticmethod
    def bobber_fix(det):
        """
        Best available position for the bobber: the dedicated 'Bobber' class
        when it is confident, otherwise the 'CatchClick' box (which in practice
        also sits on the bobber).
        """
        b = det.get("bobber")
        if b is not None and b[1] >= BOBBER_MIN_CONF:
            return b[0], b[1], "bobber"
        c = det.get("catchclick")
        if c is not None:
            return c[0], c[1], "catchclick"
        if b is not None:
            return b[0], b[1], "bobber"
        return None, 0.0, None

    def avg_inference_ms(self):
        if not self.inference_times:
            return 0.0
        return 1000.0 * sum(self.inference_times) / len(self.inference_times)

    def bobber_still_there(self, sct, click_pos):
        """
        After a real catch the bobber disappears. If the model still sees a
        bobber (either class) near where we clicked, the click was a false
        positive. Returns (still_there, catchclick_conf_seen).
        """
        time.sleep(VERIFY_DELAY)
        frame = self.capture_window(sct)
        if frame is None:
            return False, 0.0
        det = self.detect(frame)
        cc_conf = det["catchclick"][1] if det["catchclick"] else 0.0
        for key in ("bobber", "catchclick"):
            hit = det[key]
            if hit is None or hit[1] < VERIFY_MIN_CONF:
                continue
            pos = hit[0]
            dist = ((pos[0] - click_pos[0]) ** 2 + (pos[1] - click_pos[1]) ** 2) ** 0.5
            if dist <= VERIFY_RADIUS:
                return True, cc_conf
        return False, cc_conf

    def click_and_verify(self, sct, click_pos, why):
        """Click the bobber; True if it disappeared afterwards (real catch)."""
        self.log(f"{why} -- clicking bobber at {click_pos}")
        self.send_mouse_click(*click_pos)
        still_there, cc_conf = self.bobber_still_there(sct, click_pos)
        if still_there:
            return False, cc_conf
        self.log("CLICKED SPLASH! Bobber gone -> catch.")
        time.sleep(1.5)
        return True, cc_conf

    # ---------- main loop ----------
    def run_cast(self, sct):
        self.log("Casting...")
        self.send_key(self.cast_key)
        cast_start = time.time()

        # Phase 1: let the bobber land, then calibrate against the *idle*
        # state: where the bobber is, how the model scores it, and (sound
        # mode) how loud the ambient is once the cast sound has died down.
        settle = min(2.0, self.min_bite_delay)
        time.sleep(settle)
        baseline_cc = 0.0
        bobber_pos, bobber_conf, bobber_src = None, 0.0, None
        while time.time() - cast_start < self.min_bite_delay:
            frame = self.capture_window(sct)
            if frame is not None:
                det = self.detect(frame)
                if det["catchclick"]:
                    baseline_cc = max(baseline_cc, det["catchclick"][1])
                pos, conf, src = self.bobber_fix(det)
                if pos is not None and conf >= bobber_conf:
                    bobber_pos, bobber_conf, bobber_src = pos, conf, src
            time.sleep(0.05)

        cc_threshold = self.catchclick_conf
        if self.adaptive:
            cc_threshold = min(max(cc_threshold, baseline_cc + BASELINE_MARGIN), 0.95)

        sound_threshold = None
        ambient = None
        if self.mode == "sound":
            ambient = self.audio.floor_between(cast_start + settle, time.time())
            if ambient is None:
                self.log(
                    "No audio reaching the loopback device. In WoW enable 'Enable Sound in Background' "
                    "and check that it plays through the default speakers."
                )
                ambient = SILENCE_DB
            sound_threshold = max(ambient, SILENCE_DB) + self.sound_db

        if bobber_pos is None:
            self.log("Bobber not found on screen after cast (is it visible and not under the UI?). Watching anyway.")
        if self.mode == "sound":
            self.log(
                f"Watching (sound). bobber at {bobber_pos} via {bobber_src} conf={bobber_conf:.2f}, "
                f"ambient={ambient:.0f} dB, trigger>={sound_threshold:.0f} dB, inference ~{self.avg_inference_ms():.0f} ms/frame"
            )
        else:
            self.log(
                f"Watching (vision). idle bobber conf={baseline_cc:.2f}, effective threshold={cc_threshold:.2f}, "
                f"inference ~{self.avg_inference_ms():.0f} ms/frame"
            )
            if self.avg_inference_ms() > 500:
                self.log("WARNING: inference is slow; a splash may pass between frames. Lower IMGSZ or use a GPU.")

        # Phase 2: watch for the bite.
        hits = []
        top_cc = 0.0
        top_db = None
        last_track = 0.0
        last_audio_check = time.time()
        while time.time() - cast_start < self.watch_seconds:
            if not self.window_alive():
                return "window_closed"
            now = time.time()

            # Vision: every frame in vision mode, periodically in sound mode
            # (only needed to keep the bobber position current there).
            if self.mode == "vision" or now - last_track >= TRACK_INTERVAL:
                frame = self.capture_window(sct)
                if frame is None:
                    time.sleep(0.5)
                    continue
                det = self.detect(frame)
                last_track = now
                pos, conf, src = self.bobber_fix(det)
                if pos is not None:
                    bobber_pos, bobber_conf, bobber_src = pos, conf, src

                if self.mode == "vision":
                    cc = det["catchclick"]
                    cc_conf = cc[1] if cc else 0.0
                    top_cc = max(top_cc, cc_conf)
                    if cc and cc_conf >= cc_threshold:
                        hits.append((cc[0], cc_conf))
                        self.log(f"CatchClick hit {len(hits)}/{self.required_hits} conf={cc_conf:.2f}")
                    else:
                        hits = []

                    if len(hits) >= self.required_hits:
                        xs = sorted(p[0] for p, _ in hits)
                        ys = sorted(p[1] for p, _ in hits)
                        click_pos = (xs[len(xs) // 2], ys[len(ys) // 2])
                        trigger_conf = max(c for _, c in hits)
                        caught, seen = self.click_and_verify(sct, click_pos, "Confirmed splash (vision)")
                        if caught:
                            return "caught"
                        # Raise relative to what actually triggered the click, so the
                        # same idle-bobber score can't fire again on the next frame.
                        seen = max(seen, trigger_conf)
                        cc_threshold = min(max(cc_threshold, seen + BASELINE_MARGIN), 0.95)
                        self.log(
                            f"Bobber still visible after click (conf={seen:.2f}) -> false positive. "
                            f"Raising threshold to {cc_threshold:.2f} and continuing to watch."
                        )
                        hits = []
                        last_audio_check = time.time()
                        continue

            if self.mode == "sound":
                peak = self.audio.peak_since(last_audio_check)
                last_audio_check = now
                if peak is not None:
                    top_db = peak if top_db is None else max(top_db, peak)
                    if peak >= sound_threshold:
                        if bobber_pos is None:
                            self.log(f"Splash sound ({peak:.0f} dB) but no bobber position -- cannot click.")
                            time.sleep(0.5)
                            continue
                        caught, _ = self.click_and_verify(
                            sct, bobber_pos,
                            f"Splash sound {peak:.0f} dB (ambient {ambient:.0f} dB, trigger {sound_threshold:.0f} dB)",
                        )
                        if caught:
                            return "caught"
                        sound_threshold = peak + 3.0
                        self.log(
                            f"Bobber still visible after click -> that sound wasn't the bite. "
                            f"Raising trigger to {sound_threshold:.0f} dB and continuing to watch."
                        )
                        last_audio_check = time.time()
                        continue

            time.sleep(0.03)

        if self.mode == "sound":
            loudest = f"{top_db:.0f} dB" if top_db is not None else "no audio"
            self.log(f"No catch this cast. Loudest sound: {loudest} (trigger {sound_threshold:.0f} dB).")
        else:
            self.log(
                f"No catch this cast. Highest catchclick confidence seen: {top_cc:.2f} "
                f"(effective threshold {cc_threshold:.2f})"
            )
        return "timeout"

    def fishing_bot(self, sct):
        start_time = time.time()
        casts = caught = 0
        while time.time() - start_time < self.run_duration:
            if not self.window_alive():
                self.log("WoW window closed -- stopping.")
                return
            casts += 1
            outcome = self.run_cast(sct)
            if outcome == "window_closed":
                self.log("WoW window closed -- stopping.")
                return
            if outcome == "caught":
                caught += 1
            if casts % 10 == 0:
                self.log(f"Stats: {caught}/{casts} casts caught.")
        self.log(f"Time expired. {caught}/{casts} casts caught.")


def run_bot(instance_id, hwnd, run_duration, debug_queue, click_lock, settings):
    bot = None
    try:
        bot = FishingBot(instance_id, hwnd, run_duration, debug_queue, click_lock, settings)
        with mss() as sct:
            bot.fishing_bot(sct)
    except Exception:
        # Worker processes have no console the user is looking at; surface the
        # traceback in the GUI instead of dying silently.
        debug_queue.put(f"Bot {instance_id} | CRASHED:\n{traceback.format_exc()}")
    finally:
        if bot is not None:
            bot.close()


# ====================== WINDOW DISCOVERY ======================
KNOWN_WOW_EXES = ('wow.exe', 'wowclassic.exe', 'wow-64.exe', 'wowt.exe', 'wowb.exe', 'wowclassict.exe')
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def process_exe_name(pid):
    """
    Exe file name of a process. Uses QueryFullProcessImageName with limited
    query rights, which (unlike GetModuleFileNameEx + PROCESS_VM_READ) works
    across 32/64-bit boundaries and for processes we can't fully open.
    """
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]

    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        raise OSError(f"OpenProcess failed (error {ctypes.GetLastError()})")
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_ulong(len(buf))
        if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            raise OSError(f"QueryFullProcessImageName failed (error {ctypes.GetLastError()})")
        return os.path.basename(buf.value).lower()
    finally:
        k32.CloseHandle(h)


def find_wow_windows(debug_log=None):
    """
    Finds candidate WoW windows. If the exe name can be read it must be a known
    WoW executable. If it can't be read (permissions / bitness mismatch) we only
    accept windows whose title *is* "World of Warcraft" -- a loose keyword match
    would otherwise pick up this GUI ("WoW Fishing Bot") or an editor window
    with the project folder in its title.
    """
    def log(msg):
        if debug_log is None:
            return
        try:
            debug_log.put(msg)
        except AttributeError:
            debug_log.append(msg)

    my_pid = os.getpid()
    windows = []

    def cb(hwnd, res):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return
        title_lower = title.lower().strip()
        if "warcraft" not in title_lower and "wow" not in title_lower:
            return
        if "fishing bot" in title_lower:
            return

        exe_name = None
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid == my_pid:
                return
            exe_name = process_exe_name(pid)
        except Exception as e:
            log(f"[find_wow_windows] Could not read exe for '{title}' (hwnd={hwnd}): {e}")

        if exe_name is None:
            if title_lower.startswith("world of warcraft"):
                log(f"[find_wow_windows] Accepting '{title}' on exact title (exe unknown).")
                res.append(hwnd)
            else:
                log(f"[find_wow_windows] Rejecting '{title}' -- exe unknown and title not 'World of Warcraft'.")
        elif exe_name in KNOWN_WOW_EXES:
            log(f"[find_wow_windows] Accepting '{title}' ({exe_name}).")
            res.append(hwnd)
        else:
            log(f"[find_wow_windows] Rejecting '{title}' -- exe '{exe_name}' is not a WoW client.")

    win32gui.EnumWindows(cb, windows)
    return windows


# ====================== GUI ======================
def _float(value, default):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


class FishingBotApp:
    def __init__(self):
        import customtkinter as ctk
        self.ctk = ctk
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.root = ctk.CTk()
        self.root.title("WoW Fishing Bot - Debug Mode")
        self.root.geometry("880x820")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.processes = []
        self.debug_queue = Queue()
        self.click_lock = Lock()
        self.running = False
        self.create_ui()
        self.log("UI + debug panel initialized. Open WoW (windowed), then press START.")
        self.status.configure(text="Ready - Open WoW then START", text_color="green")
        self.root.after(200, self.process_debug_queue)

    def create_ui(self):
        ctk = self.ctk
        root = self.root
        ctk.CTkLabel(root, text="WoW Fishing Bot", font=ctk.CTkFont(size=28, weight="bold")).pack(pady=(12, 4))
        self.status = ctk.CTkLabel(root, text="Loading...", font=ctk.CTkFont(size=16))
        self.status.pack(pady=4)

        f = ctk.CTkFrame(root)
        f.pack(pady=10, padx=30, fill="x")
        f.grid_columnconfigure((0, 1, 2, 3), weight=1)

        ctk.CTkLabel(f, text="Bite detection").grid(row=0, column=0, columnspan=2, sticky="w", padx=16, pady=(8, 0))
        self.mode_labels = {
            "Sound: splash sound + bobber tracking (recommended)": "sound",
            "Vision: CatchClick class only": "vision",
        }
        self.mode_var = ctk.StringVar(value=next(iter(self.mode_labels)))
        ctk.CTkOptionMenu(f, values=list(self.mode_labels), variable=self.mode_var, width=380).grid(
            row=1, column=0, columnspan=4, sticky="w", padx=16, pady=(0, 4))

        self.vars = {}
        fields = [
            ("hours", "Run time (hours)", DEFAULTS["hours"]),
            ("cast_key", "Cast key (action bar key bound to Fishing)", DEFAULTS["cast_key"]),
            ("sound_db", "Sound: splash must be N dB above ambient", DEFAULTS["sound_db"]),
            ("watch_seconds", "Seconds to watch each cast before recasting", DEFAULTS["watch_seconds"]),
            ("min_bite_delay", "Ignore bites for first N seconds after cast", DEFAULTS["min_bite_delay"]),
            ("conf", "Vision: min CatchClick confidence (0-1)", DEFAULTS["conf"]),
            ("required_hits", "Vision: consecutive frames to confirm", DEFAULTS["required_hits"]),
        ]
        base_row = 2
        for i, (key, label, default) in enumerate(fields):
            r, c = divmod(i, 2)
            ctk.CTkLabel(f, text=label).grid(row=base_row + r * 2, column=c * 2, columnspan=2, sticky="w", padx=16, pady=(8, 0))
            var = ctk.StringVar(value=str(default))
            self.vars[key] = var
            ctk.CTkEntry(f, textvariable=var, width=160).grid(
                row=base_row + r * 2 + 1, column=c * 2, columnspan=2, sticky="w", padx=16, pady=(0, 4))

        row = base_row + 2 * ((len(fields) + 1) // 2)  # first free grid row after the field pairs
        self.adaptive_var = ctk.BooleanVar(value=DEFAULTS["adaptive"])
        ctk.CTkCheckBox(f, text="Adaptive threshold (learn idle bobber each cast)", variable=self.adaptive_var).grid(
            row=row, column=0, columnspan=2, sticky="w", padx=16, pady=8)
        self.shift_var = ctk.BooleanVar(value=DEFAULTS["shift_click"])
        ctk.CTkCheckBox(f, text="Hold Shift while clicking (auto-loot)", variable=self.shift_var).grid(
            row=row, column=2, columnspan=2, sticky="w", padx=16, pady=8)

        bf = ctk.CTkFrame(f, fg_color="transparent")
        bf.grid(row=row + 1, column=0, columnspan=4, pady=10)
        self.start_btn = ctk.CTkButton(bf, text="START", fg_color="green", hover_color="#116611", height=48,
                                       font=ctk.CTkFont(size=18, weight="bold"), command=self.start_bots)
        self.start_btn.pack(side="left", padx=15)
        ctk.CTkButton(bf, text="STOP", fg_color="red", hover_color="#881111", height=48,
                      font=ctk.CTkFont(size=18, weight="bold"), command=self.stop_bots).pack(side="left", padx=15)

        ctk.CTkLabel(root, text="Debug Panel (Live Detection)", font=ctk.CTkFont(size=16, weight="bold")).pack(
            anchor="w", padx=30, pady=(6, 2))
        self.log_widget = ctk.CTkTextbox(root, font=ctk.CTkFont(family="Consolas", size=13))
        self.log_widget.pack(pady=(0, 12), padx=30, fill="both", expand=True)

    def log(self, msg):
        self.log_widget.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        self.log_widget.see("end")

    def process_debug_queue(self):
        try:
            while not self.debug_queue.empty():
                self.log(self.debug_queue.get_nowait())
        except Exception:
            pass
        if self.running and self.processes and not any(p.is_alive() for p in self.processes):
            self.running = False
            self.processes.clear()
            self.status.configure(text="All bots finished", text_color="orange")
            self.start_btn.configure(state="normal")
        self.root.after(200, self.process_debug_queue)

    def read_settings(self):
        conf = min(max(_float(self.vars["conf"].get(), DEFAULTS["conf"]), 0.0), 1.0)
        watch = max(_float(self.vars["watch_seconds"].get(), DEFAULTS["watch_seconds"]), 3.0)
        min_bite = min(max(_float(self.vars["min_bite_delay"].get(), DEFAULTS["min_bite_delay"]), 1.0), watch - 1.0)
        hits = max(1, int(_float(self.vars["required_hits"].get(), DEFAULTS["required_hits"])))
        cast_key = (self.vars["cast_key"].get().strip() or DEFAULTS["cast_key"])[:1]
        return {
            "hours": max(_float(self.vars["hours"].get(), DEFAULTS["hours"]), 0.01),
            "mode": self.mode_labels.get(self.mode_var.get(), DEFAULTS["mode"]),
            "sound_db": min(max(_float(self.vars["sound_db"].get(), DEFAULTS["sound_db"]), 3.0), 60.0),
            "conf": conf,
            "watch_seconds": watch,
            "min_bite_delay": min_bite,
            "required_hits": hits,
            "cast_key": cast_key,
            "shift_click": bool(self.shift_var.get()),
            "adaptive": bool(self.adaptive_var.get()),
        }

    def start_bots(self):
        if self.running:
            return
        search_log = []
        windows = find_wow_windows(debug_log=search_log)
        for line in search_log:
            self.log(line)
        if not windows:
            self.log("No WoW window found. Visible window titles were:")

            def list_titles(hwnd, _):
                if win32gui.IsWindowVisible(hwnd):
                    title = win32gui.GetWindowText(hwnd)
                    if title.strip():
                        self.log(f"  - {title}")
            win32gui.EnumWindows(list_titles, None)
            from tkinter import messagebox
            messagebox.showerror("Error", "No WoW window found! Check the debug panel for the window titles that were seen.")
            return

        settings = self.read_settings()
        run_duration = settings["hours"] * 3600
        self.running = True
        self.start_btn.configure(state="disabled")
        self.status.configure(text=f"Running on {len(windows)} client(s)...", text_color="lime")
        self.log(
            f"Starting {len(windows)} bot(s): mode={settings['mode']}, sound trigger=+{settings['sound_db']:.0f} dB, "
            f"conf>={settings['conf']:.2f}, watch={settings['watch_seconds']:.0f}s, "
            f"min bite delay={settings['min_bite_delay']:.0f}s, hits={settings['required_hits']}, "
            f"key='{settings['cast_key']}', adaptive={settings['adaptive']}, shift-click={settings['shift_click']}"
        )
        self.log("Loading model in worker process(es) -- first cast starts after warm-up.")

        for i, hwnd in enumerate(windows):
            p = Process(
                target=run_bot,
                args=(i + 1, hwnd, run_duration, self.debug_queue, self.click_lock, settings),
                daemon=True,
            )
            self.processes.append(p)
            p.start()

    def stop_bots(self):
        for p in self.processes:
            if p.is_alive():
                p.terminate()
        for p in self.processes:
            p.join(timeout=1.0)
        self.processes.clear()
        if self.running:
            self.log("All bots stopped.")
        self.running = False
        self.start_btn.configure(state="normal")
        self.status.configure(text="Stopped by user", text_color="red")

    def on_close(self):
        self.stop_bots()
        self.root.destroy()

    def mainloop(self):
        self.root.mainloop()


if __name__ == "__main__":
    freeze_support()
    FishingBotApp().mainloop()
