import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests._home import HOME  # noqa: F401
import render


def probe(p: Path) -> tuple[int, int]:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height", "-of", "json", str(p)],
                         capture_output=True, text=True).stdout
    s = json.loads(out)["streams"][0]
    return s["width"], s["height"]


def top_row_mean(p: Path) -> float:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(p), "-frames:v", "1",
                          "-vf", "crop=iw:40:0:0,format=gray", "-f", "rawvideo", "-"],
                         capture_output=True).stdout
    return sum(raw) / max(1, len(raw))


class Formats(unittest.TestCase):
    def test_legacy_mapping(self):
        self.assertEqual(render.out_format({}), "9:16-blur")
        self.assertEqual(render.out_format({"vertical": False}), "16:9")
        self.assertEqual(render.out_format({"mode": "crop"}), "9:16-crop")
        self.assertEqual(render.out_format({"format": "9:16-bars", "vertical": False}), "9:16-bars")
        self.assertEqual(render.out_format({"format": "nonsense"}), "9:16-blur")

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_render_each_format(self):
        d = Path(tempfile.mkdtemp())
        src = d / "raw.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=1280x720:d=2:r=30",
                        "-f", "lavfi", "-i", "sine=d=2", "-shortest", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", str(src)], check=True)
        sizes = {"16:9": (1920, 1080), "9:16-blur": (1080, 1920),
                 "9:16-bars": (1080, 1920), "9:16-crop": (1080, 1920)}
        for fmt, size in sizes.items():
            cfg = {"format": fmt, "encoder": "libx264", "font": "Arial", "credit": True}
            subs = d / "subs.ass"
            self.assertTrue(render.build_subs(None, "@test · YouTube", 2, subs, cfg))
            out = d / f"out-{fmt.replace(':', '')}.mp4"
            render.render(src, subs, out, cfg)
            self.assertEqual(probe(out), size, fmt)
            if fmt == "9:16-bars":
                # rows 0-40 sit in the black bar (credit text is at y≈120)
                self.assertLess(top_row_mean(out), 20, fmt)
            if fmt == "9:16-blur":
                self.assertGreater(top_row_mean(out), 20, fmt)


if __name__ == "__main__":
    unittest.main()
