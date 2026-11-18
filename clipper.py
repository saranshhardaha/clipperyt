#!/usr/bin/env python3
"""clipperyt — record a YouTube/Twitch/Kick live stream + its chat, clip where chat spikes.

  python clipper.py watch                 wait for live → record + log chat → clip when it ends (loops)
  python clipper.py run <url>             one stream/video: live → record+clip, finished → download video + chat replay, clip
  python clipper.py process <session>     (re)make clips for a finished session
  python clipper.py spikes  <session>     just list spikes — use this to tune config.toml
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
SESSIONS = HOME / "sessions"
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
    for sec in ("stream", "spikes", "clip", "captions", "render", "limits"):
        cfg.setdefault(sec, {})
    cfg["stream"].update(cfg["stream"].pop("options", {}))  # flatten [stream.options]
    return cfg


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
    """Reserve a unique, empty sessions/<YYYYMMDD-HHMM>-<channel><suffix>[-N] folder."""
    SESSIONS.mkdir(parents=True, exist_ok=True)
    base = f"{datetime.now():%Y%m%d-%H%M}-{slug(channel)}{suffix}"
    for n in range(1, 1000):
        d = SESSIONS / (base if n == 1 else f"{base}-{n}")
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


def stream_url(platform: str, ch: str) -> str:
    return {"youtube": f"https://www.youtube.com/@{ch}/live",
            "twitch": f"https://www.twitch.tv/{ch}",
            "kick": f"https://kick.com/{ch}"}[platform]


def recorder_cmd(s: dict, out: Path) -> list[str]:
    p = record_platform(s)
    url = s.get("url") or stream_url(p, channels(s)[p])
    if s.get("recorder", "streamlink") == "yt-dlp":
        return ["yt-dlp", "--wait-for-video", str(s.get("retry_seconds", 30)),
                "--hls-use-mpegts", "--no-part", "--no-progress",
                "-f", "best" if s.get("quality", "best") == "best" else s["quality"],
                "-o", str(out), url]
    cmd = ["streamlink", "--retry-streams", str(s.get("retry_seconds", 30)), "--retry-max", "0",
           "--force", "--hls-live-edge", "2", "-o", str(out)]
    if p == "twitch":
        # drop Twitch's stitched-in ad segments; timeline.tsv keeps clips in sync across the gap
        if s.get("twitch_disable_ads", True):
            cmd.append("--twitch-disable-ads")
        if s.get("twitch_oauth_token"):  # Turbo / channel sub account → no ads at all
            cmd.append(f"--twitch-api-header=Authorization=OAuth {s['twitch_oauth_token']}")
    return cmd + [url, s.get("quality", "best")]


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
    tmp = SESSIONS / wait_name  # one per job, so parallel jobs never share it
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    video = tmp / "stream.ts"

    rp = record_platform(s)
    chans = channels(s)
    src = s.get("url") or stream_url(rp, chans[rp])
    log(f"waiting for {src} to go live …")
    set_state("waiting", url=src)
    rec = subprocess.Popen(recorder_cmd(s, video), stdout=open(tmp / "recorder.log", "w"),
                           stderr=subprocess.STDOUT)
    chats: list[subprocess.Popen] = []
    session: Path | None = None
    try:
        # recording "starts" when the first bytes hit disk — that's t=0 for the video
        while rec.poll() is None and not (video.exists() and video.stat().st_size > 0):
            time.sleep(0.5)
        if rec.poll() is not None and not video.exists():
            log("recorder exited before the stream started — see recorder.log")
            return None
        rec_start_ms = int(time.time() * 1000)

        # reserve a unique empty folder, then rename onto it (POSIX allows dir → empty dir);
        # streamlink keeps writing through its open file handle
        session = new_session_dir(chans[rp])
        tmp.rename(session)
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
        while rec.poll() is None:
            time.sleep(1)
            tl.write(f"{int(time.time() * 1000)}\t{video.stat().st_size}\n")
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


# ---------------------------------------------------------------- clipping

def session_moments(session: Path, cfg: dict) -> list[spikes.Moment]:
    meta = json.loads((session / "meta.json").read_text())
    files = sorted(session.glob("chat*.tsv"))  # chat_youtube.tsv + chat_twitch.tsv … merged
    if not files:
        log("no chat logs in this session")
        return []
    chat = sorted((row for f in files for row in spikes.load_chat(str(f))), key=lambda r: r[0])
    moments = spikes.find_moments(chat, meta["rec_start_ms"], cfg["spikes"])
    keep = sorted(moments, key=lambda m: m.score, reverse=True)[: cfg["clip"].get("max_clips", 25)]
    return sorted(keep, key=lambda m: m.onset)


def cmd_spikes(session: Path) -> None:
    cfg = load_cfg()
    ms = session_moments(session, cfg)
    print(f"{len(ms)} moments")
    for m in ms:
        print(f"  {spikes.fmt(m.onset)}  score {m.score:5.1f}  {m.messages:4d} msgs  | {' · '.join(m.top[:3])}")


def cmd_process(session: Path) -> None:
    cfg = load_cfg()
    with processing_slot(cfg["limits"].get("max_parallel_processing", 1)):
        _process(session, cfg)


def _process(session: Path, cfg: dict) -> None:
    meta = json.loads((session / "meta.json").read_text())
    video = next((session / f for f in ("stream.ts", "stream.mp4", "stream.mkv")
                  if (session / f).exists()), session / "stream.ts")
    c, cap, rnd = cfg["clip"], cfg["captions"], cfg["render"]
    moments = session_moments(session, cfg)
    log(f"{len(moments)} chat spikes in {session.name}")
    if read_state().get("pid") == os.getpid():
        set_state("processing", session=session.name, clips=len(moments))
    (session / "spikes.json").write_text(json.dumps([m.to_dict() for m in moments], indent=2,
                                                    ensure_ascii=False))
    if not moments:
        return

    chans = meta.get("chat_from") or {meta["platform"]: meta["channel"]}
    plat = " / ".join(NAMES[p] for p in chans)
    credit = f"@{meta['channel'].lstrip('@')} · {plat}" if rnd.get("credit", True) else None
    out_dir = session / "clips"
    out_dir.mkdir(exist_ok=True)
    index = [f"# Clips — {meta['channel']} ({plat}) — {meta['started']}\n",
             "Links: " + " · ".join([meta["source_url"]] if meta.get("source_url") else
                                    [stream_url(p, c) for p, c in chans.items()]) + "\n",
             "| # | time in stream | score | chat said | file |", "|---|---|---|---|---|"]

    for n, m in enumerate(moments, 1):
        # chat reacts AFTER the moment, and viewers see the stream late → shift back
        start = max(0.0, m.onset - c["stream_delay_seconds"] - c["lead_seconds"])
        name = f"{n:02d}_{spikes.fmt(start).replace(':', '-')}"
        d = out_dir / name
        d.mkdir(exist_ok=True)
        raw = d / "raw.mp4"
        log(f"clip {n}/{len(moments)} at {spikes.fmt(start)} (score {m.score})")
        update_state(done=n - 1)
        try:
            timeline = session / "timeline.tsv"
            if timeline.exists():  # gap-proof: wall clock → byte offset
                render.cut_wallclock(video, timeline, meta["rec_start_ms"] / 1000 + start,
                                     c["length_seconds"], raw)
            else:
                render.cut(video, start, c["length_seconds"], raw)
            speech = d / "speech.srt"
            have_speech = cap.get("enabled", True) and render.transcribe(raw, speech, cap, ROOT)
            subs = d / "subs.ass"
            has_subs = render.build_subs(speech if have_speech else None, credit,
                                         c["length_seconds"], subs, rnd)
            final = d / ("short.mp4" if render.is_vertical(rnd) else "captioned.mp4")
            for old in ("short.mp4", "captioned.mp4"):  # format changed since last run
                (d / old).unlink(missing_ok=True)
            render.render(raw, subs if has_subs else None, final, rnd)
            (d / "chat.txt").write_text("\n".join(m.top), encoding="utf-8")
            index.append(f"| {n} | {spikes.fmt(start)} | {m.score} | "
                         f"{' · '.join(m.top[:3]).replace('|', '/')} | {name}/{final.name} |")
        except Exception as e:
            log(f"  ! clip {n} failed: {e}")
            index.append(f"| {n} | {spikes.fmt(start)} | {m.score} | FAILED | {e!s:.60} |")

    index.append("\nCredit the streamer and link the stream in every post.")
    (session / "clips.md").write_text("\n".join(index) + "\n", encoding="utf-8")
    update_state(done=len(moments))
    log(f"done → {session.name}/clips.md")


def cmd_status() -> None:
    st = read_state()
    print(f"status: {st['status']}")
    if st["status"] == "live":
        sess = SESSIONS / st["session"]
        size = (sess / "stream.ts").stat().st_size / 1e9 if (sess / "stream.ts").exists() else 0
        mins = (time.time() * 1000 - st["rec_start_ms"]) / 60000
        print(f"  recording {st['channel']} on {NAMES[st['platform']]} for {mins:.0f} min, {size:.2f} GB")
        for f in sorted(sess.glob("chat*.tsv")):
            print(f"  {f.name}: {sum(1 for _ in open(f, encoding='utf-8', errors='ignore'))} messages")
    elif st["status"] in ("waiting", "processing"):
        print(f"  {st.get('url') or st.get('session')} (since {datetime.fromtimestamp(st['since']):%H:%M})")
    done = sorted(SESSIONS.glob("*/clips.md")) if SESSIONS.exists() else []
    if done:
        print(f"latest clips: {done[-1]}")


# ---------------------------------------------------------------- one URL (live or past)

def platform_of(url: str) -> str:
    host = re.sub(r"^www\.|^m\.", "", re.sub(r"^https?://", "", url).split("/")[0])
    for p, hosts in {"youtube": ("youtube.com", "youtu.be"), "twitch": ("twitch.tv",),
                     "kick": ("kick.com",)}.items():
        if host in hosts:
            return p
    raise SystemExit(f"don't know which platform {url} is")


def probe(url: str) -> dict:
    """yt-dlp metadata: live_status (is_live / is_upcoming / was_live / not_live), channel…"""
    r = subprocess.run(["yt-dlp", "-J", "--no-warnings", "--skip-download", url],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise SystemExit(f"yt-dlp couldn't read {url}:\n{r.stderr.strip()[-500:]}")
    return json.loads(r.stdout)


def cmd_run(url: str, wait_name: str = "_waiting", job: str | None = None) -> None:
    plat = platform_of(url)
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
            cmd_process(session)
        set_state("idle")
    else:
        cmd_vod(url, plat, name, info, job)


def youtube_replay(url: str, session: Path, out: Path) -> int:
    """Chat replay via yt-dlp (maintained) → offset-ms TSV. Returns message count."""
    import chat_youtube
    base = session / "chatreplay"
    subprocess.run(["yt-dlp", "--no-warnings", "--skip-download", "--write-subs",
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
        r = subprocess.run(["yt-dlp", "--no-warnings", "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
                            "--merge-output-format", "mp4", "-o", str(session / "stream.%(ext)s"), url])
        if r.returncode != 0:
            raise SystemExit("video download failed")
    cmd_process(session)
    set_state("idle")


def cmd_watch(job: dict | None = None) -> None:
    while True:
        cfg = load_cfg()  # re-read each stream, so edits apply next time
        if job:  # a dashboard job brings its own channels; everything else is global config
            cfg["stream"]["channels"] = job["channels"]
            cfg["stream"]["record_from"] = job.get("record_from") or next(iter(job["channels"]))
        session = record_once(cfg, f"_waiting-{job['id']}" if job else "_waiting",
                              job["id"] if job else None)
        if session:
            try:
                cmd_process(session)
            except Exception as e:
                log(f"processing failed: {e}")
        if STOP or STOPFILE.exists():
            STOPFILE.unlink(missing_ok=True)
            set_state("idle")
            return
        time.sleep(10)


def cmd_doctor() -> None:
    """Print which tools are installed — setup scripts run this at the end."""
    import stats
    t = stats.tools()
    ok = True
    rows = [("ffmpeg", bool(t["ffmpeg"]["path"]), t["ffmpeg"]["version"] or "missing", True),
            ("ffmpeg libass", t["libass"]["ok"], "captions + credit burn-in", True),
            ("streamlink", bool(t["streamlink"]["path"]), t["streamlink"]["version"] or "missing", True),
            ("yt-dlp", bool(t["yt-dlp"]["path"]), t["yt-dlp"]["version"] or "missing", True),
            ("whisper.cpp", bool(t["whisper"]["path"]), t["whisper"]["path"] or "missing", False),
            ("whisper model", t["model"]["ok"], t["model"]["path"], False)]
    for name, good, detail, required in rows:
        print(f"  {'ok ' if good else ('!! ' if required else '-- ')} {name:14s} {detail}")
        ok = ok and (good or not required)
    print(f"  ok  encoder        {t['encoder']}")
    print(f"  ok  python         {sys.version.split()[0]}  ·  data in {HOME}")
    if not ok:
        raise SystemExit("some required tools are missing (see !! above)")


def cmd_job(jid: str) -> None:
    """Run one dashboard job with its own state/stop files in jobs/<id>/."""
    global STATE, STOPFILE
    job = load_job(jid)
    d = job_dir(jid)
    STATE, STOPFILE = d / "state.json", d / ".stop"
    STOPFILE.unlink(missing_ok=True)
    # systemd/kill send SIGTERM → treat like Ctrl-C (stop recording, clip, exit)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    set_state("starting", kind=job["kind"])
    final, extra = "done", {}
    try:
        if job["kind"] == "watch":
            cmd_watch(job)
            final = "stopped"
        elif job["kind"] == "url":
            cmd_run(job["url"], f"_waiting-{jid}", jid)
        elif job["kind"] == "process":
            session = SESSIONS / job["session"]
            set_state("processing", session=session.name)
            cmd_process(session)
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
    shutil.rmtree(SESSIONS / f"_waiting-{jid}", ignore_errors=True)


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
        # systemd/kill send SIGTERM → treat like Ctrl-C (stop recording, clip, exit)
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        try:
            cmd_watch()
        except KeyboardInterrupt:
            pass
        return
    if args[0] == "job" and len(args) == 2:
        cmd_job(args[1])
        return
    if args[0] == "run" and len(args) == 2:
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        try:
            cmd_run(args[1])
        except KeyboardInterrupt:
            pass
        return
    if args[0] in ("process", "spikes") and len(args) == 2:
        session = Path(args[1])
        session = session if session.is_dir() else SESSIONS / args[1]
        (cmd_process if args[0] == "process" else cmd_spikes)(session)
        return
    print(__doc__)


if __name__ == "__main__":
    main()
