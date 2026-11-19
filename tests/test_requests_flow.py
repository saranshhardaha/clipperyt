import json
import shutil
import subprocess
import unittest

from tests._home import HOME
import clipper
import server


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class AddRequestClips(unittest.TestCase):
    def setUp(self):
        cfg = (clipper.ROOT / "config.example.toml").read_text()
        for k, v in {"warmup_seconds": "0", "baseline_seconds": "60", "bucket_seconds": "10", "min_messages": "8",
                     "length_seconds": "6", "lead_seconds": "2", "stream_delay_seconds": "0",
                     "enabled": "false", "format": '"16:9"', "encoder": '"libx264"', "clip_requests": "false"}.items():
            import re
            cfg, n = re.subn(rf"(?m)^{k}\s*=\s*[^#\n]*", f"{k} = {v} ", cfg)
            self.assertEqual(n, 1, k)
        clipper.CONFIG.write_text(cfg)
        clipper.load_cfg()
        self.s = clipper.channel_dir(clipper.SESSIONS, "reqguy") / "20990105-0000-reqguy-vod"
        self.s.mkdir(parents=True)
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=25:d=240",
                        "-f", "lavfi", "-i", "sine=d=240", "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
                        "-g", "25", "-pix_fmt", "yuv420p", "-c:a", "aac", str(self.s / "stream.mp4")], check=True)
        (self.s / "meta.json").write_text(json.dumps({"platform": "youtube", "channel": "reqguy", "rec_start_ms": 0,
                                                      "vod": True, "started": "2099-01-05T00:00:00"}))
        rows = [(t * 1000, f"bg{t % 5}", "ok") for t in range(0, 240, 3)]          # calm chat
        rows += [(80_000 + i * 200, f"h{i}", "OMG") for i in range(40)]           # spike at ~80 s
        rows += [(180_000, "a", "clip it"), (184_000, "b", "bhai clip karo"), (190_000, "c", "!clip")]  # request at 180 s
        (self.s / "chat_youtube.tsv").write_text("".join(f"{t}\t{a}\t{x}\n" for t, a, x in sorted(rows)))

    def tearDown(self):
        shutil.rmtree(self.s.parent, ignore_errors=True)

    def test_add_only_keeps_existing_and_is_idempotent(self):
        clipper.cmd_process(self.s)                                  # requests off → spike only
        first = clipper.existing_clips(self.s)
        self.assertEqual(len(first), 1)
        mtime = (first[0][2] / "captioned.mp4").stat().st_mtime

        clipper.cmd_process(self.s, requests_only=True)              # the button
        clips = clipper.existing_clips(self.s)
        self.assertEqual(len(clips), 2)
        self.assertEqual((first[0][2] / "captioned.mp4").stat().st_mtime, mtime)  # old clip untouched
        n, start, d = clips[1]
        self.assertEqual(n, 2)
        self.assertAlmostEqual(start, 178, delta=1)                  # first request − lead
        mom = json.loads((d / "moment.json").read_text())
        self.assertEqual((mom["requests"], mom["kind"]), (3, "request"))
        md = (self.s / "clips.md").read_text()
        self.assertIn("**clip requested ×3**", md)
        self.assertEqual(md.count("Credit the streamer"), 1)

        clipper.cmd_process(self.s, requests_only=True)              # again → nothing new
        self.assertEqual(len(clipper.existing_clips(self.s)), 2)

        detail = server.session_detail(self.s.name)
        self.assertEqual([c["requests"] for c in detail["clips"]], [0, 3])


if __name__ == "__main__":
    unittest.main()
