#!/usr/bin/env python3
"""clipperyt — record a YouTube/Twitch/Kick live stream + its chat, clip where chat spikes.

  python clipper.py watch                 wait for live → record + log chat → clip when it ends (loops)
  python clipper.py run <url>             one stream/video: live → record+clip, finished → download video + chat replay, clip
  python clipper.py process <session>     (re)make clips for a finished session
  python clipper.py spikes  <session>     just list spikes — use this to tune config.toml
  python clipper.py redo <session> [clip…] re-make clips in place (no clip names = every failed clip)
  python clipper.py requests <session>    add clips for clip requests (≥3 chatters said "clip") not clipped yet
  python clipper.py status                is it waiting / recording / clipping?
  python clipper.py doctor                check ffmpeg/libass, streamlink, yt-dlp, whisper
  python clipper.py job <id>              run one dashboard job (jobs/<id>/job.json) — server.py starts these
  python server.py                        web dashboard (see README)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 (e.g. Ubuntu 22.04): pip install tomli
    import tomli as tomllib

import render
import spikes

ROOT = Path(__file__).resolve().parent              # code, models/, bin/
HOME = Path(os.environ.get("CLIPPER_HOME") or ROOT).resolve()  # data: config, sessions, jobs
DEFAULT_SESSIONS = HOME / "sessions"
SESSIONS = DEFAULT_SESSIONS   # where NEW recordings go; load_cfg() applies [storage] sessions_dir
EXTRA_ROOTS: list[Path] = []  # [storage] also_scan: older folders whose sessions are still listed
JOBS = HOME / "jobs"
LOCKS = HOME / "locks"
LOGS = HOME / "logs"
CONFIG = HOME / "config.toml"
PY = sys.executable
# find venv-installed tools (streamlink, yt-dlp) and bin/whisper-cli without activating anything
# Homebrew's plain ffmpeg (8+) has no libass, so captions can't be burned in; setup.sh installs the
# keg-only ffmpeg-full next to it, which we prefer when present.
_FFMPEG_FULL = [d for d in ("/opt/homebrew/opt/ffmpeg-full/bin", "/usr/local/opt/ffmpeg-full/bin") if os.path.isdir(d)]
# CLIPPER_PATH_PREPEND: dirs searched before everything else (tests put fake streamlink/yt-dlp there)
os.environ["PATH"] = os.pathsep.join([p for p in os.environ.get("CLIPPER_PATH_PREPEND", "").split(os.pathsep) if p]
                                     + [str(Path(PY).parent), str(ROOT / "bin"), *_FFMPEG_FULL,
                                        os.environ.get("PATH", "")])
STOP = False  # set on Ctrl-C so `watch` exits after clipping the current stream


def load_cfg() -> dict:
    if not CONFIG.exists():  # config.toml is gitignored (may hold tokens); start from the example
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / "config.example.toml", CONFIG)
    with open(CONFIG, "rb") as f:
        cfg = tomllib.load(f)
    for sec in ("stream", "spikes", "clip", "captions", "render", "limits", "storage", "titles"):
        cfg.setdefault(sec, {})
    cfg["stream"].update(cfg["stream"].pop("options", {}))  # flatten [stream.options]
    global SESSIONS, EXTRA_ROOTS, YTDLP_AUTH
    YTDLP_AUTH = ytdlp_cookie_args(cfg["stream"])
    os.environ["CLIPPER_YTDLP_AUTH"] = json.dumps(YTDLP_AUTH)  # for chat_youtube.py (a child process)
    SESSIONS = resolve_dir(cfg["storage"].get("sessions_dir", ""))
    EXTRA_ROOTS = [resolve_dir(p) for p in cfg["storage"].get("also_scan", []) if str(p).strip()]
    return cfg


# ---------------------------------------------------------------- YouTube cookies (bot check)

YTDLP_AUTH: list[str] = []
BOT_CHECK = ("confirm you", "not a bot", "Sign in to confirm")
BOT_HELP = ("YouTube is asking for a sign-in (\"confirm you're not a bot\") — it does this to some IPs. "
            "Fix: export cookies.txt from a browser logged in to YouTube (a spare account is best) and "
            "upload it in Settings → Stream defaults → YouTube cookies. See README → \"YouTube asks to sign in\".")


NO_FORMATS_HELP = ("yt-dlp found the video but no downloadable formats — it can't solve YouTube's JavaScript "
                   "challenge. It needs a JS runtime (deno) and yt-dlp[default]. Linux: run ./update.sh "
                   "(installs both). macOS: brew install deno && brew upgrade yt-dlp.")


def cookies_file(s: dict) -> Path | None:
    raw = str(s.get("youtube_cookies") or "").strip()
    if not raw:
        return None
    p = Path(os.path.expanduser(raw))
    p = p if p.is_absolute() else HOME / p
    return p if p.is_file() else None


def ytdlp_cookie_args(s: dict) -> list[str]:
    f = cookies_file(s)
    if f:
        return ["--cookies", str(f)]
    b = str(s.get("cookies_from_browser") or "").strip()
    return ["--cookies-from-browser", b] if b else []


def streamlink_cookie_args(s: dict) -> list[str]:
    """YouTube cookies from the cookies.txt (Netscape format) as --http-cookie name=value."""
    f = cookies_file(s)
    if not f:
        return []
    out = []
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        parts = line.split("\t")
        if line.startswith("#") or len(parts) != 7:
            continue
        domain, name, value = parts[0], parts[5], parts[6]
        if domain.lstrip(".").endswith("youtube.com") and name:
            out.append(f"--http-cookie={name}={value}")
    return out


# ---------------------------------------------------------------- storage (sessions on another drive)

class StorageUnavailable(RuntimeError):
    pass


def wait_for_storage(cfg: dict) -> None:
    """Wait (up to [storage] wait_for_drive_seconds) for the recordings folder — after a reboot a
    NAS/USB drive can mount a little after the dashboard starts. Raises StorageUnavailable after."""
    limit = float(cfg["storage"].get("wait_for_drive_seconds", 300))
    t0, said = time.time(), False
    while True:
        load_cfg()
        err = check_dir(SESSIONS, create=SESSIONS == DEFAULT_SESSIONS)
        if not err:
            if said:
                log(f"recordings folder is available after {time.time() - t0:.0f}s")
            return
        if time.time() - t0 >= limit:
            raise StorageUnavailable(f"recordings folder unavailable: {err}")
        if not said:
            log(f"waiting up to {limit:.0f}s for the recordings folder: {err}")
            said = True
        time.sleep(5)


def resolve_dir(raw: str) -> Path:
    """'' → <home>/sessions; '~' expanded; relative paths are relative to the data folder."""
    raw = str(raw or "").strip()
    if not raw:
        return DEFAULT_SESSIONS
    p = Path(os.path.expanduser(raw))
    return Path(os.path.abspath(p if p.is_absolute() else HOME / p))


def check_dir(p: Path, create: bool = True) -> str | None:
    """None if recordings can be written to `p`, else why not.

    Never create a folder on the wrong disk when a drive isn't mounted: the parent must
    exist, and with create=False (recording time, custom folder) the folder itself must
    exist — an unmounted Linux mount point is an empty dir, so its parent always exists."""
    if p.exists() and not p.is_dir():
        return f"{p} is a file, not a folder"
    if not p.exists() and not p.parent.is_dir():
        return f"{p.parent} doesn't exist — is the drive connected?"
    if not p.exists() and not create:
        return f"{p} doesn't exist — is the drive connected?"
    try:
        p.mkdir(exist_ok=True)
        probe = p / f".write-test-{os.getpid()}"
        probe.write_bytes(b"x")
        probe.unlink()
    except OSError as e:
        return f"can't write to {p}: {e.strerror or e}"
    return None


def session_roots() -> list[Path]:
    """Every folder that may hold sessions — recordings folder first."""
    out: list[Path] = []
    for r in (SESSIONS, DEFAULT_SESSIONS, *EXTRA_ROOTS):
        if r not in out:
            out.append(r)
    return out


def find_session(sid: str) -> Path | None:
    """<root>/<channel>/<sid> (current layout) or <root>/<sid> (sessions from before channel folders)."""
    if not sid or "/" in sid or sid.startswith((".", "_")):
        return None
    for r in session_roots():
        if (r / sid / "meta.json").is_file():
            return r / sid
        for d in r.glob(f"*/{sid}") if r.is_dir() else []:
            if (d / "meta.json").is_file() and not d.parent.name.startswith((".", "_")):
                return d
    return None


def iter_sessions(root: Path):
    """Every session folder under one root: <root>/<channel>/<sid> and legacy flat <root>/<sid>."""
    if not root.is_dir():
        return
    for d in root.iterdir():
        if not d.is_dir() or d.name.startswith((".", "_")):
            continue
        if (d / "meta.json").is_file():
            yield d                      # legacy flat session
        else:
            for s in d.iterdir():        # channel folder
                if s.is_dir() and not s.name.startswith((".", "_")) and (s / "meta.json").is_file():
                    yield s


def channel_dir(root: Path, channel: str) -> Path:
    return root / slug(channel)


# Legacy CLI runs (`watch`, `run`) use these; `job <id>` points them at jobs/<id>/.
STATE = HOME / "state.json"
STOPFILE = HOME / ".stop"  # touched to stop after the current clipping job
FINAL = ("done", "failed", "stopped")


def set_state(status: str, **extra) -> None:
    """Read by server.py / `clipper.py status`:
    starting | waiting | live | queued | processing | idle | done | failed | stopped."""
    data = {"status": status, "pid": os.getpid(), "since": int(time.time()), **extra}
    if "session" not in data:  # keep pointing at the last session (dashboard links to it)
        try:
            last = json.loads(STATE.read_text()).get("session")
            if last:
                data["session"] = last
        except Exception:
            pass
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(STATE)


def update_state(**extra) -> None:
    """Merge fields into our own state without changing status/since."""
    st = read_state()
    if st.get("pid") == os.getpid():
        st.update(extra)
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=2))
        tmp.replace(STATE)


def pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def read_state(path: Path | None = None) -> dict:
    try:
        st = json.loads((path or STATE).read_text())
    except Exception:
        return {"status": "stopped"}
    if st.get("status") not in FINAL and not pid_alive(st.get("pid")):
        st["status"] = "stopped"  # process died (crash, kill -9, reboot)
    return st


# ---------------------------------------------------------------- jobs / sessions / slots

JOB_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:32] or "stream"


def new_session_dir(channel: str, suffix: str = "") -> Path:
    """Reserve a unique, empty <recordings>/<channel>/<YYYYMMDD-HHMM>-<channel><suffix>[-N] folder.
    The id (folder name) is unique across every root and channel folder."""
    parent = channel_dir(SESSIONS, channel)
    parent.mkdir(parents=True, exist_ok=True)
    base = f"{datetime.now():%Y%m%d-%H%M}-{slug(channel)}{suffix}"
    for n in range(1, 1000):
        name = base if n == 1 else f"{base}-{n}"
        if find_session(name):
            continue
        d = parent / name
        try:
            d.mkdir()
            return d
        except FileExistsError:
            continue
    raise RuntimeError("couldn't pick a session folder name")


def job_dir(jid: str) -> Path:
    if not JOB_RE.match(jid or ""):
        raise ValueError(f"bad job id {jid!r}")
    return JOBS / jid


def load_job(jid: str) -> dict:
    return json.loads((job_dir(jid) / "job.json").read_text())


def save_job(job: dict) -> None:
    d = job_dir(job["id"])
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "job.tmp"
    tmp.write_text(json.dumps(job, indent=2, ensure_ascii=False))
    tmp.replace(d / "job.json")


@contextmanager
def processing_slot(n: int):
    """Hold one of `n` clip-processing slots (whisper + ffmpeg are heavy).

    flock'd files in locks/ — the OS frees them if the process dies, so a
    crashed job never leaks a slot. Works on macOS and Linux."""
    import fcntl
    LOCKS.mkdir(parents=True, exist_ok=True)
    n = max(1, int(n or 1))
    files = [open(LOCKS / f"process-{i}.lock", "a+") for i in range(n)]
    held = None
    queued = False
    try:
        while held is None:
            for f in files:
                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    held = f
                    break
                except OSError:
                    continue
            if held is None:
                if not queued:
                    log(f"waiting for a free clipping slot ({n} in use)")
                    queued = True
                    if read_state().get("pid") == os.getpid():
                        update_state(status="queued")
                time.sleep(2)
        yield
    finally:
        for f in files:
            f.close()  # closing releases the lock


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------- recording

NAMES = {"youtube": "YouTube", "twitch": "Twitch", "kick": "Kick"}


def channels(s: dict) -> dict[str, str]:
    """{platform: channel} from config — new [stream.channels] table or old platform/channel keys."""
    chans = s.get("channels") or {s["platform"]: s["channel"]}
    chans = {p: c.lstrip("@") for p, c in chans.items() if c}
    for p in chans:
        if p not in NAMES:
            raise SystemExit(f"unknown platform {p!r} (use youtube, twitch or kick)")
    return chans


def record_platform(s: dict) -> str:
    chans = channels(s)
    p = s.get("record_from") or s.get("platform") or next(iter(chans))
    if p not in chans:
        raise SystemExit(f"record_from = {p!r} but no channel set for it in [stream.channels]")
    return p


YT_CHANNEL_ID = re.compile(r"^UC[\w-]{22}$")


def stream_url(platform: str, ch: str) -> str:
    if platform == "youtube" and YT_CHANNEL_ID.match(ch):  # youtube.com/channel/UC… links
        return f"https://www.youtube.com/channel/{ch}/live"
    return {"youtube": f"https://www.youtube.com/@{ch}/live",
            "twitch": f"https://www.twitch.tv/{ch}",
            "kick": f"https://kick.com/{ch}"}[platform]


def recorder_for(s: dict, platform: str) -> str:
    """auto: YouTube → yt-dlp (handles cookies + YouTube's JS challenge; streamlink gets
    "No playable streams" when YouTube bot-checks), Twitch/Kick → streamlink (ad skipping)."""
    r = s.get("recorder", "auto")
    if r in ("streamlink", "yt-dlp"):
        return r
    return "yt-dlp" if platform == "youtube" else "streamlink"


def ytdlp_format(quality: str) -> str:
    """streamlink-style quality ("best", "1080p60", "720p") → yt-dlp format for one muxed live stream."""
    m = re.match(r"(\d{3,4})p", quality or "")
    cap = f"[height<={m[1]}]" if m else ""
    return f"best{cap}[vcodec!=none][acodec!=none]/best{cap}/best"


def recorder_cmd(s: dict, out: Path) -> list[str]:
    p = record_platform(s)
    url = s.get("url") or stream_url(p, channels(s)[p])
    if recorder_for(s, p) == "yt-dlp":
        # live from "now" (not from the start), muxed HLS straight into one growing .ts file
        return ["yt-dlp", *YTDLP_AUTH, "--wait-for-video", str(s.get("retry_seconds", 30)),
                "--hls-use-mpegts", "--no-part", "--no-progress", "--no-mtime", "--fixup", "never",
                "--newline", "-f", ytdlp_format(s.get("quality", "best")), "-o", str(out), url]
    cmd = ["streamlink", "--retry-streams", str(s.get("retry_seconds", 30)), "--retry-max", "0",
           "--force", "--hls-live-edge", "2", "-o", str(out)]
    if p == "youtube":
        cmd += streamlink_cookie_args(s)
    if p == "twitch":
        # drop Twitch's stitched-in ad segments; timeline.tsv keeps clips in sync across the gap
        if s.get("twitch_disable_ads", True):
            cmd.append("--twitch-disable-ads")
        if s.get("twitch_oauth_token"):  # Turbo / channel sub account → no ads at all
            cmd.append(f"--twitch-api-header=Authorization=OAuth {s['twitch_oauth_token']}")
    return cmd + [url, s.get("quality", "best")]


WAIT_MARK = ".waiting"  # inside a session folder that hasn't gone live yet (no meta.json)


def clear_waiting(wait_name: str) -> None:
    """Remove folders a job left waiting (it was stopped or crashed before the stream went live)."""
    for r in session_roots():
        shutil.rmtree(r / wait_name, ignore_errors=True)  # old layout: <root>/_waiting-<job>
        for m in r.glob(f"*/*/{WAIT_MARK}") if r.is_dir() else []:
            d = m.parent
            try:
                if m.read_text().strip() == wait_name and not (d / "meta.json").exists():
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass


def finalize_session_dir(tmp: Path, channel: str) -> Path:
    """Stream went live: give the folder a name with the go-live time — a rename within the same
    folder, which shares allow. If even that's refused, keep the name it already has."""
    base = f"{datetime.now():%Y%m%d-%H%M}-{slug(channel)}"
    (tmp / WAIT_MARK).unlink(missing_ok=True)
    if tmp.name == base or tmp.name.startswith(base + "-"):
        return tmp
    for n in range(1, 100):
        name = base if n == 1 else f"{base}-{n}"
        dst = tmp.parent / name
        if find_session(name) or os.path.lexists(dst):
            continue
        try:
            os.rename(tmp, dst)
            return dst
        except OSError:
            if not os.path.lexists(tmp) and os.path.isdir(dst):  # NFS: reported error, but it happened
                return dst
            return tmp
    return tmp


def youtube_is_live(url: str) -> bool:
    try:
        r = subprocess.run(["yt-dlp", *YTDLP_AUTH, "-J", "--no-warnings", "--skip-download",
                            "--ignore-no-formats-error", url], capture_output=True, text=True, timeout=90)
        return r.returncode == 0 and json.loads(r.stdout).get("live_status") == "is_live"
    except Exception:
        return False


def recorder_problem(path: Path) -> str | None:
    """Last error-looking line of recorder.log (streamlink / yt-dlp), shortened."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 8000))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        low = line.lower()
        if any(k in low for k in ("error", "no playable streams", "not a bot", "sign in", "unable",
                                  "not currently live", "forbidden", "429")):
            msg = line.strip()
            if any(b in msg for b in BOT_CHECK):
                return "YouTube bot check — upload cookies in Settings → Stream defaults → YouTube cookies"
            return msg[:220]
    return None


def chat_cmd(platform: str, ch: str, s: dict, out: Path) -> list[str]:
    if platform == "youtube":  # a direct video URL (from `run <url>`) skips the /live lookup
        return [PY, str(ROOT / "chat_youtube.py"), s.get("url") or ch, str(out)]
    if platform == "twitch":
        return [PY, str(ROOT / "chat_twitch.py"), ch, str(out)]
    return [PY, str(ROOT / "chat_kick.py"), ch, str(out), str(s.get("kick_chatroom_id", ""))]


def stop(p: subprocess.Popen | None) -> None:
    if p and p.poll() is None:
        p.send_signal(signal.SIGINT)
        try:
            p.wait(15)
        except subprocess.TimeoutExpired:
            p.kill()


def record_once(cfg: dict, wait_name: str = "_waiting", job: str | None = None) -> Path | None:
    """Wait for the stream, record it with chat, return the session folder."""
    s = cfg["stream"]
    err = check_dir(SESSIONS, create=SESSIONS == DEFAULT_SESSIONS)
    if err:
        raise StorageUnavailable(f"recordings folder unavailable: {err}")
    rp = record_platform(s)
    chans = channels(s)
    # wait (and start recording) directly inside <recordings>/<channel>/ under a final-looking
    # name: some NAS shares (ZFS ACLs, NFS, LXC bind mounts) refuse moving a folder to another
    # parent, so the folder must already be where the session lives
    clear_waiting(wait_name)
    tmp = new_session_dir(chans[rp])
    (tmp / WAIT_MARK).write_text(wait_name)
    video = tmp / "stream.ts"
    src = s.get("url") or stream_url(rp, chans[rp])
    log(f"waiting for {src} to go live …")
    set_state("waiting", url=src)
    rec = subprocess.Popen(recorder_cmd(s, video), stdout=open(tmp / "recorder.log", "w"),
                           stderr=subprocess.STDOUT)
    chats: list[subprocess.Popen] = []
    session: Path | None = None
    pass_proc: subprocess.Popen | None = None
    try:
        # recording "starts" when the first bytes hit disk — that's t=0 for the video
        seen, last_check, last_probe = "", 0.0, 0.0
        while rec.poll() is None and not (video.exists() and video.stat().st_size > 0):
            time.sleep(0.5)
            if time.time() - last_check > 20:  # surface the recorder's complaint instead of a silent "waiting"
                last_check = time.time()
                msg = recorder_problem(tmp / "recorder.log")
                if msg and msg != seen:
                    seen = msg
                    log(f"recorder: {msg}")
                    set_state("waiting", url=src, error=f"recorder: {msg}")
                # YouTube blocks streamlink ("No playable streams") while yt-dlp + cookies still work:
                # if yt-dlp says it's live, switch recorders instead of waiting forever
                if (rp == "youtube" and recorder_for(s, rp) == "streamlink" and msg
                        and "no playable streams" in msg.lower() and time.time() - last_probe > 60):
                    last_probe = time.time()
                    if youtube_is_live(src):
                        log("streamlink can't get the stream but yt-dlp sees it live — switching to yt-dlp")
                        stop(rec)
                        video.unlink(missing_ok=True)
                        s = {**s, "recorder": "yt-dlp"}
                        rec = subprocess.Popen(recorder_cmd(s, video), stdout=open(tmp / "recorder.log", "a"),
                                               stderr=subprocess.STDOUT)
        if rec.poll() is not None and not (video.exists() and video.stat().st_size > 0):
            msg = recorder_problem(tmp / "recorder.log")
            log(f"recorder exited before the stream started{': ' + msg if msg else ' — see recorder.log'}")
            return None
        rec_start_ms = int(time.time() * 1000)

        # reserve a unique empty folder, then rename onto it (POSIX allows dir → empty dir);
        # streamlink keeps writing through its open file handle
        session = finalize_session_dir(tmp, chans[rp])
        video = session / "stream.ts"
        meta = {"platform": rp, "channel": chans[rp], "chat_from": chans,
                "rec_start_ms": rec_start_ms, "started": datetime.now().isoformat(timespec="seconds")}
        if s.get("url"):
            meta["source_url"] = s["url"]
        if job:
            meta["job"] = job
        (session / "meta.json").write_text(json.dumps(meta, indent=2))
        log(f"LIVE — recording {NAMES[rp]} to {session.name}/stream.ts, "
            f"logging chat from {', '.join(NAMES[p] for p in chans)}")
        set_state("live", session=session.name, platform=rp, channel=chans[rp],
                  rec_start_ms=rec_start_ms)
        for p, ch in chans.items():  # one logger per platform → chat_<platform>.tsv
            chats.append(subprocess.Popen(chat_cmd(p, ch, s, session / f"chat_{p}.tsv"),
                                          stdout=open(session / f"chat_{p}.log", "w"),
                                          stderr=subprocess.STDOUT))

        # timeline.tsv: <epoch_ms>\t<file bytes>, once a second. Lets us cut by wall-clock time
        # even when the file has gaps (skipped ads, stalls, reconnects).
        tl = open(session / "timeline.tsv", "a", buffering=1)
        last = time.time()
        # long streams: clip what's recorded so far every process_every_minutes, in a separate
        # process (own session, so Stop doesn't interrupt it) while recording continues
        every = float(cfg["clip"].get("process_every_minutes", 60) or 0) * 60
        next_pass = time.time() + every if every > 0 else None
        while rec.poll() is None:
            time.sleep(1)
            tl.write(f"{int(time.time() * 1000)}\t{video.stat().st_size}\n")
            if next_pass and time.time() >= next_pass:
                next_pass += every
                if pass_proc is None or pass_proc.poll() is not None:
                    since = clipped_until(session) or 0.0
                    # leave a margin so a moment still building up isn't cut short
                    until = ((time.time() * 1000 - rec_start_ms) / 1000 - float(cfg["spikes"].get("merge_seconds", 60))
                             - float(cfg["clip"].get("length_seconds", 45)))
                    if until > since:
                        log(f"clipping {spikes.fmt(since)}–{spikes.fmt(until)} in the background, recording continues")
                        pass_proc = subprocess.Popen([PY, "-u", str(ROOT / "clipper.py"), "window", str(session),
                                                      f"{since:.0f}", f"{until:.0f}"],
                                                     stdin=subprocess.DEVNULL, start_new_session=True)
            if time.time() - last > 600:
                n = sum(sum(1 for _ in open(f, encoding="utf-8", errors="ignore"))
                        for f in session.glob("chat*.tsv"))
                log(f"still recording — {video.stat().st_size / 1e9:.2f} GB, {n} chat messages")
                last = time.time()
        tl.close()
        log("stream ended")
        return session
    except KeyboardInterrupt:
        global STOP
        STOP = True
        log("stopping (Ctrl-C) — will clip what was recorded")
        return session
    finally:
        stop(rec)
        for c in chats:
            stop(c)
        if pass_proc and pass_proc.poll() is None:  # let the hourly pass finish before the final one
            log("waiting for the background clipping pass to finish …")
            pass_proc.wait()


# ---------------------------------------------------------------- clipping

def session_moments(session: Path, cfg: dict) -> list[spikes.Moment]:
    meta = json.loads((session / "meta.json").read_text())
    files = sorted(session.glob("chat*.tsv"))  # chat_youtube.tsv + chat_twitch.tsv … merged
    if not files:
        log("no chat logs in this session")
        return []
    chat = sorted((row for f in files for row in spikes.load_chat(str(f))), key=lambda r: r[0])
    moments = spikes.find_all(chat, meta["rec_start_ms"], cfg["spikes"])  # chat spikes + clip requests
    keep = sorted(moments, key=lambda m: m.score, reverse=True)[: cfg["clip"].get("max_clips", 25)]
    return sorted(keep, key=lambda m: m.onset)


def moment_label(m: spikes.Moment) -> str:
    return f"clip requested ×{m.requests}" if m.requests else ""


def cmd_spikes(session: Path) -> None:
    cfg = load_cfg()
    ms = session_moments(session, cfg)
    print(f"{len(ms)} moments")
    for m in ms:
        tag = f"  [clip requested ×{m.requests}]" if m.requests else ""
        print(f"  {spikes.fmt(m.onset)}  score {m.score:5.1f}  {m.messages:4d} msgs{tag}  | {' · '.join(m.top[:3])}")


def cmd_process(session: Path, requests_only: bool = False, after_live: bool = False,
                redo: list[str] | None | bool = False) -> None:
    """(Re)make clips. requests_only: just add clips for clip-request moments that
    aren't clipped yet — existing clips are kept. after_live: if hourly passes already
    clipped part of the stream, only clip the rest."""
    cfg = load_cfg()
    with processing_slot(cfg["limits"].get("max_parallel_processing", 1)):
        done = clipped_until(session) if after_live else None
        if redo is not False:  # list of clip folders, or None = all failed ones
            _redo_clips(session, cfg, redo)
        elif requests_only:
            _process_requests(session, cfg)
        elif done is not None:
            _process_window(session, cfg, done, None)
        else:
            _process(session, cfg)


CLIP_RE = re.compile(r"^(\d+)_(\d\d)-(\d\d)-(\d\d)$")
CREDIT_LINE = "\nCredit the streamer and link the stream in every post."


def clip_start(m: spikes.Moment, c: dict) -> float:
    # chat reacts AFTER the moment, and viewers see the stream late → shift back
    return max(0.0, m.onset - c["stream_delay_seconds"] - c["lead_seconds"])


def existing_clips(session: Path) -> list[tuple[int, float, Path]]:
    """(number, start seconds, folder) for clips/NN_HH-MM-SS that have a finished video."""
    out = []
    for d in (session / "clips").glob("*/") if (session / "clips").exists() else []:
        mm = CLIP_RE.match(d.name)
        if mm and any((d / f).exists() for f in ("short.mp4", "captioned.mp4")):
            h, mi, se = int(mm[2]), int(mm[3]), int(mm[4])
            out.append((int(mm[1]), h * 3600 + mi * 60 + se, d))
    return sorted(out)


def clip_folder_start(name: str) -> float:
    mm = CLIP_RE.match(name)
    if not mm:
        raise ValueError(f"not a clip folder: {name}")
    return int(mm[2]) * 3600 + int(mm[3]) * 60 + int(mm[4])


def failed_clips(session: Path) -> list[str]:
    """Clip folders without a finished video (cut/render failed, or interrupted)."""
    d = session / "clips"
    return sorted(p.name for p in d.glob("*/") if CLIP_RE.match(p.name)
                  and not any((p / f).exists() for f in ("short.mp4", "captioned.mp4"))) if d.exists() else []


def moment_for_clip(session: Path, folder: str, cfg: dict) -> spikes.Moment:
    """The moment a clip folder was made from: its moment.json, else the matching spikes.json
    entry, else one rebuilt from the folder's start time (clips made before moment.json)."""
    d = session / "clips" / folder
    keys = set(spikes.Moment.__dataclass_fields__)
    if (d / "moment.json").exists():
        try:
            return spikes.Moment(**{k: v for k, v in json.loads((d / "moment.json").read_text()).items() if k in keys})
        except Exception:
            pass
    start = clip_folder_start(folder)
    c = cfg["clip"]
    try:
        for e in json.loads((session / "spikes.json").read_text()):
            m = spikes.Moment(**{k: v for k, v in e.items() if k in keys})
            if abs(clip_start(m, c) - start) <= 2:
                return m
    except Exception:
        pass
    onset = start + c["stream_delay_seconds"] + c["lead_seconds"]
    top = []
    if (d / "chat.txt").exists():
        top = (d / "chat.txt").read_text(encoding="utf-8", errors="replace").splitlines()[:5]
    return spikes.Moment(onset=onset, peak=onset, end=onset, score=0, messages=0, top=top)


DELETED = ".deleted_clips"  # clip starts the user deleted — add-only runs don't bring them back


def deleted_starts(session: Path) -> list[float]:
    try:
        return [float(x) for x in json.loads((session / DELETED).read_text())]
    except Exception:
        return []


def clip_folders(session: Path) -> list[tuple[int, float, Path]]:
    """(number, start seconds, folder) for every clips/NN_HH-MM-SS folder, failed ones too."""
    d = session / "clips"
    out = []
    for p in d.iterdir() if d.exists() else []:
        mm = CLIP_RE.match(p.name)
        if mm and p.is_dir():
            out.append((int(mm[1]), clip_folder_start(p.name), p))
    return sorted(out)


def _md_row(n: int, folder: Path) -> str:
    start = spikes.fmt(clip_folder_start(folder.name))
    try:
        m = json.loads((folder / "moment.json").read_text())
    except Exception:
        m = {}
    final = next((f for f in ("short.mp4", "captioned.mp4") if (folder / f).exists()), None)
    score = m.get("score", "")
    if not final:
        why = (folder / "error.txt").read_text(encoding="utf-8").strip() if (folder / "error.txt").exists() else "no video"
        return f"| {n} | {start} | {score} | FAILED | {why[:60].replace('|', '/')} |"
    req = f"**clip requested ×{m['requests']}** · " if m.get("requests") else ""
    said = " · ".join(t for t in m.get("top", [])[:3] if not t.startswith("clip requested")).replace("|", "/")
    return f"| {n} | {start} | {score} | {req}{said} | {folder.name}/{final} |"


def write_clips_md(session: Path, maker: "_ClipMaker | None" = None) -> None:
    """clips.md always reflects the clip folders on disk (after make, redo, delete, add)."""
    maker = maker or _ClipMaker(session, load_cfg())
    rows = [_md_row(n, f) for n, _, f in clip_folders(session)]
    (session / "clips.md").write_text("\n".join(maker.header() + rows) + "\n" + CREDIT_LINE + "\n",
                                      encoding="utf-8")


def delete_clip(session: Path, folder: str) -> None:
    """Remove one clip (folder + clips.md row) and remember it so it isn't re-added."""
    if not CLIP_RE.match(folder) or not (session / "clips" / folder).is_dir():
        raise ValueError("no such clip")
    start = clip_folder_start(folder)
    shutil.rmtree(session / "clips" / folder)
    (session / DELETED).write_text(json.dumps(sorted(set(deleted_starts(session) + [start]))))
    write_clips_md(session)


def _redo_clips(session: Path, cfg: dict, folders: list[str] | None) -> None:
    """Re-cut/caption/render clips in place (None = every failed clip)."""
    folders = failed_clips(session) if folders is None else [f for f in folders if CLIP_RE.match(f)
                                                               and (session / "clips" / f).is_dir()]
    log(f"redoing {len(folders)} clip(s) in {session.name}")
    if read_state().get("pid") == os.getpid():
        set_state("processing", session=session.name, clips=len(folders), done=0)
    if not folders:
        return
    maker = _ClipMaker(session, cfg)
    ok = 0
    for i, f in enumerate(folders, 1):
        update_state(done=i - 1)
        n = int(CLIP_RE.match(f)[1])
        row = maker.make(n, moment_for_clip(session, f, cfg), folder=f)  # follows current settings
        ok += "FAILED" not in row
    write_clips_md(session, maker)
    update_state(done=len(folders))
    log(f"redo done: {ok}/{len(folders)} ok")


class _ClipMaker:
    """Cut + caption + render one moment into clips/NN_HH-MM-SS/ (shared by full and add-only runs)."""

    def __init__(self, session: Path, cfg: dict):
        self.session, self.cfg = session, cfg
        self.meta = json.loads((session / "meta.json").read_text())
        self.video = next((session / f for f in ("stream.ts", "stream.mp4", "stream.mkv")
                           if (session / f).exists()), session / "stream.ts")
        chans = self.meta.get("chat_from") or {self.meta["platform"]: self.meta["channel"]}
        self.chans = chans
        self.plat = " / ".join(NAMES[p] for p in chans)
        self.credit = (f"@{self.meta['channel'].lstrip('@')} · {self.plat}"
                       if cfg["render"].get("credit", True) else None)
        (session / "clips").mkdir(exist_ok=True)

    def header(self) -> list[str]:
        meta = self.meta
        links = [meta["source_url"]] if meta.get("source_url") else [stream_url(p, c) for p, c in self.chans.items()]
        return [f"# Clips — {meta['channel']} ({self.plat}) — {meta['started']}\n",
                "Links: " + " · ".join(links) + "\n",
                "| # | time in stream | score | chat said | file |", "|---|---|---|---|---|"]

    def make(self, n: int, m: spikes.Moment, folder: str | None = None) -> str:
        """Cut + caption + render with the CURRENT settings. Returns the clips.md row.
        folder: overwrite that existing clip (same number); it's renamed if the start moved."""
        c, cap, rnd = self.cfg["clip"], self.cfg["captions"], self.cfg["render"]
        start = clip_start(m, c)
        name = f"{n:02d}_{spikes.fmt(start).replace(':', '-')}"
        d = self.session / "clips" / name
        if folder and folder != name and (self.session / "clips" / folder).is_dir():
            if d.exists():
                shutil.rmtree(d)
            os.rename(self.session / "clips" / folder, d)
            log(f"clip {n}: start moved {spikes.fmt(clip_folder_start(folder))} → {spikes.fmt(start)}")
        d.mkdir(exist_ok=True)
        (d / "error.txt").unlink(missing_ok=True)
        raw = d / "raw.mp4"
        # saved first, so a clip that fails can still be fixed later
        (d / "moment.json").write_text(json.dumps(m.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        log(f"clip {n} at {spikes.fmt(start)} (score {m.score}{', ' + moment_label(m) if m.requests else ''})")
        try:
            timeline = self.session / "timeline.tsv"
            if timeline.exists():  # gap-proof: wall clock → byte offset
                render.cut_wallclock(self.video, timeline, self.meta["rec_start_ms"] / 1000 + start,
                                     c["length_seconds"], raw)
            else:
                render.cut(self.video, start, c["length_seconds"], raw)
            speech = d / "speech.srt"
            have_speech = cap.get("enabled", True) and render.transcribe(raw, speech, cap, ROOT)
            subs = d / "subs.ass"
            has_subs = render.build_subs(speech if have_speech else None, self.credit,
                                         c["length_seconds"], subs, rnd)
            final = d / ("short.mp4" if render.is_vertical(rnd) else "captioned.mp4")
            for old in ("short.mp4", "captioned.mp4"):  # format changed since last run
                (d / old).unlink(missing_ok=True)
            render.render(raw, subs if has_subs else None, final, rnd)
            (d / "chat.txt").write_text("\n".join(m.top), encoding="utf-8")
            return (f"| {n} | {spikes.fmt(start)} | {m.score} | "
                    f"{('**' + moment_label(m) + '** · ' if m.requests else '')}"
                    f"{' · '.join(t for t in m.top[:3] if not t.startswith('clip requested')).replace('|', '/')} "
                    f"| {name}/{final.name} |")
        except Exception as e:
            log(f"  ! clip {n} failed: {e}")
            (d / "error.txt").write_text(str(e).strip().splitlines()[-1] if str(e).strip() else repr(e), encoding="utf-8")
            return f"| {n} | {spikes.fmt(start)} | {m.score} | FAILED | {e!s:.60} |"


def _process(session: Path, cfg: dict) -> None:
    """Make / re-make clips with the CURRENT settings.

    A moment whose clip start is within half a clip length of an existing clip overwrites
    that clip (same number; renamed if the start moved). Other moments become new clips.
    Existing clips that match nothing are kept; clips the user deleted stay deleted."""
    moments = session_moments(session, cfg)
    nreq = sum(1 for m in moments if m.requests)
    log(f"{len(moments)} moments in {session.name}" + (f" ({nreq} with clip requests)" if nreq else ""))
    (session / "spikes.json").write_text(json.dumps([m.to_dict() for m in moments], indent=2,
                                                    ensure_ascii=False))
    c = cfg["clip"]
    half = c["length_seconds"] / 2
    gone = deleted_starts(session)
    have = clip_folders(session)
    free = list(have)
    next_n = max((n for n, _, _ in have), default=0) + 1
    plan: list[tuple[int, spikes.Moment, str | None]] = []
    for m in moments:
        st = clip_start(m, c)
        if any(abs(st - g) < half for g in gone):
            continue
        near = sorted((abs(st - fst), n, f) for n, fst, f in free if abs(st - fst) < half)
        if near:
            _, n, f = near[0]
            free = [x for x in free if x[2] != f]
            plan.append((n, m, f.name))
        else:
            plan.append((next_n, m, None))
            next_n += 1
    if read_state().get("pid") == os.getpid():
        set_state("processing", session=session.name, clips=len(plan))
    if not plan:
        if have:
            write_clips_md(session)
        return
    redo = sum(1 for p in plan if p[2])
    log(f"{redo} clip(s) overwritten (same time), {len(plan) - redo} new, {len(free)} other existing kept")
    maker = _ClipMaker(session, cfg)
    for i, (n, m, folder) in enumerate(plan, 1):
        update_state(done=i - 1)
        maker.make(n, m, folder=folder)
    write_clips_md(session, maker)
    update_state(done=len(plan))
    log(f"done → {session.name}/clips.md")


def _session_chat(session: Path) -> tuple[dict, list]:
    meta = json.loads((session / "meta.json").read_text())
    files = sorted(session.glob("chat*.tsv"))
    chat = sorted((row for f in files for row in spikes.load_chat(str(f))), key=lambda r: r[0])
    return meta, chat


def _add_clips(session: Path, cfg: dict, moments: list[spikes.Moment], what: str) -> int:
    """Clip the moments that don't have a clip yet; keep existing clips; append to clips.md."""
    have = clip_folders(session)  # failed ones count too — Fix handles those
    half = cfg["clip"]["length_seconds"] / 2
    gone = deleted_starts(session)
    new = [m for m in moments if not any(abs(clip_start(m, cfg["clip"]) - st) < half
                                         for st in [st for _, st, _ in have] + gone)]
    log(f"{len(moments)} {what} in {session.name}: {len(moments) - len(new)} already clipped, {len(new)} new")
    if read_state().get("pid") == os.getpid():
        set_state("processing", session=session.name, clips=len(new), done=0)
    if not new:
        return 0
    maker = _ClipMaker(session, cfg)
    n0 = max((n for n, _, _ in have), default=0)
    for i, m in enumerate(new, 1):
        update_state(done=i - 1)
        maker.make(n0 + i, m)
    write_clips_md(session, maker)
    try:  # keep spikes.json listing every moment that has a clip
        known = json.loads((session / "spikes.json").read_text()) if (session / "spikes.json").exists() else []
    except Exception:
        known = []
    (session / "spikes.json").write_text(json.dumps(known + [m.to_dict() for m in new], indent=2,
                                                    ensure_ascii=False))
    update_state(done=len(new))
    log(f"added {len(new)} clip(s) → {session.name}/clips.md")
    return len(new)


def _process_requests(session: Path, cfg: dict) -> None:
    """Add clips for clip-request moments that don't have a clip yet (existing clips are kept)."""
    meta, chat = _session_chat(session)
    sp = {**cfg["spikes"], "clip_requests": True}  # the button means "do it", even if auto is off
    moments = [m for m in spikes.find_all(chat, meta["rec_start_ms"], sp) if m.requests]
    _add_clips(session, cfg, moments, "clip-request moments")


CLIPPED_UNTIL = ".clipped_until"  # seconds into the recording already clipped by live (hourly) passes


def _process_window(session: Path, cfg: dict, since: float, until: float | None) -> None:
    """Clip moments with since <= onset < until (None = to the end). max_clips applies per window,
    so a 24-hour stream clipped hourly isn't capped at max_clips in total."""
    meta, chat = _session_chat(session)
    if not chat:
        log("no chat logs in this session")
    allm = spikes.find_all(chat, meta["rec_start_ms"], cfg["spikes"]) if chat else []
    win = [m for m in allm if m.onset >= since and (until is None or m.onset < until)]
    keep = sorted(win, key=lambda m: m.score, reverse=True)[: cfg["clip"].get("max_clips", 25)]
    span = f"{spikes.fmt(since)}–{spikes.fmt(until) if until is not None else 'end'}"
    _add_clips(session, cfg, sorted(keep, key=lambda m: m.onset), f"moments in {span}")
    if until is not None:
        (session / CLIPPED_UNTIL).write_text(f"{until:.0f}")


def clipped_until(session: Path) -> float | None:
    try:
        return float((session / CLIPPED_UNTIL).read_text().strip())
    except (OSError, ValueError):
        return None


def cmd_window(session: Path, since: float, until: float | None) -> None:
    cfg = load_cfg()
    with processing_slot(cfg["limits"].get("max_parallel_processing", 1)):
        _process_window(session, cfg, since, until)


def cmd_status() -> None:
    st = read_state()
    print(f"status: {st['status']}")
    if st["status"] == "live":
        sess = find_session(st["session"]) or SESSIONS / st["session"]
        size = (sess / "stream.ts").stat().st_size / 1e9 if (sess / "stream.ts").exists() else 0
        mins = (time.time() * 1000 - st["rec_start_ms"]) / 60000
        print(f"  recording {st['channel']} on {NAMES[st['platform']]} for {mins:.0f} min, {size:.2f} GB")
        for f in sorted(sess.glob("chat*.tsv")):
            print(f"  {f.name}: {sum(1 for _ in open(f, encoding='utf-8', errors='ignore'))} messages")
    elif st["status"] in ("waiting", "processing"):
        print(f"  {st.get('url') or st.get('session')} (since {datetime.fromtimestamp(st['since']):%H:%M})")
    done = sorted(f for r in session_roots() if r.exists() for f in r.glob("*/clips.md"))
    if done:
        print(f"latest clips: {done[-1]}")


# ---------------------------------------------------------------- one URL (live or past)

TWITCH_RESERVED = {"directory", "settings", "downloads", "jobs", "p", "search", "subscriptions", "inventory",
                   "wallet", "drops", "friends", "messages", "turbo", "prime", "store", "videos", "user"}


def parse_target(text: str) -> dict:
    """What the user pasted into "Add a channel or link".

    → {"kind": "watch", "platform", "channel", "label"}     a channel to keep watching
    → {"kind": "url", "platform", "url", "label"}           one stream or past video
    → {"kind": "ask", "name", "label"}                      a bare name: which platform?
    → {"kind": "error", "label"}                            not something we can clip
    """
    t = (text or "").strip()
    if not t:
        return {"kind": "error", "label": ""}
    if not re.match(r"^[a-z]+://", t, re.I) and re.match(r"^(www\.|m\.)?(youtube\.com|youtu\.be|twitch\.tv|kick\.com)/", t, re.I):
        t = "https://" + t
    m = re.match(r"^https?://([^/?#]+)(/[^?#]*)?(\?[^#]*)?", t, re.I)
    if not m:
        name = t.lstrip("@")
        if re.fullmatch(r"[\w.-]{2,60}", name):
            return {"kind": "ask", "name": name, "label": f"Which platform is “{name}” on?",
                    "guess": "youtube" if t.startswith("@") else None}
        return {"kind": "error", "label": "Paste a YouTube, Twitch or Kick link, or a channel name."}
    host = re.sub(r"^(www|m|music)\.", "", m[1].lower())
    parts = [p for p in (m[2] or "").split("/") if p]
    query = dict(kv.split("=", 1) for kv in (m[3] or "?")[1:].split("&") if "=" in kv)
    if host in ("youtube.com", "youtu.be"):
        vid = None
        if host == "youtu.be" and parts:
            vid = parts[0]
        elif parts[:1] == ["watch"] and query.get("v"):
            vid = query["v"]
        elif parts[:1] in (["live"], ["shorts"], ["embed"]) and len(parts) > 1:
            vid = parts[1]
        if vid and re.fullmatch(r"[\w-]{11}", vid):
            return {"kind": "url", "platform": "youtube", "url": f"https://www.youtube.com/watch?v={vid}",
                    "label": f"Clip one YouTube video or stream ({vid})"}
        if parts and parts[0].startswith("@"):
            h = parts[0][1:]
            return {"kind": "watch", "platform": "youtube", "channel": h, "label": f"Watch YouTube channel @{h}"}
        if parts[:1] == ["channel"] and len(parts) > 1 and YT_CHANNEL_ID.match(parts[1]):
            return {"kind": "watch", "platform": "youtube", "channel": parts[1],
                    "label": f"Watch YouTube channel {parts[1]}"}
        if parts[:1] in (["c"], ["user"]) and len(parts) > 1:
            return {"kind": "watch", "platform": "youtube", "channel": parts[1],
                    "label": f"Watch YouTube channel @{parts[1]} (old-style link, so check the @handle is right)"}
        return {"kind": "error", "label": "That YouTube link isn't a channel or a video."}
    if host in ("twitch.tv", "clips.twitch.tv"):
        if host == "clips.twitch.tv" or "clip" in parts[1:2]:
            return {"kind": "error", "label": "Twitch clips are already clips. Paste the channel or a past broadcast (twitch.tv/videos/…)."}
        if parts[:1] == ["videos"] and len(parts) > 1 and parts[1].isdigit():
            return {"kind": "url", "platform": "twitch", "url": f"https://www.twitch.tv/videos/{parts[1]}",
                    "label": f"Clip one past Twitch broadcast ({parts[1]})"}
        if parts and parts[0].lower() not in TWITCH_RESERVED and re.fullmatch(r"\w{2,30}", parts[0]):
            if len(parts) > 1 and parts[1] == "videos":
                return {"kind": "watch", "platform": "twitch", "channel": parts[0].lower(),
                        "label": f"Watch Twitch channel {parts[0].lower()}"}
            return {"kind": "watch", "platform": "twitch", "channel": parts[0].lower(),
                    "label": f"Watch Twitch channel {parts[0].lower()}"}
        return {"kind": "error", "label": "That Twitch link isn't a channel or a past broadcast."}
    if host == "kick.com":
        if parts[:1] == ["video"] or (len(parts) > 1 and parts[1] == "videos"):
            return {"kind": "error", "label": "Past Kick videos can't be clipped: Kick doesn't keep their chat. Watch the channel instead."}
        if parts and re.fullmatch(r"[\w-]{2,40}", parts[0]):
            return {"kind": "watch", "platform": "kick", "channel": parts[0].lower(),
                    "label": f"Watch Kick channel {parts[0].lower()}"}
        return {"kind": "error", "label": "That Kick link isn't a channel."}
    return {"kind": "error", "label": "Only YouTube, Twitch and Kick links work."}


def platform_of(url: str) -> str:
    host = re.sub(r"^www\.|^m\.", "", re.sub(r"^https?://", "", url).split("/")[0])
    for p, hosts in {"youtube": ("youtube.com", "youtu.be"), "twitch": ("twitch.tv",),
                     "kick": ("kick.com",)}.items():
        if host in hosts:
            return p
    raise SystemExit(f"don't know which platform {url} is")


def probe(url: str) -> dict:
    """yt-dlp metadata: live_status (is_live / is_upcoming / was_live / not_live), channel…"""
    # --ignore-no-formats-error: we only need live_status/channel here; a live stream is recorded
    # by streamlink, so yt-dlp failing to list formats (e.g. no JS runtime) mustn't stop it
    r = subprocess.run(["yt-dlp", *YTDLP_AUTH, "-J", "--no-warnings", "--skip-download",
                        "--ignore-no-formats-error", url], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        if any(b in r.stderr for b in BOT_CHECK):
            raise SystemExit(BOT_HELP)
        if "No video formats found" in r.stderr:
            raise SystemExit(NO_FORMATS_HELP)
        raise SystemExit(f"yt-dlp couldn't read {url}:\n{r.stderr.strip()[-500:]}")
    return json.loads(r.stdout)


def cmd_run(url: str, wait_name: str = "_waiting", job: str | None = None) -> None:
    plat = platform_of(url)
    wait_for_storage(load_cfg())  # also loads cookies for yt-dlp
    info = probe(url)
    status = info.get("live_status") or ("is_live" if info.get("is_live") else "not_live")
    name = (info.get("uploader_id") or info.get("channel") or info.get("uploader") or "streamer").lstrip("@")
    log(f"{NAMES[plat]} · {name} · “{info.get('title', '')[:70]}” · {status}")
    cfg = load_cfg()
    if status in ("is_live", "is_upcoming"):
        s = cfg["stream"]
        s.update(url=url, record_from=plat, channels={plat: name})
        session = record_once(cfg, wait_name, job)  # one stream only, no loop
        if session:
            cmd_process(session, after_live=True)
        set_state("idle")
    else:
        cmd_vod(url, plat, name, info, job)


def youtube_replay(url: str, session: Path, out: Path) -> int:
    """Chat replay via yt-dlp (maintained) → offset-ms TSV. Returns message count."""
    import chat_youtube
    base = session / "chatreplay"
    subprocess.run(["yt-dlp", *YTDLP_AUTH, "--no-warnings", "--skip-download", "--write-subs",
                    "--sub-langs", "live_chat", "-o", str(base), url])
    src = next(session.glob("chatreplay*.live_chat.json"), None)
    if not src:
        return 0
    n = 0
    with open(src, encoding="utf-8") as fin, open(out, "w", encoding="utf-8") as fout:
        for line in fin:
            try:
                rep = json.loads(line).get("replayChatItemAction") or {}
            except json.JSONDecodeError:
                continue
            off = int(rep.get("videoOffsetTimeMsec") or -1)
            if off < 0:
                continue
            for _, author, text in chat_youtube.actions_to_rows(rep.get("actions", [])):
                fout.write(f"{off}\t{author}\t{text}\n")  # ms since video start
                n += 1
    return n


def twitch_replay(url: str, out: Path) -> int:
    """Best effort: Twitch VOD chat via chat-downloader (may break when Twitch changes)."""
    from chat_downloader import ChatDownloader
    n = 0
    with open(out, "w", encoding="utf-8") as f:
        for m in ChatDownloader().get_chat(url, message_groups=["messages"]):
            t = m.get("time_in_seconds")
            if t is None or t < 0:
                continue
            author = (m.get("author") or {}).get("name", "?")
            text = (m.get("message") or "").replace("\t", " ").replace("\n", " ")
            f.write(f"{int(t * 1000)}\t{author}\t{text}\n")
            n += 1
    return n


def cmd_vod(url: str, plat: str, name: str, info: dict, job: str | None = None) -> None:
    """A finished stream: download the video + chat replay, then clip."""
    if plat == "kick":
        raise SystemExit("Kick doesn't keep chat for past streams — only live works for Kick")
    err = check_dir(SESSIONS, create=SESSIONS == DEFAULT_SESSIONS)
    if err:
        raise StorageUnavailable(f"recordings folder unavailable: {err}")
    session = new_session_dir(name, "-vod")
    meta = {"platform": plat, "channel": name, "chat_from": {plat: name}, "source_url": url,
            "title": info.get("title"), "rec_start_ms": 0, "vod": True,
            "duration": info.get("duration"),
            "started": datetime.now().isoformat(timespec="seconds")}
    if job:
        meta["job"] = job
    (session / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))
    set_state("downloading", session=session.name, step="chat replay")

    chat_path = session / f"chat_{plat}.tsv"
    if not chat_path.exists() or chat_path.stat().st_size == 0:
        log("downloading chat replay …")
        n = youtube_replay(url, session, chat_path) if plat == "youtube" else twitch_replay(url, chat_path)
        log(f"chat replay: {n} messages")
        if n == 0:
            raise SystemExit("no chat replay found (chat disabled, or replay not processed yet — "
                             "YouTube can take a while after a stream ends)")

    if not any((session / f).exists() for f in ("stream.mp4", "stream.mkv")):
        set_state("downloading", session=session.name, step="video")
        log("downloading video (up to 1080p) …")
        r = subprocess.run(["yt-dlp", *YTDLP_AUTH, "--no-warnings", "--no-progress", "-f",
                            "bv*[height<=1080]+ba/b[height<=1080]/b", "--merge-output-format", "mp4",
                            "-o", str(session / "stream.%(ext)s"), url], stderr=subprocess.PIPE, text=True)
        if r.returncode != 0:
            err = r.stderr or ""
            if "No video formats found" in err or "Requested format is not available" in err:
                raise SystemExit(NO_FORMATS_HELP)
            if any(b in err for b in BOT_CHECK):
                raise SystemExit(BOT_HELP)
            raise SystemExit("video download failed: " + (err.strip().splitlines() or ["?"])[-1][:300])
    cmd_process(session)
    set_state("idle")


def cmd_watch(job: dict | None = None) -> None:
    while True:
        cfg = load_cfg()  # re-read each stream, so edits apply next time
        if job:  # a dashboard job brings its own channels; everything else is global config
            cfg["stream"]["channels"] = job["channels"]
            cfg["stream"]["record_from"] = job.get("record_from") or next(iter(job["channels"]))
        try:
            session = record_once(cfg, f"_waiting-{job['id']}" if job else "_waiting",
                                  job["id"] if job else None)
        except StorageUnavailable as e:  # e.g. external drive unplugged — wait for it to come back
            log(f"{e} — retrying in {cfg['stream'].get('retry_seconds', 30)}s")
            set_state("waiting", error=str(e))
            for _ in range(int(cfg["stream"].get("retry_seconds", 30))):
                if STOP or STOPFILE.exists():
                    break
                time.sleep(1)
            session = None
        if session:
            try:
                cmd_process(session, after_live=True)
            except Exception as e:
                log(f"processing failed: {e}")
        if STOP or STOPFILE.exists():
            STOPFILE.unlink(missing_ok=True)
            set_state("idle")
            return
        # recorder gave up (e.g. yt-dlp: "channel is not currently live") → don't hammer the site
        pause = 10 if session else max(10, int(cfg["stream"].get("retry_seconds", 30)))
        for _ in range(pause):
            if STOP or STOPFILE.exists():
                break
            time.sleep(1)


def cmd_doctor() -> None:
    """Print which tools are installed — setup scripts run this at the end."""
    import stats
    t = stats.tools()
    ok = True
    rows = [("ffmpeg", bool(t["ffmpeg"]["path"]), t["ffmpeg"]["version"] or "missing", True),
            ("ffmpeg libass", t["libass"]["ok"], "captions + credit burn-in", True),
            ("streamlink", bool(t["streamlink"]["path"]), t["streamlink"]["version"] or "missing", True),
            ("yt-dlp", bool(t["yt-dlp"]["path"]), t["yt-dlp"]["version"] or "missing", True),
            ("yt-dlp JS", bool(t["yt-dlp-js"]["runtime"]), t["yt-dlp-js"]["runtime"] or
             "no JS runtime — YouTube downloads fail (No video formats found). Linux: ./update.sh · Mac: brew install deno", False),
            ("whisper.cpp", bool(t["whisper"]["path"]), t["whisper"]["path"] or "missing", False),
            ("whisper model", t["model"]["ok"], t["model"]["path"], False)]
    for name, good, detail, required in rows:
        print(f"  {'ok ' if good else ('!! ' if required else '-- ')} {name:14s} {detail}")
        ok = ok and (good or not required)
    cfg = load_cfg()
    yc = str(cfg["stream"].get("youtube_cookies") or "").strip()
    if yc and not cookies_file(cfg["stream"]):
        print(f"  !!  cookies        youtube_cookies = {yc!r} not found")
    elif yc:
        print(f"  ok  cookies        YouTube cookies file set")
    for d in (HOME, JOBS, LOGS, LOCKS, DEFAULT_SESSIONS, CONFIG):
        if d.exists() and not os.access(d, os.W_OK):
            print(f"  !!  permissions    can't write {d} — run: sudo chown -R $(id -un) {HOME}")
            ok = False
    print(f"  ok  encoder        {t['encoder']}")
    print(f"  ok  python         {sys.version.split()[0]}  ·  data in {HOME}")
    if not ok:
        raise SystemExit("some required tools are missing (see !! above)")


def graceful_signals() -> None:
    """SIGINT (dashboard Stop) and SIGTERM (systemd/kill) → KeyboardInterrupt → stop recording, clip, exit.

    SIGINT must be re-enabled explicitly: a dashboard started in the background (`&`, nohup,
    some service managers) passes SIG_IGN down, and Python then never raises KeyboardInterrupt."""
    def stop(*_):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)


def cmd_job(jid: str) -> None:
    """Run one dashboard job with its own state/stop files in jobs/<id>/."""
    global STATE, STOPFILE
    job = load_job(jid)
    load_cfg()  # applies [storage] → SESSIONS
    d = job_dir(jid)
    STATE, STOPFILE = d / "state.json", d / ".stop"
    STOPFILE.unlink(missing_ok=True)
    graceful_signals()
    set_state("starting", kind=job["kind"])
    final, extra = "done", {}
    try:
        if job["kind"] == "watch":
            cmd_watch(job)
            final = "stopped"
        elif job["kind"] == "url":
            cmd_run(job["url"], f"_waiting-{jid}", jid)
        elif job["kind"] == "process":
            session = find_session(job["session"])
            if not session:
                raise SystemExit(f"session {job['session']} not found (drive disconnected?)")
            set_state("processing", session=session.name)
            mode = job.get("mode")
            cmd_process(session, requests_only=mode == "requests",
                        redo=(job.get("clips") or None) if mode == "redo" else False)
        else:
            raise SystemExit(f"unknown job kind {job['kind']!r}")
    except KeyboardInterrupt:
        final = "stopped"
    except SystemExit as e:  # our own "can't do that" messages
        if e.code not in (None, 0):
            final, extra = "failed", {"error": str(e.code)}
            log(f"failed: {e.code}")
    except Exception as e:
        final, extra = "failed", {"error": f"{type(e).__name__}: {e}"}
        log(f"failed: {e!r}")
    set_state(final, kind=job["kind"], **extra)
    clear_waiting(f"_waiting-{jid}")


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return
    if args[0] == "status":
        cmd_status()
        return
    if args[0] == "doctor":
        cmd_doctor()
        return
    if args[0] == "watch":
        STOPFILE.unlink(missing_ok=True)
        graceful_signals()
        try:
            cmd_watch()
        except KeyboardInterrupt:
            pass
        return
    if args[0] == "job" and len(args) == 2:
        cmd_job(args[1])
        return
    if args[0] == "run" and len(args) == 2:
        graceful_signals()
        try:
            cmd_run(args[1])
        except KeyboardInterrupt:
            pass
        return
    if args[0] == "redo" and len(args) >= 2:
        load_cfg()
        session = Path(args[1]) if Path(args[1]).is_dir() else find_session(args[1])
        cmd_process(session, redo=args[2:] or None)
        return
    if args[0] == "window" and len(args) in (3, 4):
        session = Path(args[1])
        cmd_window(session, float(args[2]), float(args[3]) if len(args) == 4 else None)
        return
    if args[0] in ("process", "spikes", "requests") and len(args) == 2:
        session = Path(args[1])
        if not session.is_dir():
            load_cfg()
            session = find_session(args[1]) or SESSIONS / args[1]
        if args[0] == "requests":
            cmd_process(session, requests_only=True)
        else:
            (cmd_process if args[0] == "process" else cmd_spikes)(session)
        return
    print(__doc__)


if __name__ == "__main__":
    main()
