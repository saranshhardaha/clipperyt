#!/usr/bin/env python3
"""Offline test of clipping while live: a 100 s fake stream with a chat burst at 35 s and
process_every_minutes = 1.0 → a background pass clips the burst while recording continues,
and the end-of-stream pass adds nothing twice.

usage: python tests/e2e/run_hourly.py [WORKDIR]   (~2 min; needs ffmpeg)
"""
import json, os, re, signal, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_e2e as E  # noqa: E402

E.SECONDS = 100
os.environ["E2E_BURST"] = "35"


def main():
    E.setup()
    cfg = (E.HOMEDIR / "config.toml").read_text()
    cfg = re.sub(r"(?m)^process_every_minutes\s*=.*", "process_every_minutes = 1.0", cfg)
    (E.HOMEDIR / "config.toml").write_text(cfg)
    srv = E.start_server()
    try:
        jid = E.api("/api/jobs", {"kind": "watch", "name": "Long", "channels": {"twitch": "longstream"}})["job"]["id"]
        log = E.HOMEDIR / "jobs" / jid / "job.log"
        sess = E.wait(lambda: (st := [j for j in E.api("/api/jobs") if j["id"] == jid][0]["state"])
                      .get("status") == "live" and st["session"], 60, "live")
        d = E.sess_dir(sess)
        # a clip must appear while the job is still recording
        E.wait(lambda: list(d.glob("clips/*/short.mp4")) and
               [j for j in E.api("/api/jobs") if j["id"] == jid][0]["state"]["status"] == "live", 90,
               "a clip made during the recording")
        print("clip made while still live ✓", flush=True)
        E.wait(lambda: [j for j in E.api("/api/jobs") if j["id"] == jid][0]["state"]["status"] == "waiting"
               and "moments in" in log.read_text() and "end" in log.read_text(), 180, "stream end + final pass")
        text = log.read_text()
        assert "in the background, recording continues" in text, text
        assert re.search(r"moments in \d\d:\d\d:\d\d–end in .*: \d+ already clipped, 0 new", text), text
        clips = sorted(d.glob("clips/*/short.mp4"))
        assert len(clips) == 1, clips
        md = (d / "clips.md").read_text()
        assert md.count("Credit the streamer") == 1 and md.count("short.mp4") == 1, md
        print("final pass added nothing twice ✓\nHOURLY PASS", flush=True)
    finally:
        for j in E.api("/api/jobs"):
            if j["running"]:
                E.api(f"/api/jobs/{j['id']}/kill", {})
        os.killpg(srv.pid, signal.SIGTERM)


if __name__ == "__main__":
    main()
