# Realtime Translator — realtime EN→RU call subtitles

Listens to **system audio** (the other party's voice in Discord, Slack, a browser — any
app), transcribes English speech on the GPU via faster-whisper, and shows a **Russian
translation** in an always-on-top overlay window. ~1-2s latency on an NVIDIA GPU.

```
Call audio ─► WASAPI loopback ─► faster-whisper (EN) ─► DeepL (RU) ─► subtitle window
```

## Setup (Windows 11)

1. **Python 3.10-3.12** (not 3.13 — faster-whisper/PyQt wheels are more stable on 3.11/3.12).

2. Virtual environment and dependencies:
   ```powershell
   cd realtime_translator
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

3. **CUDA libraries for GPU.** faster-whisper on GPU needs cuDNN/cuBLAS.
   Easiest way is via pip:
   ```powershell
   pip install nvidia-cudnn-cu12 nvidia-cublas-cu12
   ```
   If the GPU doesn't work — set `DEVICE=cpu` and `COMPUTE_TYPE=int8` in `.env`
   (slower, but works everywhere).

4. **DeepL key** (free, *DeepL API Free* plan, 500k characters/month):
   https://www.deepl.com/pro-api → copy `.env.example` to `.env` and add the key:
   ```powershell
   copy .env.example .env
   # open .env and set DEEPL_API_KEY=...
   ```

## Running it

- **Normally:** double-click the **"Realtime Translator"** shortcut on the desktop.
  No console window appears (it launches via `launch.vbs` → `pythonw.exe` fully
  hidden). Repeated clicks are safe — if the app is already running, clicking again
  does nothing (no duplicate windows).
- **From a terminal (with logs, for debugging):** `.\.venv\Scripts\python.exe main.py`

First run downloads the Whisper model (`large-v3`, ~3GB). After that it's cached.
Click **▶ Start** in the window, wait for the green dot, and start your call.

- Drag the window by its top bar. Resize from the corner or the ▢ button.
- Minimize / maximize / close — buttons in the top-right corner.

### Recreating the desktop shortcut
```powershell
$proj = "$PWD"
$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut("$([Environment]::GetFolderPath('Desktop'))\Realtime Translator.lnk")
$sc.TargetPath = "wscript.exe"; $sc.Arguments = "`"$proj\launch.vbs`""
$sc.WorkingDirectory = $proj; $sc.IconLocation = "$proj\.venv\Scripts\pythonw.exe,0"; $sc.Save()
```

## Configuration (`.env` file)

| Parameter | What it does |
|---|---|
| `MODEL_SIZE` | `large-v3` (accuracy) · `medium`/`small` (faster, less VRAM) |
| `SILENCE_RMS` | Silence threshold. Picking up noise? Raise to `0.012-0.02` |
| `SHOW_ORIGINAL` | `0` — hide the English original, keep only the Russian |
| `FONT_SIZE_RU` | Translation font size |

## Notes

- **You only hear the other party, not yourself** — audio is captured from the output
  device (loopback), your microphone never feeds into it. That's intentional.
- **Headphones.** Works fine with headphones too: loopback taps the stream before the
  output device.
- If nothing shows up — check that Windows has the right **default playback device**
  selected (the one the call's audio is routed to).

## Call log (saved automatically)

Every call (from "Start" to "Stop") is written to its own file in `logs/` — filename =
start date-time (`2026-07-02_18-14-44.txt`). Nothing is deleted automatically; old calls
just accumulate, clean up manually whenever you like — the **"Logs folder"** button at
the bottom of the window opens it in Explorer.

File format — timestamped, EN and RU for each line:
```
[18:14:44] EN: Hello, how are you?
[18:14:44] RU: Привет, как дела?
```

## Reverse direction (you → English) — stage 2

Understanding their speech is done. To make *you* understood in English, a separate
module is needed: you speak Russian → STT → RU→EN translation → TTS → the voice goes
into a **virtual microphone** (VB-CABLE) selected as the input source in Discord.
That's the next step.
