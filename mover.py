"""Move finished sessions into the recordings folder (e.g. onto another drive).

Runs in a background thread inside server.py, one session at a time.
Same drive → instant rename. Other drive → copy into `.moving-<id>`, check
every file's size, rename into place, and only then delete the original.
If the dashboard dies mid-copy, the original is untouched and the partial
`.moving-*` folder is cleaned up next start.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import threading
from pathlib import Path

import clipper

# sid → {"status": queued|moving|done|error, "done": bytes, "total": bytes, "to": str, "error": str}
MOVES: dict[str, dict] = {}
_q: "queue.Queue[str]" = queue.Queue()
_lock = threading.Lock()
_started = False


def tree_size(d: Path) -> int:
    total = 0
    for root, _, files in os.walk(d):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def _copy_file(src: Path, dst: Path, st: dict) -> None:
    with open(src, "rb") as a, open(dst, "wb") as b:
        while chunk := a.read(8 << 20):
            b.write(chunk)
            st["done"] += len(chunk)
    shutil.copystat(src, dst)


def session_channel(src: Path) -> str:
    try:
        return json.loads((src / "meta.json").read_text()).get("channel") or "unknown"
    except Exception:
        return "unknown"


def move_session(src: Path, dst_root: Path, st: dict, copy: bool | None = None) -> Path:
    """Move one session folder to dst_root/<channel>/<id>. Returns the new path.
    copy=None → rename if same filesystem, else copy+verify+delete (tests force copy=True)."""
    err = clipper.check_dir(dst_root, create=dst_root == clipper.DEFAULT_SESSIONS)
    if err:
        raise RuntimeError(err)
    parent = clipper.channel_dir(dst_root, session_channel(src))
    parent.mkdir(exist_ok=True)
    final = parent / src.name
    if final.exists():
        raise RuntimeError(f"{final} already exists")
    st["total"] = tree_size(src)
    if copy is None:
        copy = os.stat(src).st_dev != os.stat(parent).st_dev
    if not copy:  # same filesystem
        os.rename(src, final)
        st["done"] = st["total"]
        return final
    tmp = parent / f".moving-{src.name}"
    shutil.rmtree(tmp, ignore_errors=True)
    for root, dirs, files in os.walk(src):
        rel = Path(root).relative_to(src)
        (tmp / rel).mkdir(parents=True, exist_ok=True)
        for f in files:
            _copy_file(Path(root) / f, tmp / rel / f, st)
    for root, _, files in os.walk(src):  # verify before deleting anything
        rel = Path(root).relative_to(src)
        for f in files:
            if (Path(root) / f).stat().st_size != (tmp / rel / f).stat().st_size:
                raise RuntimeError(f"size mismatch copying {rel / f}")
    os.rename(tmp, final)
    shutil.rmtree(src)
    return final


def _worker() -> None:
    while True:
        sid = _q.get()
        st = MOVES[sid]
        src = clipper.find_session(sid)
        dst_root = clipper.SESSIONS
        try:
            if not src:
                raise RuntimeError("session not found")
            if src.parent.parent == dst_root:  # already in <recordings>/<channel>/
                st.update(status="done", done=0, total=0)
                continue
            st.update(status="moving", to=str(dst_root))
            move_session(src, dst_root, st)
            st["status"] = "done"
        except Exception as e:
            st.update(status="error", error=str(e))
            for d in dst_root.glob(f"*/.moving-{sid}"):
                shutil.rmtree(d, ignore_errors=True)


def enqueue(sid: str) -> str:
    global _started
    with _lock:
        if MOVES.get(sid, {}).get("status") in ("queued", "moving"):
            return "already moving"
        MOVES[sid] = {"status": "queued", "done": 0, "total": 0, "to": str(clipper.SESSIONS)}
        if not _started:
            threading.Thread(target=_worker, daemon=True).start()
            _started = True
    _q.put(sid)
    return "queued"


def busy(sid: str) -> str | None:
    st = MOVES.get(sid)
    return st["status"] if st and st["status"] in ("queued", "moving") else None


def cleanup_partial() -> None:
    """Remove `.moving-*` leftovers from a dashboard that stopped mid-copy."""
    for r in clipper.session_roots():
        if r.is_dir():
            for d in [*r.glob(".moving-*"), *r.glob("*/.moving-*")]:
                shutil.rmtree(d, ignore_errors=True)
