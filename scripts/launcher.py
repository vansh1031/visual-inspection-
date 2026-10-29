"""Starts and stops the Hazardous Zone Detector in the background.

    launcher.py                    start it (no console window) and open it in the browser
    launcher.py --stop             stop it
    launcher.py --shortcuts        create Desktop and Start-menu shortcuts (Windows)

Run it with pythonw.exe on Windows so not even the launcher has a window.
The server's output goes to logs/server_output.log; if it fails to start, a
message box shows the end of that log.
"""

import json
import os
import socket
import struct
import subprocess
import sys
import time
import webbrowser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_FILE = os.path.join(ROOT, ".run.json")      # PIDs of the servers we started
APPS = {
    "hazard": {"name": "Hazardous Zone Detector", "dir": ".",
               "server": "hazard_server.py", "port": 8765},
}
WINDOWS = os.name == "nt"
NO_WINDOW = 0x08000000 if WINDOWS else 0              # CREATE_NO_WINDOW


def message(text, title="Hazardous Zone Detector", error=False):
    """A desktop message box on Windows; plain output elsewhere."""
    if WINDOWS:
        import ctypes
        flags = 0x10 if error else 0x40               # MB_ICONERROR / MB_ICONINFORMATION
        ctypes.windll.user32.MessageBoxW(None, text, title, flags | 0x10000)   # MB_SETFOREGROUND
    else:
        print(f"{title}: {text}", file=sys.stderr if error else sys.stdout)


def port_open(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def url(app):
    return f"http://127.0.0.1:{APPS[app]['port']}/start"


def server_python():
    """The interpreter for the servers: the window-less pythonw.exe on Windows."""
    exe = sys.executable
    if WINDOWS and os.path.basename(exe).lower() == "python.exe":
        candidate = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(candidate):
            return candidate
    return exe


def log_tail(path, lines=12):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-lines:]).strip()
    except OSError:
        return "(no output was written)"


def load_run():
    try:
        with open(RUN_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_run(data):
    with open(RUN_FILE, "w") as f:
        json.dump(data, f)


def start(apps):
    running = load_run()
    procs = {}
    for app in apps:
        spec = APPS[app]
        if port_open(spec["port"]):
            continue                                  # already running: just open it
        app_dir = os.path.join(ROOT, spec["dir"])
        os.makedirs(os.path.join(app_dir, "logs"), exist_ok=True)
        log_path = os.path.join(app_dir, "logs", "server_output.log")
        log = open(log_path, "w", encoding="utf-8")
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", OPEN_BROWSER="0")
        proc = subprocess.Popen([server_python(), spec["server"]], cwd=app_dir, env=env,
                                stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                creationflags=NO_WINDOW, start_new_session=not WINDOWS)
        procs[app] = (proc, log_path)
        running[app] = proc.pid
    save_run(running)

    # Wait until each server answers (the first start can take a while on
    # Windows while antivirus scans the AI libraries), then open it.
    deadline = time.time() + 180
    pending = list(apps)
    while pending and time.time() < deadline:
        for app in list(pending):
            if port_open(APPS[app]["port"]):
                webbrowser.open(url(app))
                pending.remove(app)
            elif app in procs and procs[app][0].poll() is not None:
                proc, log_path = procs[app]
                pending.remove(app)
                running.pop(app, None)
                save_run(running)
                message(f"{APPS[app]['name']} could not start.\n\n{log_tail(log_path)}\n\n"
                        f"Full log: {os.path.normpath(log_path)}\n\nIf this mentions a missing "
                        "module, run install_windows.bat (Jetson: ./install_jetson.sh) again.",
                        error=True)
        time.sleep(0.5)
    for app in pending:
        message(f"{APPS[app]['name']} is taking unusually long to start.\n\n"
                f"Log: {os.path.normpath(os.path.join(ROOT, APPS[app]['dir'], 'logs', 'server_output.log'))}",
                error=True)


def stop():
    running = load_run()
    stopped = []
    for app, pid in running.items():
        try:
            if WINDOWS:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                               creationflags=NO_WINDOW)
            else:
                os.killpg(pid, 15)
            stopped.append(APPS[app]["name"])
        except (OSError, KeyError):
            pass
    save_run({})
    for _ in range(20):                               # give the servers a moment to exit
        if not any(port_open(spec["port"]) for spec in APPS.values()):
            break
        time.sleep(0.25)
    still = [APPS[a]["name"] for a in APPS if port_open(APPS[a]["port"])]
    text = "Stopped." if stopped else "The detector was not running (from the launcher)."
    if still:
        text += ("\n\nIt is still running because it was started another way "
                 "(for example scripts\\start_with_console.bat). Close that window to stop it.")
    message(text)


def write_icon(path):
    """Make a Windows .ico from the logo (a PNG stored inside an ICO container)."""
    import cv2
    import numpy as np
    logo = cv2.imread(os.path.join(ROOT, "assets", "yash_logo.png"))
    size = 256
    canvas = np.full((size, size, 3), 255, np.uint8)
    h, w = logo.shape[:2]
    scale = (size - 16) / max(h, w)
    logo = cv2.resize(logo, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    y, x = (size - logo.shape[0]) // 2, (size - logo.shape[1]) // 2
    canvas[y:y + logo.shape[0], x:x + logo.shape[1]] = logo
    ok, png = cv2.imencode(".png", canvas)
    data = png.tobytes()
    header = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(data), 6 + 16)
    with open(path, "wb") as f:
        f.write(header + entry + data)


def shortcuts():
    if not WINDOWS:
        print("Shortcuts are only created on Windows. On the Jetson use ./start.sh "
              "or ./install_jetson.sh --service.")
        return
    icon = os.path.join(ROOT, "assets", "hazard_detector.ico")
    try:
        write_icon(icon)
    except Exception:
        icon = ""
    pyw = server_python()
    launcher = os.path.abspath(__file__)
    # PowerShell creates the .lnk files. The values go in as named -File
    # parameters, never pasted into script text, so paths with spaces are safe.
    ps1 = os.path.join(ROOT, ".make_shortcut.ps1")
    with open(ps1, "w", encoding="utf-8") as f:
        f.write("param($Folder, $Name, $Target, $Launcher, $Extra, $WorkDir, $Icon)\n"
                "$s = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $Folder ($Name + '.lnk')))\n"
                "$s.TargetPath = $Target\n"
                "$s.Arguments = '\"' + $Launcher + '\"' + $(if ($Extra -ne 'none') { ' ' + $Extra } else { '' })\n"
                "$s.WorkingDirectory = $WorkDir\n"
                "if ($Icon -ne 'none') { $s.IconLocation = $Icon }\n"
                "$s.Save()\n")
    folders = []
    for special in ("Desktop", "Programs"):
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              f"[Environment]::GetFolderPath('{special}')"],
                             capture_output=True, text=True, creationflags=NO_WINDOW)
        if out.stdout.strip():
            folders.append(out.stdout.strip())
    made = 0
    for folder in folders:
        for name, extra in (("Hazardous Zone Detector", "none"), ("Stop Hazardous Zone Detector", "--stop")):
            r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1,
                                "-Folder", folder, "-Name", name, "-Target", pyw, "-Launcher", launcher,
                                "-Extra", extra, "-WorkDir", ROOT, "-Icon", icon or "none"],
                               capture_output=True, text=True, creationflags=NO_WINDOW)
            made += r.returncode == 0
            if r.returncode != 0:
                print(r.stderr.strip())
    os.remove(ps1)
    print(f"Created {made} shortcut(s) on the Desktop and in the Start menu.")


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--stop" in args:
        stop()
    elif "--shortcuts" in args:
        shortcuts()
    else:
        start(list(APPS))
