"""Hazard detection engine.

Captures frames from the camera (or the built-in simulator), runs the hand /
face / body detectors, decides whether a person is inside the danger zone and
drives the Arduino buzzer over serial.

It runs in a background thread and never opens a window of its own: the web
UI served by hazard_server.py shows the video, the zone and the status, so the
whole application lives on one screen.

All coordinates handed to the UI (zone points, detection boxes, landmarks) are
normalised to 0..1, so the zone keeps its place if the camera resolution
changes.
"""

import collections
import glob
import json
import math
import os
import threading
import time

import cv2
import numpy as np

try:
    import serial
    from serial.tools import list_ports
except ImportError:          # the UI still works, the buzzer just stays offline
    serial = list_ports = None

try:
    import mediapipe as mp
    from mediapipe.tasks import python as mp_python
    from mediapipe.tasks.python import vision as mp_vision
except Exception:            # missing package or missing GPU/EGL libraries
    mp = None

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR    = os.path.join(BASE_DIR, "config")
LOG_DIR       = os.path.join(BASE_DIR, "logs")
ZONE_FILE     = os.path.join(CONFIG_DIR, "zone_config.json")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")
LOG_FILE      = os.path.join(LOG_DIR, "hazard_log.txt")
HAND_MODEL    = os.path.join(BASE_DIR, "models", "hand_landmarker.task")
FACE_MODEL    = os.path.join(BASE_DIR, "models", "face_detector.tflite")

DEFAULT_SETTINGS = {
    "source":        "camera",    # "camera" | "simulator"
    "camera_index":  0,
    "width":         1280,
    "height":        720,
    "serial_port":   "auto",      # "auto" or a device path such as /dev/ttyACM0
    "baud":          9600,
    "grace_frames":  5,           # clear frames needed before the buzzer stops
    "hand_conf":     0.6,
    "face_conf":     0.6,
    "body_detection": True,
    "fail_safe":     True,        # treat a lost camera as an alarm
    "jpeg_quality":  80,
}

HAND_CONNECTIONS = [
    (0, 1), (1, 2), (2, 3), (3, 4),
    (0, 5), (5, 6), (6, 7), (7, 8),
    (0, 9), (9, 10), (10, 11), (11, 12),
    (0, 13), (13, 14), (14, 15), (15, 16),
    (0, 17), (17, 18), (18, 19), (19, 20),
    (5, 9), (9, 13), (13, 17),
]


# ─── ZONE GEOMETRY ────────────────────────────────────────
def point_in_zone(zone, pt):
    if len(zone) < 3:
        return False
    poly = np.array(zone, np.float32)
    return cv2.pointPolygonTest(poly, (float(pt[0]), float(pt[1])), False) >= 0


def box_in_zone(zone, box):
    """True if a normalised (x, y, w, h) box overlaps the zone polygon."""
    if len(zone) < 3:
        return False
    x, y, w, h = box
    probes = [(x + w * fx, y + h * fy)
              for fx in (0.0, 0.5, 1.0) for fy in (0.0, 0.5, 1.0)]
    if any(point_in_zone(zone, p) for p in probes):
        return True
    # A zone smaller than the box: one of its corners sits inside the box.
    return any(x <= zx <= x + w and y <= zy <= y + h for zx, zy in zone)


# ─── SERIAL / ARDUINO ─────────────────────────────────────
class SerialLink:
    AUTO_PORTS = ["/dev/ttyACM0", "/dev/ttyACM1", "/dev/ttyUSB0", "/dev/ttyUSB1"]
    RETRY_SECONDS = 5

    def __init__(self, log):
        self.log       = log
        self.ser       = None
        self.port      = None
        self.wanted    = "auto"
        self.baud      = 9600
        self._last_try = 0.0
        self._lock     = threading.Lock()

    @staticmethod
    def available_ports():
        """Serial ports present now: COM3, COM4... on Windows, /dev/tty* on Linux."""
        ports = [p.device for p in list_ports.comports()] if list_ports else []
        if os.name != "nt":
            ports += glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*")
        return sorted(set(ports))

    @classmethod
    def auto_ports(cls):
        """Ports to try on "auto": USB serial devices only (the Arduino), so a
        built-in or Bluetooth serial port is never mistaken for the buzzer."""
        if os.name == "nt":
            return [p.device for p in list_ports.comports() if p.vid is not None] if list_ports else []
        return cls.AUTO_PORTS + [p for p in cls.available_ports()
                                 if p not in cls.AUTO_PORTS and ("ACM" in p or "USB" in p)]

    @property
    def connected(self):
        return self.ser is not None and self.ser.is_open

    def configure(self, port, baud):
        with self._lock:
            if port == self.wanted and baud == self.baud:
                return
            self.wanted, self.baud = port, baud
            self._close()
            self._last_try = 0.0

    def maintain(self):
        """Try to (re)connect. Returns True only when a new connection was made."""
        if serial is None or self.connected:
            return False
        if time.time() - self._last_try < self.RETRY_SECONDS:
            return False
        self._last_try = time.time()
        ports = self.auto_ports() if self.wanted == "auto" else [self.wanted]
        for port in ports:
            try:
                ser = serial.Serial(port=port, baudrate=self.baud, timeout=1)
            except Exception:
                continue
            time.sleep(2)            # the Arduino resets when the port opens
            with self._lock:
                self.ser, self.port = ser, port
            self.log("info", f"Arduino connected on {port}")
            return True
        return False

    def send(self, data):
        with self._lock:
            if not self.connected:
                return False
            try:
                self.ser.write(data)
                return True
            except Exception as e:
                self.log("warn", f"Serial write failed ({e}), reconnecting")
                self._close()
                return False

    def close(self):
        with self._lock:
            self._close()

    def _close(self):
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser, self.port = None, None


# ─── SIMULATOR ────────────────────────────────────────────
class SimulatedScene:
    """A synthetic factory floor with one walking worker.

    It needs no camera and no models: the worker's head, hands and body are
    drawn into the frame and reported as detections, so the zone, alarm and
    buzzer logic can be tested end to end. The worker walks a patrol route on
    its own, or follows a target set from the UI.
    """

    PATROL = [(0.12, 0.80), (0.42, 0.62), (0.70, 0.86), (0.88, 0.58),
              (0.55, 0.50), (0.25, 0.58)]
    SPEED  = 0.16                  # normalised floor units per second

    def __init__(self, width=1280, height=720):
        self.w, self.h  = width, height
        self.pos        = list(self.PATROL[0])   # feet position
        self.target     = None                   # manual target, or None
        self._leg       = 1
        self._pause     = 0.0
        self._phase     = 0.0
        self._last      = time.time()
        self._bg        = self._draw_background()

    def set_target(self, x, y):
        self.target = (min(max(x, 0.03), 0.97), min(max(y, 0.40), 0.97))

    def auto(self):
        self.target = None

    def _draw_background(self):
        w, h = self.w, self.h
        img = np.zeros((h, w, 3), np.uint8)
        # Back wall and floor
        horizon = int(h * 0.36)
        for y in range(h):
            if y < horizon:
                c = 34 + int(14 * y / horizon)
                img[y] = (c + 6, c + 2, c)
            else:
                t = (y - horizon) / (h - horizon)
                c = 46 + int(30 * t)
                img[y] = (c, c + 3, c + 6)
        # Floor perspective grid
        vp = (w // 2, horizon - int(h * 0.25))
        for i in range(-12, 13):
            x_bottom = w // 2 + i * w // 10
            cv2.line(img, (x_bottom, h), vp, (70, 74, 78), 1, cv2.LINE_AA)
        y = horizon
        step = 10
        while y < h:
            cv2.line(img, (0, y), (w, y), (66, 70, 74), 1, cv2.LINE_AA)
            y += step
            step = int(step * 1.35) + 2
        img[:horizon] = cv2.GaussianBlur(img[:horizon], (5, 5), 0)
        cv2.line(img, (0, horizon), (w, horizon), (90, 94, 98), 2)
        # Wall panels and windows
        for x in range(40, w, 220):
            cv2.rectangle(img, (x, int(h * 0.06)), (x + 150, int(h * 0.2)),
                          (70, 62, 52), -1)
            cv2.rectangle(img, (x, int(h * 0.06)), (x + 150, int(h * 0.2)),
                          (96, 88, 78), 2)
        # Machine: a press on the right with hazard striping on the floor
        mx, my = int(w * 0.62), int(h * 0.16)
        mw, mh = int(w * 0.22), int(h * 0.36)
        cv2.rectangle(img, (mx, my), (mx + mw, my + mh), (60, 92, 120), -1)
        cv2.rectangle(img, (mx + 14, my + 14), (mx + mw - 14, my + mh // 2),
                      (40, 60, 80), -1)
        cv2.rectangle(img, (mx + mw // 2 - 24, my + mh // 2),
                      (mx + mw // 2 + 24, my + mh - 20), (150, 160, 170), -1)
        cv2.rectangle(img, (mx - 20, my + mh - 20), (mx + mw + 20, my + mh),
                      (40, 44, 48), -1)
        for i in range(0, mw + 40, 28):
            p = np.array([[mx - 20 + i, my + mh], [mx - 6 + i, my + mh],
                          [mx - 20 + i, my + mh + 14], [mx - 34 + i, my + mh + 14]])
            cv2.fillPoly(img, [p], (0, 200, 240))
        cv2.putText(img, "PRESS 01", (mx + 20, my + 40),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (210, 220, 230), 2, cv2.LINE_AA)
        # Pallets and crates on the left
        for (cx, cy, s) in [(0.08, 0.46, 70), (0.16, 0.44, 54), (0.32, 0.42, 44)]:
            x0, y0 = int(w * cx), int(h * cy)
            cv2.rectangle(img, (x0, y0 - s), (x0 + s, y0), (40, 90, 140), -1)
            cv2.rectangle(img, (x0, y0 - s), (x0 + s, y0), (30, 60, 100), 2)
            cv2.line(img, (x0, y0 - s), (x0 + s, y0), (30, 60, 100), 2)
        cv2.putText(img, "SIMULATOR", (w - 190, h - 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (120, 200, 255), 2, cv2.LINE_AA)
        return img

    def _advance(self, dt):
        goal = self.target or self.PATROL[self._leg]
        dx, dy = goal[0] - self.pos[0], goal[1] - self.pos[1]
        dist = math.hypot(dx, dy)
        moving = dist > 0.004
        if self.target is None and not moving:
            if self._pause <= 0:
                self._pause = 1.2
            self._pause -= dt
            if self._pause <= 0:
                self._leg = (self._leg + 1) % len(self.PATROL)
        elif moving:
            step = min(dist, self.SPEED * dt * (1.6 if self.target else 1.0))
            self.pos[0] += dx / dist * step
            self.pos[1] += dy / dist * step
            self._phase += dt * 9
        return moving

    def read(self):
        now = time.time()
        dt, self._last = min(now - self._last, 0.1), now
        moving = self._advance(dt)

        frame = self._bg.copy()
        w, h = self.w, self.h
        fx, fy = self.pos[0] * w, self.pos[1] * h
        scale = 0.55 + 0.9 * (self.pos[1] - 0.4)        # nearer = bigger
        height = h * 0.42 * scale
        unit = height / 10.0
        swing = math.sin(self._phase) * (1.0 if moving else 0.0)

        def P(dx, dy):
            return int(fx + dx * unit), int(fy - dy * unit)

        cv2.ellipse(frame, P(0, 0), (int(unit * 2.4), int(unit * 0.6)), 0, 0, 360,
                    (28, 30, 32), -1, cv2.LINE_AA)
        th = max(2, int(unit * 0.9))
        # Legs
        cv2.line(frame, P(-0.6, 4.6), P(-0.6 + swing * 1.2, 0.2), (70, 50, 40), th, cv2.LINE_AA)
        cv2.line(frame, P(0.6, 4.6), P(0.6 - swing * 1.2, 0.2), (80, 58, 46), th, cv2.LINE_AA)
        # Torso: hi-vis vest with reflective strips
        x0, y0 = P(-1.4, 8.0)
        x1, y1 = P(1.4, 4.4)
        cv2.rectangle(frame, (x0, y0), (x1, y1), (0, 140, 255), -1)
        for sy in (6.9, 5.4):
            a, b = P(-1.4, sy), P(1.4, sy - 0.35)
            cv2.rectangle(frame, a, b, (210, 230, 230), -1)
        # Arms and hands
        hands = []
        for side, sgn in ((-1, 1), (1, -1)):
            shoulder = P(side * 1.5, 7.7)
            hand = P(side * 2.0 + sgn * swing * 0.9, 4.9)
            cv2.line(frame, shoulder, hand, (0, 120, 230), max(2, int(unit * 0.7)), cv2.LINE_AA)
            cv2.circle(frame, hand, max(3, int(unit * 0.45)), (120, 160, 210), -1, cv2.LINE_AA)
            hands.append(hand)
        # Head with hard hat
        head = P(0, 9.0)
        r = max(4, int(unit * 0.95))
        cv2.circle(frame, head, r, (120, 160, 210), -1, cv2.LINE_AA)
        cv2.ellipse(frame, (head[0], head[1] - r // 3), (int(r * 1.15), r), 0, 180, 360,
                    (0, 215, 255), -1, cv2.LINE_AA)

        def nbox(x0, y0, x1, y1):
            return [x0 / w, y0 / h, (x1 - x0) / w, (y1 - y0) / h]

        bx0, by0 = P(-2.6, 10.2)
        bx1, by1 = P(2.6, 0)
        detections = [
            {"kind": "body", "box": nbox(bx0, by0, bx1, by1)},
            {"kind": "face", "box": nbox(head[0] - r, head[1] - r, head[0] + r, head[1] + r)},
        ]
        hr = max(6, int(unit * 0.8))
        for hx, hy in hands:
            detections.append({
                "kind":   "hand",
                "box":    nbox(hx - hr, hy - hr, hx + hr, hy + hr),
                "points": [[hx / w, hy / h]],
            })
        return frame, detections


# ─── ENGINE ───────────────────────────────────────────────
class HazardEngine:
    def __init__(self):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        os.makedirs(LOG_DIR, exist_ok=True)
        self._lock       = threading.RLock()
        self._stop       = threading.Event()
        self._thread     = None
        self.frame_cond  = threading.Condition()
        self.frame_jpeg  = None
        self.frame_id    = 0

        self.events      = collections.deque(maxlen=200)
        self.event_seq   = 0

        self.settings    = self._load_settings()
        self.zone, self._legacy_zone = self._load_zone()
        self.serial      = SerialLink(self.log)
        self.serial.configure(self.settings["serial_port"], self.settings["baud"])
        self.sim         = None
        self.sim_control = None        # pending (x, y) target or "auto"

        self._detectors_dirty = True
        self._camera_dirty    = True
        self.hand_det = self.face_det = self.hog = None
        self.detector_status = {"hand": False, "face": False, "body": False}

        self.buzzer_on   = False
        self.no_detect   = 0
        self.status      = "stopped"
        self.parts       = []
        self.detections  = []
        self.fps         = 0.0
        self.frame_size  = (self.settings["width"], self.settings["height"])
        self.camera_ok   = False
        self.started_at  = None
        self.alarm_count = 0
        self.last_alarm  = None
        self._alarm_started = None
        self.alarm_seconds  = 0.0

    # ── Persistence ───────────────────────────────────────
    @staticmethod
    def _load_settings():
        settings = dict(DEFAULT_SETTINGS)
        try:
            with open(SETTINGS_FILE) as f:
                settings.update({k: v for k, v in json.load(f).items()
                                 if k in DEFAULT_SETTINGS})
        except (OSError, ValueError):
            pass
        return settings

    def _save_settings(self):
        with open(SETTINGS_FILE, "w") as f:
            json.dump(self.settings, f, indent=2)

    @staticmethod
    def _load_zone():
        """Returns (normalised zone, legacy pixel zone awaiting conversion)."""
        try:
            with open(ZONE_FILE) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return [], None
        points = [tuple(p) for p in data.get("zone", [])]
        if data.get("normalized"):
            return points, None
        # Zones saved by the old OpenCV window are in pixels; convert them once
        # the first frame tells us the resolution.
        return [], points if len(points) >= 3 else None

    def _save_zone(self):
        if self.zone:
            with open(ZONE_FILE, "w") as f:
                json.dump({"zone": [list(p) for p in self.zone], "normalized": True}, f)
        elif os.path.exists(ZONE_FILE):
            os.remove(ZONE_FILE)

    # ── Event log ─────────────────────────────────────────
    def log(self, level, msg):
        with self._lock:
            self.event_seq += 1
            event = {"id": self.event_seq, "t": time.time(), "level": level, "msg": msg}
            self.events.append(event)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(event["t"]))
        print(f"[{level.upper()}] {msg}")
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"{stamp}  {level.upper():5}  {msg}\n")
        except OSError:
            pass

    def events_since(self, seq):
        with self._lock:
            return [e for e in self.events if e["id"] > seq]

    # ── Control (called from the web server) ──────────────
    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        with self._lock:
            if self.running:
                return
            self._stop.clear()
            self.started_at = time.time()
            self._camera_dirty = True
            self._thread = threading.Thread(target=self._run, name="hazard-engine",
                                            daemon=True)
            self._thread.start()
        self.log("info", "Monitoring started")

    def stop(self):
        thread = self._thread
        if thread is None:
            return
        self._stop.set()
        thread.join(timeout=5)
        self._thread = None
        self.log("info", "Monitoring stopped")

    def set_zone(self, points):
        pts = [(min(max(float(x), 0.0), 1.0), min(max(float(y), 0.0), 1.0))
               for x, y in points]
        if len(pts) < 3:
            raise ValueError("A zone needs at least 3 points")
        with self._lock:
            self.zone = pts
            self._legacy_zone = None
            self._save_zone()
        self.log("info", f"Danger zone armed ({len(pts)} points)")

    def clear_zone(self):
        with self._lock:
            self.zone = []
            self._legacy_zone = None
            self._save_zone()
        self.log("warn", "Danger zone cleared - monitoring disarmed")

    def update_settings(self, changes):
        with self._lock:
            old = dict(self.settings)
            for key, value in changes.items():
                if key not in DEFAULT_SETTINGS:
                    continue
                kind = type(DEFAULT_SETTINGS[key])
                self.settings[key] = value if kind is bool else kind(value)
            if self.settings["source"] not in ("camera", "simulator"):
                self.settings["source"] = old["source"]
            s = self.settings
            if any(s[k] != old[k] for k in ("source", "camera_index", "width", "height")):
                self._camera_dirty = True
            if any(s[k] != old[k] for k in ("hand_conf", "face_conf", "body_detection")):
                self._detectors_dirty = True
            self.serial.configure(s["serial_port"], s["baud"])
            self._save_settings()
        if self.settings["source"] != old["source"]:
            self.log("info", f"Video source: {self.settings['source']}")
        return dict(self.settings)

    def simulate(self, x=None, y=None):
        """Move the simulated worker to (x, y), or back to its patrol with no args."""
        self.sim_control = "auto" if x is None else (float(x), float(y))

    def test_buzzer(self):
        if self.buzzer_on:
            return False
        self.log("info", "Buzzer test")
        self.serial.send(b"D\n")

        def release():
            time.sleep(1.0)
            if not self.buzzer_on:
                self.serial.send(b"S\n")
        threading.Thread(target=release, daemon=True).start()
        return True

    def snapshot(self):
        with self._lock:
            now = time.time()
            alarm_secs = self.alarm_seconds
            if self._alarm_started:
                alarm_secs += now - self._alarm_started
            return {
                "running":     self.running,
                "status":      self.status,
                "parts":       list(self.parts),
                "detections":  list(self.detections),
                "zone":        [list(p) for p in self.zone],
                "buzzer":      self.buzzer_on,
                "fps":         round(self.fps, 1),
                "frame":       {"w": self.frame_size[0], "h": self.frame_size[1]},
                "camera_ok":   self.camera_ok,
                "source":      self.settings["source"],
                "serial":      {"connected": self.serial.connected,
                                "port": self.serial.port,
                                "available": serial is not None},
                "detectors":   dict(self.detector_status),
                "stats":       {"alarms": self.alarm_count,
                                "last_alarm": self.last_alarm,
                                "alarm_seconds": round(alarm_secs, 1),
                                "uptime": round(now - self.started_at, 1)
                                          if self.started_at and self.running else 0},
                "settings":    dict(self.settings),
                "server_time": now,
            }

    # ── Detectors ─────────────────────────────────────────
    def _build_detectors(self):
        s = self.settings
        self.hand_det = self.face_det = None
        if mp is not None:
            try:
                self.hand_det = mp_vision.HandLandmarker.create_from_options(
                    mp_vision.HandLandmarkerOptions(
                        base_options=mp_python.BaseOptions(model_asset_path=HAND_MODEL),
                        running_mode=mp_vision.RunningMode.IMAGE,
                        num_hands=2,
                        min_hand_detection_confidence=s["hand_conf"],
                        min_hand_presence_confidence=s["hand_conf"],
                        min_tracking_confidence=0.5))
            except Exception as e:
                self.log("warn", f"Hand detector unavailable: {e}")
            try:
                self.face_det = mp_vision.FaceDetector.create_from_options(
                    mp_vision.FaceDetectorOptions(
                        base_options=mp_python.BaseOptions(model_asset_path=FACE_MODEL),
                        running_mode=mp_vision.RunningMode.IMAGE,
                        min_detection_confidence=s["face_conf"]))
            except Exception as e:
                self.log("warn", f"Face detector unavailable: {e}")
        else:
            self.log("warn", "MediaPipe not available - hand and face detection off")
        self.hog = None
        if s["body_detection"]:
            self.hog = cv2.HOGDescriptor()
            self.hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
        self.detector_status = {"hand": self.hand_det is not None,
                                "face": self.face_det is not None,
                                "body": self.hog is not None}
        self._detectors_dirty = False

    def _detect(self, frame, frame_count, cached_bodies):
        fh, fw = frame.shape[:2]
        found = []
        if self.hand_det or self.face_det:
            image = mp.Image(image_format=mp.ImageFormat.SRGB,
                             data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            if self.hand_det:
                for lms in self.hand_det.detect(image).hand_landmarks or []:
                    pts = [[lm.x, lm.y] for lm in lms]
                    xs = [p[0] for p in pts]
                    ys = [p[1] for p in pts]
                    pad_x, pad_y = 15 / fw, 15 / fh
                    x0, y0 = max(0, min(xs) - pad_x), max(0, min(ys) - pad_y)
                    x1, y1 = min(1, max(xs) + pad_x), min(1, max(ys) + pad_y)
                    found.append({"kind": "hand", "box": [x0, y0, x1 - x0, y1 - y0],
                                  "points": pts})
            if self.face_det:
                for det in self.face_det.detect(image).detections or []:
                    b = det.bounding_box
                    found.append({"kind": "face",
                                  "box": [b.origin_x / fw, b.origin_y / fh,
                                          b.width / fw, b.height / fh]})
        if self.hog is not None:
            # HOG is the slowest detector: run it on a smaller copy every 4th frame.
            if frame_count % 4 == 0:
                scale = 640 / fw if fw > 640 else 1.0
                small = cv2.resize(frame, None, fx=scale, fy=scale) if scale != 1.0 else frame
                gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
                bodies, _ = self.hog.detectMultiScale(gray, winStride=(8, 8),
                                                      padding=(4, 4), scale=1.05)
                sw, sh = gray.shape[1], gray.shape[0]
                cached_bodies[:] = [[bx / sw, by / sh, bw / sw, bh / sh]
                                    for (bx, by, bw, bh) in bodies]
            found.extend({"kind": "body", "box": list(b)} for b in cached_bodies)
        return found

    # ── Hazard logic ──────────────────────────────────────
    def _set_buzzer(self, on, reason):
        self.buzzer_on = on
        self.serial.send(b"D\n" if on else b"S\n")
        now = time.time()
        if on:
            self.alarm_count += 1
            self.last_alarm = now
            self._alarm_started = now
            self.log("alarm", reason)
        else:
            if self._alarm_started:
                self.alarm_seconds += now - self._alarm_started
            self._alarm_started = None
            self.log("ok", reason)

    def _update_hazard(self, parts, camera_lost=False):
        armed = len(self.zone) >= 3
        if not armed:
            if self.buzzer_on:
                self._set_buzzer(False, "Buzzer OFF - zone disarmed")
            self.status = "disarmed"
            self.no_detect = 0
            return
        if camera_lost:
            if self.settings["fail_safe"] and not self.buzzer_on:
                self._set_buzzer(True, "Camera lost - machine stopped (fail-safe)")
            self.status = "camera_error"
            self.no_detect = 0
            return
        if parts:
            self.no_detect = 0
            if not self.buzzer_on:
                self._set_buzzer(True, f"Human in danger zone: {', '.join(parts)}")
        else:
            self.no_detect += 1
            if self.buzzer_on and self.no_detect >= self.settings["grace_frames"]:
                self._set_buzzer(False, "Zone clear - machine can resume")
        self.status = "alarm" if self.buzzer_on else "safe"

    # ── Capture loop ──────────────────────────────────────
    def _open_camera(self):
        s = self.settings
        # DirectShow opens USB webcams far more reliably than the default on Windows.
        backend = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY
        cap = cv2.VideoCapture(s["camera_index"], backend)
        if not cap.isOpened():
            cap.release()
            return None
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, s["width"])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, s["height"])
        return cap

    def _publish(self, frame):
        ok, jpg = cv2.imencode(".jpg", frame,
                               [cv2.IMWRITE_JPEG_QUALITY, self.settings["jpeg_quality"]])
        if not ok:
            return
        with self.frame_cond:
            self.frame_jpeg = jpg.tobytes()
            self.frame_id += 1
            self.frame_cond.notify_all()

    def _placeholder(self, text):
        w, h = self.frame_size
        img = np.full((h, w, 3), 18, np.uint8)
        (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 1.0, 2)
        cv2.putText(img, text, ((w - tw) // 2, (h + th) // 2), cv2.FONT_HERSHEY_SIMPLEX,
                    1.0, (90, 90, 230), 2, cv2.LINE_AA)
        return img

    def _run(self):
        cap = None
        frame_count = 0
        cached_bodies = []
        fps_t, fps_n = time.time(), 0
        last_serial_check = 0.0
        camera_warned = False
        crashed = False

        try:
            while not self._stop.is_set():
                now = time.time()
                if now - last_serial_check > 1.0:
                    last_serial_check = now
                    if self.serial.maintain() and self.buzzer_on:
                        self.serial.send(b"D\n")     # re-assert the alarm after a replug

                with self._lock:
                    if self._camera_dirty:
                        if cap is not None:
                            cap.release()
                        cap = None
                        self.sim = None
                        self._camera_dirty = False
                        camera_warned = False
                    if self._detectors_dirty:
                        self._build_detectors()
                    source = self.settings["source"]

                if source == "simulator":
                    if self.sim is None:
                        self.sim = SimulatedScene(self.settings["width"],
                                                  self.settings["height"])
                    control, self.sim_control = self.sim_control, None
                    if control == "auto":
                        self.sim.auto()
                    elif control:
                        self.sim.set_target(*control)
                    frame, found = self.sim.read()
                    time.sleep(1 / 30)
                else:
                    if cap is None:
                        cap = self._open_camera()
                    ok, frame = cap.read() if cap is not None else (False, None)
                    if not ok:
                        if cap is not None:
                            cap.release()
                            cap = None
                        if not camera_warned:
                            self.log("alarm" if self.zone else "warn",
                                     f"Camera {self.settings['camera_index']} not available")
                            camera_warned = True
                        with self._lock:
                            self.camera_ok = False
                            self.parts, self.detections = [], []
                            self._update_hazard([], camera_lost=True)
                        self._publish(self._placeholder("CAMERA NOT AVAILABLE - retrying..."))
                        self._stop.wait(2.0)
                        continue
                    if camera_warned:
                        self.log("ok", "Camera connected")
                        camera_warned = False
                    found = self._detect(frame, frame_count, cached_bodies)

                frame_count += 1
                fh, fw = frame.shape[:2]
                with self._lock:
                    self.camera_ok = True
                    self.frame_size = (fw, fh)
                    if self._legacy_zone:
                        self.zone = [(x / fw, y / fh) for x, y in self._legacy_zone]
                        self._legacy_zone = None
                        self._save_zone()
                        self.log("info", "Converted saved zone to the new format")
                    parts = []
                    for det in found:
                        det["in_zone"] = box_in_zone(self.zone, det["box"]) or any(
                            point_in_zone(self.zone, p) for p in det.get("points", []))
                        name = det["kind"].capitalize()
                        if det["in_zone"] and name not in parts:
                            parts.append(name)
                    self.parts, self.detections = parts, found
                    self._update_hazard(parts)

                self._publish(frame)

                fps_n += 1
                if now - fps_t >= 1.0:
                    self.fps = fps_n / (now - fps_t)
                    fps_t, fps_n = now, 0
        except Exception as e:
            crashed = True
            self.log("alarm", f"Engine crashed: {e}")
            raise
        finally:
            if cap is not None:
                cap.release()
            with self._lock:
                if crashed and self.zone and self.settings["fail_safe"]:
                    # Nobody is watching the zone any more: keep the machine stopped.
                    if not self.buzzer_on:
                        self._set_buzzer(True, "Machine stopped - detector error (fail-safe)")
                    self.status = "error"
                else:
                    if self.buzzer_on:
                        self._set_buzzer(False, "Buzzer OFF - monitoring stopped")
                    self.status = "stopped"
                self.parts, self.detections = [], []
                self.camera_ok = False
                self.fps = 0.0
