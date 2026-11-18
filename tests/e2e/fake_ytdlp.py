#!/usr/bin/env python3
"""Fake yt-dlp for a finished YouTube stream: -J metadata, --write-subs chat replay, video download."""
import json, os, shutil, sys
from pathlib import Path
args = sys.argv[1:]
work = Path(os.environ["E2E_WORK"])
url = args[-1]
if "-J" in args and "LIVEVIDEO" in url:
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
