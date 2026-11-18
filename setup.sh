#!/usr/bin/env bash
# One-time setup on macOS (Apple Silicon or Intel). Safe to re-run.
#   ./setup.sh             install everything
#   ./setup.sh --service   …and start the dashboard at login (LaunchAgent)
set -euo pipefail
cd "$(dirname "$0")"

command -v brew >/dev/null || { echo "Install Homebrew first: https://brew.sh"; exit 1; }

echo "==> brew packages"
brew install ffmpeg streamlink yt-dlp whisper-cpp python@3.12

echo "==> ffmpeg with libass (burns in captions + the streamer credit)"
if ffmpeg -hide_banner -filters 2>/dev/null | grep -qE '^\s*\S+\s+ass\s'; then
  echo "   ffmpeg already has libass"
else
  # Homebrew's plain ffmpeg (8+) is built without libass. ffmpeg-full is keg-only, so it
  # doesn't replace the normal one; clipper.py puts it first on PATH automatically.
  brew install ffmpeg-full
fi

[ -f config.toml ] || cp config.example.toml config.toml
mkdir -p models sessions jobs logs locks

echo "==> python venv"
PYBIN="$(brew --prefix python@3.12)/bin/python3.12"
if [ -x .venv/bin/python ] && ! .venv/bin/python -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
  echo "   .venv uses an old Python — recreating"; rm -rf .venv
fi
[ -d .venv ] || "$PYBIN" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

echo "==> whisper model (large-v3-turbo q5, ~550 MB, multilingual incl. Hindi)"
M=models/ggml-large-v3-turbo-q5_0.bin
[ -f "$M" ] || curl -L --fail -o "$M" \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin

echo "==> check"
.venv/bin/python clipper.py doctor || true
streamlink --plugins 2>/dev/null | tr ',' '\n' | grep -qi kick \
  || echo "   note: streamlink has no Kick plugin — for Kick set Recorder = yt-dlp in Settings"

if [ "${1:-}" = "--service" ]; then
  ./deploy/install_service.sh
else
  echo
  echo "Done. Start the dashboard:  .venv/bin/python server.py   → http://localhost:8787"
  echo "Start it at login instead:  ./deploy/install_service.sh"
fi
