#!/usr/bin/env python3
"""Fake streamlink: first call per URL 'goes live' and writes a .ts slowly; later calls wait forever (offline)."""
import os, sys, time, hashlib
from pathlib import Path
args = sys.argv[1:]
out = Path(args[args.index("-o") + 1])
url = args[-2]
work = Path(os.environ["E2E_WORK"])
mark = work / ("streamed-" + hashlib.md5(url.encode()).hexdigest()[:8])
try:
    time.sleep(2)
    if "BLOCKED" in url:  # YouTube bot check: streamlink can't get the stream
        while True:
            print(f"error: No playable streams found on this URL: {url}", flush=True)
            time.sleep(5)
    if mark.exists():
        while True:
            time.sleep(1)
    mark.touch()
    data = (work / "src.ts").read_bytes()
    secs = float(os.environ.get("E2E_SECONDS", "90"))
    step = len(data) / secs / 4
    with open(out, "wb") as f:
        i = 0.0
        while i < len(data):
            f.write(data[int(i):int(i + step)]); f.flush()
            i += step
            time.sleep(0.25)
    print("stream ended", flush=True)
except KeyboardInterrupt:
    pass
