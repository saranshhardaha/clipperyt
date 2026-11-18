#!/usr/bin/env python3
"""clipperyt — record a YouTube/Twitch/Kick live stream + its chat, clip where chat spikes.

  python clipper.py watch                 wait for live → record + log chat → clip when it ends (loops)
  python clipper.py run <url>             one stream/video: live → record+clip, finished → download video + chat replay, clip
  python clipper.py process <session>     (re)make clips for a finished session
  python clipper.py spikes  <session>     just list spikes — use this to tune config.toml
  python clipper.py status                is it waiting / recording / clipping?
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
import tomllib
from datetime import datetime
from pathlib import Path

import render
import spikes

ROOT = Path(__file__).resolve().parent
SESSIONS = ROOT / "sessions"
PY = sys.executable
# find venv-installed tools (streamlink, yt-dlp) and bin/whisper-cli without activating anything
os.environ["PATH"] = os.pathsep.join([str(Path(PY).parent), str(ROOT / "bin"), os.environ.get("PATH", "")])
STOP = False  # set on Ctrl-C so `watch` exits after clipping the current stream


def load_cfg() -> dict:
    cfg_path = ROOT / "config.toml"
    if not cfg_path.exists():  # config.toml is gitignored (may hold tokens); start from the example
        shutil.copy(ROOT / "config.example.toml", cfg_path)
    with open(cfg_path, "rb") as f:
        cfg = tomllib.load(f)
    cfg["stream"].update(cfg["stream"].pop("options", {}))  # flatten [stream.options]
    return cfg


STATE = ROOT / "state.json"
STOPFILE = ROOT / ".stop"  # server.py touches this to stop after the current clipping job


def set_state(status: str, **extra) -> None:
    """Shared with server.py / `clipper.py status`: idle | waiting | live | processing."""
    data = {"status": status, "pid": os.getpid(), "since": int(time.time()), **extra}
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(STATE)


def read_state() -> dict:
    try:
        st = json.loads(STATE.read_text())
    except Exception:
        return {"status": "stopped"}
    try:
        os.kill(st["pid"], 0)  # is the watcher still alive?
    except (OSError, KeyError):
        st["status"] = "stopped"
    return st


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


def record_once(cfg: dict) -> Path | None:
    """Wait for the stream, record it with chat, return the session folder."""
    s = cfg["stream"]
    tmp = SESSIONS / "_waiting"
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

        session = SESSIONS / datetime.now().strftime("%Y%m%d-%H%M")
        tmp.rename(session)
        video = session / "stream.ts"
        (session / "meta.json").write_text(json.dumps({
            "platform": rp, "channel": chans[rp], "chat_from": chans,
            "rec_start_ms": rec_start_ms, "started": datetime.now().isoformat(timespec="seconds"),
        }, indent=2))
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
            final = d / ("short.mp4" if rnd.get("vertical", True) else "captioned.mp4")
            render.render(raw, subs if has_subs else None, final, rnd)
            (d / "chat.txt").write_text("\n".join(m.top), encoding="utf-8")
            index.append(f"| {n} | {spikes.fmt(start)} | {m.score} | "
                         f"{' · '.join(m.top[:3]).replace('|', '/')} | {name}/{final.name} |")
        except Exception as e:
            log(f"  ! clip {n} failed: {e}")
            index.append(f"| {n} | {spikes.fmt(start)} | {m.score} | FAILED | {e!s:.60} |")

    index.append("\nCredit the streamer and link the stream in every post.")
    (session / "clips.md").write_text("\n".join(index) + "\n", encoding="utf-8")
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


def cmd_run(url: str) -> None:
    plat = platform_of(url)
    info = probe(url)
    status = info.get("live_status") or ("is_live" if info.get("is_live") else "not_live")
    name = (info.get("uploader_id") or info.get("channel") or info.get("uploader") or "streamer").lstrip("@")
    log(f"{NAMES[plat]} · {name} · “{info.get('title', '')[:70]}” · {status}")
    cfg = load_cfg()
    if status in ("is_live", "is_upcoming"):
        s = cfg["stream"]
        s.update(url=url, record_from=plat, channels={plat: name})
        session = record_once(cfg)  # one stream only, no loop
        if session:
            meta = json.loads((session / "meta.json").read_text())
            meta["source_url"] = url
            (session / "meta.json").write_text(json.dumps(meta, indent=2))
            cmd_process(session)
        set_state("idle")
    else:
        cmd_vod(url, plat, name, info)


def cmd_vod(url: str, plat: str, name: str, info: dict) -> None:
    """A finished stream: download the video + chat replay, then clip."""
    if plat == "kick":
        raise SystemExit("Kick doesn't keep chat for past streams — only live works for Kick")
    session = SESSIONS / f"{datetime.now():%Y%m%d-%H%M}-vod"
    session.mkdir(parents=True, exist_ok=True)
    (session / "meta.json").write_text(json.dumps({
        "platform": plat, "channel": name, "chat_from": {plat: name}, "source_url": url,
        "title": info.get("title"), "rec_start_ms": 0, "vod": True,
        "started": datetime.now().isoformat(timespec="seconds"),
    }, indent=2, ensure_ascii=False))
    set_state("processing", session=session.name, step="downloading chat replay")

    chat_path = session / f"chat_{plat}.tsv"
    if not chat_path.exists() or chat_path.stat().st_size == 0:
        log("downloading chat replay …")
        from chat_downloader import ChatDownloader  # replay messages carry time_in_seconds
        n = 0
        with open(chat_path, "w", encoding="utf-8") as f:
            for m in ChatDownloader().get_chat(url, message_groups=["messages", "superchat"]):
                t = m.get("time_in_seconds")
                if t is None or t < 0:
                    continue
                author = (m.get("author") or {}).get("name", "?")
                text = (m.get("message") or "").replace("\t", " ").replace("\n", " ")
                f.write(f"{int(t * 1000)}\t{author}\t{text}\n")  # ms since video start
                n += 1
                if n % 5000 == 0:
                    log(f"  {n} messages …")
        log(f"chat replay: {n} messages")

    if not any((session / f).exists() for f in ("stream.mp4", "stream.mkv")):
        set_state("processing", session=session.name, step="downloading video")
        log("downloading video (up to 1080p) …")
        r = subprocess.run(["yt-dlp", "--no-warnings", "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
                            "--merge-output-format", "mp4", "-o", str(session / "stream.%(ext)s"), url])
        if r.returncode != 0:
            raise SystemExit("video download failed")
    cmd_process(session)
    set_state("idle")


def cmd_watch() -> None:
    while True:
        cfg = load_cfg()  # re-read each stream, so edits apply next time
        session = record_once(cfg)
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


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return
    if args[0] == "status":
        cmd_status()
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
