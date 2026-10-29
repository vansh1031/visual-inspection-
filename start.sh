#!/bin/bash
# Starts the Hazardous Zone Detector on http://127.0.0.1:8765 and, when there is
# a desktop, opens it in the browser once it is ready. Ctrl+C stops it.
# Also used by the systemd service that ./install_jetson.sh --service creates.
cd "$(dirname "$0")"
# Use $PYTHON if set, else the project's .venv (made by install_jetson.sh),
# else the Jetson's pyenv 3.12, else whatever python3 is on the PATH.
if [ -z "$PYTHON" ]; then
  if [ -x .venv/bin/python ]; then PYTHON=.venv/bin/python
  elif [ -x "$HOME/.pyenv/versions/3.12.12/bin/python3" ]; then PYTHON="$HOME/.pyenv/versions/3.12.12/bin/python3"
  else PYTHON=python3
  fi
fi
[ -n "$DISPLAY" ] && [ -z "$INVOCATION_ID" ] && export OPEN_BROWSER=1   # not under systemd
exec "$PYTHON" hazard_server.py
