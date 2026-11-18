#!/usr/bin/env bash
# Linux: put deno (a JS runtime) in bin/. yt-dlp needs it to solve YouTube's JavaScript
# challenge — without it YouTube downloads fail with "No video formats found".
# macOS gets deno through Homebrew's yt-dlp, so this does nothing there.
set -euo pipefail
APP="$(cd "$(dirname "$0")/.." && pwd)"
[ "$(uname)" = "Linux" ] || exit 0
if command -v deno >/dev/null 2>&1; then echo "   deno: $(command -v deno)"; exit 0; fi
if [ -x "$APP/bin/deno" ]; then echo "   deno: $APP/bin/deno"; exit 0; fi
case "$(uname -m)" in
  x86_64|amd64)  T=x86_64-unknown-linux-gnu ;;
  aarch64|arm64) T=aarch64-unknown-linux-gnu ;;
  *) echo "!! no deno build for $(uname -m) — YouTube video downloads may fail"; exit 0 ;;
esac
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$APP/bin"
if curl -fsSL -o "$tmp/deno.zip" "https://github.com/denoland/deno/releases/latest/download/deno-$T.zip" \
   && python3 -m zipfile -e "$tmp/deno.zip" "$tmp/x" && install -m 755 "$tmp/x/deno" "$APP/bin/deno"; then
  echo "   deno: $APP/bin/deno ($("$APP/bin/deno" --version | head -1))"
else
  echo "!! couldn't install deno — YouTube video downloads may fail (No video formats found)"
fi
