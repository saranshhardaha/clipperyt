#!/usr/bin/env bash
# One-time setup on macOS (Apple Silicon). Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")"

command -v brew >/dev/null || { echo "Install Homebrew first: https://brew.sh"; exit 1; }

echo "==> brew packages"
brew install ffmpeg streamlink yt-dlp whisper-cpp python@3.12

echo "==> checking ffmpeg can burn subtitles (needs libass)"
if ! ffmpeg -hide_banner -filters 2>/dev/null | grep -q " subtitles "; then
  echo "!! Your ffmpeg has no 'subtitles' filter. Fix:"
  echo "   brew uninstall ffmpeg && brew tap homebrew-ffmpeg/ffmpeg && brew install homebrew-ffmpeg/ffmpeg/ffmpeg"
fi

[ -f config.toml ] || cp config.example.toml config.toml

echo "==> python venv"
PYBIN="$(brew --prefix python@3.12)/bin/python3.12"
[ -d .venv ] || "$PYBIN" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

echo "==> whisper model (large-v3-turbo q5, ~550 MB, multilingual incl. Hindi)"
mkdir -p models sessions
M=models/ggml-large-v3-turbo-q5_0.bin
[ -f "$M" ] || curl -L --fail -o "$M" \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin

echo
echo "Done. Next: edit config.toml (platform + channel), then ./run.sh"
echo "Kick check: 'streamlink --plugins | tr , \"\\n\" | grep -i kick' — if empty, set recorder = \"yt-dlp\""
