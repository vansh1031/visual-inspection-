"use strict";

/* Hazardous Zone Detector - single-screen UI.
 *
 * The server streams video as MJPEG (/video_feed) and state as server-sent
 * events (/events). Everything drawn on top of the video - the zone, the
 * detection boxes, the zone editor - is drawn here on a canvas, using the
 * normalised 0..1 coordinates the engine reports.
 */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const COLORS = { hand: "#22d3ee", face: "#60a5fa", body: "#4ade80",
                 alarm: "#ef4444", warn: "#f59e0b" };
const HAND_CONNECTIONS = [[0,1],[1,2],[2,3],[3,4],[0,5],[5,6],[6,7],[7,8],[0,9],[9,10],
  [10,11],[11,12],[0,13],[13,14],[14,15],[15,16],[0,17],[17,18],[18,19],[19,20],[5,9],[9,13],[13,17]];

let state = null;
let editing = false;
let draft = [];
let drag = null;              // { idx, x, y, moved } while dragging a zone point
let pointer = null;           // last pointer position over the video (normalised)
let simDragging = false;
let simTarget = null;
const bootAt = performance.now();

// ─── Helpers ────────────────────────────────────────────
async function api(path, body, method = "POST") {
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

let toastTimer;
function toast(msg, kind = "") {
  const el = $("#toast");
  el.textContent = msg;
  el.className = `toast show ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 2600);
}

function fmtTime(t) {
  return new Date(t * 1000).toLocaleTimeString([], { hour12: false });
}

function fmtDur(s) {
  s = Math.max(0, Math.round(s));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(s / 3600)}h ${String(Math.floor(s / 60) % 60).padStart(2, "0")}m`;
}

/** A button that needs a second tap within 3 s - for actions that disarm safety. */
function confirmTap(btn, label, action) {
  btn.addEventListener("click", async () => {
    if (!btn.dataset.armed) {
      btn.dataset.armed = "1";
      btn.dataset.label = (btn.querySelector("span") ?? btn).textContent.trim();
      setBtnLabel(btn, label);
      btn.classList.add("confirming");
      btn._timer = setTimeout(() => resetConfirm(btn), 3000);
      return;
    }
    resetConfirm(btn);
    await action();
  });
}
function resetConfirm(btn) {
  clearTimeout(btn._timer);
  if (btn.dataset.armed) setBtnLabel(btn, btn.dataset.label);
  delete btn.dataset.armed;
  btn.classList.remove("confirming");
}
function setBtnLabel(btn, text) {
  let span = btn.querySelector("span");
  if (!span) {
    // Wrap the button's bare text node so it can be swapped.
    const node = Array.from(btn.childNodes).find((n) => n.nodeType === 3 && n.textContent.trim());
    span = document.createElement("span");
    if (node) node.replaceWith(span);
    else btn.append(span);
  }
  span.textContent = text;
}

// ─── Live state ─────────────────────────────────────────
function connect() {
  const es = new EventSource("/events");
  es.addEventListener("state", (e) => onState(JSON.parse(e.data)));
  es.addEventListener("log", (e) => addLog(JSON.parse(e.data)));
  es.onerror = () => {
    $("#splashSub").textContent = "Reconnecting to the detector…";
    setStatusCard("connecting");
  };
}

function startVideo() {
  const img = $("#video");
  img.onload = () => img.classList.add("ready");
  img.onerror = () => {
    img.classList.remove("ready");
    setTimeout(() => { img.src = `/video_feed?t=${Date.now()}`; }, 1500);
  };
  img.src = "/video_feed";
}

const STATUS_TEXT = {
  connecting:   ["System",         "Connecting…",          "Waiting for the detector"],
  stopped:      ["Monitoring off", "Detector stopped",     "Press Start to resume monitoring"],
  disarmed:     ["Not armed",      "No danger zone",       "Draw a zone on the video to arm monitoring"],
  safe:         ["Protected",      "Zone clear",           "Machine running · zone is being watched"],
  alarm:        ["Alarm",          "Human in danger zone", "Machine stopped"],
  camera_error: ["Fail-safe",      "Camera lost",          "Machine stopped until the camera is back"],
  error:        ["Fail-safe",      "Detector error",       "Machine stopped · restart monitoring"],
};

function setStatusCard(status, sub) {
  const [kicker, title, defSub] = STATUS_TEXT[status] || STATUS_TEXT.connecting;
  $("#statusCard").dataset.status = status;
  $("#statusKicker").textContent = kicker;
  $("#statusTitle").textContent = title;
  $("#statusSub").textContent = sub || defSub;
}

function onState(s) {
  const first = !state;
  state = s;
  if (first) {
    const wait = Math.max(0, 900 - (performance.now() - bootAt));
    setTimeout(() => document.body.classList.remove("booting"), wait);
  }

  const body = document.body;
  body.classList.toggle("alarm", s.buzzer);
  body.classList.toggle("source-simulator", s.source === "simulator");
  alarmSound(s.buzzer);

  // Video frame follows the stream's aspect ratio
  const frame = $("#stageFrame");
  if (s.frame.w && s.frame.h) frame.style.setProperty("--ar", (s.frame.w / s.frame.h).toFixed(4));
  frame.classList.toggle("offline", !s.running);
  frame.classList.toggle("sim", s.source === "simulator" && !editing);

  $$("#sourceSwitch button").forEach((b) =>
    b.classList.toggle("active", b.dataset.source === s.source));

  // Serial pill
  const pill = $("#serialPill");
  pill.classList.toggle("ok", s.serial.connected);
  pill.classList.toggle("bad", !s.serial.connected);
  $("#serialText").textContent = !s.serial.available ? "pyserial missing"
    : s.serial.connected ? s.serial.port.replace("/dev/", "") : "offline";

  // Status card + banner
  let sub;
  if (s.status === "alarm") sub = `Machine stopped · ${s.parts.join(", ") || "clearing…"}`;
  if (s.status === "safe") sub = `Machine running · ${s.zone.length}-point zone watched`;
  if (s.status === "camera_error" && !s.settings.fail_safe) sub = "Monitoring paused until the camera is back";
  setStatusCard(s.status, sub);
  if (s.status === "alarm") {
    $("#bannerTitle").textContent = "HUMAN IN DANGER ZONE";
    $("#bannerSub").textContent = `Machine stopped · Detected: ${s.parts.join(" · ") || "clearing…"}`;
  } else if (s.buzzer) {
    $("#bannerTitle").textContent = s.status === "camera_error" ? "CAMERA LOST" : "DETECTOR ERROR";
    $("#bannerSub").textContent = "Machine stopped (fail-safe)";
  }

  // Engine button
  const eb = $("#engineBtn");
  const action = s.running ? "stop" : "start";
  if (eb.dataset.action !== action) {
    resetConfirm(eb);
    eb.dataset.action = action;
    setBtnLabel(eb, s.running ? "Stop" : "Start");
  }

  $("#stageTagText").textContent = !s.running ? "OFF"
    : s.source === "simulator" ? "SIMULATOR" : s.camera_ok ? "LIVE" : "NO SIGNAL";
  $("#editZoneLabel").textContent = s.zone.length >= 3 ? "Edit zone" : "Draw zone";
  $("#clearZoneBtn").disabled = s.zone.length < 3;

  // Detection chips
  for (const chip of $$(".detect")) {
    const kind = chip.dataset.kind;
    const enabled = s.source === "simulator" || s.detectors[kind];
    const seen = s.detections.filter((d) => d.kind === kind);
    const inZone = seen.some((d) => d.in_zone);
    chip.classList.toggle("off", !enabled);
    chip.classList.toggle("seen", seen.length > 0 && !inZone);
    chip.classList.toggle("inzone", inZone);
    chip.querySelector(".detect-state").textContent =
      !enabled ? "off" : inZone ? "in zone" : seen.length ? `seen ×${seen.length}` : "clear";
  }

  $("#fps").textContent = `${s.fps.toFixed(0)} fps`;
  $("#statAlarms").textContent = s.stats.alarms;
  $("#statLast").textContent = s.stats.last_alarm ? fmtTime(s.stats.last_alarm).slice(0, 5) : "—";
  $("#statAlarmTime").textContent = fmtDur(s.stats.alarm_seconds);
  $("#statUptime").textContent = fmtDur(s.stats.uptime);

  machine.mode = s.buzzer ? "stopped" : (s.running && s.status === "safe") ? "running" : "unmonitored";
  updateHint();
}

function updateHint() {
  const hint = $("#stageHint");
  let text = "";
  if (editing) {
    text = draft.length < 3
      ? `Tap to place zone corners (${draft.length}/3 minimum) · drag a corner to move it`
      : state && state.zone.length >= 3
        ? "Drag corners to adjust · “Clear” to draw a new zone · “Arm zone” to finish"
        : "Tap the first corner or press “Arm zone” to finish · drag corners to adjust";
  } else if (state && state.running && state.zone.length < 3) {
    text = "No danger zone yet - press “Draw zone” to create one";
  } else if (state && state.source === "simulator") {
    text = "Tap or drag on the video to move the simulated worker";
  }
  hint.textContent = text;
  hint.classList.toggle("show", !!text);
}

// ─── Event log ──────────────────────────────────────────
const seenLogs = new Set();
function addLog(ev) {
  const key = `${ev.id}:${ev.t}`;
  if (seenLogs.has(key)) return;
  seenLogs.add(key);
  const list = $("#log");
  list.querySelector(".log-empty")?.remove();
  const li = document.createElement("li");
  if (ev.level === "alarm") li.className = "lv-alarm-row";
  const time = document.createElement("time");
  time.textContent = fmtTime(ev.t);
  const dot = document.createElement("span");
  dot.className = `lv lv-${ev.level}`;
  const msg = document.createElement("span");
  msg.textContent = ev.msg;
  li.append(time, dot, msg);
  list.prepend(li);
  while (list.children.length > 120) list.lastChild.remove();
}

function emptyLog() {
  $("#log").innerHTML = '<li class="log-empty">No events yet</li>';
}

// ─── Overlay canvas ─────────────────────────────────────
const canvas = $("#overlay");
const ctx = canvas.getContext("2d");
const shown = new Map();      // smoothed detection boxes, keyed by kind + index

function drawOverlay(ts) {
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.clientWidth, H = canvas.clientHeight;
  if (canvas.width !== Math.round(W * dpr) || canvas.height !== Math.round(H * dpr)) {
    canvas.width = Math.round(W * dpr);
    canvas.height = Math.round(H * dpr);
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  if (!state) return;

  if (editing) drawDraft(W, H, ts);
  else if (state.zone.length >= 3) drawZone(state.zone, W, H, ts);
  if (state.running) drawDetections(W, H);
  if (simTarget && state.source === "simulator" && !editing) drawTarget(W, H, ts);
}

function tracePoly(points, W, H) {
  ctx.beginPath();
  points.forEach(([x, y], i) => (i ? ctx.lineTo(x * W, y * H) : ctx.moveTo(x * W, y * H)));
  ctx.closePath();
}

function drawZone(zone, W, H, ts) {
  const alarm = state.buzzer;
  const pulse = alarm ? 0.5 + 0.5 * Math.sin(ts / 160) : 0;
  tracePoly(zone, W, H);
  ctx.fillStyle = `rgba(239, 68, 68, ${0.12 + pulse * 0.18})`;
  ctx.fill();
  ctx.save();
  ctx.shadowColor = "rgba(239, 68, 68, .8)";
  ctx.shadowBlur = alarm ? 18 : 8;
  ctx.strokeStyle = "rgba(239, 68, 68, .35)";
  ctx.lineWidth = 6;
  ctx.stroke();
  ctx.restore();
  ctx.setLineDash([12, 8]);
  ctx.lineDashOffset = -ts / 30;
  ctx.strokeStyle = COLORS.alarm;
  ctx.lineWidth = 2.5;
  ctx.stroke();
  ctx.setLineDash([]);

  // Label at the top-most corner
  const top = zone.reduce((a, b) => (b[1] < a[1] ? b : a));
  label(top[0] * W, top[1] * H - 10, alarm ? "⚠ DANGER ZONE" : "DANGER ZONE", COLORS.alarm, true);
}

function drawDraft(W, H, ts) {
  if (draft.length >= 3) {
    tracePoly(draft, W, H);
    ctx.fillStyle = "rgba(245, 158, 11, .10)";
    ctx.fill();
  }
  ctx.strokeStyle = COLORS.warn;
  ctx.lineWidth = 2.5;
  if (draft.length > 1) {
    ctx.beginPath();
    draft.forEach(([x, y], i) => (i ? ctx.lineTo(x * W, y * H) : ctx.moveTo(x * W, y * H)));
    ctx.stroke();
  }
  // Rubber band to the pointer and the closing edge
  if (draft.length && pointer && !drag) {
    const [lx, ly] = draft[draft.length - 1];
    ctx.setLineDash([6, 6]);
    ctx.beginPath();
    ctx.moveTo(lx * W, ly * H);
    ctx.lineTo(pointer[0] * W, pointer[1] * H);
    ctx.stroke();
    ctx.setLineDash([]);
  }
  if (draft.length >= 3) {
    const [fx, fy] = draft[0], [lx, ly] = draft[draft.length - 1];
    ctx.setLineDash([4, 6]);
    ctx.strokeStyle = "rgba(245, 158, 11, .6)";
    ctx.beginPath();
    ctx.moveTo(lx * W, ly * H);
    ctx.lineTo(fx * W, fy * H);
    ctx.stroke();
    ctx.setLineDash([]);
  }
  draft.forEach(([x, y], i) => {
    const px = x * W, py = y * H;
    if (i === 0 && draft.length >= 3) {
      const r = 14 + 6 * ((ts / 900) % 1);
      ctx.beginPath();
      ctx.arc(px, py, r, 0, Math.PI * 2);
      ctx.strokeStyle = `rgba(34, 197, 94, ${1 - ((ts / 900) % 1)})`;
      ctx.lineWidth = 2;
      ctx.stroke();
    }
    ctx.beginPath();
    ctx.arc(px, py, 11, 0, Math.PI * 2);
    ctx.fillStyle = i === 0 && draft.length >= 3 ? "#16a34a" : "#b45309";
    ctx.fill();
    ctx.lineWidth = 2;
    ctx.strokeStyle = "#fff";
    ctx.stroke();
    ctx.fillStyle = "#fff";
    ctx.font = "700 11px Inter, Segoe UI, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(String(i + 1), px, py + 0.5);
  });
}

function drawDetections(W, H) {
  const live = new Set();
  const counts = {};
  for (const det of state.detections) {
    const n = (counts[det.kind] = (counts[det.kind] || 0) + 1);
    const key = det.kind + n;
    live.add(key);
    // Ease boxes toward their latest position: the state arrives at ~10 Hz,
    // the canvas redraws at the display rate.
    const prev = shown.get(key);
    const box = prev ? prev.box.map((v, i) => v + (det.box[i] - v) * 0.35) : det.box.slice();
    shown.set(key, { box });

    const color = det.in_zone ? COLORS.alarm : COLORS[det.kind];
    const [x, y, w, h] = [box[0] * W, box[1] * H, box[2] * W, box[3] * H];
    ctx.strokeStyle = color;
    ctx.globalAlpha = 0.35;
    ctx.lineWidth = 1;
    ctx.strokeRect(x, y, w, h);
    ctx.globalAlpha = 1;
    ctx.lineWidth = det.in_zone ? 3.5 : 2.5;
    const c = Math.max(6, Math.min(22, Math.min(w, h) * 0.28));
    ctx.beginPath();
    for (const [cx, cy, dx, dy] of [[x, y, 1, 1], [x + w, y, -1, 1], [x, y + h, 1, -1], [x + w, y + h, -1, -1]]) {
      ctx.moveTo(cx + dx * c, cy);
      ctx.lineTo(cx, cy);
      ctx.lineTo(cx, cy + dy * c);
    }
    ctx.stroke();
    if (det.in_zone) {
      ctx.fillStyle = "rgba(239, 68, 68, .12)";
      ctx.fillRect(x, y, w, h);
    }

    const pts = det.points || [];
    if (pts.length === 21) {
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      for (const [a, b] of HAND_CONNECTIONS) {
        ctx.moveTo(pts[a][0] * W, pts[a][1] * H);
        ctx.lineTo(pts[b][0] * W, pts[b][1] * H);
      }
      ctx.stroke();
    }
    if (pts.length === 21) {
      ctx.fillStyle = "#fff";
      for (const [px, py] of pts) {
        ctx.beginPath();
        ctx.arc(px * W, py * H, 2.5, 0, Math.PI * 2);
        ctx.fill();
      }
    }
    // Body boxes enclose the face, so their tag goes underneath.
    if (det.kind === "body") label(x, y + h + 22, det.in_zone ? "BODY · IN ZONE" : "BODY", color, false);
    else if (w > 24) label(x, y - 6, det.kind.toUpperCase(), color, false);
  }
  for (const key of shown.keys()) if (!live.has(key)) shown.delete(key);
}

function label(x, y, text, color, centered) {
  ctx.font = "700 11px Inter, Segoe UI, sans-serif";
  const tw = ctx.measureText(text).width + 14;
  const lx = centered ? x - tw / 2 : x;
  const ly = Math.max(2, y - 18);
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.roundRect(lx, ly, tw, 18, 5);
  ctx.fill();
  ctx.fillStyle = "#fff";
  ctx.textAlign = "left";
  ctx.textBaseline = "middle";
  ctx.fillText(text, lx + 7, ly + 9.5);
}

function drawTarget(W, H, ts) {
  const [x, y] = [simTarget[0] * W, simTarget[1] * H];
  const t = (ts / 1000) % 1;
  ctx.strokeStyle = `rgba(96, 165, 250, ${1 - t})`;
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.ellipse(x, y, 10 + t * 18, (10 + t * 18) * 0.4, 0, 0, Math.PI * 2);
  ctx.stroke();
  ctx.fillStyle = "#60a5fa";
  ctx.beginPath();
  ctx.ellipse(x, y, 5, 2.5, 0, 0, Math.PI * 2);
  ctx.fill();
}

// ─── Pointer input on the video ─────────────────────────
function norm(e) {
  const r = canvas.getBoundingClientRect();
  return [Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)),
          Math.min(1, Math.max(0, (e.clientY - r.top) / r.height))];
}

function nearestPoint(p) {
  const r = canvas.getBoundingClientRect();
  let best = -1, bestD = 24;                       // px hit radius (touch friendly)
  draft.forEach(([x, y], i) => {
    const d = Math.hypot((x - p[0]) * r.width, (y - p[1]) * r.height);
    if (d < bestD) { best = i; bestD = d; }
  });
  return best;
}

let lastSimSend = 0;
function sendSim(p, force) {
  simTarget = p;
  const now = performance.now();
  if (!force && now - lastSimSend < 80) return;
  lastSimSend = now;
  api("/api/sim", { x: p[0], y: p[1] }).catch(() => {});
}

canvas.addEventListener("pointerdown", (e) => {
  if (!state) return;
  const p = norm(e);
  canvas.setPointerCapture(e.pointerId);
  if (editing) {
    const idx = nearestPoint(p);
    if (idx >= 0) {
      drag = { idx, x: e.clientX, y: e.clientY, moved: false };
    } else {
      draft.push(p);
      updateEditUI();
    }
  } else if (state.source === "simulator") {
    simDragging = true;
    sendSim(p, true);
  }
});

canvas.addEventListener("pointermove", (e) => {
  const p = norm(e);
  pointer = p;
  if (drag) {
    if (Math.hypot(e.clientX - drag.x, e.clientY - drag.y) > 5) drag.moved = true;
    if (drag.moved) draft[drag.idx] = p;
  } else if (simDragging) {
    sendSim(p);
  }
});

canvas.addEventListener("pointerup", (e) => {
  if (drag && !drag.moved && drag.idx === 0 && draft.length >= 3) saveZone();
  drag = null;
  if (simDragging) sendSim(norm(e), true);
  simDragging = false;
});
canvas.addEventListener("pointerleave", () => { pointer = null; });
canvas.addEventListener("pointercancel", () => { drag = null; simDragging = false; });

// ─── Zone editing ───────────────────────────────────────
function setEditing(on) {
  editing = on;
  document.body.classList.toggle("editing", on);
  $("#stageFrame").classList.toggle("editing", on);
  draft = on && state ? state.zone.map((p) => p.slice()) : [];
  updateEditUI();
}

function updateEditUI() {
  $("#editCount").textContent = `${draft.length} point${draft.length === 1 ? "" : "s"}`;
  $("#saveZoneBtn").disabled = draft.length < 3;
  $("#undoBtn").disabled = !draft.length;
  $("#clearDraftBtn").disabled = !draft.length;
  updateHint();
}

async function saveZone() {
  if (draft.length < 3) return;
  try {
    await api("/api/zone", { points: draft });
    setEditing(false);
    toast("Danger zone armed", "success");
  } catch (err) {
    toast(err.message, "error");
  }
}

$("#editZoneBtn").addEventListener("click", () => setEditing(true));
$("#cancelEditBtn").addEventListener("click", () => setEditing(false));
$("#saveZoneBtn").addEventListener("click", saveZone);
$("#undoBtn").addEventListener("click", () => { draft.pop(); updateEditUI(); });
$("#clearDraftBtn").addEventListener("click", () => { draft = []; updateEditUI(); });

confirmTap($("#clearZoneBtn"), "Tap again to disarm", async () => {
  try {
    await api("/api/zone", undefined, "DELETE");
    toast("Zone cleared - monitoring disarmed");
  } catch (err) { toast(err.message, "error"); }
});

document.addEventListener("keydown", (e) => {
  if (document.body.classList.contains("drawer-open")) {
    if (e.key === "Escape") closeDrawer();
    return;
  }
  if (!editing) return;
  if (e.key === "Escape") setEditing(false);
  else if (e.key === "Enter") saveZone();
  else if (e.key === "Backspace" || (e.key === "z" && (e.ctrlKey || e.metaKey))) {
    e.preventDefault();
    draft.pop();
    updateEditUI();
  }
});

// ─── Toolbar / top bar ──────────────────────────────────
$$("#sourceSwitch button").forEach((b) => b.addEventListener("click", async () => {
  if (state && b.dataset.source === state.source) return;
  try {
    await api("/api/settings", { source: b.dataset.source });
    simTarget = null;
    toast(b.dataset.source === "simulator" ? "Simulator on" : "Switched to camera");
  } catch (err) { toast(err.message, "error"); }
}));

$("#autoWalkBtn").addEventListener("click", () => {
  simTarget = null;
  api("/api/sim", { mode: "auto" }).catch(() => {});
});

const engineBtn = $("#engineBtn");
engineBtn.addEventListener("click", async (e) => {
  if (engineBtn.dataset.action !== "start") return;      // stop goes through confirmTap
  e.stopImmediatePropagation();
  await engineAction("start");
});
confirmTap(engineBtn, "Tap again to stop", () => engineAction("stop"));
$$('[data-action="start"]').forEach((b) => b !== engineBtn &&
  b.addEventListener("click", () => engineAction("start")));

async function engineAction(action) {
  try {
    await api("/api/engine", { action });
    toast(action === "start" ? "Monitoring started" : "Monitoring stopped");
  } catch (err) { toast(err.message, "error"); }
}

$("#fullscreenBtn").addEventListener("click", () => {
  if (document.fullscreenElement) document.exitFullscreen();
  else document.documentElement.requestFullscreen?.();
});

$("#clearLogBtn").addEventListener("click", emptyLog);

setInterval(() => {
  $("#clock").textContent = new Date().toLocaleTimeString([], { hour12: false });
}, 1000);

// ─── Alarm sound (Web Audio, this browser only) ─────────
let audio = null, siren = null;
let soundOn = true;
try { soundOn = localStorage.getItem("hz-sound") !== "0"; } catch (_) {}
$("#soundBtn").classList.toggle("muted", !soundOn);

$("#soundBtn").addEventListener("click", () => {
  soundOn = !soundOn;
  try { localStorage.setItem("hz-sound", soundOn ? "1" : "0"); } catch (_) {}
  $("#soundBtn").classList.toggle("muted", !soundOn);
  toast(soundOn ? "Alarm sound on" : "Alarm sound muted");
  if (!soundOn) stopSiren();
});
// Browsers only allow audio after a user gesture.
document.addEventListener("pointerdown", () => {
  if (!audio && window.AudioContext) audio = new AudioContext();
  audio?.resume();
}, { once: false, passive: true });

function alarmSound(on) {
  if (on && soundOn && audio && !siren) {
    const osc = audio.createOscillator();
    const gain = audio.createGain();
    osc.type = "square";
    gain.gain.value = 0.05;
    osc.connect(gain).connect(audio.destination);
    osc.start();
    let hi = false;
    const timer = setInterval(() => {
      hi = !hi;
      osc.frequency.setValueAtTime(hi ? 880 : 660, audio.currentTime);
    }, 350);
    siren = { osc, timer };
  } else if (!on) {
    stopSiren();
  }
}
function stopSiren() {
  if (!siren) return;
  clearInterval(siren.timer);
  siren.osc.stop();
  siren = null;
}

// ─── Settings drawer ────────────────────────────────────
const form = $("#settingsForm");

async function openDrawer() {
  if (!state) return;
  const s = state.settings;
  let ports = [];
  try { ports = (await (await fetch("/api/serial/ports")).json()).ports; } catch (_) {}
  const sel = $("#portSelect");
  sel.innerHTML = "";
  const options = ["auto", ...new Set([...ports, ...(s.serial_port !== "auto" ? [s.serial_port] : [])])];
  for (const p of options) {
    const opt = document.createElement("option");
    opt.value = p;
    opt.textContent = p === "auto" ? "Auto-detect" : p + (ports.includes(p) ? "" : " (not found)");
    sel.append(opt);
  }
  for (const el of form.elements) {
    if (!el.name || !(el.name in s)) continue;
    if (el.type === "checkbox") el.checked = !!s[el.name];
    else el.value = String(s[el.name]);
  }
  syncOutputs();
  document.body.classList.add("drawer-open");
}

function closeDrawer() {
  document.body.classList.remove("drawer-open");
}

function syncOutputs() {
  $("#handConfOut").textContent = Number(form.hand_conf.value).toFixed(2);
  $("#faceConfOut").textContent = Number(form.face_conf.value).toFixed(2);
}

form.addEventListener("input", syncOutputs);
form.addEventListener("submit", (e) => e.preventDefault());
$("#settingsBtn").addEventListener("click", openDrawer);
$("#closeDrawerBtn").addEventListener("click", closeDrawer);
$("#cancelSettingsBtn").addEventListener("click", closeDrawer);
$("#drawerBackdrop").addEventListener("click", closeDrawer);

$("#saveSettingsBtn").addEventListener("click", async () => {
  const data = {};
  for (const el of form.elements) {
    if (!el.name) continue;
    if (el.type === "checkbox") data[el.name] = el.checked;
    else if (el.type === "number" || el.type === "range" || el.name === "baud") data[el.name] = Number(el.value);
    else data[el.name] = el.value;
  }
  try {
    await api("/api/settings", data);
    closeDrawer();
    toast("Settings saved", "success");
  } catch (err) { toast(err.message, "error"); }
});

$("#testBuzzerBtn").addEventListener("click", async () => {
  try {
    await api("/api/buzzer/test");
    toast(state?.serial.connected ? "Buzzer test sent" : "No Arduino connected", state?.serial.connected ? "success" : "error");
  } catch (err) { toast(err.message, "error"); }
});

// ─── Machine simulator ──────────────────────────────────
const machine = {
  mode: "unmonitored",       // running | unmonitored | stopped
  speed: 0, belt: 0, gear: 0, ram: 0, made: 0,
  crates: [],
};

(function buildMachine() {
  const NS = "http://www.w3.org/2000/svg";
  const el = (tag, attrs) => {
    const n = document.createElementNS(NS, tag);
    for (const k in attrs) n.setAttribute(k, attrs[k]);
    return n;
  };
  const stripes = $("#beltStripes");
  for (let x = 4; x < 372; x += 16) {
    stripes.append(el("path", { d: `M${x} 120 l6 5 -6 5`, stroke: "#3c4558", "stroke-width": 2, fill: "none" }));
  }
  const rollers = $("#rollers");
  for (let x = 30; x <= 330; x += 50) {
    const g = el("g", { transform: `translate(${x} 125)` });
    g.append(el("circle", { r: 4, fill: "#4b5871", stroke: "#6b7a94" }),
             el("path", { d: "M-4 0h8", stroke: "#1b2130", "stroke-width": 1.5 }));
    rollers.append(g);
  }
  const crates = $("#crates");
  for (let i = 0; i < 5; i++) {
    const g = el("g", { class: "crate" });
    g.append(el("rect", { x: -11, y: -18, width: 22, height: 18, rx: 2 }),
             el("path", { d: "M-11 -9h22M0 -18v18" }));
    crates.append(g);
    machine.crates.push({ g, base: i * 80, prevX: null, done: false });
  }
})();

let lastFrame = performance.now();
function animate(ts) {
  const dt = Math.min(0.1, (ts - lastFrame) / 1000);
  lastFrame = ts;
  const m = machine;
  const target = m.mode === "stopped" ? 0 : 1;
  // Brake hard, spin up gently.
  const rate = target < m.speed ? 4.5 : 0.9;
  m.speed += (target - m.speed) * (1 - Math.exp(-dt * rate));
  if (Math.abs(target - m.speed) < 0.002) m.speed = target;

  m.belt += m.speed * 46 * dt;
  m.gear += m.speed * 160 * dt;
  m.ram += m.speed * dt / 1.7;

  $("#beltStripes").setAttribute("transform", `translate(${(m.belt % 16).toFixed(2)} 0)`);
  $("#gear").setAttribute("transform", `rotate(${(m.gear % 360).toFixed(1)})`);
  $$("#rollers g").forEach((g, i) => {
    g.setAttribute("transform", `translate(${30 + i * 50} 125) rotate(${(m.gear * 2.2) % 360})`);
  });
  const stroke = 14 * Math.pow(Math.max(0, Math.sin(m.ram * Math.PI * 2)), 3);
  $("#ram").setAttribute("transform", `translate(0 ${stroke.toFixed(2)})`);

  const PRESS_X = 156;
  for (const c of m.crates) {
    const x = ((c.base + m.belt) % 400) - 30;
    if (c.prevX !== null && x < c.prevX) c.done = false;          // wrapped round
    if (c.prevX !== null && c.prevX < PRESS_X && x >= PRESS_X) { c.done = true; m.made++; }
    c.prevX = x;
    c.g.setAttribute("transform", `translate(${x.toFixed(2)} 114)`);
    c.g.classList.toggle("done", c.done);
  }

  const svg = $("#machine");
  svg.classList.toggle("stopped", m.mode === "stopped");
  $("#lightR").classList.toggle("on", m.mode === "stopped");
  $("#lightA").classList.toggle("on", m.mode === "unmonitored");
  $("#lightG").classList.toggle("on", m.mode === "running");
  const badge = $("#machineBadge");
  const badgeText = { running: "RUNNING", unmonitored: "UNMONITORED", stopped: "E-STOP" }[m.mode];
  if (badge.textContent !== badgeText) badge.textContent = badgeText;
  badge.className = `badge ${m.mode === "stopped" ? "stopped" : m.mode === "unmonitored" ? "idle" : ""}`;
  $("#rpm").textContent = `${Math.round(m.speed * 1450)} rpm`;
  $("#speedFill").style.width = `${(m.speed * 100).toFixed(1)}%`;
  $("#parts").textContent = m.made;

  drawOverlay(ts);
  requestAnimationFrame(animate);
}

// ─── Boot ───────────────────────────────────────────────
emptyLog();
updateEditUI();
startVideo();
connect();
requestAnimationFrame(animate);
