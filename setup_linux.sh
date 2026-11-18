#!/usr/bin/env bash
# One-time setup on Debian/Ubuntu (e.g. a Proxmox LXC). Safe to re-run.
# NVIDIA GPU passed through? whisper builds with CUDA and ffmpeg uses NVENC automatically.
set -euo pipefail
cd "$(dirname "$0")"

echo "==> apt packages"
sudo apt-get update -qq
sudo apt-get install -y -qq ffmpeg python3 python3-venv git cmake build-essential curl fonts-noto-core

[ -f config.toml ] || cp config.example.toml config.toml

echo "==> python venv (streamlink + yt-dlp live here too)"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt streamlink yt-dlp

echo "==> whisper.cpp → bin/whisper-cli"
mkdir -p bin models sessions logs
if [ ! -x bin/whisper-cli ]; then
  [ -d .whisper.cpp ] || git clone -q --depth 1 https://github.com/ggml-org/whisper.cpp .whisper.cpp
  CUDA=OFF; command -v nvcc >/dev/null && CUDA=ON
  echo "   CUDA=$CUDA"
  cmake -S .whisper.cpp -B .whisper.cpp/build -DBUILD_SHARED_LIBS=OFF -DGGML_CUDA=$CUDA -DCMAKE_BUILD_TYPE=Release >/dev/null
  cmake --build .whisper.cpp/build -j"$(nproc)" --target whisper-cli >/dev/null
  cp .whisper.cpp/build/bin/whisper-cli bin/whisper-cli
fi

echo "==> whisper model (large-v3-turbo q5, ~550 MB)"
M=models/ggml-large-v3-turbo-q5_0.bin
[ -f "$M" ] || curl -L --fail -o "$M" \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin

ffmpeg -hide_banner -filters 2>/dev/null | grep -q " ass " || echo "!! ffmpeg lacks libass (ass filter) — captions won't burn in"
# yt-dlp/streamlink are in the venv; make them visible to clipper.py
echo
echo "Done. Edit config.toml, then:  .venv/bin/python server.py   (dashboard on :8787)"
echo "Run on boot: see deploy/README-systemd.md"
