#!/bin/bash
# One-time setup of the Hazardous Zone Detector on the Jetson (or any Linux PC).
#
#   ./install_jetson.sh             install the libraries into .venv and check the setup
#   ./install_jetson.sh --service   ...and also start the detector automatically on boot
#
# Safe to run again: it reuses .venv and only installs what is missing.
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

echo "=== Hazardous Zone Detector setup (Linux / Jetson) ==="

# Pick a Python: $PYTHON, the Jetson's pyenv 3.12, python3.12, then python3.
for candidate in "$PYTHON" "$HOME/.pyenv/versions/3.12.12/bin/python3" python3.12 python3; do
  if [ -n "$candidate" ] && command -v "$candidate" >/dev/null 2>&1; then
    PY="$candidate"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3 was not found. Install it with:  sudo apt install python3 python3-venv"
  exit 1
fi
echo "Using $("$PY" --version) at $(command -v "$PY")"

if [ ! -x .venv/bin/python ]; then
  echo "Creating the virtual environment (.venv)..."
  if ! "$PY" -m venv .venv; then
    echo "Could not create .venv. On Ubuntu / JetPack install venv support with:"
    echo "  sudo apt install python3-venv"
    exit 1
  fi
fi

echo "Installing libraries (the first run can take several minutes)..."
.venv/bin/python -m pip install --upgrade pip >/dev/null
.venv/bin/python -m pip install -r requirements.txt

chmod +x start.sh

echo
.venv/bin/python scripts/check_setup.py || true

if [ "$1" = "--service" ]; then
  echo
  echo "Installing systemd services (needs sudo)..."
  install_unit() {   # name, description, script
    local unit="/etc/systemd/system/$1.service"
    if [ -f "$unit" ]; then
      sudo cp "$unit" "$unit.bak"
      echo "  existing $1.service backed up to $unit.bak"
    fi
    sudo tee "$unit" >/dev/null <<EOF
[Unit]
Description=$2
After=network.target

[Service]
User=$USER
ExecStart="$ROOT/$3"
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
    echo "  wrote $unit"
  }
  install_unit hazard-server "Hazardous Zone Detector (port 8765)" start.sh
  # The old Tkinter launcher service is replaced by the web app.
  if systemctl list-unit-files hazard-detector.service >/dev/null 2>&1 &&
     systemctl list-unit-files hazard-detector.service | grep -q hazard-detector; then
    sudo systemctl disable --now hazard-detector.service || true
    echo "  disabled the old hazard-detector.service"
  fi
  sudo systemctl daemon-reload
  sudo systemctl enable --now hazard-server.service
  echo "  the detector now starts on boot (check with: systemctl status hazard-server)"
fi

echo
echo "Setup finished."
echo "  Start:   ./start.sh   (Ctrl+C stops it)"
echo "  Open:    http://127.0.0.1:8765/start"
