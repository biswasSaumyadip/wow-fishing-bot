# WoW Fishing Bot (YOLO)

Automated fishing bot for World of Warcraft using YOLOv8 object detection + window capture.

> **Warning**: Using bots violates Blizzard's Terms of Service.  
> Use at your own risk. This project is for educational / research purposes only.

## Features

- Multi-client support (detects all open WoW windows)
- YOLO-based splash / catchclick detection
- Clean dark GUI with live debug log
- Configurable run duration (hours)
- Automatic lure reapplication + junk deletion

## Requirements

- Windows 10/11
- Python 3.10 or newer
- World of Warcraft (Retail or Classic)
- NVIDIA GPU recommended (CUDA) for faster YOLO inference

## Installation

1. **Clone the repository**
   ```bash
   git clone https://github.com/YOUR_USERNAME/wow-fishing-bot.git
   cd wow-fishing-bot
   ```

2. **(Recommended) Create a virtual environment**
   ```bash
   python -m venv venv
   venv\Scripts\activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Add the model**
   - Place your trained `best.pt` file in the same folder as `fishing_bot.py`.
   - The script will automatically find it.

## Usage

1. Open one or more WoW clients and log in.
2. Make sure these keybinds match the bot (or change them in the code):
   - `1` → Cast fishing
   - `3`, `4`, `5` → Delete junk items
   - `9` → Apply lure
3. Run the bot:
   ```bash
   python fishing_bot.py
   ```
4. Click **▶ START** and set the desired hours.
5. Click **■ STOP** to terminate all bot instances.

## Notes

- The bot only works while the WoW window is **visible** (not minimized).
- Run as Administrator if mouse/keyboard simulation fails.
- Default detection confidence is `0.65` (can be changed in the code).
- The model looks for the class name `catchclick`.

## Model

`best.pt` is a custom-trained YOLOv8 model that detects the fishing splash / catchclick indicator.

If the file is large (>50–100 MB), consider using [Git LFS](https://git-lfs.github.com/) when uploading:

```bash
git lfs install
git lfs track "*.pt"
git add .gitattributes
```

## Disclaimer

This software is provided "as is". The authors are not responsible for any account bans, data loss, or other consequences resulting from its use.
