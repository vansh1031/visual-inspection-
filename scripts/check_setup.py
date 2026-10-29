"""Checks that this computer is ready to run the Hazardous Zone Detector.

Works on Windows and on the Jetson / Linux. Run it from the project folder:

    python scripts/check_setup.py            (Windows: .venv\\Scripts\\python ...)

It never changes anything; it only reports what is installed and what is
missing, with a hint for each problem.
"""

import importlib
import importlib.metadata
import os
import platform
import socket
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
problems = 0
warnings = 0


def report(level, what, detail="", hint=""):
    global problems, warnings
    if level == "FAIL":
        problems += 1
    elif level == "WARN":
        warnings += 1
    line = f"  [{level:4}] {what}"
    if detail:
        line += f" - {detail}"
    print(line)
    if hint and level != "OK":
        print(f"         -> {hint}")


def section(title):
    print(f"\n{title}")


print("Hazardous Zone Detector - setup check")
print(f"  {platform.system()} {platform.release()} ({platform.machine()}), "
      f"Python {platform.python_version()}")
print(f"  Interpreter: {sys.executable}")

# ─── Python ───────────────────────────────────────────────
section("Python")
if sys.version_info < (3, 9):
    report("FAIL", "Python version", platform.python_version(), "Install Python 3.12")
elif sys.version_info >= (3, 13):
    report("WARN", "Python version", platform.python_version(),
           "Newer than the tested 3.12; if MediaPipe fails to install, use Python 3.12")
else:
    report("OK", "Python version", platform.python_version())
in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
report("OK" if in_venv else "WARN", "Virtual environment", "active" if in_venv else "not used",
       "Run the installer (install_windows.bat / install_jetson.sh) to create .venv")

# ─── Libraries ────────────────────────────────────────────
section("Libraries")
mods = {}
for name, pip_name, needed_by in [
    ("flask", "flask", "the web screen"),
    ("numpy", "numpy", "the detector"),
    ("cv2", "opencv-contrib-python", "the camera and body detection"),
    ("serial", "pyserial", "the Arduino buzzer"),
    ("mediapipe", "mediapipe", "hand and face detection"),
]:
    try:
        mods[name] = importlib.import_module(name)
        try:
            version = importlib.metadata.version(pip_name)
        except importlib.metadata.PackageNotFoundError:   # e.g. opencv-python-headless
            version = getattr(mods[name], "__version__", "")
        report("OK", name, version)
    except Exception as e:
        level = "WARN" if name == "mediapipe" else "FAIL"
        report(level, name, f"not importable ({e.__class__.__name__}: {e})",
               f"pip install {pip_name}   (needed for {needed_by})")

# ─── AI models ────────────────────────────────────────────
section("AI models")
hand = os.path.join(ROOT, "models", "hand_landmarker.task")
face = os.path.join(ROOT, "models", "face_detector.tflite")
for path in (hand, face):
    if os.path.exists(path) and os.path.getsize(path) > 10_000:
        report("OK", os.path.basename(path), f"{os.path.getsize(path) // 1024} KB")
    else:
        report("FAIL", os.path.basename(path), "missing or empty",
               "Download the repository again (the model files are part of it)")
if "mediapipe" in mods and os.path.exists(hand):
    try:
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision as mp_vision
        det = mp_vision.HandLandmarker.create_from_options(mp_vision.HandLandmarkerOptions(
            base_options=mp_python.BaseOptions(model_asset_path=hand),
            running_mode=mp_vision.RunningMode.IMAGE))
        det.close()
        report("OK", "MediaPipe hand model loads")
    except Exception as e:
        report("WARN", "MediaPipe hand model", f"does not load ({e})",
               "Hand/face detection will be off; body detection and the simulator still work")

# ─── Camera ───────────────────────────────────────────────
section("Camera")
if "cv2" in mods:
    cv2 = mods["cv2"]
    try:
        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_SILENT)
    except Exception:
        pass
    found = False
    for index in range(3):
        backend = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY
        cap = cv2.VideoCapture(index, backend)
        ok, frame = cap.read() if cap.isOpened() else (False, None)
        cap.release()
        if ok and frame is not None:
            h, w = frame.shape[:2]
            report("OK", f"Camera index {index}", f"{w}x{h}")
            found = True
    if not found:
        report("WARN", "No camera found", "",
               "Plug in the USB camera, close other apps using it, or use Simulator mode")
else:
    report("WARN", "Camera check skipped", "OpenCV is not installed")

# ─── Serial ports ─────────────────────────────────────────
section("Serial ports (Arduino)")
if "serial" in mods:
    from serial.tools import list_ports
    ports = list(list_ports.comports())
    usb = [p for p in ports if p.vid is not None]
    for p in ports:
        kind = "USB" if p.vid is not None else "built-in"
        report("OK", p.device, f"{kind}: {p.description}")
    if not usb:
        report("WARN", "No USB serial device", "",
               "Plug in the Arduino; on Windows check Device Manager > Ports (COM & LPT)")
    if os.name != "nt" and usb:
        import grp
        try:
            groups = {grp.getgrgid(g).gr_name for g in os.getgroups()}
        except Exception:
            groups = set()
        if "dialout" not in groups and os.geteuid() != 0:
            report("WARN", "Serial permission", "user is not in the 'dialout' group",
                   f"sudo usermod -aG dialout {os.environ.get('USER', '$USER')}   then log out and in")
else:
    report("WARN", "Serial check skipped", "pyserial is not installed")

# ─── Network ports ────────────────────────────────────────
section("Web ports")
for port, app in ((8765, "Hazardous Zone Detector"),):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        busy = s.connect_ex(("127.0.0.1", port)) == 0
    report("OK", f"Port {port}", f"{app} is already running" if busy else "free")

# ─── Summary ──────────────────────────────────────────────
print()
if problems:
    print(f"Result: {problems} problem(s) to fix before running, {warnings} warning(s).")
elif warnings:
    print(f"Result: ready to run, with {warnings} warning(s). Simulator mode works without hardware.")
else:
    print("Result: everything looks good.")
sys.exit(1 if problems else 0)
