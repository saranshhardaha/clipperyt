import json
import os
import time
import unittest

from tests._home import HOME  # noqa: F401
import clipper


class Trash(unittest.TestCase):
    def setUp(self):
        clipper.CONFIG.write_text((clipper.ROOT / "config.example.toml").read_text())
        clipper.load_cfg()
        self.s = clipper.channel_dir(clipper.SESSIONS, "trashchan") / f"20990113-0000-trashchan-{time.time_ns()}"
        for name in ("01_00-00-10", "02_00-00-40"):
            c = self.s / "clips" / name
            c.mkdir(parents=True)
            (c / "short.mp4").write_bytes(b"video")
        (self.s / "meta.json").write_text(json.dumps({"channel": "trashchan", "platform": "twitch",
                                                      "started": "2099-01-13T00:00:00"}))

    def names(self):
        return [f.name for _, _, f in clipper.clip_folders(self.s)]

    def test_delete_is_undoable(self):
        tid = clipper.delete_clip(self.s, "02_00-00-40")
        self.assertEqual(self.names(), ["01_00-00-10"])
        self.assertTrue((self.s / clipper.TRASH / tid / "short.mp4").exists())
        self.assertEqual(clipper.deleted_starts(self.s), [40])
        self.assertNotIn("02_00-00-40", (self.s / "clips.md").read_text())
        self.assertEqual(clipper.restore_clip(self.s, tid), "02_00-00-40")
        self.assertEqual(self.names(), ["01_00-00-10", "02_00-00-40"])
        self.assertEqual(clipper.deleted_starts(self.s), [])            # add-only may use that spot again
        self.assertIn("02_00-00-40", (self.s / "clips.md").read_text())
        with self.assertRaises(ValueError):
            clipper.restore_clip(self.s, tid)                           # gone from the trash now
        with self.assertRaises(ValueError):
            clipper.restore_clip(self.s, "../../etc~1")

    def test_purge_after_seven_days(self):
        old = clipper.delete_clip(self.s, "01_00-00-10")
        aged = old.split("~")[0] + f"~{int(time.time()) - 8 * 86400}"
        os.rename(self.s / clipper.TRASH / old, self.s / clipper.TRASH / aged)
        new = clipper.delete_clip(self.s, "02_00-00-40")
        self.assertEqual(clipper.purge_trash(self.s), 1)
        left = os.listdir(self.s / clipper.TRASH)
        self.assertEqual(left, [new])


if __name__ == "__main__":
    unittest.main()
