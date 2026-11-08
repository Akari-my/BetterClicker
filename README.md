<p align="center">
<img width="2048" height="255" alt="minecraft_title" src="https://github.com/user-attachments/assets/684c8f4f-95a5-466f-9447-2662e6e68e76" />
</p>

## Features

- **Start/Stop hotkey toggle** (global while the app is open)
  - Default hotkey: `F6`
  - **Configurable hotkey**: use **Change hotkey**, then press any key to set a new Start/Stop key (`Esc` cancels)
- **Click interval** (UI cadence)
  - Set hours, minutes, seconds, and milliseconds between click cycles
- **Click options**
  - Mouse button: **Left / Right / Middle**
  - Click type: **Single / Double**
- **Repeat modes**
  - Repeat a fixed number of times
  - Repeat until stopped
- **Click behavior**
  - Clicks at the **current cursor position**
  - Uses Windows SendInput/Win32 APIs via `ctypes`
  - Includes proven down/up delay handling in the click actuation path
- **Modern UI**
  - Black & violet theme
  - Clear running/idle status and click count feedback
- **Custom app icon**
  - Window icon support and packaged executable icon (when built with the provided assets)

## Requirements

- **Windows**
- **Python 3.10+** (tested with Python 3.12)
- No third-party runtime dependencies (uses Python standard library: `tkinter`, `ctypes`, etc.)

## Run from source

From the `app/` directory:

```powershell
python main.py
```

## Build a Windows executable (PyInstaller)

From the `app/` directory (with PyInstaller installed):

```powershell
python -m PyInstaller --noconfirm --clean --onefile --windowed --name BetterClicker --icon "assets\icon.ico" --add-data "assets\icon.png;assets" main.py
```

The executable will be created in the selected dist/output folder.

## Project structure

- `app/main.py` — main application (UI + click logic)
- `app/assets/icon.png` — app icon (PNG)
- `app/assets/icon.ico` — Windows executable icon
- `app/pyproject.toml` — project metadata
- `README.md` — this file

## Notes

- Hotkeys are detected while the application is running.
- If Windows Explorer shows an old icon after rebuilding, it may be due to the Windows icon cache; restarting Explorer or refreshing the icon cache usually fixes it.

## Credits

- **by @Akari-my**
