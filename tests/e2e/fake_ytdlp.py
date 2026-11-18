#!/usr/bin/env python3
"""Fake yt-dlp for a finished YouTube stream: -J metadata, --write-subs chat replay, video download."""
import json, os, shutil, sys
from pathlib import Path
args = sys.argv[1:]
work = Path(os.environ["E2E_WORK"])
url = args[-1]
if "--hls-use-mpegts" in args:  # live recording (recorder = yt-dlp)
    import hashlib, time
    out = Path(args[args.index("-o") + 1])
    mark = work / ("streamed-" + hashlib.md5(url.encode()).hexdigest()[:8])
    if mark.exists():  # already streamed once → channel is offline now
        print(f"ERROR: [youtube:tab] {url}: The channel is not currently live", flush=True)
        sys.exit(1)
    mark.touch()
    data = (work / "src.ts").read_bytes()
    secs = float(os.environ.get("E2E_SECONDS", "90"))
    step = len(data) / secs / 4
    try:
        with open(out, "wb") as f:
            i = 0.0
            while i < len(data):
                f.write(data[int(i):int(i + step)]); f.flush()
                i += step
                time.sleep(0.25)
    except KeyboardInterrupt:
        pass
    sys.exit(0)
if "-J" in args and ("LIVEVIDEO" in url or "BLOCKED" in url):
    print(json.dumps({"live_status": "is_live", "uploader_id": "@liveguy", "title": "Live now"}))
elif "-J" in args:
    print(json.dumps({"live_status": "was_live", "uploader_id": "@vodguy", "title": "Old stream", "duration": 90}))
elif "--write-subs" in args:
    base = args[args.index("-o") + 1]
    with open(base + ".live_chat.json", "w") as f:
        n = 0
        for ms in range(0, 90_000, 500):
            burst = 60_000 <= ms < 66_000
            for k in range(8 if burst else 1):
                n += 1
                item = {"liveChatTextMessageRenderer": {"timestampUsec": str((1_800_000_000_000 + ms) * 1000),
                        "authorName": {"simpleText": f"user{n % 40}"},
                        "message": {"runs": [{"text": "OMG bhai" if burst else f"hello {n}"}]}}}
                f.write(json.dumps({"replayChatItemAction": {"videoOffsetTimeMsec": str(ms + k * 50),
                        "actions": [{"addChatItemAction": {"item": item}}]}}) + "\n")
elif "--print" in args:
    print("FAKEVIDEO01")
else:
    out = args[args.index("-o") + 1].replace("%(ext)s", "mp4")
    shutil.copy(work / "src.mp4", out)
