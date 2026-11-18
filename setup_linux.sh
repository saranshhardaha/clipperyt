#!/usr/bin/env bash
# One-time setup on Debian/Ubuntu (e.g. a Proxmox LXC). Safe to re-run.
#   ./setup_linux.sh             install everything
#   ./setup_linux.sh --service   …and run the dashboard on boot (systemd)
# NVIDIA GPU passed through? whisper builds with CUDA and ffmpeg uses NVENC automatically.
set -euo pipefail
cd "$(dirname "$0")"

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null || { echo "Run as root, or install sudo"; exit 1; }
  SUDO="sudo"
fi

echo "==> apt packages"
$SUDO apt-get update -qq
$SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  ffmpeg python3 python3-venv git cmake build-essential curl ca-certificates fonts-noto-core

echo "==> python ≥ 3.11"
newest_ok() {
  for p in python3.13 python3.12 python3.11 python3; do
    if command -v "$p" >/dev/null && "$p" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
      command -v "$p"; return 0
    fi
  done
  return 1
}
PYBIN="$(newest_ok || true)"
if [ -z "$PYBIN" ]; then
  # e.g. Ubuntu 22.04 ships 3.10 as python3, but has python3.11 in the archive
  $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3.11 python3.11-venv 2>/dev/null || true
  PYBIN="$(newest_ok || true)"
fi
if [ -z "$PYBIN" ]; then
  PYBIN="$(command -v python3)"
  echo "   only $($PYBIN --version) available — using it with the tomli backport"
else
  V="$($PYBIN -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
  $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "python$V-venv" 2>/dev/null || true
  echo "   using $PYBIN ($V)"
fi

[ -f config.toml ] || cp config.example.toml config.toml
mkdir -p bin models sessions jobs logs locks

echo "==> python venv (streamlink + yt-dlp live here too)"
if [ -x .venv/bin/python ] && [ "$(readlink -f .venv/bin/python)" != "$(readlink -f "$PYBIN")" ] \
   && ! .venv/bin/python -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
  echo "   .venv uses an older Python — recreating"; rm -rf .venv
fi
[ -d .venv ] || "$PYBIN" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q --upgrade -r requirements.txt streamlink yt-dlp

echo "==> whisper.cpp → bin/whisper-cli"
if [ ! -x bin/whisper-cli ]; then
  [ -d .whisper.cpp ] || git clone -q --depth 1 https://github.com/ggml-org/whisper.cpp .whisper.cpp
  CUDA=OFF; command -v nvcc >/dev/null && CUDA=ON
  build() {  # $1 = GGML_NATIVE
    rm -rf .whisper.cpp/build
    cmake -S .whisper.cpp -B .whisper.cpp/build -DBUILD_SHARED_LIBS=OFF -DGGML_CUDA=$CUDA \
      -DGGML_NATIVE="$1" -DCMAKE_BUILD_TYPE=Release >.whisper.cpp/build.log 2>&1 &&
    cmake --build .whisper.cpp/build -j"$(nproc)" --target whisper-cli >>.whisper.cpp/build.log 2>&1
  }
  echo "   CUDA=$CUDA"
  # native = fastest; some CPU/compiler combos (e.g. arm64 + gcc 12) fail → portable build
  if build ON || { echo "   native build failed — trying a portable build"; build OFF; }; then
    cp .whisper.cpp/build/bin/whisper-cli bin/whisper-cli
  else
    echo "!! whisper.cpp didn't build (see .whisper.cpp/build.log) — clips will have no captions"
  fi
fi

echo "==> whisper model (large-v3-turbo q5, ~550 MB)"
M=models/ggml-large-v3-turbo-q5_0.bin
[ -f "$M" ] || curl -L --fail -o "$M" \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin

echo "==> check"
.venv/bin/python clipper.py doctor || true

if [ "${1:-}" = "--service" ]; then
  ./deploy/install_service.sh
else
  echo
  echo "Done. Start the dashboard:  .venv/bin/python server.py   (http://<this-machine>:8787)"
  echo "Run it on boot instead:     ./deploy/install_service.sh"
fi
