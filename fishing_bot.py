"""
WoW Fishing Bot -- YOLO-based bobber/splash detection with a debug GUI.

Run:  python fishing_bot.py
Needs: best.pt (your trained YOLO model) in the same folder as this file.
"""
__version__ = "1.11.2"

import os
import sys
import json
import math
import random
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
            # Optional on modern pywin32 (the venv import works without it);
            # the script lives in Scripts/, not as an importable module.
            script = os.path.join(os.path.dirname(sys.executable), "pywin32_postinstall.py")
            if os.path.exists(script):
                try:
                    subprocess.call([sys.executable, script, "-install"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception:
                    pass
        # Freshly installed packages that register .pth files (pywin32 adds
        # win32/ and win32/lib/ to sys.path that way) are only visible to a new
        # interpreter, so hand over to one instead of failing on the import.
        print("Packages installed; restarting.")
        sys.stdout.flush()
        if os.environ.get("FISHING_BOT_RESTARTED") != "1":
            env = dict(os.environ, FISHING_BOT_RESTARTED="1")
            code = subprocess.call([sys.executable, *sys.argv], env=env)
            sys.exit(code)


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
VERIFY_DELAY = 0.8    # seconds to wait after a click before checking whether the bobber vanished
VERIFY_RADIUS = 90    # px: a box this close to the click position means "bobber still there"
VERIFY_MIN_CONF = 0.25  # ignore very weak boxes during that check (e.g. the loot window popping up)
VERIFY_REL_CONF = 0.5   # ...and boxes far weaker than the bobber we were tracking
NEAR_MISS_DB = 4.0    # log spikes within this many dB below the trigger, so the margin can be tuned
BOBBER_MIN_CONF = 0.35  # minimum confidence (either class) to trust a position fix -- and to click it
AMBIENT_FLOOR_DB = -60.0  # never measure "rise" from dead silence (WoW muted in background)
TRACK_INTERVAL = 0.4  # seconds between bobber position updates in sound mode
CROP_PAD = 180        # once the bobber is found, only the square around it is re-scanned
CROP_IMGSZ = 640      # smaller network input for that crop (full frame still uses IMGSZ)
AUDIO_CHUNK = 1024    # loopback frames per level sample (~21 ms at 48 kHz)
SILENCE_DB = -90.0
SPIKE_DB = 6.0        # a bite is a short spike: peak must clear the last second's median by this much
MOUSE_PAUSE_PX = 40   # cursor movement beyond this, while we are not clicking, pauses the bot
LURE_CAST_SECONDS = 3.0
SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
LANDINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "landings.json")
DEBUG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug_frames")
DEBUG_FRAME_LIMIT = 30  # per run

# FishBotLink addon: PX-pixel squares at the client's top-left corner.
ADDON_PX = 16
ADDON_CELLS = ("marker", "bobber", "channel", "loot", "combat", "afk")
ADDON_COLOR_TOL = 70
HOVER_STEP = 24          # px between cursor positions in the fallback grid sweep (bobber hitbox is ~15 px)
HOVER_SETTLE = 0.03      # seconds for the tooltip to appear after a move
HOVER_REGION = (0.15, 0.85, 0.20, 0.75)  # x0, x1, y0, y1 fractions of the client swept for the bobber
HOVER_SWEEP_BUDGET = 6.0  # seconds a single grid sweep may take before giving the loop control back
LOCAL_RADIUS = 16        # px: spiral searched around each change-detection candidate
LOCAL_STEP = 8
DIFF_THRESHOLD = 28      # grey-level change that counts as "something appeared" between pre- and post-cast frames
DIFF_MAX_CANDIDATES = 4
DIFF_MAX_BLOB = 90       # px: wider/taller changes are chat lines, players, the loot window -- not a bobber
CANDIDATE_BUDGET = 4.0   # seconds spent on change-detection candidates before the band/grid fallback
BAND_ROWS = (-6, 6, -18, 18, -30, 30)  # row offsets around the usual landing height (hitbox is ~12 px tall)
BAND_STEP = 12           # px between columns in the band sweep
BAND_HALF = 160          # px: fan out this far from the usual x on the inner rows before trying outer rows
RED_MIN_AREA = 6         # px: red blobs (the bobber's cap) smaller than this are noise
RED_MAX_SIZE = 60
MOTION_CROP = 64         # px square around the bobber watched for the bite
MOTION_INSTANT = 12.0    # mean grey change: click immediately, no sound needed (huge plunge)
MOTION_STRONG = 4.0      # mean grey change: a plunge, click once a splash sound agrees
MOTION_WEAK = 1.5        # mean grey change: enough movement to accept a splash sound as ours
MOTION_SOUND_WINDOW = 1.5  # seconds within which sound and bobber movement must agree
CAP_DIP_WEAK = 4.0       # px the red cap sinks below its resting height, with no splash yet
CAP_DIP_STRONG = 8.0     # px: a plunge; held for three frames it clicks without sound
CAP_SHRINK_WEAK = 0.55   # fraction of the cap lost, with no splash yet
CAP_SHRINK_STRONG = 0.85
CAP_SHRINK_WITH_SPLASH = 0.30  # a real splash plus the cap a third gone is the bite (42% was a miss)
CAP_MIN_AREA = 4         # red px needed in the crop for the cap tracker to be used
BASELINE_FRAMES = 40     # rolling window for the resting cap height / idle motion
LOUD_AMBIENT_DB = -32.0  # ambient above this means music/ambience is on and will bury the splash
LOOT_WAIT = 1.5          # seconds after a click to see the loot window

DEFAULTS = {
    "hours": 6.0,
    "mode": "sound",      # "sound" (splash sound + bobber tracking) or "vision" (CatchClick class)
    "splash_db": 8.0,     # splash must be this many dB louder than the ambient measured after the cast
    "audio_device": "",   # substring of the WASAPI loopback device; blank = default speakers
    "conf": 0.5,
    "watch_seconds": 30.0,  # a fishing cast lasts 30 s; the bite can come at any point in it
    "min_bite_delay": 4.0,
    "required_hits": 2,
    "cast_key": "1",
    "lure_key": "",
    "lure_minutes": 10.0,
    "anti_afk_minutes": 3.0,  # jump between casts this often so the game never flags you AFK; 0 = off
    "miss_limit": 5,      # stop after this many casts in a row with no bobber; 0 = never
    "shift_click": False,
    "adaptive": True,
    "save_debug": True,   # write an annotated screenshot to debug_frames/ when the bobber is not found
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
VK_SPACE = 0x20
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
def _db(rms):
    return 20.0 * math.log10(max(float(rms), 1e-5))


def highpass_rms(samples):
    """
    Pre-emphasis (y[n] = x[n] - 0.97 x[n-1]). A water splash is a burst of
    high-frequency energy; bass, music beds, and rumble mostly disappear.
    """
    if samples.size < 2:
        return 0.0
    hp = samples[1:] - 0.97 * samples[:-1]
    return float(np.sqrt(np.mean(hp * hp)))


BANDS = ("full", "high")


class AudioMonitor:
    """
    Captures whatever is playing on the speakers (WASAPI loopback) and keeps a
    short history of loudness in dBFS for two bands: the full signal and a
    high-passed copy. Which band shows the splash more clearly depends on the
    client's mix, so both are measured against their own ambient.
    """

    def __init__(self, log=print, device_query=""):
        import pyaudiowpatch as pyaudio
        self._pa_mod = pyaudio
        self.pa = pyaudio.PyAudio()
        try:
            wasapi = self.pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        except OSError as e:
            raise RuntimeError("WASAPI is not available on this system") from e

        loopbacks = list(self.pa.get_loopback_device_info_generator())
        query = (device_query or "").strip().lower()
        if query:
            matches = [d for d in loopbacks if query in d["name"].lower()]
            if not matches:
                names = ", ".join(d["name"] for d in loopbacks) or "(none)"
                raise RuntimeError(f"No loopback device matching '{device_query}'. Available: {names}")
            device = matches[0]
        else:
            device = self.pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
            if not device.get("isLoopbackDevice", False):
                for loopback in loopbacks:
                    if device["name"] in loopback["name"]:
                        device = loopback
                        break
                else:
                    raise RuntimeError(f"No loopback device found for default speakers '{device['name']}'")

        self.device_name = device["name"]
        self.rate = int(device["defaultSampleRate"])
        self.channels = max(1, int(device["maxInputChannels"]))
        self.levels = deque(maxlen=6000)  # (timestamp, full-band dBFS, high-band dBFS)
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
        self.levels.append((time.time(), _db(rms), _db(highpass_rms(samples))))
        return (None, self._pa_mod.paContinue)

    def _series(self, band):
        # list(deque) copies atomically under the GIL; iterating the live deque
        # while the audio callback appends would raise "mutated during iteration".
        i = 1 + BANDS.index(band)
        return [(s[0], s[i]) for s in list(self.levels)]

    def peak_since(self, ts, band="full"):
        """Loudest sample at/after ts, or None if nothing arrived."""
        vals = [db for t, db in self._series(band) if t >= ts]
        return max(vals) if vals else None

    def transient_since(self, ts, band="full"):
        """
        Peak level at/after ts, but only if it is a spike: the peak has to
        clear the median of the surrounding second. A sustained sound (music,
        a long spell effect) raises the median with it and is ignored.
        """
        series = self._series(band)
        recent = [db for t, db in series if t >= ts]
        if not recent:
            return None
        window = sorted(db for t, db in series if t >= ts - 1.0)
        peak = max(recent)
        if peak - window[len(window) // 2] < SPIKE_DB:
            return None
        return peak

    def floor_between(self, t0, t1, band="full"):
        """Median level in [t0, t1] -- robust ambient estimate."""
        vals = sorted(db for t, db in self._series(band) if t0 <= t <= t1)
        if not vals:
            return None
        return vals[len(vals) // 2]

    def close(self):
        try:
            self.stream.stop_stream()
            self.stream.close()
        finally:
            self.pa.terminate()


# ====================== ADDON PIXEL LINK ======================
def _is_color(bgr, target_rgb, tol=ADDON_COLOR_TOL):
    b, g, r = (int(v) for v in bgr[:3])
    tr, tg, tb = target_rgb
    return abs(r - tr) <= tol and abs(g - tg) <= tol and abs(b - tb) <= tol


def read_addon_strip(strip):
    """
    Decode the FishBotLink squares from a BGR image whose top-left is the
    client's top-left. Returns {"bobber": bool, "channel": bool, ...} or None
    when the magenta marker is absent (addon not installed / not loaded).
    """
    if strip is None or strip.shape[0] < ADDON_PX or strip.shape[1] < ADDON_PX * len(ADDON_CELLS):
        return None
    y = ADDON_PX // 2
    px = [strip[y, i * ADDON_PX + ADDON_PX // 2] for i in range(len(ADDON_CELLS))]
    if not _is_color(px[0], (255, 0, 255)):
        return None
    out = {}
    for name, p in zip(ADDON_CELLS[1:], px[1:]):
        if _is_color(p, (0, 255, 0)):
            out[name] = True
        elif _is_color(p, (255, 0, 0)):
            out[name] = False
        elif name == "afk":
            out[name] = False  # older addon build without the AFK cell
        else:
            return None  # something is drawn over the strip
    return out


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
        self.log(f"fishing_bot {__version__}")

        self.catchclick_conf = float(self.settings["conf"])
        self.watch_seconds = float(self.settings["watch_seconds"])
        self.min_bite_delay = float(self.settings["min_bite_delay"])
        self.required_hits = max(1, int(self.settings["required_hits"]))
        self.cast_key = str(self.settings["cast_key"])[:1] or "1"
        self.lure_key = str(self.settings.get("lure_key") or "").strip()[:1]
        self.lure_minutes = max(float(self.settings.get("lure_minutes") or DEFAULTS["lure_minutes"]), 0.5)
        self.miss_limit = max(0, int(float(self.settings.get("miss_limit", DEFAULTS["miss_limit"]))))
        self.shift_click = bool(self.settings["shift_click"])
        self.adaptive = bool(self.settings["adaptive"])
        self.mode = str(self.settings["mode"]).lower()
        self.splash_db = float(self.settings["splash_db"])
        self.audio_device = str(self.settings.get("audio_device") or "")

        self.inference_times = []
        self.audio = None
        self.save_debug = bool(self.settings.get("save_debug", True))
        self.debug_saved = 0
        self.last_boxes = []
        self.last_frame = None
        self.addon = None          # last decoded FishBotLink state, or None
        self.addon_seen = False    # logged once
        self.hovering = False      # cursor is parked on the bobber (addon confirmed)
        self.last_bobber_pos = None  # where the bobber landed last cast (sweep starts there)
        self.pre_cast_frame = None   # client frame grabbed just before the cast key
        self.landings = []           # recent confirmed bobber positions (fallback sweep band)
        self.landings_loaded = False
        self.last_candidates = (0, 0)
        self.heard_sound = False     # any splash-level transient heard so far this run
        self.next_lure = 0.0
        self.anti_afk_minutes = max(0.0, float(self.settings.get("anti_afk_minutes", DEFAULTS["anti_afk_minutes"])))
        self.next_anti_afk = time.time() + self.anti_afk_minutes * 60
        self.deadline = time.time() + self.run_duration

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
                self.audio = AudioMonitor(log=self.log, device_query=self.audio_device)
                self.log(
                    "Sound mode: in WoW turn ON 'Enable Sound in Background' and Sound Effects, "
                    "turn Music/Ambience down, and make sure WoW outputs to the playback device above."
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

    def stat(self, kind):
        try:
            self.debug_queue.put(("stat", self.instance_id, kind))
        except Exception:
            pass

    def maybe_lure(self):
        if not self.lure_key or time.time() < self.next_lure:
            return
        self.log(f"Applying lure (key {self.lure_key}).")
        self.send_key(self.lure_key)
        time.sleep(LURE_CAST_SECONDS)
        self.next_lure = time.time() + self.lure_minutes * 60

    def maybe_anti_afk(self, sct):
        """
        WoW flags you AFK after 5 minutes without real input and logs you out
        30 minutes later. The cast key goes in by PostMessage, which the AFK
        timer does not count, so between casts we jump with a genuine key
        press. The addon also reports the AFK flag, in which case we jump now.
        """
        if not self.anti_afk_minutes:
            return
        flagged = bool(self.addon_state(sct) and self.addon.get("afk")) if self.addon is not None else False
        if not flagged and time.time() < self.next_anti_afk:
            return
        self.log("Jumping to clear AFK." if flagged else "Anti-AFK: jumping.")
        with self.click_lock:
            force_foreground(self.hwnd)
            _send_inputs(key_input(VK_SPACE))
            time.sleep(0.08)
            _send_inputs(key_input(VK_SPACE, up=True))
        time.sleep(1.6)  # cannot cast while airborne
        jitter = random.uniform(0.75, 1.25)
        self.next_anti_afk = time.time() + self.anti_afk_minutes * 60 * jitter

    def mouse_taken(self, anchor):
        try:
            x, y = win32api.GetCursorPos()
        except Exception:
            return False
        return abs(x - anchor[0]) > MOUSE_PAUSE_PX or abs(y - anchor[1]) > MOUSE_PAUSE_PX

    def wait_until_mouse_still(self):
        """Block while the user is moving the cursor. Returns the resting position."""
        self.log("Mouse moved. Pausing so you can use the PC; holding still for 2s resumes.")
        deadline = getattr(self, "deadline", time.time() + 3600)
        while time.time() < deadline:
            try:
                pos = win32api.GetCursorPos()
            except Exception:
                return (0, 0)
            stable = time.time()
            while time.time() - stable < 2.0 and time.time() < deadline:
                time.sleep(0.1)
                try:
                    now = win32api.GetCursorPos()
                except Exception:
                    return pos
                if abs(now[0] - pos[0]) > 8 or abs(now[1] - pos[1]) > 8:
                    pos = now
                    stable = time.time()
            if time.time() - stable >= 2.0:
                self.log("Mouse still. Resuming.")
                return pos
        return win32api.GetCursorPos()

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

    # ---------- addon link ----------
    def addon_state(self, sct):
        """Read the FishBotLink strip (cheap: a 80x16 grab). Updates self.addon."""
        try:
            left, top = win32gui.ClientToScreen(self.hwnd, (0, 0))
            img = np.array(sct.grab({"top": top, "left": left,
                                     "width": ADDON_PX * len(ADDON_CELLS), "height": ADDON_PX}))
            state = read_addon_strip(cv.cvtColor(img, cv.COLOR_BGRA2BGR))
        except Exception:
            state = None
        self.addon = state
        if state is not None and not self.addon_seen:
            self.addon_seen = True
            self.log("FishBotLink addon detected: exact bobber finding by tooltip, catches confirmed by loot window.")
        return state

    def client_to_screen(self, x, y):
        return win32gui.ClientToScreen(self.hwnd, (int(x), int(y)))

    def hover(self, sct, x, y):
        """Move the cursor to client (x, y) and report whether the addon says it is on the bobber."""
        sx, sy = self.client_to_screen(x, y)
        move_mouse_absolute(sx, sy)
        time.sleep(HOVER_SETTLE)
        state = self.addon_state(sct)
        return bool(state and state["bobber"])

    def cursor_client_pos(self):
        try:
            return win32gui.ScreenToClient(self.hwnd, win32api.GetCursorPos())
        except Exception:
            return None

    def nudge(self):
        """
        WoW only re-tests what is under a *stationary* cursor when the mouse
        moves, so a bobber tooltip can outlive the bobber by many seconds.
        A 2 px out-and-back move forces a fresh test.
        """
        try:
            x, y = win32api.GetCursorPos()
        except Exception:
            return
        move_mouse_absolute(x + 2, y)
        time.sleep(0.015)
        move_mouse_absolute(x, y)

    def cursor_on_bobber(self, sct, reads=1):
        """True if, after a nudge, the addon reports the bobber under the cursor on `reads` consecutive reads."""
        for _ in range(reads):
            self.nudge()
            time.sleep(HOVER_SETTLE)
            state = self.addon_state(sct)
            if not (state and state["bobber"]):
                return False
        return True

    def grab_crop(self, sct, pos, size=MOTION_CROP, color=False):
        """Crop of the client around `pos` (grey, or BGR with color=True), or None."""
        try:
            w, h = self.get_window_size()
            x0 = int(min(max(pos[0] - size // 2, 0), max(w - size, 0)))
            y0 = int(min(max(pos[1] - size // 2, 0), max(h - size, 0)))
            left, top = win32gui.ClientToScreen(self.hwnd, (x0, y0))
            img = np.array(sct.grab({"top": top, "left": left, "width": size, "height": size}))
            bgr = cv.cvtColor(img, cv.COLOR_BGRA2BGR)
            return bgr if color else cv.cvtColor(bgr, cv.COLOR_BGR2GRAY)
        except Exception:
            return None

    def refine_hover(self, sct, x, y):
        """
        The sweep stops at the first grid point that touches the bobber's
        hitbox, which may be its very edge; the bobber bobs, so an edge hit
        comes and goes. Walk out in each direction until the tooltip drops and
        park the cursor in the middle of that extent.
        """
        step = 10
        ext = {}
        for name, dx, dy in (("l", -1, 0), ("r", 1, 0), ("u", 0, -1), ("d", 0, 1)):
            n = 0
            while n < 6 and self.hover(sct, x + dx * step * (n + 1), y + dy * step * (n + 1)):
                n += 1
            ext[name] = n * step
        cx = x + (ext["r"] - ext["l"]) // 2
        cy = y + (ext["d"] - ext["u"]) // 2
        if self.hover(sct, cx, cy):
            return (cx, cy)
        self.hover(sct, x, y)
        return (x, y)

    @staticmethod
    def red_mask(frame):
        b, g, r = cv.split(frame.astype(np.int16))
        return (((r - g) > 50) & ((r - b) > 50)).astype(np.uint8) * 255

    def red_candidates(self, frame):
        """
        The bobber's cap is the only saturated red thing on open water, so
        red blobs inside the sweep region are the best possible candidates.
        Each yields its centroid and a point a few px below it (the hitbox is
        the whole bobber, the red is only its top). Nearest the usual landing
        height first.
        """
        h, w = frame.shape[:2]
        x0, x1 = int(w * HOVER_REGION[0]), int(w * HOVER_REGION[1])
        y0, y1 = int(h * HOVER_REGION[2]), int(h * HOVER_REGION[3])
        m = self.red_mask(frame)
        mask = np.zeros_like(m)
        mask[y0:y1, x0:x1] = 255
        m = cv.bitwise_and(m, mask)
        m = cv.dilate(m, np.ones((3, 3), np.uint8))
        n, _, st, cen = cv.connectedComponentsWithStats(m)
        by = self.band_y()
        blobs = []
        for i in range(1, n):
            x, y, bw, bh, area = st[i]
            if area < RED_MIN_AREA or bw > RED_MAX_SIZE or bh > RED_MAX_SIZE:
                continue
            cx, cy = int(cen[i][0]), int(cen[i][1])
            dy = abs(cy - by) if by is not None else 0
            blobs.append((dy // 30, -area, (cx, cy)))
        blobs.sort()
        pts = []
        for _, _, (cx, cy) in blobs[:DIFF_MAX_CANDIDATES]:
            pts += [(cx, cy + 5), (cx, cy)]
        return pts

    def change_candidates(self, frame1, frame2):
        """
        Where did something appear since just before the cast? Differencing
        the pre-cast frame against two post-cast frames (ANDed, so moving
        sparkle drops out) highlights the new bobber. Restricted to the band
        where the bobber usually lands when that is known. Each blob yields its
        topmost point and its centroid, red-containing blobs first.
        """
        if self.pre_cast_frame is None or frame1 is None or frame2 is None:
            return []
        if frame1.shape != self.pre_cast_frame.shape or frame2.shape != self.pre_cast_frame.shape:
            return []
        h, w = frame1.shape[:2]
        x0, x1 = int(w * HOVER_REGION[0]), int(w * HOVER_REGION[1])
        y0, y1 = int(h * HOVER_REGION[2]), int(h * HOVER_REGION[3])
        by = self.band_y()
        if by is not None:
            y0, y1 = max(y0, by - 60), min(y1, by + 60)
        a = cv.GaussianBlur(cv.cvtColor(self.pre_cast_frame, cv.COLOR_BGR2GRAY), (5, 5), 0)
        th = None
        for frame in (frame1, frame2):
            b = cv.GaussianBlur(cv.cvtColor(frame, cv.COLOR_BGR2GRAY), (5, 5), 0)
            _, t = cv.threshold(cv.absdiff(a, b), DIFF_THRESHOLD, 255, cv.THRESH_BINARY)
            th = t if th is None else cv.bitwise_and(th, t)
        red = self.red_mask(frame2)
        mask = np.zeros_like(th)
        mask[y0:y1, x0:x1] = 255
        th = cv.bitwise_and(th, mask)
        th = cv.morphologyEx(th, cv.MORPH_OPEN, np.ones((3, 3), np.uint8))
        th = cv.dilate(th, np.ones((5, 5), np.uint8), iterations=1)
        contours, _ = cv.findContours(th, cv.RETR_EXTERNAL, cv.CHAIN_APPROX_SIMPLE)
        blobs = []
        for c in contours:
            area = cv.contourArea(c)
            if area < 10:
                continue
            _, _, bw, bh = cv.boundingRect(c)
            if bw > DIFF_MAX_BLOB or bh > DIFF_MAX_BLOB:
                continue
            m = cv.moments(c)
            if m["m00"] == 0:
                continue
            cx, cy = int(m["m10"] / m["m00"]), int(m["m01"] / m["m00"])
            top = tuple(int(v) for v in c[c[:, :, 1].argmin()][0])
            bx, bby, bw, bh = cv.boundingRect(c)
            has_red = int(red[bby:bby + bh, bx:bx + bw].any())
            blobs.append((area, (cx, cy), top, has_red))
        # The character stands bottom-centre and changes pose when casting, so
        # changes there go last; red (bobber-coloured) blobs and those near the
        # usual landing height go first.

        def rank(t):
            area, (cx, cy), _, has_red = t
            near_player = 0.35 * w < cx < 0.65 * w and cy > 0.45 * h
            dy = abs(cy - by) if by is not None else 0
            return (near_player, -has_red, dy // 40, -area)

        blobs.sort(key=rank)
        pts = []
        for _, cen, top, _ in blobs[:DIFF_MAX_CANDIDATES]:
            if abs(top[0] - cen[0]) + abs(top[1] - cen[1]) > 6:
                pts.append(top)
            pts.append(cen)
        return pts

    def hover_local(self, sct, cx, cy, radius=LOCAL_RADIUS, step=LOCAL_STEP):
        """Spiral of fine steps around (cx, cy); the hitbox is small so the grid here must be too."""
        pts = [(cx, cy)]
        for r in range(step, radius + 1, step):
            ring = []
            for x in range(cx - r, cx + r + 1, step):
                ring += [(x, cy - r), (x, cy + r)]
            for y in range(cy - r + step, cy + r, step):
                ring += [(cx - r, y), (cx + r, y)]
            pts += ring
        for x, y in pts:
            if self.hover(sct, x, y):
                return self.refine_hover(sct, x, y)
        return None

    def hover_scan(self, sct, start=None, radius=None):
        """
        Put the cursor on the bobber, confirmed by the addon reporting the
        game's "Fishing Bobber" tooltip. Order of attack:
          1. already under the cursor;
          2. fine spiral around each spot that changed since the pre-cast frame;
          3. grid sweep nearest-first from `start` (where the bobber landed
             last cast), bounded by HOVER_SWEEP_BUDGET seconds.
        With `radius` only a small neighbourhood is searched (re-acquiring a
        drifting bobber). Returns the client position, or None.
        """
        w, h = self.get_window_size()
        if w < 64 or h < 64:
            return None
        if not self.landings_loaded:
            self.load_landings()
        cur = self.cursor_client_pos()
        if cur and 0 <= cur[0] < w and 0 <= cur[1] < h and self.cursor_on_bobber(sct, reads=2):
            return cur
        if start is None:
            start = self.last_bobber_pos or (w // 2, int(h * 0.40))

        with self.click_lock:
            force_foreground(self.hwnd)
            if radius is None:
                # Two frames a moment apart: the red cap is looked for in the
                # second; both feed the change detector.
                frame1 = self.capture_window(sct)
                time.sleep(0.2)
                frame2 = self.capture_window(sct)
                reds = self.red_candidates(frame2) if frame2 is not None else []
                changes = self.change_candidates(frame1, frame2)
                self.last_candidates = (len(reds) // 2, len(changes))
                deadline = time.time() + CANDIDATE_BUDGET
                tried = []
                for cx, cy in reds + changes:
                    if not self.window_alive() or time.time() > deadline:
                        break
                    if any(abs(cx - tx) <= 6 and abs(cy - ty) <= 6 for tx, ty in tried):
                        continue
                    tried.append((cx, cy))
                    found = self.hover_local(sct, cx, cy, radius=8 if (cx, cy) in reds else LOCAL_RADIUS)
                    if found is not None:
                        return found

            x0, x1 = int(w * HOVER_REGION[0]), int(w * HOVER_REGION[1])
            y0, y1 = int(h * HOVER_REGION[2]), int(h * HOVER_REGION[3])
            if radius is not None:
                step = max(12, HOVER_STEP // 2)
                x0, x1 = max(x0, start[0] - radius), min(x1, start[0] + radius)
                y0, y1 = max(y0, start[1] - radius), min(y1, start[1] + radius)
                pts = [(x, y) for y in range(y0, y1 + 1, step) for x in range(x0, x1 + 1, step)]
                pts.sort(key=lambda p: (p[0] - start[0]) ** 2 + (p[1] - start[1]) ** 2)
            elif len(self.landings) >= 2:
                # The bobber lands at a nearly constant distance in front of the
                # character, i.e. at a nearly constant screen height, with the
                # spread left-right. Sweep that band finely, nearest the usual x first.
                recent = self.landings[-8:]
                ys = sorted(p[1] for p in recent)
                xs = sorted(p[0] for p in recent)
                my, mx = ys[len(ys) // 2], xs[len(xs) // 2]
                pts = []
                for dy in BAND_ROWS:
                    y = my + dy
                    if y0 <= y <= y1:
                        pts += [(x, y) for x in range(x0, x1 + 1, BAND_STEP)]
                # Fan out from the usual x on the two inner rows first (they
                # straddle the hitbox), then the same span on outer rows, then wider.
                pts.sort(key=lambda p: (max(abs(p[0] - mx) // BAND_HALF, abs(p[1] - my) // 12),
                                        abs(p[0] - mx)))
            else:
                pts = [(x, y) for y in range(y0, y1 + 1, HOVER_STEP) for x in range(x0, x1 + 1, HOVER_STEP)]
                pts.sort(key=lambda p: (p[0] - start[0]) ** 2 + (p[1] - start[1]) ** 2)
            pts.insert(0, (int(start[0]), int(start[1])))
            deadline = time.time() + HOVER_SWEEP_BUDGET
            for x, y in pts:
                if not self.window_alive() or time.time() > deadline:
                    return None
                if self.hover(sct, x, y):
                    return self.refine_hover(sct, x, y)
        return None

    def load_landings(self):
        """Previous runs' landing spots, if the window is still the same size (same camera framing)."""
        self.landings_loaded = True
        try:
            with open(LANDINGS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            w, h = self.get_window_size()
            if data.get("size") == [w, h]:
                self.landings = [tuple(p) for p in data.get("pts", [])][-20:]
                if self.landings:
                    self.last_bobber_pos = self.landings[-1]
                    ys = sorted(p[1] for p in self.landings)
                    self.log(f"Loaded {len(self.landings)} earlier bobber landings; sweeps start around "
                             f"y={ys[len(ys) // 2]}.")
        except Exception:
            pass

    def note_landing(self, pos):
        self.last_bobber_pos = pos
        self.landings.append(tuple(int(v) for v in pos))
        del self.landings[:-20]
        try:
            w, h = self.get_window_size()
            with open(LANDINGS_PATH, "w", encoding="utf-8") as f:
                json.dump({"size": [w, h], "pts": self.landings}, f)
        except Exception:
            pass

    def band_y(self):
        if len(self.landings) < 2:
            return None
        ys = sorted(p[1] for p in self.landings)
        return ys[len(ys) // 2]

    def wait_for_loot(self, sct, timeout=LOOT_WAIT):
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self.addon_state(sct)
            if state and state["loot"]:
                return True
            time.sleep(0.05)
        return False

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
    def detect(self, screenshot, imgsz=IMGSZ):
        """
        Runs the model once and returns the best box per class:
            {"bobber": (pos, conf) | None, "catchclick": (pos, conf) | None}
        at or above FLOOR_CONF. Positions are in the coordinate space of
        `screenshot`. Thresholding happens in the cast loop so near-misses
        stay visible in the log.
        """
        t0 = time.perf_counter()
        results = self.model(screenshot, imgsz=imgsz, conf=FLOOR_CONF, verbose=False)
        self.inference_times.append(time.perf_counter() - t0)
        if len(self.inference_times) > 50:
            self.inference_times.pop(0)

        best = {"bobber": None, "catchclick": None}
        raw = []
        for result in results:
            for box in result.boxes:
                name = result.names[int(box.cls)].lower()
                conf = float(box.conf.item())
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                raw.append((name, conf, (x1, y1, x2, y2)))
                if name not in best:
                    continue
                if best[name] is None or conf > best[name][1]:
                    best[name] = (((x1 + x2) // 2, (y1 + y2) // 2), conf)
        self.last_boxes = raw
        return best

    def detect_tiled(self, frame):
        """
        Second look for a bobber that is too small for the full-frame pass:
        the frame is split into 2x2 overlapping tiles and each tile is run at
        full network size, so the bobber is effectively seen at 2x. Four
        inferences, so only used while the bobber is still unknown.
        """
        h, w = frame.shape[:2]
        if h < 128 or w < 128:
            return self.detect(frame)
        best = {"bobber": None, "catchclick": None}
        all_boxes = []
        ox, oy = int(w * 0.1), int(h * 0.1)
        for ty in (0, 1):
            for tx in (0, 1):
                x1 = max(0, tx * w // 2 - ox)
                y1 = max(0, ty * h // 2 - oy)
                x2 = min(w, (tx + 1) * w // 2 + ox)
                y2 = min(h, (ty + 1) * h // 2 + oy)
                det = self.offset_detection(self.detect(frame[y1:y2, x1:x2]), (x1, y1))
                all_boxes += [(n, c, (bx1 + x1, by1 + y1, bx2 + x1, by2 + y1)) for n, c, (bx1, by1, bx2, by2) in self.last_boxes]
                for key, hit in det.items():
                    if hit is not None and (best[key] is None or hit[1] > best[key][1]):
                        best[key] = hit
        self.last_boxes = all_boxes
        return best

    def save_debug_frame(self, frame, tag):
        """Write the frame with every box the model produced, so a miss can be inspected."""
        if not self.save_debug or self.debug_saved >= DEBUG_FRAME_LIMIT:
            return
        try:
            os.makedirs(DEBUG_DIR, exist_ok=True)
            img = frame.copy()
            for name, conf, (x1, y1, x2, y2) in getattr(self, "last_boxes", []):
                color = (0, 255, 0) if conf >= BOBBER_MIN_CONF else (0, 165, 255)
                cv.rectangle(img, (x1, y1), (x2, y2), color, 2)
                cv.putText(img, f"{name} {conf:.2f}", (x1, max(12, y1 - 4)), cv.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)
            path = os.path.join(DEBUG_DIR, f"bot{self.instance_id}_{time.strftime('%H%M%S')}_{tag}.png")
            cv.imwrite(path, img)
            self.debug_saved += 1
            self.log(f"Saved what the model saw to {path} ({len(getattr(self, 'last_boxes', []))} boxes drawn).")
        except Exception as e:
            self.log(f"Could not save debug frame: {e}")

    @staticmethod
    def offset_detection(det, origin):
        ox, oy = origin
        if ox == 0 and oy == 0:
            return det
        out = {}
        for key, hit in det.items():
            if hit is None:
                out[key] = None
            else:
                (x, y), conf = hit
                out[key] = ((x + ox, y + oy), conf)
        return out

    def scan(self, sct, around=None):
        """
        Capture the client and run detection. When `around` is a bobber
        position, only a square around it is scanned (faster, and the bobber
        fills more of the frame). If that crop misses, the full frame is
        scanned once so a drifted bobber is not lost. Returns a detection
        dict in full-frame coordinates, or None if the window can't be captured.
        """
        frame = self.capture_window(sct)
        if frame is None:
            return None
        self.last_frame = frame
        if around is None:
            det = self.detect(frame)
            if self.bobber_fix(det)[0] is None:
                det = self.detect_tiled(frame)
            return det

        h, w = frame.shape[:2]
        cx, cy = around
        x1 = max(0, int(cx) - CROP_PAD)
        y1 = max(0, int(cy) - CROP_PAD)
        x2 = min(w, int(cx) + CROP_PAD)
        y2 = min(h, int(cy) + CROP_PAD)
        if x2 - x1 < 64 or y2 - y1 < 64:
            return self.detect(frame)

        det = self.offset_detection(self.detect(frame[y1:y2, x1:x2], imgsz=CROP_IMGSZ), (x1, y1))
        if self.bobber_fix(det)[0] is not None:
            return det
        return self.detect(frame)

    def detect_objects(self, screenshot):
        """(pos, conf) of the best 'catchclick' box, or (None, 0.0)."""
        hit = self.detect(screenshot)["catchclick"]
        return hit if hit is not None else (None, 0.0)

    @staticmethod
    def bobber_fix(det):
        """
        Best available position for the bobber: the dedicated 'Bobber' class
        when it is confident, otherwise a confident 'CatchClick' box (which in
        practice also sits on the bobber). Weak boxes are never used -- a
        0.15-confidence box is as likely to be the minimap as the bobber, and
        clicking it (and then finding "nothing there") would be a fake catch.
        """
        b = det.get("bobber")
        if b is not None and b[1] >= BOBBER_MIN_CONF:
            return b[0], b[1], "bobber"
        c = det.get("catchclick")
        if c is not None and c[1] >= BOBBER_MIN_CONF:
            return c[0], c[1], "catchclick"
        return None, 0.0, None

    def avg_inference_ms(self):
        if not self.inference_times:
            return 0.0
        return 1000.0 * sum(self.inference_times) / len(self.inference_times)

    def _bobber_near(self, det, click_pos, min_conf):
        for key in ("bobber", "catchclick"):
            hit = det[key]
            if hit is None or hit[1] < min_conf:
                continue
            pos = hit[0]
            if ((pos[0] - click_pos[0]) ** 2 + (pos[1] - click_pos[1]) ** 2) ** 0.5 <= VERIFY_RADIUS:
                return pos
        return None

    def bobber_still_there(self, sct, click_pos, tracked_conf=0.0):
        """
        After a real catch the bobber disappears. It counts as "still there"
        only if two frames in a row show a box near the click that is about as
        strong as the bobber we were tracking; one weak box on the water is
        not enough to throw a catch away.
        Returns (still_there, catchclick_conf_seen, fresh_position).
        """
        min_conf = max(VERIFY_MIN_CONF, VERIFY_REL_CONF * tracked_conf)
        time.sleep(VERIFY_DELAY)
        cc_conf = 0.0
        pos = None
        for i in range(2):
            frame = self.capture_window(sct)
            if frame is None:
                return False, cc_conf, None
            det = self.detect(frame)
            if det["catchclick"]:
                cc_conf = max(cc_conf, det["catchclick"][1])
            pos = self._bobber_near(det, click_pos, min_conf)
            if pos is None:
                return False, cc_conf, None
            if i == 0:
                time.sleep(0.25)
        return True, cc_conf, pos

    def click_and_verify(self, sct, click_pos, why, tracked_conf=0.0, retry=True):
        """
        Click the bobber; True if it disappeared afterwards (real catch). If
        it is still there, click once more at its freshly detected position
        before giving up -- a click a few pixels off the bobber is the
        commonest reason a real bite is lost.

        With the addon the answer is definitive: a loot window opened, or it
        did not. If the cursor is already parked on the bobber we only click.
        """
        if click_pos is None:
            self.log(f"{why} -- no bobber position, not clicking.")
            return False, 0.0

        if self.addon is not None:
            self.log(f"{why} -- clicking bobber at {click_pos}")
            if self.hovering and self.addon_state(sct) and self.addon["bobber"]:
                with self.click_lock:
                    force_foreground(self.hwnd)
                    right_click(hold_shift=self.shift_click)
            else:
                self.send_mouse_click(*click_pos)
            if self.wait_for_loot(sct):
                self.log("CLICKED SPLASH! Loot window opened -> catch.")
                time.sleep(0.5)
                return True, 0.0
            if self.cursor_on_bobber(sct, reads=2):
                # The click landed on the bobber and nothing was caught: that
                # sound was not our bite. Stay put; no need to go looking.
                self.log("No loot, cursor still on the bobber -> that splash was not ours.")
                self.hovering = True
                return False, 0.0
            if retry:
                # The cursor slipped off the bobber; re-acquire it nearby and try once more.
                fresh = self.hover_scan(sct, start=click_pos, radius=90)
                if fresh is not None:
                    self.log(f"No loot -- re-found the bobber at {fresh}, clicking again")
                    with self.click_lock:
                        right_click(hold_shift=self.shift_click)
                    if self.wait_for_loot(sct):
                        self.log("CLICKED SPLASH! Loot window opened -> catch.")
                        time.sleep(0.5)
                        return True, 0.0
                    self.hovering = True
                    self.note_landing(fresh)
                else:
                    self.hovering = False
            return False, 0.0

        self.log(f"{why} -- clicking bobber at {click_pos}")
        self.send_mouse_click(*click_pos)
        still_there, cc_conf, fresh = self.bobber_still_there(sct, click_pos, tracked_conf)
        if still_there and retry and fresh is not None:
            self.log(f"Bobber still there -- clicking again at {fresh}")
            self.send_mouse_click(*fresh)
            still_there, cc_conf, _ = self.bobber_still_there(sct, fresh, tracked_conf)
        if still_there:
            return False, cc_conf
        self.log("CLICKED SPLASH! Bobber gone -> catch.")
        time.sleep(1.5)
        return True, cc_conf

    # ---------- main loop ----------
    def run_cast(self, sct):
        self.log("Casting...")
        # Reference frame for change detection: the previous bobber is gone by
        # now, so whatever appears relative to this is the new one.
        try:
            self.pre_cast_frame = self.capture_window(sct)
        except Exception:
            self.pre_cast_frame = None
        self.send_key(self.cast_key)
        cast_start = time.time()
        try:
            anchor = win32api.GetCursorPos()
        except Exception:
            anchor = (0, 0)

        # Phase 1: let the bobber land, then calibrate against the *idle*
        # state: where the bobber is, how the model scores it, and (sound
        # mode) how loud the ambient is once the cast sound has died down.
        settle = min(2.0, self.min_bite_delay)
        self.hovering = False

        # With the addon we can see whether the cast actually started.
        addon = self.addon_state(sct)
        if addon is None and not self.addon_seen and not getattr(self, "_warned_no_addon", False):
            self._warned_no_addon = True
            self.log("FishBotLink addon not detected (no magenta marker at the top-left of the game view) -- "
                     "relying on the model alone. Install it from the addon/ folder for exact bobber finding.")
        if addon is not None:
            t0 = time.time()
            while time.time() - t0 < 1.5 and not (self.addon and self.addon["channel"]):
                time.sleep(0.1)
                self.addon_state(sct)
            if not (self.addon and self.addon["channel"]):
                self.log("Cast key pressed but no Fishing channel started (loot window open, not facing water, "
                         "or key not bound). Pressing it again.")
                try:
                    self.pre_cast_frame = self.capture_window(sct)
                except Exception:
                    pass
                self.send_key(self.cast_key)
                time.sleep(1.0)
                cast_start = time.time()

        time.sleep(settle)
        baseline_cc = 0.0
        bobber_pos, bobber_conf, bobber_src = None, 0.0, None
        # Model-only: calibrate against the idle bobber until the earliest
        # possible bite. Addon: one quick look for a starting guess, then find
        # the bobber for certain with the cursor (bites can come early).
        while time.time() - cast_start < self.min_bite_delay:
            det = self.scan(sct, around=bobber_pos)
            if det is not None:
                if det["catchclick"]:
                    baseline_cc = max(baseline_cc, det["catchclick"][1])
                pos, conf, src = self.bobber_fix(det)
                if pos is not None and conf >= bobber_conf:
                    bobber_pos, bobber_conf, bobber_src = pos, conf, src
            if self.addon is not None:
                break
            time.sleep(0.05)

        # Addon: confirm the model's guess by hovering it, or find the bobber by
        # sweeping the cursor when the model saw nothing. Either way the cursor
        # ends up parked on the bobber, ready to click the instant we hear the splash.
        if self.addon is not None:
            t0 = time.time()
            # Start where the bobber landed last cast; the model's guess only
            # when there is no history. The model is often wrong here, so a
            # miss around its guess falls back to the full sweep.
            found = self.hover_scan(sct)
            if found is not None:
                self.hovering = True
                self.note_landing(found)
                bobber_pos, bobber_conf, bobber_src = found, 1.0, "tooltip"
                reds, changes = self.last_candidates
                self.log(f"Cursor is on the bobber at {found} (tooltip confirmed, {time.time() - t0:.1f}s; "
                         f"{reds} red spot(s), {changes} change spot(s)).")
            else:
                self.hovering = False
                self.log("Swept the water with the cursor but the game never showed a 'Fishing Bobber' tooltip "
                         "(bobber outside the swept area, hidden under UI, or the cast did not happen).")
            try:
                anchor = win32api.GetCursorPos()
            except Exception:
                pass
            while time.time() - cast_start < self.min_bite_delay:
                time.sleep(0.05)

        cc_threshold = self.catchclick_conf
        if self.adaptive:
            cc_threshold = min(max(cc_threshold, baseline_cc + BASELINE_MARGIN), 0.95)

        ambient = {}
        if self.mode == "sound":
            for band in BANDS:
                level = self.audio.floor_between(cast_start + settle, time.time(), band)
                ambient[band] = max(level, AMBIENT_FLOOR_DB) if level is not None else None
            if ambient["full"] is None or ambient["full"] <= AMBIENT_FLOOR_DB:
                # A silent floor is fine (music off) as long as effects still arrive.
                if not self.heard_sound and not getattr(self, "_warned_silent", False):
                    self._warned_silent = True
                    self.log(
                        "No game audio heard yet. If the next casts show no splash sounds, enable "
                        "'Enable Sound in Background' (System > Sound) and check WoW plays through the selected speakers."
                    )
                ambient = {band: AMBIENT_FLOOR_DB for band in BANDS}
            elif ambient["full"] >= LOUD_AMBIENT_DB and not getattr(self, "_warned_loud", False):
                self._warned_loud = True
                self.log(
                    f"Ambient sound is loud ({ambient['full']:.0f} dB): Music or Ambience is on, and the splash "
                    "will be buried in it. In WoW System > Sound set Music and Ambience to 0, Sound Effects high."
                )

        need_rise = self.splash_db  # raised within a cast after a sound that turned out not to be the bite
        if self.mode == "sound" and ambient.get("full", 0) <= AMBIENT_FLOOR_DB + 1:
            # The floor is a stand-in for "we measured silence", so a small sound
            # looks like a huge rise over it. Demand a louder one.
            need_rise = max(need_rise, 18.0)

        if bobber_pos is None and self.addon is None:
            self.log(
                "Bobber not found on screen after cast. Zoom the camera in and tilt it down so the bobber "
                "is large and near the middle of the screen; the model cannot see a far-away dot. "
                "Installing the FishBotLink addon (see the addon/ folder) removes this dependence on the model."
            )
            if self.last_frame is not None:
                self.save_debug_frame(self.last_frame, "no_bobber")
        if self.mode == "sound":
            trigger = "trigger: bobber motion + splash sound" if self.hovering else "trigger: splash sound"
            self.log(
                f"Watching ({trigger}). bobber at {bobber_pos} via {bobber_src} conf={bobber_conf:.2f}, "
                f"ambient {ambient['full']:.0f} dB, splash needs +{need_rise:.0f} dB, "
                f"inference ~{self.avg_inference_ms():.0f} ms/frame"
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
        top_rise = None
        last_near_miss = 0.0
        blind_splashes = 0  # splash-level sounds heard while we had no bobber to click
        saw_bobber = bobber_pos is not None
        last_track = 0.0
        last_sweep = time.time()
        off_bobber = 0
        prev_crop, motion_samples, cap_hist = None, [], []
        motion_idle, motion_peak = None, 0.0
        cap_dip_peak, cap_shrink_peak, vis_run = 0.0, 0.0, 0
        last_motion, last_sound, last_sound_rise = 0.0, 0.0, 0.0
        ignored_splashes = ignored_motion = 0
        pending_sound = pending_motion = 0.0
        pending_motion_why = ""
        last_audio_check = time.time()
        while time.time() - cast_start < self.watch_seconds:
            if not self.window_alive():
                return "window_closed"
            if self.mouse_taken(anchor):
                anchor = self.wait_until_mouse_still()
                last_audio_check = time.time()
            now = time.time()

            # Addon: the game tells us when the cast ran out, and whether the
            # cursor is still on the (slowly drifting) bobber.
            if self.addon is not None and now - last_track >= TRACK_INTERVAL:
                last_track = now
                state = self.addon_state(sct)
                if state is not None:
                    if state["combat"]:
                        self.log("In combat -- stopping this cast.")
                        return "timeout"
                    if not state["channel"] and now - cast_start > 3.0:
                        # UnitChannelInfo can blink nil for a frame; ask again
                        # before giving up a live cast.
                        time.sleep(0.25)
                        state = self.addon_state(sct)
                        if state is not None and not state["channel"]:
                            if self.wait_for_loot(sct, 0.3):
                                return "caught"
                            # Bobber still under the cursor with no channel: on some
                            # servers the bite ends the channel. Click before recasting.
                            if self.hovering and self.cursor_on_bobber(sct):
                                self.log(f"Channel ended at {now - cast_start:.0f}s but the bobber is still under "
                                         "the cursor -- clicking it.")
                                with self.click_lock:
                                    force_foreground(self.hwnd)
                                    right_click(hold_shift=self.shift_click)
                                if self.wait_for_loot(sct, 0.8):
                                    self.log("CLICKED SPLASH! Loot window opened -> catch.")
                                    return "caught"
                            self.log(f"Fishing channel ended at {now - cast_start:.0f}s without a catch "
                                     f"(cap dipped at most {cap_dip_peak:.0f} px / {cap_shrink_peak * 100:.0f}% under, "
                                     f"motion peak {motion_peak:.1f}) -- recasting.")
                            return "timeout" if saw_bobber else "no_bobber"
                    if self.hovering and not self.cursor_on_bobber(sct):
                        off_bobber += 1
                    else:
                        off_bobber = 0
                    # Three consecutive misses (~1.2s): the bobber really drifted
                    # off the cursor, not just bobbed for a frame.
                    if self.hovering and off_bobber >= 3:
                        off_bobber = 0
                        fresh = self.hover_scan(sct, start=bobber_pos, radius=90)
                        if fresh is not None:
                            bobber_pos = fresh
                            self.note_landing(fresh)
                            prev_crop, motion_samples, cap_hist, vis_run = None, [], [], 0
                        else:
                            self.hovering = False
                        try:
                            anchor = win32api.GetCursorPos()
                        except Exception:
                            pass
                    elif not self.hovering and now - last_sweep >= 3.0:
                        last_sweep = now
                        fresh = self.hover_scan(sct, start=bobber_pos)
                        if fresh is not None:
                            self.hovering = True
                            self.note_landing(fresh)
                            bobber_pos, bobber_conf, bobber_src = fresh, 1.0, "tooltip"
                            saw_bobber = True
                            prev_crop, motion_samples, cap_hist, vis_run = None, [], [], 0
                            self.log(f"Cursor is on the bobber at {fresh}.")
                        try:
                            anchor = win32api.GetCursorPos()
                        except Exception:
                            pass
                    if self.mode == "sound":
                        time.sleep(0.03)
                        continue

            # Vision: every frame in vision mode, periodically in sound mode
            # (only needed to keep the bobber position current there).
            if self.mode == "vision" or (now - last_track >= TRACK_INTERVAL and not self.hovering):
                det = self.scan(sct, around=bobber_pos)
                if det is None:
                    time.sleep(0.5)
                    continue
                last_track = now
                pos, conf, src = self.bobber_fix(det)
                if pos is not None and not self.hovering:
                    bobber_pos, bobber_conf, bobber_src = pos, conf, src
                    saw_bobber = True

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
                        caught, seen = self.click_and_verify(
                            sct, click_pos, "Confirmed splash (vision)", tracked_conf=bobber_conf, retry=False)
                        if caught:
                            return "caught"
                        # Raise relative to what actually triggered the click, so the
                        # same idle-bobber score can't fire again on the next frame.
                        seen = max(seen, trigger_conf)
                        cc_threshold = min(max(cc_threshold, seen + BASELINE_MARGIN), 0.95)
                        self.stat("false")
                        self.log(
                            f"Bobber still visible after click (conf={seen:.2f}) -> false positive. "
                            f"Raising threshold to {cc_threshold:.2f} and continuing to watch."
                        )
                        hits = []
                        last_audio_check = time.time()
                        try:
                            anchor = win32api.GetCursorPos()
                        except Exception:
                            pass
                        continue

            if self.mode == "sound":
                # Rise of each band over its own ambient; the splash is
                # whichever band jumps more.
                rise, band = None, None
                for b in BANDS:
                    peak = self.audio.transient_since(last_audio_check, b)
                    if peak is not None and (rise is None or peak - ambient[b] > rise):
                        rise, band = peak - ambient[b], b
                last_audio_check = now
                at = now - cast_start
                if rise is not None:
                    top_rise = rise if top_rise is None else max(top_rise, rise)
                    if rise >= 6.0:
                        self.heard_sound = True
                if rise is not None and rise < need_rise:
                    # Must not fall through: this branch used to leave `rise` set,
                    # and the click below then fired on every sample, including
                    # sounds quieter than the room.
                    if rise >= need_rise - NEAR_MISS_DB and now - last_near_miss >= 1.0:
                        self.log(f"Heard a +{rise:.0f} dB spike at {at:.1f}s, below the +{need_rise:.0f} dB needed.")
                        last_near_miss = now
                    rise = None
                elif rise is not None:
                    if bobber_pos is None:
                        blind_splashes += 1
                        if blind_splashes == 1:
                            self.log(f"Splash +{rise:.0f} dB at {at:.1f}s but no bobber position -- cannot click.")
                        time.sleep(0.5)
                        continue
                    if self.hovering:
                        # Sound alone is not enough with other anglers around, and an
                        # early click on the bobber ends the cast. The decision is
                        # made in the motion block below; just remember the sound.
                        last_sound, last_sound_rise = now, rise
                        if rise >= need_rise:
                            pending_sound = now
                        rise = None

                # The bite, seen from the parked cursor: the bobber's red cap sinks
                # (and often disappears under the water) for about half a second,
                # with a splash sound. Sparkle is not red, and neighbours' bites do
                # not move our cap, so cap height + sound is the test. Where the cap
                # cannot be tracked (no red), mean frame change stands in for it.
                why = None
                if self.hovering and bobber_pos is not None:
                    crop = self.grab_crop(sct, bobber_pos, color=True)
                    if crop is not None:
                        grey = cv.cvtColor(crop, cv.COLOR_BGR2GRAY)
                        red = self.red_mask(crop)
                        ys = np.where(red > 0)[0]
                        area = int(len(ys))
                        cy = float(ys.mean()) if area else None

                        # Rolling baselines: medians over the last ~2-3 s, so a
                        # half-second dip cannot drag them along.
                        m = None
                        if prev_crop is not None and grey.shape == prev_crop.shape:
                            m = float(cv.absdiff(grey, prev_crop).mean())
                            motion_samples.append(m)
                            del motion_samples[:-BASELINE_FRAMES]
                        prev_crop = grey
                        cap_hist.append((cy, area))
                        del cap_hist[:-BASELINE_FRAMES]
                        ready = len(cap_hist) >= 10 and len(motion_samples) >= 10
                        if ready:
                            srt = sorted(motion_samples)
                            motion_idle = max(srt[len(srt) // 2], 0.5)
                            areas = sorted(a for _, a in cap_hist)
                            area_med = areas[len(areas) // 2]
                            cys = sorted(c for c, a in cap_hist if c is not None)
                            cy_med = cys[len(cys) // 2] if cys else None
                            cap_ok = area_med >= CAP_MIN_AREA and cy_med is not None

                            if cap_ok:
                                # Dip must beat the idle bob, not a fixed number: the
                                # spread of recent cap heights is how much it bobs at rest.
                                spread = 0.0
                                if len(cys) >= 8:
                                    spread = cys[int(len(cys) * 0.9)] - cys[int(len(cys) * 0.1)]
                                dip = (cy - cy_med) if cy is not None else 0.0
                                shrink = 1.0 - area / float(area_med)
                                cap_dip_peak = max(cap_dip_peak, dip)
                                cap_shrink_peak = max(cap_shrink_peak, shrink)
                                splash_open = (pending_sound and now - pending_sound <= MOTION_SOUND_WINDOW) or (
                                    last_sound and now - last_sound <= MOTION_SOUND_WINDOW
                                    and last_sound_rise >= need_rise - 3.0)
                                # Without a splash the cap has to really go under, or sparkle
                                # and a small bob click. With a splash, the 3 px / 42% dips in
                                # the logs were the bite and the fish then got away.
                                if splash_open:
                                    weak = dip >= max(2.0, spread) or shrink >= CAP_SHRINK_WITH_SPLASH
                                else:
                                    weak = dip >= max(CAP_DIP_WEAK, spread + 2) or shrink >= CAP_SHRINK_WEAK
                                strong = dip >= max(CAP_DIP_STRONG, spread + 5) or shrink >= CAP_SHRINK_STRONG
                                # A one-frame red flicker is not a plunge; it has to hold.
                                instant = strong and vis_run >= 2
                                desc = f"cap down {dip:.0f} px, {shrink * 100:.0f}% under"
                            else:
                                if m is not None:
                                    motion_peak = max(motion_peak, m)
                                weak = m is not None and m >= max(MOTION_WEAK, 2 * motion_idle)
                                strong = m is not None and m >= max(MOTION_STRONG, 3 * motion_idle)
                                instant = m is not None and m >= max(MOTION_INSTANT, 8 * motion_idle)
                                desc = f"motion {m:.1f} vs idle {motion_idle:.1f}" if m is not None else ""
                            vis_run = vis_run + 1 if weak else 0
                            if weak:
                                last_motion = now
                            if strong and vis_run >= 1 and not pending_motion:
                                pending_motion, pending_motion_why = now, desc
                            heard = now - last_sound <= MOTION_SOUND_WINDOW and last_sound_rise >= need_rise - 3.0

                            if instant:
                                why = f"Bobber plunged ({desc}) at {at:.1f}s"
                            elif weak and heard:
                                why = f"Bobber moved ({desc}) with a +{last_sound_rise:.0f} dB splash at {at:.1f}s"
                            elif pending_motion and heard and last_sound >= pending_motion - 0.1:
                                why = (f"Bobber moved ({pending_motion_why}) then a +{last_sound_rise:.0f} dB "
                                       f"splash at {at:.1f}s")
                            elif (pending_sound and now - pending_sound > MOTION_SOUND_WINDOW
                                  and last_motion < pending_sound - MOTION_SOUND_WINDOW):
                                ignored_splashes += 1
                                if ignored_splashes <= 2:
                                    self.log(f"Heard a +{last_sound_rise:.0f} dB splash at {pending_sound - cast_start:.1f}s "
                                             "but our bobber did not move -- someone else's bite, not clicking.")
                                pending_sound = 0.0
                            elif pending_motion and now - pending_motion > MOTION_SOUND_WINDOW:
                                if ignored_motion < 2:
                                    self.log(f"Bobber area moved ({pending_motion_why}) at "
                                             f"{pending_motion - cast_start:.1f}s with no splash sound -- not clicking.")
                                ignored_motion += 1
                                pending_motion = 0.0
                            if why is not None:
                                pending_sound = pending_motion = 0.0

                if why is not None or (rise is not None and rise >= need_rise and not self.hovering):
                    if bobber_pos is None:
                        self.log(f"{why or 'Splash'} but no bobber position -- not clicking.")
                        time.sleep(0.3)
                        continue
                    if why is None:
                        why = f"Splash +{rise:.0f} dB over ambient ({band} band) at {at:.1f}s"
                        rise_fired = rise
                    else:
                        rise_fired = max(last_sound_rise, need_rise)
                    caught, _ = self.click_and_verify(
                        sct, bobber_pos, why,
                        tracked_conf=bobber_conf if bobber_src == "bobber" else 0.0,
                    )
                    rise = rise_fired
                    if caught:
                        return "caught"
                    # The retry already covered a click that merely missed, so
                    # this sound was not the bite. The real one can still come
                    # this cast, and it will be louder than what just fired.
                    self.stat("false")
                    need_rise = rise + 3.0
                    if self.addon is not None:
                        self.log("No loot from that click -> not our bite. (An early click on the bobber usually "
                                 "ends the cast; if the channel drops now, that is why.)")
                    else:
                        self.log(
                            f"Bobber still there after two clicks -> not the bite. "
                            f"Now needs +{need_rise:.0f} dB; still watching this cast."
                        )
                    try:
                        anchor = win32api.GetCursorPos()
                    except Exception:
                        pass
                    # Deliberately not resetting last_audio_check: the bite may
                    # have splashed while we were clicking and verifying, and
                    # that audio is still in the buffer.
                    continue

            time.sleep(0.03)

        if not saw_bobber:
            extra = ""
            if blind_splashes:
                extra = (
                    f" Heard {blind_splashes} splash-level sound(s) meanwhile -- if other players are fishing "
                    "next to you, those are their bites, and sound mode cannot tell them from yours."
                )
            self.log("No bobber found this cast (camera, range, or the bobber is under the UI)." + extra)
            return "no_bobber"
        if self.mode == "sound":
            biggest = f"+{top_rise:.0f} dB" if top_rise is not None else "none"
            self.log(f"No catch this cast. Biggest spike: {biggest} (splash needs +{need_rise:.0f} dB); "
                     f"cap dipped at most {cap_dip_peak:.0f} px / {cap_shrink_peak * 100:.0f}% under, "
                     f"motion peak {motion_peak:.1f}.")
        else:
            self.log(
                f"No catch this cast. Highest catchclick confidence seen: {top_cc:.2f} "
                f"(effective threshold {cc_threshold:.2f})"
            )
        return "timeout"

    def fishing_bot(self, sct):
        if self.mode == "sound" and self.splash_db > 20:
            self.log(
                f"Splash threshold is +{self.splash_db:.0f} dB. Real bites are usually +8 to +20 over ambient; "
                "set that field back to 8 or the bot will wait out almost every cast."
            )
        start_time = time.time()
        self.deadline = start_time + self.run_duration
        casts = caught = misses = 0
        while time.time() - start_time < self.run_duration:
            if not self.window_alive():
                self.log("WoW window closed -- stopping.")
                return
            self.maybe_lure()
            self.maybe_anti_afk(sct)
            casts += 1
            self.stat("cast")
            outcome = self.run_cast(sct)
            if outcome == "window_closed":
                self.log("WoW window closed -- stopping.")
                return
            if outcome == "caught":
                caught += 1
                misses = 0
                self.stat("caught")
            elif outcome == "no_bobber":
                misses += 1
                self.stat("no_bobber")
                if self.miss_limit and misses >= self.miss_limit:
                    self.log(
                        f"Bobber missing {misses} casts in a row -- stopping. "
                        "Zoom the camera IN so the bobber is big, face the water, and keep the bobber away from the UI. "
                        "Open the debug_frames folder to see exactly what the model saw."
                    )
                    return
            else:
                misses = 0
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


# ====================== SETTINGS ======================
def load_settings_file(path=None):
    path = path or SETTINGS_PATH
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def save_settings_file(data, path=None):
    path = path or SETTINGS_PATH
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


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
        self.root.title(f"WoW Fishing Bot {__version__}")
        self.root.geometry("900x1010")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.processes = []
        self.debug_queue = Queue()
        self.click_lock = Lock()
        self.running = False
        self.saved = load_settings_file()
        self.totals = {"cast": 0, "caught": 0, "no_bobber": 0, "false": 0}
        self.create_ui()
        self.log(f"fishing_bot {__version__} — UI ready. Open WoW (windowed), then press START.")
        self.status.configure(text="Ready - Open WoW then START", text_color="green")
        self.root.after(200, self.process_debug_queue)

    def create_ui(self):
        ctk = self.ctk
        root = self.root
        ctk.CTkLabel(root, text=f"WoW Fishing Bot  {__version__}", font=ctk.CTkFont(size=28, weight="bold")).pack(pady=(12, 4))
        self.status = ctk.CTkLabel(root, text="Loading...", font=ctk.CTkFont(size=16))
        self.status.pack(pady=4)
        self.stats_label = ctk.CTkLabel(root, text=self.stats_text(), font=ctk.CTkFont(size=14))
        self.stats_label.pack(pady=(0, 4))

        f = ctk.CTkFrame(root)
        f.pack(pady=10, padx=30, fill="x")
        f.grid_columnconfigure((0, 1, 2, 3), weight=1)

        ctk.CTkLabel(f, text="Bite detection").grid(row=0, column=0, columnspan=2, sticky="w", padx=16, pady=(8, 0))
        self.mode_labels = {
            "Sound: splash sound + bobber tracking (recommended)": "sound",
            "Vision: CatchClick class only": "vision",
        }
        saved_mode = self.saved.get("mode", DEFAULTS["mode"])
        mode_label = next((k for k, v in self.mode_labels.items() if v == saved_mode), next(iter(self.mode_labels)))
        self.mode_var = ctk.StringVar(value=mode_label)
        ctk.CTkOptionMenu(f, values=list(self.mode_labels), variable=self.mode_var, width=380).grid(
            row=1, column=0, columnspan=4, sticky="w", padx=16, pady=(0, 4))

        self.vars = {}
        fields = [
            ("hours", "Run time (hours)", DEFAULTS["hours"]),
            ("cast_key", "Cast key (action bar key bound to Fishing)", DEFAULTS["cast_key"]),
            ("lure_key", "Lure key (blank = off)", DEFAULTS["lure_key"]),
            ("lure_minutes", "Reapply lure every N minutes", DEFAULTS["lure_minutes"]),
            ("anti_afk_minutes", "Anti-AFK: jump every N minutes (0 = off)", DEFAULTS["anti_afk_minutes"]),
            ("splash_db", "Sound: splash must be N dB above ambient", DEFAULTS["splash_db"]),
            ("audio_device", "Sound device name contains (blank = default)", DEFAULTS["audio_device"]),
            ("watch_seconds", "Seconds to watch each cast before recasting", DEFAULTS["watch_seconds"]),
            ("min_bite_delay", "Ignore bites for first N seconds after cast", DEFAULTS["min_bite_delay"]),
            ("miss_limit", "Stop after N casts with no bobber (0 = never)", DEFAULTS["miss_limit"]),
            ("conf", "Vision: min CatchClick confidence (0-1)", DEFAULTS["conf"]),
            ("required_hits", "Vision: consecutive frames to confirm", DEFAULTS["required_hits"]),
        ]
        base_row = 2
        for i, (key, label, default) in enumerate(fields):
            r, c = divmod(i, 2)
            ctk.CTkLabel(f, text=label).grid(row=base_row + r * 2, column=c * 2, columnspan=2, sticky="w", padx=16, pady=(8, 0))
            var = ctk.StringVar(value=str(self.saved.get(key, default)))
            self.vars[key] = var
            ctk.CTkEntry(f, textvariable=var, width=160).grid(
                row=base_row + r * 2 + 1, column=c * 2, columnspan=2, sticky="w", padx=16, pady=(0, 4))

        row = base_row + 2 * ((len(fields) + 1) // 2)  # first free grid row after the field pairs
        self.adaptive_var = ctk.BooleanVar(value=bool(self.saved.get("adaptive", DEFAULTS["adaptive"])))
        ctk.CTkCheckBox(f, text="Adaptive threshold (learn idle bobber each cast)", variable=self.adaptive_var).grid(
            row=row, column=0, columnspan=2, sticky="w", padx=16, pady=8)
        self.shift_var = ctk.BooleanVar(value=bool(self.saved.get("shift_click", DEFAULTS["shift_click"])))
        ctk.CTkCheckBox(f, text="Hold Shift while clicking (auto-loot)", variable=self.shift_var).grid(
            row=row, column=2, columnspan=2, sticky="w", padx=16, pady=8)
        self.debug_var = ctk.BooleanVar(value=bool(self.saved.get("save_debug", DEFAULTS["save_debug"])))
        ctk.CTkCheckBox(f, text="Save a screenshot to debug_frames/ when the bobber is not found",
                        variable=self.debug_var).grid(row=row + 1, column=0, columnspan=4, sticky="w", padx=16, pady=(0, 8))

        bf = ctk.CTkFrame(f, fg_color="transparent")
        bf.grid(row=row + 2, column=0, columnspan=4, pady=10)
        self.start_btn = ctk.CTkButton(bf, text="START", fg_color="green", hover_color="#116611", height=48,
                                       font=ctk.CTkFont(size=18, weight="bold"), command=self.start_bots)
        self.start_btn.pack(side="left", padx=15)
        ctk.CTkButton(bf, text="STOP", fg_color="red", hover_color="#881111", height=48,
                      font=ctk.CTkFont(size=18, weight="bold"), command=self.stop_bots).pack(side="left", padx=15)

        ctk.CTkLabel(root, text="Debug Panel (Live Detection)", font=ctk.CTkFont(size=16, weight="bold")).pack(
            anchor="w", padx=30, pady=(6, 2))
        self.log_widget = ctk.CTkTextbox(root, font=ctk.CTkFont(family="Consolas", size=13))
        self.log_widget.pack(pady=(0, 12), padx=30, fill="both", expand=True)

    def stats_text(self):
        t = self.totals
        return (
            f"Casts {t['cast']}    Catches {t['caught']}    "
            f"No bobber {t['no_bobber']}    False clicks {t['false']}"
        )

    def note_stat(self, kind):
        if kind in self.totals:
            self.totals[kind] += 1
            self.stats_label.configure(text=self.stats_text())

    def log(self, msg):
        self.log_widget.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        self.log_widget.see("end")

    def process_debug_queue(self):
        try:
            while not self.debug_queue.empty():
                item = self.debug_queue.get_nowait()
                if isinstance(item, tuple) and item and item[0] == "stat":
                    self.note_stat(item[2])
                else:
                    self.log(item)
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
        lure_key = self.vars["lure_key"].get().strip()[:1]
        return {
            "hours": max(_float(self.vars["hours"].get(), DEFAULTS["hours"]), 0.01),
            "mode": self.mode_labels.get(self.mode_var.get(), DEFAULTS["mode"]),
            "splash_db": min(max(_float(self.vars["splash_db"].get(), DEFAULTS["splash_db"]), 3.0), 60.0),
            "audio_device": self.vars["audio_device"].get().strip(),
            "conf": conf,
            "watch_seconds": watch,
            "min_bite_delay": min_bite,
            "required_hits": hits,
            "cast_key": cast_key,
            "lure_key": lure_key,
            "lure_minutes": max(_float(self.vars["lure_minutes"].get(), DEFAULTS["lure_minutes"]), 0.5),
            "anti_afk_minutes": min(max(_float(self.vars["anti_afk_minutes"].get(), DEFAULTS["anti_afk_minutes"]), 0.0), 4.5),
            "miss_limit": max(0, int(_float(self.vars["miss_limit"].get(), DEFAULTS["miss_limit"]))),
            "shift_click": bool(self.shift_var.get()),
            "adaptive": bool(self.adaptive_var.get()),
            "save_debug": bool(self.debug_var.get()),
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
        try:
            save_settings_file(settings)
        except Exception as e:
            self.log(f"Could not save settings: {e}")
        run_duration = settings["hours"] * 3600
        self.running = True
        self.start_btn.configure(state="disabled")
        self.status.configure(text=f"Running on {len(windows)} client(s)...", text_color="lime")
        lure = f"key '{settings['lure_key']}' every {settings['lure_minutes']:.0f} min" if settings["lure_key"] else "off"
        device = settings["audio_device"] or "default"
        self.log(
            f"Starting {len(windows)} bot(s) (v{__version__}): mode={settings['mode']}, splash needs +{settings['splash_db']:.0f} dB, "
            f"device='{device}', conf>={settings['conf']:.2f}, watch={settings['watch_seconds']:.0f}s, "
            f"min bite delay={settings['min_bite_delay']:.0f}s, hits={settings['required_hits']}, "
            f"key='{settings['cast_key']}', lure={lure}, anti-AFK jump every {settings['anti_afk_minutes']:.0f} min, "
            f"stop after {settings['miss_limit']} missed bobbers, "
            f"adaptive={settings['adaptive']}, shift-click={settings['shift_click']}"
        )
        self.log("Loading model in worker process(es) -- first cast starts after warm-up.")

        # A bot killed by STOP while holding the click lock never releases it,
        # and a kill mid-put can wedge the queue, so neither survives a run.
        self.debug_queue = Queue()
        self.click_lock = Lock()
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
        try:
            # A bot killed between Shift-down and Shift-up would leave it held.
            _send_inputs(key_input(VK_SHIFT, up=True))
        except Exception:
            pass
        if self.running:
            self.log("All bots stopped.")
        self.running = False
        self.start_btn.configure(state="normal")
        self.status.configure(text="Stopped by user", text_color="red")

    def on_close(self):
        try:
            save_settings_file(self.read_settings())
        except Exception:
            pass
        self.stop_bots()
        self.root.destroy()

    def mainloop(self):
        self.root.mainloop()


if __name__ == "__main__":
    freeze_support()
    FishingBotApp().mainloop()
