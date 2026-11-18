#!/usr/bin/env python3
"""Offline end-to-end test: 2 live watch jobs in parallel + 1 past-video job, through the dashboard API.

usage: python tests/e2e/run_e2e.py [WORKDIR]     (~3 min; needs ffmpeg)
Nothing touches the network or the real sessions/ folder.
"""
import json, os, re, shutil, signal, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
WORK = Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="clipper-e2e-")).resolve()
APP, HOMEDIR, BIN = WORK / "app", WORK / "home", WORK / "bin"
PORT = 18787 + os.getpid() % 1000
SECONDS = 90


def sh(*a):
    subprocess.run(a, check=True, capture_output=True)


def setup():
    shutil.rmtree(WORK, ignore_errors=True)
    for d in (APP, HOMEDIR, BIN):
        d.mkdir(parents=True)
    for f in REPO.glob("*.py"):
        shutil.copy(f, APP)
    for f in ("dashboard.html", "config.example.toml"):
        shutil.copy(REPO / f, APP)
    fake = (HERE / "fake_chat.py").read_text()
    for name in ("chat_youtube.py", "chat_twitch.py"):  # keep the parsers, swap the live reader
        src = (APP / name).read_text().replace('if __name__ == "__main__":', "if False:")
        (APP / name).write_text(src + "\n" + fake)
    for name, dst in (("fake_streamlink.py", "streamlink"), ("fake_ytdlp.py", "yt-dlp")):
        (BIN / dst).write_text(f"#!{sys.executable}\n" + (HERE / name).read_text())
        (BIN / dst).chmod(0o755)
    print("making a synthetic stream …", flush=True)
    sh("ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=s=1280x720:r=30:d={SECONDS}",
       "-f", "lavfi", "-i", f"sine=f=440:d={SECONDS}", "-c:v", "libx264", "-preset", "ultrafast",
       "-g", "30", "-pix_fmt", "yuv420p", "-c:a", "aac", "-f", "mpegts", str(WORK / "src.ts"))
    sh("ffmpeg", "-v", "error", "-i", str(WORK / "src.ts"), "-c", "copy", str(WORK / "src.mp4"))
    cfg = (APP / "config.example.toml").read_text()
    for k, v in {"bucket_seconds": 5, "baseline_seconds": 30, "min_messages": 5, "warmup_seconds": 0,
                 "merge_seconds": 10, "stream_delay_seconds": 0, "lead_seconds": 4, "length_seconds": 8,
                 "enabled": "false", "format": '"9:16-bars"', "encoder": '"libx264"', "retry_seconds": 5}.items():
        cfg, n = re.subn(rf"(?m)^{k}\s*=\s*[^#\n]*", f"{k} = {v} ", cfg)
        assert n == 1, k
    (HOMEDIR / "config.toml").write_text(cfg)


def env():
    return {**os.environ, "CLIPPER_HOME": str(HOMEDIR), "CLIPPER_PORT": str(PORT), "CLIPPER_HOST": "127.0.0.1",
            "E2E_WORK": str(WORK), "E2E_SECONDS": str(SECONDS), "CLIPPER_PATH_PREPEND": str(BIN)}


def start_server():
    log = open(WORK / "server.log", "a")
    p = subprocess.Popen([sys.executable, "-u", str(APP / "server.py")], cwd=APP, env=env(),
                         stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(50):
        try:
            api("/api/jobs")
            return p
        except Exception:
            time.sleep(0.2)
    raise SystemExit("server didn't start — see " + str(WORK / "server.log"))


def api(path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", headers={"Content-Type": "application/json"},
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())


def wait(pred, timeout, what):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = pred()
        if v:
            return v
        time.sleep(2)
    raise AssertionError(f"timed out waiting for {what}")


def probe(p):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height", "-of", "csv=p=0", str(p)], capture_output=True, text=True)
    return out.stdout.strip()


def main():
    setup()
    srv = start_server()
    try:
        a = api("/api/jobs", {"kind": "watch", "name": "Alpha", "channels": {"twitch": "alpha"}, "record_from": "twitch"})
        b = api("/api/jobs", {"kind": "watch", "name": "Beta", "channels": {"youtube": "beta", "twitch": "beta"},
                              "record_from": "youtube"})
        v = api("/api/jobs", {"kind": "url", "url": "https://www.youtube.com/watch?v=VODVIDEO001"})
        ids = {"a": a["job"]["id"], "b": b["job"]["id"], "v": v["job"]["id"]}
        print("jobs:", ids, flush=True)

        def states():
            return {j["id"]: j for j in api("/api/jobs")}

        live = wait(lambda: (s := states()) and all(s[ids[k]]["state"]["status"] == "live" for k in "ab") and s,
                    60, "both watch jobs live")
        sa, sb = live[ids["a"]]["state"]["session"], live[ids["b"]]["state"]["session"]
        assert sa != sb, (sa, sb)
        print("live:", sa, sb, flush=True)

        # restart the dashboard mid-recording: jobs must keep going
        os.killpg(srv.pid, signal.SIGTERM)
        srv.wait()
        time.sleep(3)
        srv = start_server()
        s = states()
        assert s[ids["a"]]["state"]["status"] == "live", s[ids["a"]]["state"]
        assert s[ids["a"]]["live"]["minutes"] >= 0 and "twitch" in s[ids["a"]]["live"]["chat"], s[ids["a"]]["live"]
        print("survived dashboard restart; live stats:", s[ids["a"]]["live"], flush=True)

        def finished():
            s = states()
            ok = all(s[ids[k]]["state"]["status"] == "waiting" and (HOMEDIR / "sessions" / ss / "clips.md").exists()
                     for k, ss in (("a", sa), ("b", sb)))
            return ok and s[ids["v"]]["state"]["status"] == "done" and s
        s = wait(finished, SECONDS + 240, "both streams clipped + vod done")

        vs = s[ids["v"]]["state"]["session"]
        assert vs.endswith("-vodguy-vod"), vs
        for ss in (sa, sb, vs):
            d = HOMEDIR / "sessions" / ss
            shorts = sorted(d.glob("clips/*/short.mp4"))
            assert shorts, f"no clips in {ss}: " + (d / "clips.md").read_text()
            assert probe(shorts[0]) == "1080,1920", probe(shorts[0])
            md = (d / "clips.md").read_text()
            assert "Credit the streamer" in md and "http" in md, md
            print(f"{ss}: {len(shorts)} clips", flush=True)

        logs = "".join((HOMEDIR / "jobs" / ids[k] / "job.log").read_text() for k in "abv")
        assert "waiting for a free clipping slot" in logs, "processing was not serialized"
        print("processing slots: serialized ✓", flush=True)

        sess = {x["id"]: x for x in api("/api/sessions")}
        assert sess[sa]["stats"]["chat"] > 100 and sess[sa]["stats"]["spikes"] >= 1, sess[sa]["stats"]
        sysinfo = api("/api/system")
        assert sysinfo["totals"]["sessions"] == 3, sysinfo["totals"]

        for k in "ab":
            api(f"/api/jobs/{ids[k]}/stop", {})
        s = wait(lambda: (s := states()) and all(not s[ids[k]]["running"] for k in "ab") and s, 30, "jobs to stop")
        assert all(s[ids[k]]["state"]["status"] == "stopped" for k in "ab"), [s[ids[k]]["state"] for k in "ab"]
        assert not list((HOMEDIR / "sessions").glob("_waiting*")), "waiting dirs left behind"
        print("E2E PASS", flush=True)
    finally:
        for j in api("/api/jobs") if srv.poll() is None else []:
            if j["running"]:
                api(f"/api/jobs/{j['id']}/kill", {})
        os.killpg(srv.pid, signal.SIGTERM)


if __name__ == "__main__":
    main()
