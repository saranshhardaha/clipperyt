#!/usr/bin/env bash
# Start the clipperyt dashboard automatically:
#   Linux  → systemd service (on boot)
#   macOS  → LaunchAgent (when you log in)
#
#   ./deploy/install_service.sh              install / update
#   ./deploy/install_service.sh --uninstall  remove it
#   CLIPPER_TOKEN=… CLIPPER_PORT=… ./deploy/install_service.sh   (else a token is generated)
#
# Jobs marked "autostart" in the dashboard start with it. Recordings keep running
# if the dashboard itself restarts.
set -euo pipefail
APP="$(cd "$(dirname "$0")/.." && pwd)"
PY="$APP/.venv/bin/python"
PORT="${CLIPPER_PORT:-8787}"
[ -x "$PY" ] || { echo "Run setup first (no $PY)"; exit 1; }

token() {
  if [ -n "${CLIPPER_TOKEN:-}" ]; then echo "$CLIPPER_TOKEN"; return; fi
  if [ -f "$APP/.dashboard_token" ]; then cat "$APP/.dashboard_token"; return; fi
  "$PY" -c 'import secrets; print(secrets.token_urlsafe(18))' | tee "$APP/.dashboard_token"
  chmod 600 "$APP/.dashboard_token"
}

ip_hint() {
  if command -v ipconfig >/dev/null; then ipconfig getifaddr en0 2>/dev/null || echo localhost
  else hostname -I 2>/dev/null | awk '{print $1}' || echo localhost; fi
}

case "$(uname)" in
Darwin)
  LABEL=com.clipperyt.dashboard
  PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
  if [ "${1:-}" = "--uninstall" ]; then
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"; echo "removed $PLIST"; exit 0
  fi
  TOKEN="$(token)"
  mkdir -p "$HOME/Library/LaunchAgents" "$APP/logs"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>-u</string><string>$APP/server.py</string></array>
  <key>WorkingDirectory</key><string>$APP</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/opt/homebrew/opt/ffmpeg-full/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>CLIPPER_TOKEN</key><string>$TOKEN</string>
    <key>CLIPPER_PORT</key><string>$PORT</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <!-- jobs run in their own process groups; never kill them when the dashboard restarts -->
  <key>AbandonProcessGroup</key><true/>
  <key>StandardOutPath</key><string>$APP/logs/dashboard.log</string>
  <key>StandardErrorPath</key><string>$APP/logs/dashboard.log</string>
</dict></plist>
EOF
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  echo "LaunchAgent installed: $PLIST  (log: logs/dashboard.log)"
  echo "Keep the Mac plugged in with the lid open — jobs keep it awake with caffeinate."
  ;;
Linux)
  SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
  UNIT=/etc/systemd/system/clipperyt.service
  if [ "${1:-}" = "--uninstall" ]; then
    $SUDO systemctl disable --now clipperyt 2>/dev/null || true
    $SUDO rm -f "$UNIT"; $SUDO systemctl daemon-reload; echo "removed $UNIT"; exit 0
  fi
  TOKEN="$(token)"
  RUN_USER="${SUDO_USER:-$(id -un)}"
  sed -e "s|@USER@|$RUN_USER|g" -e "s|@APP@|$APP|g" -e "s|@TOKEN@|$TOKEN|g" -e "s|@PORT@|$PORT|g" \
    "$APP/deploy/clipperyt.service" | $SUDO tee "$UNIT" >/dev/null
  $SUDO chmod 600 "$UNIT"  # holds the token
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable --now clipperyt
  $SUDO systemctl restart clipperyt
  echo "systemd service installed: $UNIT   (logs: journalctl -u clipperyt -f)"
  ;;
*) echo "unsupported OS $(uname)"; exit 1 ;;
esac

echo
echo "Dashboard: http://$(ip_hint):$PORT/?token=$TOKEN"
echo "(open that link once per device; a cookie remembers it. The token is in $APP/.dashboard_token)"
