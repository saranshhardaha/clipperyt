#!/usr/bin/env bash
# Update clipperyt: git pull → python deps → reload the dashboard → restart idle jobs.
#
#   ./update.sh               update + reload (macOS and Linux)
#   ./update.sh --keep-jobs       don't restart waiting jobs (they pick up new code on their next start)
#   ./update.sh --upgrade-tools   macOS: also `brew upgrade streamlink yt-dlp` (Linux always upgrades them in the venv)
#
# Recordings are never interrupted: jobs run as their own processes, and a job that's
# recording or clipping keeps its old code until it's restarted after the stream.
set -euo pipefail
cd "$(dirname "$0")"
APP="$(pwd)"
PORT="${CLIPPER_PORT:-8787}"
TOKEN="${CLIPPER_TOKEN:-$( [ -f .dashboard_token ] && cat .dashboard_token || true)}"
KEEP_JOBS=0; UPGRADE_TOOLS=0
for a in "$@"; do
  case "$a" in
    --keep-jobs) KEEP_JOBS=1 ;;
    --upgrade-tools) UPGRADE_TOOLS=1 ;;
    *) echo "unknown option $a"; exit 1 ;;
  esac
done
PY=.venv/bin/python
[ -x "$PY" ] || { echo "No .venv — run ./setup.sh (macOS) or ./setup_linux.sh (Linux) first"; exit 1; }

echo "==> git pull"
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "!! You have local changes to tracked files — commit or stash them first:"
  git status --short --untracked-files=no
  exit 1
fi
OLD="$(git rev-parse HEAD)"
git pull --ff-only
NEW="$(git rev-parse HEAD)"
if [ "$OLD" = "$NEW" ]; then
  echo "   already up to date"
else
  git --no-pager log --oneline "$OLD..$NEW"
fi
changed() { [ "$OLD" != "$NEW" ] && git diff --name-only "$OLD" "$NEW" | grep -qx "$1"; }

echo "==> python packages"
if [ "$(uname)" = "Linux" ]; then
  "$PY" -m pip install -q --upgrade -r requirements.txt streamlink yt-dlp  # Linux keeps these in the venv
else
  "$PY" -m pip install -q -r requirements.txt
  if [ "$UPGRADE_TOOLS" = 1 ] && command -v brew >/dev/null; then
    brew upgrade streamlink yt-dlp || true   # sites change often; old yt-dlp/streamlink break first
  else
    echo "   (streamlink/yt-dlp not upgraded — add --upgrade-tools if a site stopped working)"
  fi
fi
mkdir -p jobs logs locks sessions
if changed setup.sh || changed setup_linux.sh; then
  echo "   note: the setup script changed — re-run it if a new system tool is needed"
fi

api() {  # api METHOD PATH  → body (empty on failure)
  local auth=(); [ -n "$TOKEN" ] && auth=(-H "Cookie: clipper=$TOKEN")
  curl -s -m 10 -X "$1" ${auth[@]+"${auth[@]}"} "http://127.0.0.1:$PORT$2" || true  # bash 3.2-safe empty array
}

echo "==> reload dashboard"
if [ "$(uname)" = "Linux" ] && systemctl is-active --quiet clipperyt 2>/dev/null; then
  SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
  $SUDO systemctl restart clipperyt   # KillMode=process → jobs keep running
  echo "   restarted systemd service clipperyt"
elif [ "$(uname)" = "Darwin" ] && launchctl print "gui/$(id -u)/com.clipperyt.dashboard" >/dev/null 2>&1; then
  launchctl kickstart -k "gui/$(id -u)/com.clipperyt.dashboard"  # AbandonProcessGroup → jobs keep running
  echo "   restarted LaunchAgent com.clipperyt.dashboard"
else
  PIDS="$(pgrep -f "$APP/server.py|[[:space:]]server.py" 2>/dev/null || true)"
  MINE=""
  for p in $PIDS; do  # only dashboards started from this folder
    cwd="$(lsof -a -p "$p" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' || readlink "/proc/$p/cwd" 2>/dev/null || true)"
    [ "$cwd" = "$APP" ] && MINE="$MINE $p"
  done
  if [ -n "$MINE" ]; then
    kill $MINE 2>/dev/null || true
    sleep 1
    nohup "$PY" -u server.py >> logs/dashboard.log 2>&1 &
    echo "   restarted dashboard (pid $!) — log: logs/dashboard.log"
    [ -z "${CLIPPER_TOKEN:-}" ] && [ -z "$TOKEN" ] && echo "   (started without CLIPPER_TOKEN — set it in this shell first if you use one)"
  else
    echo "   dashboard isn't running — start it with: .venv/bin/python server.py"
  fi
fi

# wait for it to answer
for _ in $(seq 30); do
  out="$(api GET /api/jobs)"; [ -n "$out" ] && [ "${out:0:1}" = "[" ] && break; sleep 0.5; out=""
done

if [ -n "${out:-}" ]; then
  echo "==> jobs"
  # restart jobs that are only waiting for a stream (safe: nothing is being recorded)
  echo "$out" | "$PY" -c '
import json, sys
keep = sys.argv[1] == "1"
for j in json.load(sys.stdin):
    st = j["state"].get("status")
    if not j.get("running") or j.get("readonly"):
        continue
    if st == "waiting" and not keep:
        print("RESTART", j["id"], j["name"])
    else:
        print("KEEP", j["id"], j["name"], st)
' "$KEEP_JOBS" | while read -r what id name; do
    if [ "$what" = "RESTART" ]; then
      api POST "/api/jobs/$id/stop" >/dev/null
      for _ in $(seq 40); do
        api GET /api/jobs | "$PY" -c "import json,sys; sys.exit(0 if any(j['id']=='$id' and not j['running'] for j in json.load(sys.stdin)) else 1)" && break
        sleep 0.5
      done
      api POST "/api/jobs/$id/start" >/dev/null
      echo "   restarted $id ($name) on the new code"
    else
      echo "   left running: $id $name — restart it from the dashboard after this stream to use the new code"
    fi
  done
fi

echo "==> check"
"$PY" clipper.py doctor || true
echo "Updated to $(git rev-parse --short HEAD)."
