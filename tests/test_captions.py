import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests._home import HOME  # noqa: F401
import clipper
import render


class Cues(unittest.TestCase):
    def test_cue_starts_at_first_word_not_before(self):
        cues = render.words_to_cues([("bhai", 3.0), ("ye", 3.3), ("dekho,", 3.5), ("kya", 4.2), ("shot", 4.5)])
        self.assertEqual(cues[0][2], "bhai ye dekho,")
        self.assertAlmostEqual(cues[0][0], 2.95, places=2)
        self.assertLessEqual(cues[0][1], cues[1][0])            # no overlap
        self.assertEqual(cues[1][2], "kya shot")

    def test_gap_splits_and_lone_word_merges(self):
        cues = render.words_to_cues([("a", 1.0), ("b", 1.2), ("c", 1.4), ("d", 1.6), ("e", 1.8), ("hai", 2.0),
                                     ("ab", 6.0), ("police", 6.4)])
        self.assertEqual([c[2] for c in cues], ["a b c d e hai", "ab police"])  # "hai" not alone
        self.assertGreaterEqual(cues[1][0], 5.9)                 # starts at the speech after the pause

    def test_silence_trim_fallback(self):
        segs = [(0.0, 5.2, "line one"), (5.2, 10.7, "line two")]
        quiet = [(0.0, 2.93), (5.23, 7.91)]
        out = render.trim_to_speech(segs, quiet)
        self.assertAlmostEqual(out[0][0], 2.93)
        self.assertAlmostEqual(out[1][0], 7.91)

    def test_dtw_preset(self):
        self.assertEqual(render.dtw_preset(Path("ggml-large-v3-turbo-q5_0.bin")), "large.v3.turbo")
        self.assertEqual(render.dtw_preset(Path("ggml-medium.bin")), "medium")
        self.assertIsNone(render.dtw_preset(Path("custom.bin")))


MODEL = clipper.ROOT / "models" / "ggml-large-v3-turbo-q5_0.bin"


@unittest.skipUnless(shutil.which("say") and shutil.which("whisper-cli") and MODEL.exists(),
                     "needs macOS `say`, whisper-cli and the model")
class RealSpeech(unittest.TestCase):
    def test_captions_wait_for_speech(self):
        d = Path(tempfile.mkdtemp())
        subprocess.run(["say", "-v", "Rishi", "-o", str(d / "s.aiff"),
                        "[[slnc 3000]] Bhai ye dekho, kya shot maara hai. [[slnc 2500]] Ab police aa rahi hai, bhaago jaldi."],
                       check=True)
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=320x180:r=25:d=11",
                        "-i", str(d / "s.aiff"), "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", str(d / "raw.mp4")], check=True)
        quiet = render.silences(d / "s.aiff")                     # ground truth: when nobody speaks
        speech1, speech2 = quiet[0][1], quiet[1][1]
        cap = {**clipper.load_cfg()["captions"], "enabled": True, "model": str(MODEL), "language": "auto"}
        self.assertTrue(render.transcribe(d / "raw.mp4", d / "speech.srt", cap, clipper.ROOT))
        starts = [render._ass_time(m) for m in re.findall(r"(\d\d:\d\d:\d\d,\d\d\d) -->", (d / "speech.srt").read_text())]
        secs = [int(t.split(":")[0]) * 3600 + int(t.split(":")[1]) * 60 + float(t.split(":")[2]) for t in starts]
        self.assertAlmostEqual(secs[0], speech1, delta=0.4)        # first caption appears with the voice
        self.assertFalse(any(quiet[1][0] + 0.2 < s < speech2 - 0.6 for s in secs),  # none in the long pause
                         (secs, quiet))


if __name__ == "__main__":
    unittest.main()
