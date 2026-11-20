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


class FixAndDelete(AddRequestClips):
    def test_add_only_keeps_existing_and_is_idempotent(self):  # covered by the parent class
        pass

    def test_fix_redo_delete(self):
        clipper.load_cfg()
        clipper.CONFIG.write_text(clipper.CONFIG.read_text().replace("clip_requests = false", "clip_requests = true"))
        clipper.cmd_process(self.s)                                   # spike clip #1 + request clip #2
        clips = clipper.existing_clips(self.s)
        self.assertEqual(len(clips), 2)
        one, two = clips[0][2], clips[1][2]
        md_before = (self.s / "clips.md").read_text()

        # break #1 the old way (no moment.json, no video) → it's "failed"
        for f in ("captioned.mp4", "raw.mp4", "moment.json"):
            (one / f).unlink(missing_ok=True)
        self.assertEqual(clipper.failed_clips(self.s), [one.name])
        detail = server.session_detail(self.s.name)
        self.assertEqual([c["failed"] for c in detail["clips"]], [True, False])

        clipper.cmd_process(self.s, redo=None)                        # "Fix failed clips"
        self.assertEqual(clipper.failed_clips(self.s), [])
        self.assertTrue((one / "captioned.mp4").exists())
        mom = json.loads((one / "moment.json").read_text())          # rebuilt from spikes.json
        self.assertGreater(mom["score"], 0)
        md = (self.s / "clips.md").read_text()
        self.assertEqual(md.count("| 1 |"), 1)                        # row replaced, not duplicated
        self.assertEqual(md.count("Credit the streamer"), 1)
        self.assertEqual(len(md.splitlines()), len(md_before.splitlines()))

        mtime = (two / "captioned.mp4").stat().st_mtime
        clipper.cmd_process(self.s, redo=[two.name])                  # "Redo" a working clip
        self.assertGreater((two / "captioned.mp4").stat().st_mtime, mtime)

        clipper.delete_clip(self.s, two.name)                         # "Delete"
        self.assertFalse(two.exists())
        self.assertNotIn(f"| 2 |", (self.s / "clips.md").read_text())
        clipper.cmd_process(self.s, requests_only=True)              # must not bring it back
        self.assertEqual([d.name for _, _, d in clipper.existing_clips(self.s)], [one.name])
        with self.assertRaises(ValueError):
            clipper.delete_clip(self.s, "../etc")


class RemakeFollowsSettings(AddRequestClips):
    def test_add_only_keeps_existing_and_is_idempotent(self):  # covered by the parent class
        pass

    def set(self, **kv):
        import re
        cfg = clipper.CONFIG.read_text()
        for k, v in kv.items():
            cfg, n = re.subn(rf"(?m)^{k}\s*=\s*[^#\n]*", f"{k} = {v} ", cfg)
            self.assertEqual(n, 1, k)
        clipper.CONFIG.write_text(cfg)

    def names(self):
        return [f.name for _, _, f in clipper.clip_folders(self.s)]

    def test_remake_overwrites_time_matches_with_new_settings(self):
        from tests.test_render import duration
        self.set(clip_requests="true")
        clipper.cmd_process(self.s)                                    # lead 2, length 6
        before = self.names()
        self.assertEqual(len(before), 2, before)
        # an older clip that no current moment matches (e.g. made with other settings)
        stray = self.s / "clips" / "07_00-00-10"
        shutil.copytree(self.s / "clips" / before[0], stray)
        stray_mtime = (stray / "captioned.mp4").stat().st_mtime

        self.set(lead_seconds="5", length_seconds="8")                 # change settings, then Re-make
        clipper.cmd_process(self.s)
        after = self.names()
        self.assertEqual(len(after), 3, after)                         # 2 overwritten + stray kept, nothing new
        self.assertEqual([a.split("_")[0] for a in after], ["01", "02", "07"])  # same numbers
        for old, new in zip(before, after[:2]):
            self.assertAlmostEqual(clipper.clip_folder_start(new), clipper.clip_folder_start(old) - 3, delta=0.01)
            self.assertFalse((self.s / "clips" / old).exists())        # renamed, not duplicated
            self.assertAlmostEqual(duration(self.s / "clips" / new / "captioned.mp4"), 8, delta=1)
        self.assertEqual((stray / "captioned.mp4").stat().st_mtime, stray_mtime)  # untouched
        md = (self.s / "clips.md").read_text()
        self.assertEqual(sum(1 for l in md.splitlines() if l.startswith("| ") and "---" not in l
                             and not l.startswith("| #")), 3)
        for a in after:
            self.assertIn(a, md)

        self.set(lead_seconds="7")                                     # single-clip Redo follows settings too
        clipper.cmd_process(self.s, redo=[after[1]])
        self.assertAlmostEqual(clipper.clip_folder_start(self.names()[1]),
                               clipper.clip_folder_start(after[1]) - 2, delta=0.01)
        self.assertEqual(len(self.names()), 3)


class TimelineAndManual(AddRequestClips):
    def test_add_only_keeps_existing_and_is_idempotent(self):  # covered by the parent class
        pass

    def test_timeline_and_clip_here(self):
        clipper.cmd_process(self.s)                                    # spike clip (requests off)
        t = clipper.session_timeline(self.s, clipper.load_cfg())
        self.assertGreaterEqual(t["duration"], 239)
        self.assertEqual(sum(t["bins"]), sum(1 for _ in open(self.s / "chat_youtube.tsv")))
        self.assertGreater(max(t["bins"]), t["bins"][0])               # the burst stands out
        self.assertEqual(len(t["clips"]), 1)
        self.assertTrue(t["moments"])

        clipper.cmd_process(self.s, manual_at=200.0)                   # "make a clip here"
        names = [f.name for _, _, f in clipper.clip_folders(self.s)]
        self.assertEqual(len(names), 2, names)
        manual = self.s / "clips" / names[1]
        self.assertAlmostEqual(clipper.clip_folder_start(names[1]), 200 - 0 - 2, delta=0.01)  # minus delay + lead
        mom = json.loads((manual / "moment.json").read_text())
        self.assertEqual(mom["kind"], "manual")
        self.assertTrue((manual / "captioned.mp4").exists())

        clipper.cmd_process(self.s, manual_at=201.0)                   # same spot again → no duplicate
        self.assertEqual(len(clipper.clip_folders(self.s)), 2)

        clipper.delete_clip(self.s, names[1])                          # deleted, then clicked again → back
        clipper.cmd_process(self.s, manual_at=200.0)
        self.assertEqual(len(clipper.clip_folders(self.s)), 2)
        self.assertEqual(clipper.deleted_starts(self.s), [])
        tl = clipper.session_timeline(self.s, clipper.load_cfg())
        self.assertIn("manual", [c["kind"] for c in tl["clips"]])


if __name__ == "__main__":
    unittest.main()


class RemakeKeepsPostedAndScheduled(RemakeFollowsSettings):
    def test_remake_overwrites_time_matches_with_new_settings(self):  # covered by the parent class
        pass

    def tearDown(self):
        (clipper.HOME / clipper.YT_QUEUE).unlink(missing_ok=True)
        super().tearDown()

    def test_posted_and_scheduled_clips_are_left_alone(self):
        self.set(clip_requests="true")
        clipper.cmd_process(self.s)
        one, two = self.names()
        (self.s / "clips" / one / "review.json").write_text(json.dumps(
            {"state": "posted", "video_id": "v1", "url": "https://www.youtube.com/shorts/v1"}))
        (clipper.HOME / clipper.YT_QUEUE).write_text(json.dumps({"paused": False, "history": [], "items": [
            {"key": f"{self.s.name}/{two}", "sid": self.s.name, "name": two, "at": "2099-01-06T04:30:00Z",
             "status": "waiting"}]}))
        self.assertEqual(clipper.locked_clips(self.s), {one: "posted", two: "scheduled"})
        mt = {n: (self.s / "clips" / n / "captioned.mp4").stat().st_mtime for n in (one, two)}

        self.set(lead_seconds="5", length_seconds="8")                 # Re-make with other settings
        clipper.cmd_process(self.s)
        self.assertEqual(self.names(), [one, two])                     # not renamed, no duplicates
        for n in (one, two):
            self.assertEqual((self.s / "clips" / n / "captioned.mp4").stat().st_mtime, mt[n])
        clipper.cmd_process(self.s, redo=[one, two])                   # Redo skips them too
        for n in (one, two):
            self.assertEqual((self.s / "clips" / n / "captioned.mp4").stat().st_mtime, mt[n])

        # the dashboard won't delete a scheduled clip (or its session), and marks both
        det = server.session_detail(self.s.name)
        self.assertEqual({c["name"]: c["locked"] for c in det["clips"]}, {one: "posted", two: "scheduled"})
        self.assertIn("unschedule", server.delete_session(self.s.name, False))
        self.assertTrue((self.s / "clips" / two).is_dir())
        # a clip with a posting copy (e.g. a Post now still uploading) counts as scheduled too
        (clipper.HOME / clipper.YT_QUEUE).unlink()
        bk = clipper.backup_clip_dir(self.s.name, two)
        bk.mkdir(parents=True)
        try:
            self.assertEqual(clipper.locked_clips(self.s), {one: "posted", two: "scheduled"})
        finally:
            import shutil
            shutil.rmtree(clipper.HOME / clipper.POST_BACKUP)
