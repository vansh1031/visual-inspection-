"""Web server for the Hazardous Zone Detector.

Serves the single-screen UI and streams the detector's video and state to it.
The dashboard card opens http://127.0.0.1:8765/start, which shows the whole
application on one screen.

    python3 hazard_server.py              # listens on 127.0.0.1:8765
    HAZARD_HOST=0.0.0.0 python3 hazard_server.py   # allow other machines
"""

import json
import os
import socket
import threading
import time
import webbrowser

if __name__ == "__main__":
    print("Loading the Hazardous Zone Detector (the first start can take up to a minute)...", flush=True)

from flask import Flask, Response, jsonify, request, send_from_directory

from detector_engine import BASE_DIR, HazardEngine, SerialLink

WEB_DIR    = os.path.join(BASE_DIR, "web")
ASSETS_DIR = os.path.join(BASE_DIR, "assets")

app = Flask(__name__, static_folder=WEB_DIR, static_url_path="/static")
engine = HazardEngine()


@app.route("/")
@app.route("/start")
def index():
    engine.start()
    return send_from_directory(WEB_DIR, "index.html")


@app.route("/logo.svg")
def logo():
    return send_from_directory(ASSETS_DIR, "yash_logo.svg")


@app.route("/video_feed")
def video_feed():
    def frames():
        last_id = -1
        while True:
            with engine.frame_cond:
                engine.frame_cond.wait_for(lambda: engine.frame_id != last_id, timeout=2)
                jpg, last_id = engine.frame_jpeg, engine.frame_id
            if jpg is None:
                continue
            yield (b"--frame\r\nContent-Type: image/jpeg\r\n"
                   b"Content-Length: " + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")

    return Response(frames(), mimetype="multipart/x-mixed-replace; boundary=frame",
                    headers={"Cache-Control": "no-store"})


@app.route("/events")
def events():
    """Server-sent events: the engine state ~10x a second plus new log entries."""
    def stream():
        seq = max(0, engine.event_seq - 50)
        yield "retry: 1000\n\n"
        while True:
            for event in engine.events_since(seq):
                seq = event["id"]
                yield f"event: log\ndata: {json.dumps(event)}\n\n"
            yield f"event: state\ndata: {json.dumps(engine.snapshot())}\n\n"
            time.sleep(0.1)

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.get("/api/state")
def state():
    return jsonify(engine.snapshot())


@app.post("/api/engine")
def engine_control():
    action = (request.get_json(silent=True) or {}).get("action")
    if action == "start":
        engine.start()
    elif action == "stop":
        engine.stop()
    else:
        return jsonify(error="action must be 'start' or 'stop'"), 400
    return jsonify(running=engine.running)


@app.route("/api/zone", methods=["POST", "DELETE"])
def zone():
    if request.method == "DELETE":
        engine.clear_zone()
        return jsonify(zone=[])
    points = (request.get_json(silent=True) or {}).get("points", [])
    try:
        engine.set_zone(points)
    except (TypeError, ValueError) as e:
        return jsonify(error=str(e)), 400
    return jsonify(zone=[list(p) for p in engine.zone])


@app.post("/api/settings")
def settings():
    try:
        return jsonify(engine.update_settings(request.get_json(silent=True) or {}))
    except (TypeError, ValueError) as e:
        return jsonify(error=str(e)), 400


@app.get("/api/serial/ports")
def serial_ports():
    return jsonify(ports=SerialLink.available_ports())


@app.post("/api/buzzer/test")
def buzzer_test():
    if not engine.test_buzzer():
        return jsonify(error="Buzzer is already sounding"), 409
    return jsonify(ok=True)


@app.post("/api/sim")
def sim():
    data = request.get_json(silent=True) or {}
    if data.get("mode") == "auto":
        engine.simulate()
    else:
        try:
            engine.simulate(float(data["x"]), float(data["y"]))
        except (KeyError, TypeError, ValueError):
            return jsonify(error="x and y are required"), 400
    return jsonify(ok=True)


def port_in_use(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1" if host == "0.0.0.0" else host, port)) == 0


def open_browser_when_ready(port, url):
    """Print the address and open it in the browser once the server answers.

    Opening on a timer showed "connection refused" whenever the first start
    (loading OpenCV / MediaPipe, antivirus scans on Windows) took longer.
    """
    def wait():
        for _ in range(240):
            if port_in_use("127.0.0.1", port):
                print(f"\nReady: {url}\n", flush=True)
                if os.environ.get("OPEN_BROWSER") == "1":
                    webbrowser.open(url)
                return
            time.sleep(0.5)
    threading.Thread(target=wait, daemon=True).start()


if __name__ == "__main__":
    host = os.environ.get("HAZARD_HOST", "127.0.0.1")
    port = int(os.environ.get("HAZARD_PORT", "8765"))
    url = f"http://127.0.0.1:{port}/start"
    if port_in_use(host, port):
        # Usually the app is already running (started twice); just show it.
        print(f"Port {port} is already in use - the app is probably already running: {url}")
        if os.environ.get("OPEN_BROWSER") == "1":
            webbrowser.open(url)
        raise SystemExit(0)
    engine.start()
    open_browser_when_ready(port, url)
    app.run(host=host, port=port, threaded=True)
