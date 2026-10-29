# 🚨 Hazardous Zone Detector

AI-powered hazardous zone detection for the **NVIDIA Jetson Orin** and **Windows**. A camera
watches a danger zone you draw on screen. When a **hand, face or body** enters it, an Arduino
buzzer sounds so the machine stops.

Everything runs on **one browser screen**: live video, drawing the zone, alarm status, an animated
machine simulator, detection stats, an event log and settings. A built-in **Simulator** lets you
try it with no camera or Arduino.

**Open it at http://127.0.0.1:8765/start** (the same address for your dashboard card).

## 🚀 Quick start
**Step-by-step instructions and troubleshooting: [SETUP_GUIDE.md](SETUP_GUIDE.md)**

| | Windows | Jetson / Linux |
|---|---|---|
| 1. Install (once) | double-click `install_windows.bat` | `./install_jetson.sh` |
| 2. Start | **Hazardous Zone Detector** Desktop shortcut (or `start.bat`), runs with no window | `./start.sh` |
| Stop | **Stop Hazardous Zone Detector** shortcut (or `stop.bat`) | Ctrl+C |
| Start on boot | — | `./install_jetson.sh --service` |
| Check the setup | `.venv\Scripts\python scripts\check_setup.py` | `.venv/bin/python scripts/check_setup.py` |

Windows needs Python 3.12 (64-bit) from python.org with "Add python.exe to PATH" ticked.

## 🖥️ Using the screen
| Action | How |
|--------|-----|
| Draw / edit the danger zone | **Draw zone**, tap the corners, then tap the first corner or **Arm zone**. Drag corners to adjust. |
| Remove the zone | **Reset zone** (tap twice: this disarms monitoring) |
| Pause / resume monitoring | **Stop** (tap twice) / **Start** |
| Test without a camera | Top bar → **Simulator**. Tap or drag on the video to move the worker, or press **Auto walk** |
| Camera, serial port, confidence, grace frames | ⚙️ **Settings**, which also has a 1-second **Test buzzer** button |
| Alarm sound in the browser | 🔊 button in the top bar |

The **machine simulator** card shows what the buzzer does to the machine: it runs while the zone
is clear, brakes to an emergency stop when someone enters, and spins back up once the zone is clear.

The layout adapts to the screen, from 7" Jetson touchscreens to 4K monitors, tablets and phones.

## 🛡️ Safety behaviour
- If the camera disconnects while a zone is armed, the buzzer turns **on**, so the machine stays
  stopped until the camera is back. You can turn this off in Settings ("Fail-safe").
- If the detector crashes while a zone is armed, the buzzer also stays on.
- If the Arduino is unplugged and plugged back in during an alarm, the alarm is sent again.
- Resetting the zone or stopping monitoring turns the buzzer off.
- The server only accepts connections from this computer. To open it from another machine, start
  it with `HAZARD_HOST=0.0.0.0`. Anyone who can reach port 8765 can then control it.

## 🔌 Hardware
| Device | Connection |
|--------|------------|
| Arduino (buzzer) | USB serial, 9600 baud. Auto-detects `/dev/ttyACM*` / `/dev/ttyUSB*` on Linux and USB COM ports on Windows, or pick one in ⚙️ Settings |
| USB camera | index `0` by default, changeable in ⚙️ Settings |

Serial protocol: `D\n` = person detected (buzzer on), `S\n` = safe (buzzer off).

## 📁 Project Structure
```
├── hazard_server.py        # web server: UI, video stream, API (port 8765)
├── detector_engine.py      # camera/simulator, hand/face/body detection, zone + buzzer logic
├── web/                    # the single-screen UI (index.html, style.css, app.js)
├── models/                 # MediaPipe models (hand_landmarker.task, face_detector.tflite)
├── config/                 # zone_config.json, settings.json (created from the UI)
├── logs/                   # hazard_log.txt, server_output.log (created at runtime)
├── assets/                 # YASH logo
├── scripts/
│   ├── launcher.py         # starts/stops the detector in the background, makes the shortcuts
│   ├── check_setup.py      # checks Python, libraries, models, camera, serial port
│   └── start_with_console.bat   # Windows: run with a console window, for troubleshooting
├── install_windows.bat / install_jetson.sh   # one-time setup
├── start.bat / start.sh    # start
├── stop.bat                # stop (Windows)
├── requirements.txt
└── SETUP_GUIDE.md
```

## 🛠️ Tech Stack
- **Platform:** NVIDIA Jetson Orin, Windows 10/11
- **Language:** Python 3.12
- **Libraries:** OpenCV, MediaPipe, NumPy, Flask, PySerial
- **Hardware:** Arduino (buzzer), USB camera

## 👤 Author
**Somya** — NVIDIA Jetson Orin Project
