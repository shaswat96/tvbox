#!/bin/bash
# Start TV Box automatically at login (LaunchAgent). Run ./install-autostart.sh --remove to undo.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/com.tvbox.server.plist"
# Framework Python's bin/python3.x just re-launches Python.app; run that directly so the
# Accessibility permission granted to Python.app is the one that applies.
PY="$(python3 -c 'import sys, os; print(os.path.realpath(sys.executable))')"
APP_PY="$(dirname "$(dirname "$PY")")/Resources/Python.app/Contents/MacOS/Python"
[ -x "$APP_PY" ] && PY="$APP_PY"

launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
if [ "$1" = "--remove" ]; then rm -f "$PLIST"; echo "Autostart removed."; exit 0; fi

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/.tvbox"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.tvbox.server</string>
  <key>ProgramArguments</key><array>
    <string>$PY</string><string>$DIR/server.py</string><string>--open</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/.tvbox/server.log</string>
  <key>StandardErrorPath</key><string>$HOME/.tvbox/server.log</string>
</dict></plist>
PL
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "TV Box will now start at login (log: ~/.tvbox/server.log)."
echo
echo "One-time step so the remote can press keys / move the mouse:"
echo "  System Settings > Privacy & Security > Accessibility > + > add:"
echo "  ${PY%/Contents/MacOS/Python}"
echo "  (press Cmd+Shift+G in the file picker and paste that path), then run this script again."
