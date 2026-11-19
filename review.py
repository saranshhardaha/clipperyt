"""Review state of a clip: new → kept / skipped → posted. Stored in clips/<clip>/review.json
so it survives reloads and is the same on every device."""
from __future__ import annotations

import json
import time
from pathlib import Path

STATES = ("new", "kept", "skipped", "posted")


def get(clip: Path) -> dict:
    try:
        r = json.loads((clip / "review.json").read_text())
        if r.get("state") in STATES:
            return r
    except Exception:
        pass
    return {"state": "new"}


def put(clip: Path, state: str, title: str | None = None, url: str | None = None, **extra) -> dict:
    if state not in STATES:
        raise ValueError(f"state must be one of {', '.join(STATES)}")
    r = get(clip)
    r.update(state=state, at=int(time.time()))
    for k, v in extra.items():  # video_id / publish_at / privacy from a YouTube upload; None removes
        if v is None:
            r.pop(k, None)
        else:
            r[k] = v
    if title is not None:
        r["title"] = str(title)[:200]
    if url is not None:
        r["url"] = str(url)[:300]
    if state == "new":
        r = {"state": "new"}
    tmp = clip / "review.tmp"
    tmp.write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(clip / "review.json")
    return r
