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


def duration(p: Path) -> float:
    return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                 str(p)], capture_output=True, text=True).stdout.strip() or 0)


def first_frame_luma(p: Path) -> float:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(p), "-frames:v", "1", "-vf", "scale=32:18,format=gray",
                          "-f", "rawvideo", "-"], capture_output=True).stdout
    return sum(raw) / max(1, len(raw))


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class WallclockCut(unittest.TestCase):
    """cut_wallclock on MPEG-TS and fragmented MP4 (Twitch Enhanced Broadcasting / HEVC)."""

    @classmethod
    def setUpClass(cls):
        cls.d = Path(tempfile.mkdtemp())
        # brightness ramps 0→1 over 20 s, so the first frame tells where a clip starts
        src = ["-f", "lavfi", "-i", "color=c=white:s=320x180:r=25:d=20,fade=in:st=0:d=20",
               "-f", "lavfi", "-i", "sine=d=20", "-shortest", "-c:v", "libx264", "-g", "25",
               "-pix_fmt", "yuv420p", "-c:a", "aac"]
        subprocess.run(["ffmpeg", "-v", "error", *src, "-f", "mpegts", str(cls.d / "rec.ts")], check=True)
        subprocess.run(["ffmpeg", "-v", "error", *src, "-movflags", "frag_keyframe+empty_moov+default_base_moof",
                        "-frag_duration", "1000000", "-f", "mp4", str(cls.d / "rec_frag.ts")], check=True)
        subprocess.run(["ffmpeg", "-v", "error", *src, "-f", "mp4", str(cls.d / "rec_plain.mp4")], check=True)

    def timeline(self, f: Path) -> Path:
        n = f.stat().st_size
        tl = self.d / (f.name + ".tl")
        tl.write_text("".join(f"{1_800_000_000_000 + i * 1000}\t{n * i // 20}\n" for i in range(21)))
        return tl

    def check(self, name: str, kind: str):
        f = self.d / name
        self.assertEqual(render.container_of(f), kind)
        early, mid = self.d / f"{name}.early.mp4", self.d / f"{name}.mid.mp4"
        render.cut_wallclock(f, self.timeline(f), 1_800_000_000 + 1, 4, early)
        render.cut_wallclock(f, self.timeline(f), 1_800_000_000 + 12, 4, mid)
        self.assertAlmostEqual(duration(mid), 4, delta=1.2)
        self.assertGreater(first_frame_luma(mid), first_frame_luma(early) + 60)  # really from the middle

    def test_ts(self):
        self.check("rec.ts", "ts")

    def test_fragmented_mp4(self):
        self.check("rec_frag.ts", "mp4")  # streamlink names it .ts whatever the container

    def test_plain_mp4_falls_back(self):
        self.check("rec_plain.mp4", "mp4")


if __name__ == "__main__":
    unittest.main()
