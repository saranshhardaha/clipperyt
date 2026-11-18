"""Numbers for the dashboard: per live job, per session, and for the machine.

Chat files can grow to hundreds of thousands of lines over a night, so they
are read incrementally (we remember the byte offset) and cached in memory.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import clipper
import render
import spikes

_lock = threading.Lock()


class ChatTail:
    """Incrementally parsed chat_<platform>.tsv."""

    def __init__(self, path: Path):
        self.path = path
        self.offset = 0
        self.rows: list[tuple[int, str, str]] = []
        self.authors: set[str] = set()
        self.inode = None

    def update(self) -> "ChatTail":
        try:
            st = self.path.stat()
        except OSError:
            return self
        if st.st_ino != self.inode or st.st_size < self.offset:  # replaced / truncated
            self.__init__(self.path)
            self.inode = st.st_ino
        if st.st_size == self.offset:
            return self
        with open(self.path, "rb") as f:
            f.seek(self.offset)
            data = f.read()
        end = data.rfind(b"\n") + 1  # only whole lines; the rest is still being written
        self.offset += end
        for line in data[:end].decode("utf-8", "replace").splitlines():
            parts = line.split("\t", 2)
            if len(parts) == 3 and parts[0].isdigit():
                self.rows.append((int(parts[0]), parts[1], parts[2]))
                self.authors.add(parts[1])
        return self


_tails: dict[str, ChatTail] = {}


def tail_for(path: Path) -> ChatTail:
    with _lock:
        t = _tails.get(str(path))
        if t is None:
            t = _tails[str(path)] = ChatTail(path)
    return t.update()


def chat_of(session: Path) -> dict[str, ChatTail]:
    return {f.stem.replace("chat_", "") or "chat": tail_for(f) for f in sorted(session.glob("chat*.tsv"))}


def per_minute(rows: list[tuple[int, str, str]], start_ms: int, end_ms: int, n: int) -> list[int]:
    """Messages per minute for the n minutes ending at end_ms (epoch or offset ms)."""
    out = [0] * n
    lo = end_ms - n * 60_000
    for ts, _, _ in rows:
        if lo <= ts < end_ms and ts >= start_ms:
            out[int((ts - lo) // 60_000)] += 1
    return out


def bitrate_mbps(session: Path) -> float | None:
    """From the last ~30 s of timeline.tsv (epoch_ms \\t bytes)."""
    tl = session / "timeline.tsv"
    if not tl.exists():
        return None
    with open(tl, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 2000))
        lines = [l.split(b"\t") for l in f.read().splitlines()[1:]]
    pts = [(int(a), int(b)) for a, b in (l for l in lines if len(l) == 2) if a.isdigit() and b.isdigit()]
    if len(pts) < 2 or pts[-1][0] == pts[0][0]:
        return None
    (t0, b0), (t1, b1) = pts[0], pts[-1]
    return round((b1 - b0) * 8 / ((t1 - t0) / 1000) / 1e6, 2)


_live_spikes: dict[str, tuple[float, int]] = {}


def live_spikes(session: Path, rec_start_ms: int, rows) -> int:
    """How many moments so far (recomputed at most once a minute)."""
    key = str(session)
    hit = _live_spikes.get(key)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    try:
        cfg = clipper.load_cfg()["spikes"]
        n = len(spikes.find_moments(sorted(rows), rec_start_ms, cfg))
    except Exception:
        n = 0
    _live_spikes[key] = (time.time(), n)
    return n


def dir_bytes(d: Path) -> int:
    total = 0
    for root, _, files in os.walk(d):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def live_stats(state: dict) -> dict | None:
    """While a job is recording: minutes, GB, Mbps, chat per platform, msgs/min, sparkline."""
    sid = state.get("session")
    if state.get("status") != "live" or not sid:
        return None
    sess = clipper.find_session(sid) or clipper.SESSIONS / sid
    ts = sess / "stream.ts"
    chats = chat_of(sess)
    rows = [r for t in chats.values() for r in t.rows]
    now = int(time.time() * 1000)
    start = int(state.get("rec_start_ms") or now)
    spark = per_minute(rows, start, now, 30)
    return {
        "minutes": round((now - start) / 60000, 1),
        "gb": round(ts.stat().st_size / 1e9, 2) if ts.exists() else 0,
        "mbps": bitrate_mbps(sess),
        "chat": {p: len(t.rows) for p, t in chats.items()},
        "chatters": len(set().union(*(t.authors for t in chats.values()))) if chats else 0,
        "per_min": per_minute(rows, start, now, 1)[0],
        "spark": spark,
        "spikes": live_spikes(sess, start, rows),
    }


_sess_cache: dict[str, tuple[tuple, dict]] = {}


def session_stats(d: Path) -> dict:
    """Duration, chat, chatters, spikes, clips, size for one session folder (cached)."""
    files = sorted(d.glob("chat*.tsv"))
    clips_dir = d / "clips"
    sig = (tuple((f.name, f.stat().st_size) for f in files),
           (d / "spikes.json").stat().st_mtime if (d / "spikes.json").exists() else 0,
           clips_dir.stat().st_mtime if clips_dir.exists() else 0,
           (d / "stream.ts").stat().st_size if (d / "stream.ts").exists() else 0)
    hit = _sess_cache.get(str(d))
    if hit and hit[0] == sig:
        return hit[1]
    try:
        meta = json.loads((d / "meta.json").read_text())
    except Exception:
        meta = {}
    chats = chat_of(d)
    rows = [r for t in chats.values() for r in t.rows]
    duration = None
    tl = d / "timeline.tsv"
    if tl.exists():
        lines = tl.read_text().split()
        stamps = [int(x) for x in lines[0::2] if x.isdigit()]
        if stamps:
            duration = (stamps[-1] - stamps[0]) / 1000
    elif meta.get("duration"):
        duration = float(meta["duration"])
    elif meta.get("vod") and rows:
        duration = max(r[0] for r in rows) / 1000
    spk = []
    if (d / "spikes.json").exists():
        try:
            spk = json.loads((d / "spikes.json").read_text())
        except Exception:
            pass
    clips = [c for c in clips_dir.glob("*/") if c.is_dir()] if clips_dir.exists() else []
    finals = [f for c in clips for f in (c / "short.mp4", c / "captioned.mp4") if f.exists()]
    out = {
        "duration_min": round(duration / 60, 1) if duration else None,
        "chat": len(rows), "chat_by_platform": {p: len(t.rows) for p, t in chats.items()},
        "chatters": len(set().union(*(t.authors for t in chats.values()))) if chats else 0,
        "spikes": len(spk), "clips": len(finals),
        "failed": len(clips) - len(finals),
        "gb": round(dir_bytes(d) / 1e9, 2),
        "has_source": any((d / f).exists() for f in ("stream.ts", "stream.mp4", "stream.mkv")),
        "vertical": any(f.name == "short.mp4" for f in finals) if finals else None,
    }
    _sess_cache[str(d)] = (sig, out)
    return out


_sys_cache: dict = {}


def _version(cmd: list[str]) -> str | None:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return "installed (version check failed)"
    line = (r.stdout or r.stderr).strip().splitlines()
    return line[0][:80] if line else ""


def ytdlp_js() -> dict:
    """yt-dlp's JS runtime (deno…) + challenge solver — needed for YouTube formats."""
    try:
        r = subprocess.run(["yt-dlp", "-v"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return {"runtime": None}
    m = re.search(r"JS runtimes: (.+)", r.stderr + r.stdout)
    rt = m[1].strip() if m and "none" not in m[1].lower() else None
    ejs = "yt_dlp_ejs" in (r.stderr + r.stdout)
    return {"runtime": f"{rt}{'' if ejs else ' (yt-dlp-ejs missing)'}" if rt else None, "ejs": ejs}


def tools() -> dict:
    """Which external tools exist + versions. Cached 10 min (each check spawns a process)."""
    if _sys_cache.get("t", 0) > time.time() - 600:
        return _sys_cache["tools"]
    cfg = clipper.load_cfg()
    cap = cfg["captions"]
    wbin = cap.get("whisper_bin", "whisper-cli")
    if wbin == "whisper-cli" and (clipper.ROOT / "bin" / "whisper-cli").exists():
        wbin = str(clipper.ROOT / "bin" / "whisper-cli")
    model = Path(cap.get("model", ""))
    model = model if model.is_absolute() else clipper.ROOT / model

    def which(name):
        return shutil.which(name)

    out = {
        "ffmpeg": {"path": which("ffmpeg"), "version": _version(["ffmpeg", "-version"]) if which("ffmpeg") else None},
        "libass": {"ok": bool(which("ffmpeg")) and render.has_ass(),
                   "hint": "captions + credit need it — run setup again"},
        "streamlink": {"path": which("streamlink"),
                       "version": _version(["streamlink", "--version"]) if which("streamlink") else None},
        "yt-dlp": {"path": which("yt-dlp"), "version": _version(["yt-dlp", "--version"]) if which("yt-dlp") else None},
        "yt-dlp-js": ytdlp_js() if which("yt-dlp") else {"runtime": None},
        "whisper": {"path": which(wbin) or (wbin if Path(wbin).exists() else None)},
        "model": {"path": str(model), "ok": model.exists(),
                  "mb": round(model.stat().st_size / 1e6) if model.exists() else None},
        "encoder": render.pick_encoder(cfg["render"].get("encoder", "auto")) if which("ffmpeg") else None,
    }
    _sys_cache.update(t=time.time(), tools=out)
    return out


def system_stats(sessions: list[dict]) -> dict:
    du = shutil.disk_usage(clipper.HOME)
    disks, seen = [], set()
    for label, p in [("recordings", clipper.SESSIONS), ("app data", clipper.HOME),
                     *[("older sessions", r) for r in clipper.session_roots()[1:]]]:
        try:
            dev = os.stat(p).st_dev
            u = shutil.disk_usage(p)
        except OSError:
            if label == "recordings":
                disks.append({"label": label, "path": str(p), "missing": True})
            continue
        if dev in seen:
            continue
        seen.add(dev)
        disks.append({"label": label, "path": str(p), "free_gb": round(u.free / 1e9, 1),
                      "total_gb": round(u.total / 1e9, 1), "used_pct": round(100 * (u.total - u.free) / u.total, 1)})
    try:
        load = [round(x, 2) for x in os.getloadavg()]
    except OSError:
        load = None
    return {
        "disk": {"free_gb": round(du.free / 1e9, 1), "total_gb": round(du.total / 1e9, 1),
                 "used_pct": round(100 * (du.total - du.free) / du.total, 1)},
        "disks": disks, "recordings_dir": str(clipper.SESSIONS),
        "load": load, "cpus": os.cpu_count(),
        "platform": f"{platform.system()} {platform.release()} · {platform.machine()}",
        "python": sys.version.split()[0],
        "home": str(clipper.HOME),
        "totals": {"sessions": len(sessions),
                   "clips": sum(s["stats"]["clips"] for s in sessions),
                   "gb": round(sum(s["stats"]["gb"] for s in sessions), 1),
                   "chat": sum(s["stats"]["chat"] for s in sessions),
                   "hours": round(sum(s["stats"]["duration_min"] or 0 for s in sessions) / 60, 1)},
        "tools": tools(),
    }
