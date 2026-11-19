#!/usr/bin/env python3
"""clipperyt dashboard — run many streams at once, edit settings, see stats, watch clips.

  python server.py                       → http://0.0.0.0:8787
  CLIPPER_TOKEN=secret python server.py  → requires ?token=secret once (then a cookie)
  CLIPPER_PORT=9000 / CLIPPER_HOST=127.0.0.1 to change where it listens
  CLIPPER_HOME=/data/clipperyt           → keep config/sessions/jobs somewhere else

Standard library only. Every job runs as its own detached `clipper.py job <id>`
process, so restarting this server never interrupts a recording.
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import clipper
import mover
import review
import settings_schema
import stats
import titles
import youtube
import queue as _queue

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

ROOT = clipper.ROOT
HOME = clipper.HOME
JOBS = clipper.JOBS
TOKEN = os.environ.get("CLIPPER_TOKEN", "")
PY = sys.executable
SESSION_RE = re.compile(r"^\d{8}-\d{4}[\w-]*$")
RUNNING = ("starting", "waiting", "live", "queued", "processing", "downloading", "idle")


# ------------------------------------------------------------------ jobs

def job_state(jid: str) -> dict:
    return clipper.read_state(clipper.job_dir(jid) / "state.json")


def is_running(st: dict) -> bool:
    return st.get("status") in RUNNING and clipper.pid_alive(st.get("pid"))


def start_job(jid: str) -> str:
    st = job_state(jid)
    if is_running(st):
        return "already running"
    d = clipper.job_dir(jid)
    (d / ".stop").unlink(missing_ok=True)
    logf = open(d / "job.log", "a")
    logf.write(f"\n===== started {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
    logf.flush()
    cmd = [PY, "-u", str(ROOT / "clipper.py"), "job", jid]
    if sys.platform == "darwin" and shutil.which("caffeinate"):
        cmd = ["caffeinate", "-is"] + cmd  # keep the Mac awake while this job runs
    # own session: survives server restarts, gets its own Ctrl-C
    p = subprocess.Popen(cmd, cwd=ROOT, stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         start_new_session=True, env={**os.environ, "CLIPPER_HOME": str(HOME)})
    logf.close()
    (d / "state.json").write_text(json.dumps({"status": "starting", "pid": p.pid, "since": int(time.time())}))
    threading.Thread(target=p.wait, daemon=True).start()  # reap when it exits
    return "started"


def stop_job(jid: str, force: bool = False) -> str:
    st = job_state(jid)
    if not is_running(st):
        return "not running"
    if st["status"] in ("processing", "queued") and not force:
        (clipper.job_dir(jid) / ".stop").touch()  # finish the clips, then exit
        return "will stop after the current clips are done"
    return signal_group(st["pid"], force)


def signal_group(pid: int, force: bool) -> str:
    try:
        # SIGINT = graceful: stops recording, still clips what was recorded
        os.killpg(os.getpgid(pid), signal.SIGKILL if force else signal.SIGINT)
    except OSError as e:
        return f"error: {e}"
    return "killed" if force else "stopping — clipping what was recorded"


def create_job(data: dict) -> tuple[dict | None, str | None]:
    kind = data.get("kind")
    job: dict = {"kind": kind, "autostart": bool(data.get("autostart")),
                 "created": int(time.time())}
    if kind == "watch":
        chans = {p: str(c).strip().lstrip("@") for p, c in (data.get("channels") or {}).items()
                 if p in clipper.NAMES and str(c or "").strip()}
        if not chans:
            return None, "add at least one channel"
        rf = data.get("record_from")
        if rf not in chans:  # e.g. only Twitch filled in while the dropdown still says YouTube
            rf = next(iter(chans))
        job.update(channels=chans, record_from=rf)
        name = data.get("name") or chans[rf]
    elif kind == "url":
        url = str(data.get("url") or "").strip()
        if not re.match(r"^https?://", url):
            return None, "paste a full https:// link"
        try:
            clipper.platform_of(url)
        except SystemExit as e:
            return None, str(e)
        job["url"] = url
        name = data.get("name") or re.sub(r"^https?://(www\.)?", "", url)[:40]
    elif kind == "process":
        sid = data.get("session", "")
        if not SESSION_RE.match(sid) or not clipper.find_session(sid):
            return None, "no such session"
        job["session"] = sid
        if data.get("mode") in ("requests", "redo", "manual"):
            job["mode"] = data["mode"]
        if job.get("mode") == "manual":
            try:
                job["at"] = max(0.0, float(data.get("at")))
            except (TypeError, ValueError):
                return None, "pick a time on the timeline"
        if job.get("mode") == "redo":
            job["clips"] = [c for c in (data.get("clips") or []) if clipper.CLIP_RE.match(str(c))]
        label = {"requests": "clip requests", "redo": "fix clips",
                 "manual": f"clip at {clipper.spikes.fmt(job.get('at', 0))}"}.get(job.get("mode"), "re-clip")
        name = data.get("name") or f"{label} {sid}"
    else:
        return None, "kind must be watch, url or process"
    job["name"] = str(name)[:60]
    job["id"] = f"{clipper.slug(job['name'])[:36]}-{secrets.token_hex(2)}"
    clipper.save_job(job)
    return job, None


def delete_job(jid: str) -> str:
    if is_running(job_state(jid)):
        return "stop it first"
    shutil.rmtree(clipper.job_dir(jid), ignore_errors=True)
    return "deleted"


def prune_done() -> None:
    """Finished one-link and fix/re-clip jobs leave the list by themselves. Their sessions and
    clips stay, and the job's log is appended to the session's job.log. Failed jobs stay."""
    for f in JOBS.glob("*/job.json") if JOBS.exists() else []:
        try:
            job = json.loads(f.read_text())
        except Exception:
            continue
        if job.get("kind") not in ("url", "process"):
            continue
        st = clipper.read_state(f.parent / "state.json")
        if st.get("status") != "done" or clipper.pid_alive(st.get("pid")):
            continue
        sid = st.get("session") or job.get("session")
        sess = clipper.find_session(sid) if sid else None
        log = f.parent / "job.log"
        if sess and log.exists():
            try:
                with open(sess / "job.log", "a", encoding="utf-8") as out:
                    out.write(f"\n===== {job.get('name', job['id'])} ({job['id']}) =====\n")
                    out.write(log.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue  # can't save the log (drive gone?) → keep the job for now
        shutil.rmtree(f.parent, ignore_errors=True)


def all_jobs() -> list[dict]:
    prune_done()
    out = []
    for f in sorted(JOBS.glob("*/job.json")) if JOBS.exists() else []:
        try:
            job = json.loads(f.read_text())
        except Exception:
            continue
        st = clipper.read_state(f.parent / "state.json")
        job["state"] = st
        job["running"] = is_running(st)
        job["live"] = stats.live_stats(st) if job["running"] else None
        out.append(job)
    # a `clipper.py watch|run` started from a terminal shows up too
    st = clipper.read_state()
    if is_running(st):
        out.append({"id": "cli", "kind": "cli", "name": f"terminal run · {st.get('channel') or st.get('url') or ''}",
                    "state": st, "running": True, "live": stats.live_stats(st), "readonly": True})
    # stable order: by name, never by activity (the UI groups by status itself)
    out.sort(key=lambda j: (str(j.get("name", "")).lower(), j["id"]))
    return out


def autostart() -> None:
    """Start jobs marked autostart — in the background, once the recordings folder is reachable
    (a NAS/USB drive may mount after the dashboard starts on boot)."""
    todo = []
    for f in sorted(JOBS.glob("*/job.json")) if JOBS.exists() else []:
        try:
            job = json.loads(f.read_text())
        except Exception:
            continue
        if job.get("autostart") and not is_running(job_state(job["id"])):
            todo.append(job["id"])
    if not todo:
        return

    def run():
        try:
            clipper.wait_for_storage(clipper.load_cfg())
        except clipper.StorageUnavailable as e:
            print(f"autostart: {e} — starting anyway; watch jobs keep retrying", flush=True)
        for jid in todo:
            print(f"autostart: {jid} → {start_job(jid)}", flush=True)
    threading.Thread(target=run, daemon=True).start()


# ------------------------------------------------------------------ data

def tail(path: Path | None, n: int = 80) -> str:
    if not path or not path.exists():
        return ""
    with open(path, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 64_000))
        return "\n".join(f.read().decode("utf-8", "replace").splitlines()[-n:])


def busy_sessions() -> dict[str, str]:
    """session id → status of the job (or move) currently working on it."""
    out = {sid: f"move {st['status']}" for sid, st in mover.MOVES.items() if mover.busy(sid)}
    for j in all_jobs():
        if j["running"] and j["state"].get("session") and j["state"].get("status") != "waiting":
            out[j["state"]["session"]] = j["state"]["status"]
    return out


def all_session_dirs() -> list[Path]:
    """Sessions from every folder (recordings folder wins if an id exists twice), newest first."""
    seen: dict[str, Path] = {}
    for r in clipper.session_roots():
        for d in clipper.iter_sessions(r):
            if SESSION_RE.match(d.name) and d.name not in seen:
                seen[d.name] = d
    return [seen[k] for k in sorted(seen, reverse=True)]


def where(d: Path) -> dict:
    """in_recordings_folder = already at <recordings>/<channel>/<id>; 'unsorted' = flat, from before channel folders."""
    return {"folder": str(d.parent), "in_recordings_folder": d.parent.parent == clipper.SESSIONS,
            "unsorted": d.parent == clipper.SESSIONS, "channel_folder": d.parent.name}


def needs_move(d: Path) -> bool:
    return d.parent.parent != clipper.SESSIONS


def sessions() -> list[dict]:
    res = []
    busy = busy_sessions()
    for d in all_session_dirs():
        try:
            meta = json.loads((d / "meta.json").read_text())
        except Exception:
            meta = {}
        res.append({
            "id": d.name, "channel": meta.get("channel", "?"), "platform": meta.get("platform", "?"),
            "title": meta.get("title"), "vod": bool(meta.get("vod")), "started": meta.get("started"),
            "job": meta.get("job"), "busy": busy.get(d.name), "stats": stats.session_stats(d),
            "move": mover.MOVES.get(d.name), **where(d),
        })
    return res


def session_detail(sid: str) -> dict:
    d = clipper.find_session(sid)
    spikes_ = json.loads((d / "spikes.json").read_text()) if (d / "spikes.json").exists() else []
    locked = clipper.locked_clips(d)
    clips = []
    for n, c in enumerate(sorted(p for p in (d / "clips").glob("*/") if p.is_dir())
                          if (d / "clips").exists() else []):
        final = next((c / f for f in ("short.mp4", "captioned.mp4") if (c / f).exists()), None)
        m = spikes_[n] if n < len(spikes_) else {}
        if (c / "moment.json").exists():  # exact moment for this clip (clips can be added later)
            try:
                m = json.loads((c / "moment.json").read_text())
            except Exception:
                pass
        clips.append({
            "name": c.name, "video": f"/files/{sid}/clips/{c.name}/{final.name}" if final else None,
            "vertical": bool(final and final.name == "short.mp4"),
            "raw": f"/files/{sid}/clips/{c.name}/raw.mp4" if (c / "raw.mp4").exists() else None,
            "srt": f"/files/{sid}/clips/{c.name}/speech.srt" if (c / "speech.srt").exists() else None,
            "score": m.get("score"), "messages": m.get("messages"), "top": m.get("top", []),
            "requests": m.get("requests", 0), "kind": m.get("kind", "spike"), "failed": not final,
            "review": review.get(c), "locked": locked.get(c.name),
            "titles": titles.cached(c),
        })
    try:
        meta = json.loads((d / "meta.json").read_text())
    except Exception:
        meta = {}
    return {"id": sid, "meta": meta, "stats": stats.session_stats(d), "clips": clips,
            "busy": busy_sessions().get(sid), "move": mover.MOVES.get(sid), **where(d),
            "clips_md": f"/files/{sid}/clips.md" if (d / "clips.md").exists() else None,
            "process_log": tail(d / "process.log", 30)}


def all_clips(channel: str = "", requested: bool = False, sort: str = "stream",
              offset: int = 0, limit: int = 48, state: str = "") -> dict:
    """Every finished clip across all sessions/folders, for the Review tab.
    sort: stream (newest session first, then in stream order) | new (newest made) | score."""
    items, chans = [], set()
    counts = {s_: 0 for s_ in review.STATES}
    for d in all_session_dirs():
        try:
            meta = json.loads((d / "meta.json").read_text())
        except Exception:
            meta = {}
        ch = meta.get("channel", "?")
        chans.add(ch)
        if channel and ch != channel:
            continue
        spikes_ = []
        if (d / "spikes.json").exists():
            try:
                spikes_ = json.loads((d / "spikes.json").read_text())
            except Exception:
                pass
        for idx, (n, start, c) in enumerate(clipper.clip_folders(d)):
            final = next((c / f for f in ("short.mp4", "captioned.mp4") if (c / f).exists()), None)
            if not final:
                continue
            m = spikes_[idx] if idx < len(spikes_) else {}
            if (c / "moment.json").exists():
                try:
                    m = json.loads((c / "moment.json").read_text())
                except Exception:
                    pass
            if requested and not m.get("requests"):
                continue
            rv = review.get(c)
            counts[rv["state"]] += 1
            if state and rv["state"] != state:
                continue
            base = f"/files/{d.name}/clips/{c.name}"
            items.append({"session": d.name, "channel": ch, "platform": meta.get("platform"),
                          "title": meta.get("title"), "name": c.name, "n": n, "start": start,
                          "made": int(final.stat().st_mtime), "review": rv,
                          "video": f"{base}/{final.name}", "vertical": final.name == "short.mp4",
                          "raw": f"{base}/raw.mp4" if (c / "raw.mp4").exists() else None,
                          "srt": f"{base}/speech.srt" if (c / "speech.srt").exists() else None,
                          "score": m.get("score"), "requests": m.get("requests", 0),
                          "top": [t for t in m.get("top", []) if not t.startswith("clip requested")][:3],
                          "titles": titles.cached(c)})
    if sort == "score":
        items.sort(key=lambda x: x["score"] or 0, reverse=True)
    elif sort == "new":
        items.sort(key=lambda x: x["made"], reverse=True)
    else:  # review order: newest session first, clips in stream order inside it
        items.sort(key=lambda x: (x["session"], -x["start"]), reverse=True)
    return {"total": len(items), "clips": items[offset: offset + limit], "channels": sorted(chans),
            "counts": counts, "ai_ready": ai_ready()}


def ai_ready() -> bool:
    cfg = clipper.load_cfg()["titles"]
    return bool(cfg.get("openrouter_api_key") or os.environ.get("OPENROUTER_API_KEY"))


# ------------------------------------------------------------------ YouTube posting

YT: dict = {"pending": None, "error": None}  # device sign-in in progress
UPLOADS: dict[str, dict] = {}                # "sid/clip" → {status, done, total, error, url, publish_at}
_upq: "_queue.Queue[dict]" = _queue.Queue()
_yt_lock = threading.Lock()


def yt_cfg() -> dict:
    return clipper.load_cfg()["youtube"]


def scheduled_clips() -> list[dict]:
    """Clips uploaded with a future publish time (YouTube will publish them)."""
    out, now = [], time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for d in all_session_dirs():
        for _, _, c in clipper.clip_folders(d):
            r = review.get(c)
            if r.get("publish_at") and r["publish_at"] > now:
                try:
                    ch = json.loads((d / "meta.json").read_text()).get("channel", "?")
                except Exception:
                    ch = "?"
                out.append({"key": f"{d.name}/{c.name}", "session": d.name, "name": c.name, "channel": ch,
                            "publish_at": r["publish_at"], "label": youtube.local_label(yt_cfg(), r["publish_at"]),
                            "url": r.get("url"), "title": r.get("title")})
    return sorted(out, key=lambda x: x["publish_at"])


def yt_status() -> dict:
    cfg = yt_cfg()
    tok = youtube.load_token(HOME) or {}
    configured = bool(str(cfg.get("client_id") or "").strip() and str(cfg.get("client_secret") or "").strip())
    p = YT["pending"]
    sched = scheduled_clips()
    with QUEUE_LOCK:
        q = load_queue()
    nxt = None
    try:
        iso = queue_slot(q, sched=sched)
        nxt = {"iso": iso, "label": youtube.local_label(cfg, iso)}
    except youtube.YouTubeError:
        pass
    items = []
    for i in q["items"]:
        d = clipper.find_session(i["sid"])
        c = d / "clips" / i["name"] if d else None
        final = next((c / f for f in ("short.mp4", "captioned.mp4") if c and (c / f).exists()), None)
        try:
            meta = json.loads((d / "meta.json").read_text()) if d else {}
        except Exception:
            meta = {}
        ch = meta.get("channel", "?")
        t = titles.cached(c) if c else None
        r = review.get(c) if c else {}
        tags = [x for x in ((t or {}).get("hashtags") or []) if x]
        ai_desc = (t or {}).get("description")
        items.append({**i, "channel": ch, "label": youtube.local_label(cfg, i["at"]), "day": youtube.local_day(cfg, i["at"]),
                      "time": youtube.local_time(cfg, i["at"]), "video": f"/files/{i['sid']}/clips/{i['name']}/{final.name}" if final else None,
                      "vertical": bool(final and final.name == "short.mp4"),
                      "title": i.get("title") or review.get(c).get("title") if c else i.get("title"),
                      "suggested": (t or {}).get("titles") or [], "upload": UPLOADS.get(i["key"]),
                      "streamer": streamer_of(meta), "ai_description": ai_desc, "custom_description": bool(r.get("description")),
                      "description": with_credit(r.get("description") or ai_desc or default_description(meta, tags), meta)})
    hist = [{**h, "label": youtube.local_label(cfg, h["posted_at"])} for h in q["history"][:15]]
    return {"configured": configured, "connected": bool(tok.get("refresh_token")),
            "channel": tok.get("channel_title"), "error": YT["error"], "privacy": cfg.get("privacy") or "public",
            "pending": {"user_code": p["user_code"], "url": p["verification_url"],
                        "left": max(0, int(p["expires_at"] - time.time()))} if p else None,
            "next_slot": nxt, "scheduled": sched, "uploads": UPLOADS, "queue": items, "paused": q["paused"],
            "history": hist, "ai_ready": ai_ready(), "plan": {"per_day": cfg.get("slots_per_day", 4), "gap": cfg.get("slot_gap_hours", 3), "random": cfg.get("random_minutes", 60),
                                      "start": cfg.get("window_start", "10:00"), "end": cfg.get("window_end", "22:00"),
                                      "tz": cfg.get("timezone", "Asia/Kolkata")}}


def yt_connect() -> dict:
    cfg = yt_cfg()
    flow = youtube.start_device_flow(cfg)
    YT.update(error=None, pending={**flow, "expires_at": time.time() + int(flow.get("expires_in", 1800))})

    def poll():
        interval = max(2, int(flow.get("interval", 5)))
        def mine():  # still the sign-in on screen (not cancelled or replaced by a newer Connect)
            return bool(YT["pending"]) and YT["pending"]["device_code"] == flow["device_code"]
        while mine():
            time.sleep(interval)
            if not mine():
                return
            try:
                tok = youtube.poll_device_flow(cfg, flow["device_code"])
            except youtube.YouTubeError as e:
                YT.update(pending=None, error=str(e))
                return
            if tok:
                youtube.save_token(HOME, tok)
                try:
                    tok.update(youtube.channel_info(cfg, HOME))
                    youtube.save_token(HOME, tok)
                    YT.update(pending=None, error=None)
                except youtube.YouTubeError as e:
                    YT.update(pending=None, error=str(e))
                return
            if mine() and time.time() > YT["pending"]["expires_at"]:
                YT.update(pending=None, error="The sign-in code expired. Press Connect again.")
                return
    threading.Thread(target=poll, daemon=True).start()
    return yt_status()


def streamer_of(meta: dict) -> dict:
    """Who the clip is from: the name plus every platform it streams on (simulcast)."""
    chans = meta.get("chat_from") or {meta.get("platform", ""): meta.get("channel", "")}
    return {"name": str(meta.get("channel") or "").lstrip("@") or "the streamer",
            "platforms": [titles.NAMES.get(p, p) for p in chans if p]}


def default_description(meta: dict, tags: list[str]) -> str:
    return titles.credit_description(meta) + "\n\n" + " ".join("#" + x for x in tags + ["Shorts"])


def with_credit(desc: str, meta: dict) -> str:
    """Keep the streamer credit in any description, however it was edited."""
    desc = desc.strip()
    who = streamer_of(meta)["name"]
    if f"@{who}".lower() not in desc.lower():
        desc = titles.credit_description(meta) + ("\n\n" + desc if desc else "")
    if "#shorts" not in desc.lower():
        desc += " #Shorts"
    return desc[:4900]


def _clip_title_desc(d: Path, c: Path, title: str | None) -> tuple[str, str, list[str]]:
    meta = json.loads((d / "meta.json").read_text())
    t = titles.cached(c) or {}
    r = review.get(c)
    title = (title or r.get("title") or (t.get("titles") or [None])[0] or f"{meta.get('channel', 'Stream')} clip").strip()
    tags = [x for x in (t.get("hashtags") or []) if x]
    desc = with_credit(r.get("description") or t.get("description") or default_description(meta, tags), meta)
    return title, desc, tags + [streamer_of(meta)["name"], "Shorts"]


def yt_worker() -> None:
    while True:
        job = _upq.get()
        key, st = job["key"], UPLOADS.get(job["key"])
        if st is None or st.get("status") != "queued":  # cleared before it started
            continue
        try:
            d = clipper.find_session(job["sid"])
            c = d / "clips" / job["name"] if d else None
            video = next((c / f for f in ("short.mp4", "captioned.mp4") if c and (c / f).exists()), None)
            if not video:
                raise youtube.YouTubeError("The clip's video is gone (deleted or re-made). Nothing was uploaded.")
            title, desc, tags = _clip_title_desc(d, c, job.get("title"))
            st.update(status="uploading", total=video.stat().st_size, done=0)
            res = youtube.upload(yt_cfg(), HOME, video, title, desc, tags, job.get("publish_at"),
                                 lambda done, total: st.update(done=done, total=total))
            review.put(c, "posted", title=title, url=res["url"], video_id=res["id"], privacy=res["privacy"],
                       publish_at=res["publish_at"])
            st.update(status="done", url=res["url"], publish_at=res["publish_at"])
            queue_done(key, True, url=res["url"], title=title)
        except Exception as e:  # keep the worker alive whatever happens
            st.update(status="error", error=str(e) if isinstance(e, youtube.YouTubeError) else f"{type(e).__name__}: {e}")
            queue_done(key, False, error=st["error"])


def _clip_dir(sid: str, name: str) -> Path | None:
    d = clipper.find_session(sid)
    c = d / "clips" / name if d and clipper.CLIP_RE.match(name) else None
    return c if c and any((c / f).exists() for f in ("short.mp4", "captioned.mp4")) else None


def _start_upload(key: str, sid: str, name: str, title: str | None) -> None:
    UPLOADS[key] = {"status": "queued", "done": 0, "total": 0, "error": None, "url": None, "publish_at": None}
    _upq.put({"key": key, "sid": sid, "name": name, "title": title, "publish_at": None})


def yt_post(sid: str, name: str, title: str | None) -> tuple[int, dict]:
    """Post now (also used for a scheduled clip: it leaves the schedule)."""
    c = _clip_dir(sid, name)
    if not c:
        return 404, {"error": "no such clip (or it has no video yet)"}
    key = f"{sid}/{name}"
    if UPLOADS.get(key, {}).get("status") in ("queued", "uploading"):
        return 400, {"error": "already uploading"}
    if review.get(c).get("video_id"):
        return 400, {"error": "already on YouTube — open it from the clip's link"}
    if not (youtube.load_token(HOME) or {}).get("refresh_token"):
        return 400, {"error": "Connect YouTube first in Settings → YouTube."}
    with QUEUE_LOCK:
        q = load_queue()
        it = next((i for i in q["items"] if i["key"] == key), None)
        if it:
            it["status"] = "uploading"
            title = title or it.get("title")
            save_queue(q)
        _start_upload(key, sid, name, title)
    return 200, {"result": "Uploading…"}


# ---------------------------------------------------------------- posting schedule (a list of clips, each with a time)

QUEUE_LOCK = threading.RLock()


def queue_path() -> Path:
    return HOME / "youtube-queue.json"


def load_queue() -> dict:
    try:
        q = json.loads(queue_path().read_text())
    except Exception:
        q = {}
    q.setdefault("paused", False)
    q.setdefault("items", [])
    q.setdefault("history", [])
    return q


def save_queue(q: dict) -> None:
    q["items"].sort(key=lambda i: i["at"])
    tmp = queue_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(q, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(queue_path())


def queue_slot(q: dict, skip: str | None = None, now=None, sched: list | None = None) -> str:
    """Next free time, around the clips already scheduled and today's posts."""
    sched = scheduled_clips() if sched is None else sched
    taken = [x["publish_at"] for x in sched] + [i["at"] for i in q["items"] if i["key"] != skip and i["status"] != "error"]
    return youtube.next_slot(yt_cfg(), taken, now, posted=[h["posted_at"] for h in q["history"]])


def queue_add(sid: str, name: str, title: str | None) -> tuple[int, dict]:
    c = _clip_dir(sid, name)
    if not c:
        return 404, {"error": "no such clip (or it has no video yet)"}
    key = f"{sid}/{name}"
    if review.get(c).get("video_id"):
        return 400, {"error": "already on YouTube"}
    with QUEUE_LOCK:
        q = load_queue()
        if any(i["key"] == key for i in q["items"]):
            return 400, {"error": "already scheduled"}
        try:
            at = queue_slot(q)
        except youtube.YouTubeError as e:
            return 400, {"error": str(e)}
        q["items"].append({"key": key, "sid": sid, "name": name, "title": (title or "").strip()[:100] or None,
                           "at": at, "status": "waiting", "error": None, "added": int(time.time())})
        save_queue(q)
    r = review.get(c)
    if r["state"] in ("new", "skipped") or (title and title != r.get("title")):
        review.put(c, "kept" if r["state"] in ("new", "skipped") else r["state"], title=title or None)
    return 200, {"result": f"Scheduled for {youtube.local_label(yt_cfg(), at)}", "at": at}


def queue_done(key: str, ok: bool, url: str | None = None, title: str | None = None, error: str | None = None) -> None:
    with QUEUE_LOCK:
        q = load_queue()
        it = next((i for i in q["items"] if i["key"] == key), None)
        if ok:
            if it:
                q["items"].remove(it)
            q["history"].insert(0, {"key": key, "title": title, "url": url, "scheduled": bool(it),
                                    "posted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
            del q["history"][50:]
        elif it:
            it.update(status="error", error=error)
        save_queue(q)


def queue_action(data: dict) -> tuple[int, dict]:
    act, key = data.get("action"), str(data.get("key") or "")
    with QUEUE_LOCK:
        q = load_queue()
        if act == "pause":
            q["paused"] = bool(data.get("paused"))
            save_queue(q)
            return 200, {"result": "Posting paused" if q["paused"] else "Posting resumed"}
        if act == "reslot":
            try:
                return 200, {"result": reslot(q)}
            except youtube.YouTubeError as e:
                return 400, {"error": str(e)}
        it = next((i for i in q["items"] if i["key"] == key), None)
        if not it:
            return 404, {"error": "that clip isn't in the schedule"}
        if act in ("post-now", "retry"):
            sid, name = key.split("/", 1)
            if it["status"] == "error":
                it.update(status="waiting", error=None)
                save_queue(q)
            return yt_post(sid, name, it.get("title"))
        if it["status"] == "uploading":
            return 400, {"error": "it's uploading right now"}
        if act == "remove":
            q["items"].remove(it)
            save_queue(q)
            return 200, {"result": "Removed from the schedule. The clip stays in Kept."}
        if act == "title":
            it["title"] = str(data.get("title") or "").strip()[:100] or None
            save_queue(q)
            return 200, {"result": "Title saved"}
        if act == "description":
            sid, name = key.split("/", 1)
            c = _clip_dir(sid, name)
            if not c:
                return 404, {"error": "no such clip"}
            text = str(data.get("description") or "").strip()[:4900] or None
            review.put(c, review.get(c)["state"], description=text)
            return 200, {"result": "Description saved" if text else "Description reset"}
        if act == "move":  # swap times with the neighbour, so the order changes but the slots don't
            waiting = [i for i in q["items"] if i["status"] == "waiting"]
            if it not in waiting:
                return 400, {"error": "only waiting clips can move"}
            j = waiting.index(it) + (-1 if int(data.get("dir", 0)) < 0 else 1)
            if not 0 <= j < len(waiting):
                return 200, {"result": "ok"}
            it["at"], waiting[j]["at"] = waiting[j]["at"], it["at"]
            save_queue(q)
            return 200, {"result": "ok"}
        return 400, {"error": f"unknown action {act!r}"}


def reslot(q: dict, now=None) -> str:
    """Give every waiting clip a new time from the current [youtube] settings, keeping their
    order. Clips uploading, failed or already on YouTube (private until publishAt) keep theirs."""
    waiting = sorted((i for i in q["items"] if i["status"] == "waiting"), key=lambda i: i["at"])
    if not waiting:
        return "Nothing to reschedule"
    cfg = yt_cfg()
    taken = [x["publish_at"] for x in scheduled_clips()] + [i["at"] for i in q["items"]
                                                            if i["status"] == "uploading"]
    posted = [h["posted_at"] for h in q["history"]]
    new = []
    for i in waiting:  # all or nothing: a clip that finds no slot leaves the list untouched
        at = youtube.next_slot(cfg, taken, now, posted=posted)
        taken.append(at)
        new.append(at)
    moved = sum(1 for i, at in zip(waiting, new) if i["at"] != at)
    for i, at in zip(waiting, new):
        i["at"] = at
    save_queue(q)
    return (f"Rescheduled {moved} clip{'s' if moved != 1 else ''}; the first now posts "
            f"{youtube.local_label(cfg, new[0])}") if moved else "Times already match the settings"


def yt_tick(now=None) -> None:
    """Post the next clip whose time has come. Slots missed while the machine was off are
    spread out again instead of all going up at once."""
    from datetime import datetime, timezone
    now = now or datetime.now(timezone.utc)
    iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    with QUEUE_LOCK:
        q = load_queue()
        if q["paused"] or not (youtube.load_token(HOME) or {}).get("refresh_token"):
            return
        if any(i["status"] == "uploading" for i in q["items"]) or any(ytb(u) for u in UPLOADS.values()):
            return
        due = [i for i in q["items"] if i["status"] == "waiting" and i["at"] <= iso]
        if not due:
            return
        first = due[0]
        first["status"] = "uploading"
        for i in due[1:]:
            try:
                i["at"] = queue_slot(q, skip=i["key"], now=now)
            except youtube.YouTubeError:
                pass
        save_queue(q)
        _start_upload(first["key"], first["sid"], first["name"], first.get("title"))


def ytb(u: dict) -> bool:
    return u.get("status") in ("queued", "uploading")


def yt_scheduler() -> None:
    with QUEUE_LOCK:  # an upload cut off by a restart goes again
        q = load_queue()
        for i in q["items"]:
            if i["status"] == "uploading":
                i["status"] = "waiting"
        save_queue(q)
    while True:
        try:
            yt_tick()
        except Exception as e:
            print(f"[youtube] scheduler: {e}", flush=True)
        time.sleep(20)


def yt_unschedule(sid: str, name: str) -> tuple[int, dict]:
    d = clipper.find_session(sid)
    c = d / "clips" / name if d else None
    r = review.get(c) if c and c.is_dir() else {}
    if not r.get("video_id"):
        return queue_action({"action": "remove", "key": f"{sid}/{name}"})
    if not r.get("publish_at"):
        return 400, {"error": "that clip isn't scheduled"}
    try:
        youtube.delete_video(yt_cfg(), HOME, r["video_id"])
    except youtube.YouTubeError as e:
        return 400, {"error": str(e)}
    review.put(c, "kept", url=None, video_id=None, publish_at=None, privacy=None)
    UPLOADS.pop(f"{sid}/{name}", None)
    return 200, {"result": "Unscheduled: the video was removed from YouTube and the clip is back in Kept"}


def attention() -> dict:
    """Everything that needs the user, each with the action that fixes it."""
    items: list[dict] = []
    for j in all_jobs():
        st, s_ = j["state"], j["state"].get("status")
        if s_ == "failed" and j["kind"] in ("url", "process"):
            items.append({"level": "bad", "text": f"{j['name']} failed: {st.get('error') or 'the log says why'}",
                          "action": {"label": "Show job", "go": "jobs", "filter": "failed"}})
        elif s_ == "waiting" and st.get("error"):
            e = str(st["error"]).replace("recorder: ", "")
            bot = any(w in e.lower() for w in ("bot check", "not a bot", "sign in", "cookies"))
            items.append({"level": "warn", "text": f"{j['name']}: {e}",
                          "action": {"label": "Upload cookies", "go": "settings", "anchor": "cookies"} if bot
                          else {"label": "Show job", "go": "jobs", "filter": "waiting"}})
    all_s = sessions()
    for x in all_s:
        nf = x["stats"].get("failed") or 0
        if nf and not x["busy"] and x["stats"].get("has_source"):
            items.append({"level": "bad", "text": f"{nf} clip{'s' if nf > 1 else ''} failed in {x['channel']} ({x['id']})",
                          "action": {"label": "Fix clips", "post": f"/api/sessions/{x['id']}/redo",
                                     "done": "Fixing failed clips…"}})
    sysinfo = stats.system_stats(all_s)
    for k in sysinfo["disks"]:
        if k.get("missing"):
            items.append({"level": "bad", "text": f"The {k['label']} drive isn't connected ({k['path']}). Jobs wait for it.",
                          "action": {"label": "Open System", "go": "system"}})
        elif k["label"] == "recordings" and (k["used_pct"] >= 90 or k["free_gb"] < 20):
            items.append({"level": "warn", "text": f"Recordings drive is {k['used_pct']:.0f}% full ({k['free_gb']} GB left, "
                                                   f"about {int(k['free_gb'] / 5)} h of 1080p60)",
                          "action": {"label": "Open sessions", "go": "sessions"}})
    t = sysinfo["tools"]
    cfg = clipper.load_cfg()
    if t.get("ffmpeg", {}).get("path") and not t.get("libass", {}).get("ok"):
        items.append({"level": "warn", "text": "Captions and the streamer credit can't be burned in: this ffmpeg has no libass",
                      "action": {"label": "How to fix", "go": "system"}})
    if cfg["captions"].get("enabled", True) and not (t.get("whisper", {}).get("path") and t.get("model", {}).get("ok")):
        items.append({"level": "warn", "text": "Clips are made without captions: whisper or its model is missing",
                      "action": {"label": "How to fix", "go": "system"}})
    if t.get("yt-dlp", {}).get("path") and not (t.get("yt-dlp-js") or {}).get("runtime"):
        items.append({"level": "warn", "text": "YouTube links may fail (“No video formats found”): yt-dlp has no JS runtime",
                      "action": {"label": "How to fix", "go": "system"}})
    with QUEUE_LOCK:
        q = load_queue()
    failed = {i["key"]: i.get("error") for i in q["items"] if i["status"] == "error"}
    if failed:
        items.append({"level": "bad", "text": f"{len(failed)} scheduled post{'s' if len(failed) > 1 else ''} failed: "
                      f"{next(iter(failed.values()))}", "action": {"label": "Open Scheduled", "go": "sched"}})
    for key, u in list(UPLOADS.items()):
        if u.get("status") == "error" and key not in failed:
            items.append({"level": "bad", "text": f"YouTube upload of {key} failed: {u.get('error')}",
                          "action": {"label": "Open Review", "go": "clips"}})
    if YT.get("error"):
        items.append({"level": "warn", "text": f"YouTube: {YT['error']}", "action": {"label": "Open", "go": "settings", "anchor": "youtube"}})
    for p in write_problems():
        items.append({"level": "bad", "text": f"The dashboard can't write to {p}. New jobs will fail.",
                      "action": {"label": "How to fix", "go": "system"}})
    new = 0
    for x in all_s:
        d = clipper.find_session(x["id"])
        for _, _, c in clipper.clip_folders(d) if d else []:
            if any((c / f).exists() for f in ("short.mp4", "captioned.mp4")) and review.get(c)["state"] == "new":
                new += 1
    items.sort(key=lambda i: i["level"] != "bad")
    return {"items": items, "new_clips": new}


def session_spikes(sid: str) -> list[dict]:
    cfg = clipper.load_cfg()
    c = cfg["clip"]
    out = []
    for m in clipper.session_moments(clipper.find_session(sid), cfg):
        start = max(0.0, m.onset - c["stream_delay_seconds"] - c["lead_seconds"])
        out.append({"onset": clipper.spikes.fmt(m.onset), "clip_start": clipper.spikes.fmt(start),
                    "score": m.score, "messages": m.messages, "top": m.top[:3],
                    "requests": m.requests, "kind": m.kind})
    return out


def delete_session(sid: str, source_only: bool) -> str:
    d = clipper.find_session(sid)
    if sid in busy_sessions():
        return "a job is using this session — stop it first"
    sched = [n for n, why in clipper.locked_clips(d).items() if why == "scheduled"]
    if not source_only and sched:
        return f"{len(sched)} clip(s) in this session are scheduled to post — unschedule them first (or delete only the recording)"
    if source_only:
        freed = 0
        for f in ("stream.ts", "stream.mp4", "stream.mkv"):
            p = d / f
            if p.exists():
                freed += p.stat().st_size
                p.unlink()
        return f"freed {freed / 1e9:.2f} GB (clips kept)"
    shutil.rmtree(d, ignore_errors=True)
    return "deleted"


COOKIE_NAME = "youtube-cookies.txt"


def cookie_status() -> dict:
    s = clipper.load_cfg()["stream"]
    raw = str(s.get("youtube_cookies") or "").strip()
    f = clipper.cookies_file(s)
    if not f:
        return {"set": False, "configured": raw, "missing": bool(raw)}
    yt = clipper.streamlink_cookie_args(s)
    return {"set": True, "path": str(f), "youtube_cookies": len(yt),
            "logged_in": any(a.split("=", 1)[1].split("=")[0] in ("SID", "__Secure-3PSID", "LOGIN_INFO") for a in yt),
            "modified": int(f.stat().st_mtime)}


def save_cookies(text: str) -> tuple[int, dict]:
    """Store an uploaded cookies.txt privately and point the config at it."""
    lines = [l for l in text.splitlines() if l.strip()]
    yt = [l for l in lines if l.split("\t")[0].replace("#HttpOnly_", "").lstrip(".").endswith("youtube.com")
          and len(l.split("\t")) == 7]
    if not yt:
        return 400, {"error": "that doesn't look like a cookies.txt with youtube.com cookies "
                              "(Netscape format: 7 tab-separated columns per line)"}
    dst = HOME / COOKIE_NAME
    tmp = dst.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text if text.endswith("\n") else text + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(dst)
    raw = tomllib.loads(clipper.CONFIG.read_text(encoding="utf-8"))
    raw.setdefault("stream", {}).setdefault("options", {})["youtube_cookies"] = COOKIE_NAME
    write_config(settings_schema.dump_toml(raw))
    clipper.load_cfg()
    st = cookie_status()
    msg = f"saved {st.get('youtube_cookies', 0)} YouTube cookies"
    if not st.get("logged_in"):
        msg += " — but no login cookie (SID / LOGIN_INFO) found; export while logged in to YouTube"
    return 200, {"result": msg, "cookies": st}


def remove_cookies() -> dict:
    (HOME / COOKIE_NAME).unlink(missing_ok=True)
    raw = tomllib.loads(clipper.CONFIG.read_text(encoding="utf-8"))
    raw.setdefault("stream", {}).setdefault("options", {})["youtube_cookies"] = ""
    write_config(settings_schema.dump_toml(raw))
    clipper.load_cfg()
    return {"result": "cookies removed", "cookies": cookie_status()}


def get_settings() -> dict:
    clipper.load_cfg()  # creates config.toml from the example if missing
    text = clipper.CONFIG.read_text(encoding="utf-8")
    raw = tomllib.loads(text)
    return {"schema": settings_schema.SCHEMA, "sections": settings_schema.SECTION_TITLES,
            "groups": settings_schema.GROUPS, "defaults": settings_schema.defaults(),
            "values": settings_schema.flatten(raw), "toml": text, "cookies": cookie_status()}


def settings_preview(values: dict, sid: str = "") -> tuple[int, dict]:
    """What detection settings (not saved yet) would find on a session, next to the saved ones."""
    raw = tomllib.loads(clipper.CONFIG.read_text(encoding="utf-8"))
    errors = settings_schema.apply_values(raw, {k: v for k, v in values.items() if k.startswith(("spikes.", "clip."))})
    if errors:
        return 400, {"error": "; ".join(errors)}
    d = clipper.find_session(sid) if sid else None
    if d is None:  # newest session that has chat
        d = next((x for x in all_session_dirs() if any(x.glob("chat*.tsv"))), None)
    if d is None:
        return 200, {"session": None}
    meta, chat = clipper._session_chat(d)
    rec = meta.get("rec_start_ms", 0)
    new_cfg = {"spikes": raw.get("spikes", {}), "clip": raw.get("clip", {})}
    found = clipper.spikes.find_all(chat, rec, new_cfg["spikes"]) if chat else []
    kept = clipper.session_moments(d, new_cfg) if chat else []
    saved = clipper.session_moments(d, clipper.load_cfg()) if chat else []
    return 200, {"session": d.name, "channel": meta.get("channel"), "moments": len(kept), "found": len(found),
                 "requests": sum(1 for m in kept if m.requests), "saved": len(saved),
                 "clips": len(clipper.clip_folders(d)),
                 "list": [{"at": clipper.spikes.fmt(m.onset), "score": m.score, "requests": m.requests,
                           "kind": m.kind} for m in kept[:12]]}


def write_config(text: str) -> None:
    cur = clipper.CONFIG
    if cur.exists():
        cur.with_suffix(".toml.bak").write_text(cur.read_text(encoding="utf-8"), encoding="utf-8")
    tmp = cur.with_suffix(".toml.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(cur)


def save_settings(values: dict) -> tuple[int, dict]:
    raw = tomllib.loads(clipper.CONFIG.read_text(encoding="utf-8"))
    old_dir = clipper.resolve_dir(raw.get("storage", {}).get("sessions_dir", ""))
    errors = settings_schema.apply_values(raw, values)
    if errors:
        return 400, {"error": "; ".join(errors)}
    st = raw.setdefault("storage", {})
    new_dir = clipper.resolve_dir(st.get("sessions_dir", ""))
    if new_dir != old_dir:
        err = clipper.check_dir(new_dir)
        if err:
            return 400, {"error": f"Recordings folder: {err}"}
        # keep listing sessions left in the old folder (until they're moved)
        scan = [p for p in st.get("also_scan", []) if clipper.resolve_dir(p) != new_dir]
        if old_dir != clipper.DEFAULT_SESSIONS and str(old_dir) not in scan:
            scan.append(str(old_dir))
        st["also_scan"] = scan
    text = settings_schema.dump_toml(raw)
    tomllib.loads(text)  # never write something we can't read back
    write_config(text)
    clipper.load_cfg()
    stats._sys_cache.clear()
    return 200, {"result": "saved — running jobs pick it up at their next stream / clip run"}


# ------------------------------------------------------------------ http

class H(BaseHTTPRequestHandler):
    server_version = "clipperyt"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter
        if self.command != "GET":
            sys.stderr.write(f"[http] {self.address_string()} {fmt % args}\n")

    # -- helpers
    def authed(self) -> bool:
        if not TOKEN:
            return True
        q = parse_qs(urlparse(self.path).query)
        if secrets.compare_digest(q.get("token", [""])[0], TOKEN):
            return True
        c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        return "clipper" in c and secrets.compare_digest(c["clipper"].value, TOKEN)

    def send(self, code: int, body: bytes | str | dict | list, ctype: str = "application/json",
             extra: dict | None = None) -> None:
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith(("text", "application/json")) else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if TOKEN and parse_qs(urlparse(self.path).query).get("token", [""])[0] == TOKEN:
            self.send_header("Set-Cookie", f"clipper={TOKEN}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json_body(self) -> dict:
        n = min(int(self.headers.get("Content-Length") or 0), 5 << 20)  # 5 MB is plenty (cookies.txt)
        try:
            return json.loads(self.rfile.read(n) or b"{}") if n else {}
        except json.JSONDecodeError:
            return {}

    def job_or_404(self, jid: str) -> bool:
        if jid == "cli" or (clipper.JOB_RE.match(jid) and (JOBS / jid / "job.json").exists()):
            return True
        self.send(404, {"error": "no such job"})
        return False

    def session_or_404(self, sid: str) -> bool:
        if SESSION_RE.match(sid) and clipper.find_session(sid):
            return True
        self.send(404, {"error": "no such session"})
        return False

    # -- routes
    def safely(self, fn) -> None:
        """Any exception → JSON 500 with the reason (instead of a dropped connection,
        which browsers only show as net::ERR_EMPTY_RESPONSE)."""
        try:
            fn()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            traceback.print_exc()
            msg = f"{type(e).__name__}: {e}"
            if isinstance(e, PermissionError):
                msg += (f" — the dashboard runs as user '{_user()}', which can't write there. "
                        "Fix: sudo chown -R " + _user() + f" {HOME}")
            try:
                self.send(500, {"error": msg})
            except Exception:
                pass

    def do_GET(self):
        self.safely(self._get)

    def do_HEAD(self):
        self.safely(self._get)

    def do_POST(self):
        self.safely(self._post)

    def _get(self):
        if not self.authed():
            return self.send(401, "Unauthorized — open /?token=YOUR_TOKEN once", "text/plain")
        clipper.load_cfg()  # cheap; picks up [storage] changes made by hand
        path = urlparse(self.path).path
        q = parse_qs(urlparse(self.path).query)
        if path == "/":
            return self.send(200, (ROOT / "dashboard.html").read_bytes(), "text/html")
        if path == "/api/jobs":
            return self.send(200, all_jobs())
        if m := re.fullmatch(r"/api/jobs/([\w-]+)/log", path):
            if not self.job_or_404(m[1]):
                return
            if m[1] == "cli":
                logs = sorted(clipper.LOGS.glob("*.log")) if clipper.LOGS.exists() else []
                return self.send(200, {"log": tail(logs[-1] if logs else None, 200)})
            return self.send(200, {"log": tail(JOBS / m[1] / "job.log", int(q.get("n", ["200"])[0]))})
        if path == "/api/sessions":
            return self.send(200, sessions())
        if path == "/api/parse":  # the "add a channel or link" box previews what you'll get
            return self.send(200, clipper.parse_target(q.get("text", [""])[0]))
        if path == "/api/youtube":
            return self.send(200, yt_status())
        if path == "/api/titles/models":
            try:
                return self.send(200, titles.models())
            except titles.TitleError as e:
                return self.send(502, {"error": str(e)})
        if path == "/api/clips":
            g = lambda k, d="": q.get(k, [d])[0]
            st = g("state")
            return self.send(200, all_clips(g("channel"), g("requested") == "1", g("sort", "stream"),
                                            int(g("offset", "0") or 0), min(200, int(g("limit", "48") or 48)),
                                            st if st in review.STATES else ""))
        if path == "/api/attention":
            return self.send(200, attention())
        if m := re.fullmatch(r"/api/sessions/([\w-]+)", path):
            if self.session_or_404(m[1]):
                self.send(200, session_detail(m[1]))
            return
        if m := re.fullmatch(r"/api/sessions/([\w-]+)/timeline", path):
            if self.session_or_404(m[1]):
                self.send(200, clipper.session_timeline(clipper.find_session(m[1]), clipper.load_cfg()))
            return
        if m := re.fullmatch(r"/api/sessions/([\w-]+)/spikes", path):
            if self.session_or_404(m[1]):
                try:
                    self.send(200, session_spikes(m[1]))
                except Exception as e:
                    self.send(500, {"error": str(e)})
            return
        if path == "/api/settings":
            return self.send(200, get_settings())
        if path == "/api/system":
            return self.send(200, {**stats.system_stats(sessions()), "user": _user(),
                                   "write_problems": write_problems()})
        if path.startswith("/files/"):
            return self.file(unquote(path[len("/files/"):]))
        self.send(404, {"error": "not found"})

    def _post(self):
        if not self.authed():
            return self.send(401, {"error": "unauthorized"})
        clipper.load_cfg()
        path = urlparse(self.path).path
        data = self.json_body()
        if path == "/api/jobs":
            job, err = create_job(data)
            if err:
                return self.send(400, {"error": err})
            result = start_job(job["id"]) if data.get("start", True) else "saved"
            return self.send(200, {"job": job, "result": result})
        if path == "/api/jobs/clear-finished":  # done ones leave by themselves; this clears failed/stopped links
            n = 0
            for j in all_jobs():
                if j["kind"] in ("url", "process") and not j["running"] and j["state"].get("status") in clipper.FINAL:
                    delete_job(j["id"])
                    n += 1
            return self.send(200, {"result": f"cleared {n} job{'s' if n != 1 else ''}"})
        if m := re.fullmatch(r"/api/jobs/([\w-]+)/edit", path):
            jid = m[1]
            if jid == "cli" or not self.job_or_404(jid):
                return self.send(400, {"error": "can't edit a terminal run"}) if jid == "cli" else None
            job = clipper.load_job(jid)
            if job["kind"] != "watch":
                return self.send(400, {"error": "only channel-watching jobs can be edited"})
            chans = {p: str(c).strip().lstrip("@") for p, c in (data.get("channels") or {}).items()
                     if p in clipper.NAMES and str(c or "").strip()}
            if not chans:
                return self.send(400, {"error": "add at least one channel"})
            rf = data.get("record_from")
            job.update(channels=chans, record_from=rf if rf in chans else next(iter(chans)))
            if str(data.get("name") or "").strip():
                job["name"] = str(data["name"]).strip()[:60]
            clipper.save_job(job)
            running = is_running(job_state(jid))
            return self.send(200, {"job": job, "result": "saved — applies from the next stream" if running else "saved"})
        if m := re.fullmatch(r"/api/jobs/([\w-]+)/(start|stop|kill|delete|autostart)", path):
            jid, act = m[1], m[2]
            if not self.job_or_404(jid):
                return
            if jid == "cli":  # terminal run: only stop/kill
                st = clipper.read_state()
                if act not in ("stop", "kill") or not is_running(st):
                    return self.send(400, {"error": "a terminal run can only be stopped"})
                return self.send(200, {"result": signal_group(st["pid"], act == "kill")})
            if act == "start":
                return self.send(200, {"result": start_job(jid)})
            if act in ("stop", "kill"):
                return self.send(200, {"result": stop_job(jid, act == "kill")})
            if act == "autostart":
                job = clipper.load_job(jid)
                job["autostart"] = bool(data.get("on"))
                clipper.save_job(job)
                return self.send(200, {"result": "autostart " + ("on" if job["autostart"] else "off")})
            return self.send(200, {"result": delete_job(jid)})
        if path == "/api/sessions/move-all":
            busy = busy_sessions()
            todo = [d.name for d in all_session_dirs() if needs_move(d) and d.name not in busy]
            for sid in todo:
                mover.enqueue(sid)
            skipped = sum(1 for d in all_session_dirs() if needs_move(d) and d.name in busy)
            return self.send(200, {"result": f"moving {len(todo)} session(s)"
                                             + (f", {skipped} busy — try again later" if skipped else "")})
        if m := re.fullmatch(r"/api/sessions/([\w-]+)/clips/([\w-]+)/titles", path):
            sid, name = m[1], m[2]
            if not self.session_or_404(sid):
                return
            d = clipper.find_session(sid)
            clip = d / "clips" / name
            if not clipper.CLIP_RE.match(name) or not clip.is_dir():
                return self.send(404, {"error": "no such clip"})
            try:  # only on demand — jobs never call an LLM
                return self.send(200, titles.suggest(d, clip, clipper.load_cfg()["titles"]))
            except titles.TitleError as e:
                return self.send(400, {"error": str(e)})
        if m := re.fullmatch(r"/api/sessions/([\w-]+)/clips/([\w-]+)/review", path):
            sid, name = m[1], m[2]
            if not self.session_or_404(sid):
                return
            clip = clipper.find_session(sid) / "clips" / name
            if not clipper.CLIP_RE.match(name) or not clip.is_dir():
                return self.send(404, {"error": "no such clip"})
            try:
                return self.send(200, review.put(clip, str(data.get("state", "")), data.get("title"), data.get("url")))
            except ValueError as e:
                return self.send(400, {"error": str(e)})
        if m := re.fullmatch(r"/api/sessions/([\w-]+)/clips/([\w-]+)/delete", path):
            sid, name = m[1], m[2]
            if not self.session_or_404(sid):
                return
            if sid in busy_sessions():
                return self.send(400, {"error": "a job is using this session — wait for it to finish"})
            if clipper.locked_clips(clipper.find_session(sid)).get(name) == "scheduled":
                return self.send(400, {"error": "This clip is scheduled to post. Unschedule it first."})
            try:
                tid = clipper.delete_clip(clipper.find_session(sid), name)
            except ValueError as e:
                return self.send(404, {"error": str(e)})
            return self.send(200, {"result": f"Deleted {name}", "trash": tid})
        if m := re.fullmatch(r"/api/sessions/([\w-]+)/clips/restore", path):
            if not self.session_or_404(m[1]):
                return
            try:
                name = clipper.restore_clip(clipper.find_session(m[1]), str(data.get("trash") or ""))
            except ValueError as e:
                return self.send(400, {"error": str(e)})
            return self.send(200, {"result": f"Restored {name}", "clip": name})
        if m := re.fullmatch(r"/api/sessions/([\w-]+)/clip-at", path):
            sid = m[1]
            if not self.session_or_404(sid):
                return
            if sid in busy_sessions():
                return self.send(400, {"error": "a job is using this session — try again when it's done"})
            job, err = create_job({"kind": "process", "session": sid, "mode": "manual", "at": data.get("at")})
            if err:
                return self.send(400, {"error": err})
            return self.send(200, {"result": start_job(job["id"]), "job": job})
        if m := re.fullmatch(r"/api/sessions/([\w-]+)/(process|requests|redo|delete-source|delete|move)", path):
            sid, act = m[1], m[2]
            if not self.session_or_404(sid):
                return
            if act == "move":
                if sid in busy_sessions():
                    return self.send(400, {"error": "busy — wait for it to finish"})
                err = clipper.check_dir(clipper.SESSIONS, create=clipper.SESSIONS == clipper.DEFAULT_SESSIONS)
                if err:
                    return self.send(400, {"error": err})
                return self.send(200, {"result": mover.enqueue(sid)})
            if act in ("process", "requests", "redo"):
                if sid in busy_sessions():
                    return self.send(400, {"error": "already being worked on"})
                if act == "redo" and not data.get("clips") and not clipper.failed_clips(clipper.find_session(sid)):
                    return self.send(400, {"error": "no failed clips to fix"})
                job, err = create_job({"kind": "process", "session": sid,
                                       "mode": act if act != "process" else None, "clips": data.get("clips")})
                return self.send(200, {"result": start_job(job["id"]), "job": job})
            return self.send(200, {"result": delete_session(sid, act == "delete-source")})
        if path == "/api/settings":
            code, body = save_settings(data.get("values") or {})
            return self.send(code, body)
        if path == "/api/youtube/connect":
            try:
                return self.send(200, yt_connect())
            except youtube.YouTubeError as e:
                return self.send(400, {"error": str(e)})
        if path == "/api/youtube/queue":
            code, body = queue_action(data)
            return self.send(code, body)
        if path == "/api/youtube/connect/cancel":
            YT.update(pending=None, error=None)
            return self.send(200, {"result": "ok"})
        if path == "/api/youtube/disconnect":
            youtube.forget(HOME)
            YT.update(pending=None, error=None)
            return self.send(200, {"result": "YouTube disconnected", **yt_status()})
        if m := re.fullmatch(r"/api/sessions/([\w-]+)/clips/([\w-]+)/(post|schedule|unschedule|clear-upload)", path):
            sid, name, act = m[1], m[2], m[3]
            if not self.session_or_404(sid):
                return
            if act == "unschedule":
                code, body = yt_unschedule(sid, name)
            elif act == "clear-upload":
                UPLOADS.pop(f"{sid}/{name}", None)
                code, body = 200, {"result": "ok"}
            elif act == "schedule":
                code, body = queue_add(sid, name, data.get("title"))
            else:
                code, body = yt_post(sid, name, data.get("title"))
            return self.send(code, body)
        if path == "/api/settings/preview":
            code, body = settings_preview(data.get("values") or {}, str(data.get("session") or ""))
            return self.send(code, body)
        if path == "/api/cookies":
            code, body = save_cookies(str(data.get("text") or ""))
            return self.send(code, body)
        if path == "/api/cookies/remove":
            return self.send(200, remove_cookies())
        if path == "/api/config":  # raw TOML (Advanced)
            text = data.get("toml", "")
            try:
                tomllib.loads(text)
            except tomllib.TOMLDecodeError as e:
                return self.send(400, {"error": f"invalid TOML: {e}"})
            write_config(text)
            clipper.load_cfg()
            return self.send(200, {"result": "saved"})
        self.send(404, {"error": "not found"})

    def file(self, rel: str) -> None:
        sid, _, rest = rel.partition("/")
        base = clipper.find_session(sid) if SESSION_RE.match(sid) else None
        p = (base / rest).resolve() if base else None
        if not p or not str(p).startswith(str(base.resolve()) + os.sep) or not p.is_file():
            return self.send(404, {"error": "not found"})
        size = p.stat().st_size
        ctype = mimetypes.guess_type(p.name)[0] or ("text/plain" if p.suffix in (".srt", ".md") else
                                                    "application/octet-stream")
        start, end = 0, size - 1
        rng = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if rng and size:  # browsers need Range requests to scrub video
            if rng[1]:
                start, end = int(rng[1]), int(rng[2]) if rng[2] else size - 1
            else:
                start = max(0, size - int(rng[2] or 0))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text") else ""))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if "download" in self.path:
            self.send_header("Content-Disposition", f'attachment; filename="{p.parent.name}-{p.name}"')
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(p, "rb") as f:
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = f.read(min(left, 1 << 20))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass


def _user() -> str:
    try:
        import pwd
        return pwd.getpwuid(os.geteuid()).pw_name
    except Exception:
        return str(os.geteuid())


def write_problems() -> list[str]:
    """Folders the dashboard/jobs must be able to write to but can't (e.g. setup ran with sudo)."""
    out = []
    for d in (HOME, JOBS, clipper.LOGS, clipper.LOCKS, clipper.DEFAULT_SESSIONS, clipper.SESSIONS, clipper.CONFIG):
        if d.exists() and not os.access(d, os.W_OK):
            out.append(str(d))
    return out


def main() -> None:
    host = os.environ.get("CLIPPER_HOST", "0.0.0.0")
    port = int(os.environ.get("CLIPPER_PORT", "8787"))
    clipper.load_cfg()
    for d in (clipper.DEFAULT_SESSIONS, JOBS, clipper.LOGS):
        d.mkdir(parents=True, exist_ok=True)
    mover.cleanup_partial()
    threading.Thread(target=yt_worker, daemon=True).start()
    threading.Thread(target=yt_scheduler, daemon=True).start()

    def purge_loop():  # deleted clips stay restorable for clipper.TRASH_DAYS days
        while True:
            n = sum(clipper.purge_trash(d) for d in all_session_dirs())
            if n:
                print(f"trash: removed {n} clip(s) deleted more than {clipper.TRASH_DAYS} days ago", flush=True)
            time.sleep(3600)
    threading.Thread(target=purge_loop, daemon=True).start()
    bad = write_problems()
    if bad:
        print(f"!! user '{_user()}' can't write to: {', '.join(bad)}\n"
              f"   creating jobs will fail. Fix: sudo chown -R {_user()} {HOME}", flush=True)
    autostart()
    srv = ThreadingHTTPServer((host, port), H)
    srv.daemon_threads = True
    print(f"clipperyt dashboard on http://{host}:{port}  (data: {HOME})"
          + ("  (token required: add ?token=… once)" if TOKEN else "  (no token — keep it on your LAN/Tailscale)"),
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
