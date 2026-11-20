"""Facecam on top: the camera box per channel, the 9:16-facecam render, and the dashboard routes."""
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from tests._home import HOME
import clipper
import render
import server
from tests.test_server import call

BOX = [0.75, 0.0, 0.25, 0.25]  # top-right quarter: where the test "camera" is


def pixel(p: Path, x: int, y: int) -> tuple[int, int, int]:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(p), "-frames:v", "1",
                          "-vf", f"format=rgb24,crop=2:2:{x}:{y}", "-f", "rawvideo", "-"],
                         capture_output=True).stdout
    return tuple(raw[:3])


def make_source(out: Path, seconds: int = 3) -> None:
    """Black 'gameplay' with a solid red 'camera' in the top-right quarter."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=black:s=1280x720:r=25:d={seconds}",
                    "-f", "lavfi", "-i", f"sine=d={seconds}", "-shortest",
                    "-vf", "drawbox=x=960:y=0:w=320:h=180:color=red:t=fill",
                    "-c:v", "libx264", "-g", "25", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    *(["-f", "mpegts"] if out.suffix == ".ts" else []), str(out)], check=True)


class Layout(unittest.TestCase):
    def test_layout_needs_a_box_and_is_clamped(self):
        self.assertIsNone(render.facecam_layout({}))
        self.assertIsNone(render.facecam_layout({"facecam_box": "nope"}))
        box, ch, game = render.facecam_layout({"facecam_box": [0.9, -1, 0.5, 0.3], "facecam_height": 0.9})
        self.assertEqual(box[:2], [0.9, 0.0])
        self.assertAlmostEqual(box[2], 0.1)                       # cut to the frame edge
        self.assertEqual((ch, game), (1152, "fill"))               # height capped at 60%
        self.assertEqual(render.facecam_layout({"facecam_box": BOX, "facecam_game": "whole"})[1:], (768, "whole"))
        self.assertTrue(render.is_vertical({"format": "9:16-facecam"}))
        self.assertIn("vstack", render.filter_graph("9:16-facecam", "", {"facecam_box": BOX}))
        # no box yet: the same picture as 9:16-blur
        self.assertEqual(render.filter_graph("9:16-facecam", ""), render.filter_graph("9:16-blur", ""))

    def test_captions_sit_under_the_seam_and_credit_moves_off_the_face(self):
        d = Path(tempfile.mkdtemp())
        srt = d / "s.srt"
        srt.write_text("1\n00:00:00,500 --> 00:00:01,500\nbhai kya scene hai\n", encoding="utf-8")
        cfg = {"format": "9:16-facecam", "facecam_box": BOX, "facecam_height": 0.4, "font": "Arial"}
        self.assertTrue(render.build_subs(srt, "@speedy · YouTube", 2, d / "a.ass", cfg))
        styles = {ln.split(",")[0][7:]: ln.split(",") for ln in (d / "a.ass").read_text().splitlines()
                  if ln.startswith("Style:")}
        self.assertEqual((styles["Caption"][18], styles["Caption"][21]), ("8", str(768 + 40)))
        self.assertEqual((styles["Credit"][18], styles["Credit"][21]), ("2", "140"))

    def test_box_per_channel(self):
        clipper.set_facecam_box("@Speedy Gamer", [0.7, 0.05, 0.28, 0.3])
        self.assertEqual(clipper.facecam_box("speedy gamer"), [0.7, 0.05, 0.28, 0.3])
        self.assertIsNone(clipper.facecam_box("someone-else"))
        for bad in ([1.2, 0, 0.1, 0.1], [0.5, 0.5, 0.6, 0.2], [0, 0, 0.01, 0.5]):
            with self.assertRaises(ValueError):
                clipper.set_facecam_box("x", bad)
        clipper.set_facecam_box("@Speedy Gamer", None)
        self.assertIsNone(clipper.facecam_box("speedy gamer"))


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class Render(unittest.TestCase):
    def test_camera_on_top_gameplay_below(self):
        d = Path(tempfile.mkdtemp())
        make_source(d / "raw.mp4")
        for game in ("fill", "whole"):
            out = d / f"short-{game}.mp4"
            render.render(d / "raw.mp4", None, out, {"format": "9:16-facecam", "facecam_box": BOX,
                                                     "facecam_game": game, "encoder": "libx264"})
            r, g, b = pixel(out, 540, 300)                           # the camera strip: red
            self.assertGreater(r, 180, game)
            self.assertLess(g + b, 120, game)
            self.assertLess(sum(pixel(out, 200, 1400)), 90, game)    # gameplay's left: black


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class Routes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.s = HOME / "sessions" / "camguy" / "20990120-0000-camguy"
        cls.s.mkdir(parents=True)
        make_source(cls.s / "stream.ts", 8)
        (cls.s / "meta.json").write_text(json.dumps({"platform": "twitch", "channel": "CamGuy", "rec_start_ms": 0,
                                                     "started": "2099-01-20T00:00:00"}))
        (cls.s / "chat_twitch.tsv").write_text("")

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        clipper.set_facecam_box("CamGuy", None)
        server.save_settings({"render.format": "9:16-blur"})
        shutil.rmtree(cls.s, ignore_errors=True)

    def test_mark_the_camera_from_the_dashboard(self):
        sid = self.s.name
        code, info = call(self.port, f"/api/sessions/{sid}/facecam")
        self.assertEqual(code, 200, info)
        self.assertEqual((info["channel"], info["box"]), ("CamGuy", None))
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/sessions/{sid}/frame?at=2", timeout=40) as r:
            self.assertEqual(r.headers["Content-Type"], "image/jpeg")
            self.assertEqual(r.read()[:2], b"\xff\xd8")
        self.assertEqual(call(self.port, f"/api/sessions/{sid}/facecam", {"box": [0.9, 0, 0.5, 0.2]})[0], 400)
        code, r = call(self.port, f"/api/sessions/{sid}/facecam", {"box": BOX, "use": True})
        self.assertEqual(code, 200, r)
        self.assertIn("Re-make", r["result"])
        info = call(self.port, f"/api/sessions/{sid}/facecam")[1]
        self.assertEqual((info["box"], info["format"]), (BOX, "9:16-facecam"))

        # a clip made now uses it: red camera on top
        maker = clipper._ClipMaker(self.s, clipper.load_cfg())
        self.assertEqual(maker.cfg["render"]["facecam_box"], BOX)
        cfg = {**maker.cfg["render"], "encoder": "libx264"}
        render.cut(self.s / "stream.ts", 1, 3, self.s / "raw.mp4")
        render.render(self.s / "raw.mp4", None, self.s / "short.mp4", cfg)
        self.assertGreater(pixel(self.s / "short.mp4", 540, 300)[0], 180)

        code, r = call(self.port, f"/api/sessions/{sid}/facecam", {"box": None})
        self.assertEqual(code, 200, r)
        self.assertIsNone(call(self.port, f"/api/sessions/{sid}/facecam")[1]["box"])


if __name__ == "__main__":
    unittest.main()
