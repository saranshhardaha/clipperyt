#!/usr/bin/env bash
# Start watching in this terminal. Logs to logs/. Ctrl-C stops (still clips what was recorded).
# For a server, prefer the dashboard (python server.py) or the systemd units in deploy/.
cd "$(dirname "$0")"
mkdir -p logs
LOG="logs/watch-$(date +%Y%m%d-%H%M%S).log"
echo "logging to $LOG"
if [[ "$(uname)" == "Darwin" ]]; then
  caffeinate -is .venv/bin/python -u clipper.py watch 2>&1 | tee -a "$LOG"   # keep the Mac awake
else
  .venv/bin/python -u clipper.py watch 2>&1 | tee -a "$LOG"
fi
