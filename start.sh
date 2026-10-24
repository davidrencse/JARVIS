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
# `voice start` exits 0 even when it declines (no voiceprint yet), so read its answer.
voice=$("$PY" -m jarvis voice start 2>&1)
if ! printf '%s' "$voice" | grep -qE '"(started|listening)": true'; then
    note=$(printf '%s' "$voice" | sed -n 's/.*"note": "\(.*\)",*$/\1/p' | head -1)
    echo "  Voice listener is off: ${note:-see .jarvis/logs/voice.log}"
    echo "  The console still works; type in it or use the live call."
fi

# Detached: the app window is the UI. Console log: .jarvis/logs/console.log
mkdir -p .jarvis/logs
setsid -f "$PY" -m jarvis app >>.jarvis/logs/console.log 2>&1 </dev/null
