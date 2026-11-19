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
import settings_schema
import stats

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
        if data.get("mode") in ("requests", "redo"):
            job["mode"] = data["mode"]
        if job.get("mode") == "redo":
            job["clips"] = [c for c in (data.get("clips") or []) if clipper.CLIP_RE.match(str(c))]
        label = {"requests": "clip requests", "redo": "fix clips"}.get(job.get("mode"), "re-clip")
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


def all_jobs() -> list[dict]:
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
        })
    try:
        meta = json.loads((d / "meta.json").read_text())
    except Exception:
        meta = {}
    return {"id": sid, "meta": meta, "stats": stats.session_stats(d), "clips": clips,
            "busy": busy_sessions().get(sid), "move": mover.MOVES.get(sid), **where(d),
            "clips_md": f"/files/{sid}/clips.md" if (d / "clips.md").exists() else None,
            "process_log": tail(d / "process.log", 30)}


def all_clips(channel: str = "", requested: bool = False, sort: str = "new",
              offset: int = 0, limit: int = 48) -> dict:
    """Every finished clip across all sessions/folders, for the Clips tab."""
    items, chans = [], set()
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
        clip_dirs = sorted(p for p in (d / "clips").glob("*/") if p.is_dir()) if (d / "clips").exists() else []
        for n, c in enumerate(clip_dirs):
            final = next((c / f for f in ("short.mp4", "captioned.mp4") if (c / f).exists()), None)
            if not final:
                continue
            m = spikes_[n] if n < len(spikes_) else {}
            if (c / "moment.json").exists():
                try:
                    m = json.loads((c / "moment.json").read_text())
                except Exception:
                    pass
            if requested and not m.get("requests"):
                continue
            base = f"/files/{d.name}/clips/{c.name}"
            items.append({"session": d.name, "channel": ch, "platform": meta.get("platform"),
                          "title": meta.get("title"), "name": c.name, "made": int(final.stat().st_mtime),
                          "video": f"{base}/{final.name}", "vertical": final.name == "short.mp4",
                          "raw": f"{base}/raw.mp4" if (c / "raw.mp4").exists() else None,
                          "srt": f"{base}/speech.srt" if (c / "speech.srt").exists() else None,
                          "score": m.get("score"), "requests": m.get("requests", 0),
                          "top": [t for t in m.get("top", []) if not t.startswith("clip requested")][:3]})
    items.sort(key=(lambda x: x["score"] or 0) if sort == "score" else (lambda x: x["made"]), reverse=True)
    return {"total": len(items), "clips": items[offset: offset + limit], "channels": sorted(chans)}


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
            "values": settings_schema.flatten(raw), "toml": text, "cookies": cookie_status()}


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
        if path == "/api/clips":
            g = lambda k, d="": q.get(k, [d])[0]
            return self.send(200, all_clips(g("channel"), g("requested") == "1", g("sort", "new"),
                                            int(g("offset", "0") or 0), min(200, int(g("limit", "48") or 48))))
        if m := re.fullmatch(r"/api/sessions/([\w-]+)", path):
            if self.session_or_404(m[1]):
                self.send(200, session_detail(m[1]))
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
        if path == "/api/jobs/clear-finished":
            n = 0
            for j in all_jobs():
                if j["kind"] in ("url", "process") and not j["running"] and j["state"].get("status") in clipper.FINAL:
                    delete_job(j["id"])
                    n += 1
            return self.send(200, {"result": f"cleared {n}"})
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
        if m := re.fullmatch(r"/api/sessions/([\w-]+)/clips/([\w-]+)/delete", path):
            sid, name = m[1], m[2]
            if not self.session_or_404(sid):
                return
            if sid in busy_sessions():
                return self.send(400, {"error": "a job is using this session — wait for it to finish"})
            try:
                clipper.delete_clip(clipper.find_session(sid), name)
            except ValueError as e:
                return self.send(404, {"error": str(e)})
            return self.send(200, {"result": f"deleted {name}"})
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
