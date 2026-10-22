#!/usr/bin/env bash
# JARVIS on Arch: start the voice listener (if it isn't already running), then open the
# console as its own app window. Closing the window stops the console server; the
# listener keeps running. Bind it in Hyprland, e.g. bind = SUPER, J, exec, ~/projects/JARVIS/start.sh
set -u
cd "$(dirname "$(readlink -f "$0")")"

# A project venv has the voice packages; the system python doesn't.
PY=python
[ -x .venv/bin/python ] && PY=.venv/bin/python

echo "Starting JARVIS..."
"$PY" -m jarvis voice start >/dev/null 2>&1 ||
    echo "  The voice listener didn't start; see .jarvis/logs/voice.log"

# Detached: the app window is the UI. Console log: .jarvis/logs/console.log
mkdir -p .jarvis/logs
setsid -f "$PY" -m jarvis app >>.jarvis/logs/console.log 2>&1 </dev/null
