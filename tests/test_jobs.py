import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

from tests._home import HOME  # noqa: F401  (sets CLIPPER_HOME)
import clipper


class Jobs(unittest.TestCase):
    def test_home_is_isolated(self):
        self.assertEqual(clipper.SESSIONS, HOME.resolve() / "sessions")
        self.assertEqual(clipper.JOBS, HOME.resolve() / "jobs")

    def test_slug(self):
        self.assertEqual(clipper.slug("@IShow Speed!!"), "ishow-speed")
        self.assertEqual(clipper.slug(""), "stream")
        self.assertEqual(len(clipper.slug("x" * 99)), 32)

    def test_session_dirs_unique(self):
        a = clipper.new_session_dir("Same")
        b = clipper.new_session_dir("Same")
        c = clipper.new_session_dir("Same", "-vod")
        self.assertNotEqual(a, b)
        self.assertTrue(b.name.endswith("-same-2"), b.name)
        self.assertTrue(c.name.endswith("-same-vod"), c.name)
        for d in (a, b, c):
            self.assertTrue(d.is_dir())

    def test_rename_onto_reserved_dir(self):
        tmp = clipper.SESSIONS / "_waiting-x"
        tmp.mkdir(parents=True)
        (tmp / "stream.ts").write_bytes(b"x")
        dst = clipper.new_session_dir("rename")
        tmp.rename(dst)
        self.assertTrue((dst / "stream.ts").exists())

    def test_job_roundtrip_and_bad_id(self):
        clipper.save_job({"id": "abc-1", "kind": "url", "url": "https://youtu.be/x"})
        self.assertEqual(clipper.load_job("abc-1")["kind"], "url")
        with self.assertRaises(ValueError):
            clipper.job_dir("../etc")

    def test_dead_pid_reads_stopped_but_final_kept(self):
        p = HOME / "st.json"
        p.write_text(json.dumps({"status": "live", "pid": 999999}))
        self.assertEqual(clipper.read_state(p)["status"], "stopped")
        p.write_text(json.dumps({"status": "failed", "pid": 999999, "error": "x"}))
        self.assertEqual(clipper.read_state(p)["status"], "failed")

    def test_processing_slot_serializes(self):
        code = ("import time,clipper\n"
                "with clipper.processing_slot(1):\n"
                "    print(time.time(), flush=True); time.sleep(1.5); print(time.time(), flush=True)\n")
        env = {**os.environ, "CLIPPER_HOME": str(HOME)}
        root = Path(clipper.__file__).parent
        ps = [subprocess.Popen([sys.executable, "-c", code], cwd=root, env=env,
                               stdout=subprocess.PIPE, text=True) for _ in range(2)]
        spans = sorted(tuple(float(x) for x in p.communicate()[0].split() if x[0].isdigit()) for p in ps)
        self.assertGreaterEqual(spans[1][0], spans[0][1] - 0.05, spans)

    def test_two_slots_run_together(self):
        code = ("import time,clipper\n"
                "with clipper.processing_slot(2):\n"
                "    print(time.time(), flush=True); time.sleep(1.5); print(time.time(), flush=True)\n")
        env = {**os.environ, "CLIPPER_HOME": str(HOME)}
        root = Path(clipper.__file__).parent
        ps = [subprocess.Popen([sys.executable, "-c", code], cwd=root, env=env,
                               stdout=subprocess.PIPE, text=True) for _ in range(2)]
        spans = sorted(tuple(float(x) for x in p.communicate()[0].split() if x[0].isdigit()) for p in ps)
        self.assertLess(spans[1][0], spans[0][1], spans)


if __name__ == "__main__":
    unittest.main()
