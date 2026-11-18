import json
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from tests._home import HOME
import clipper
import mover
import server
from tests.test_server import call


def make_session(root: Path, sid: str) -> Path:
    d = root / sid
    (d / "clips" / "01_x").mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({"platform": "twitch", "channel": "x", "rec_start_ms": 0}))
    (d / "stream.ts").write_bytes(b"\0" * 300_000)
    (d / "chat_twitch.tsv").write_text("1\ta\thi\n")
    (d / "clips" / "01_x" / "short.mp4").write_bytes(b"mp4")
    return d


class Storage(unittest.TestCase):
    def setUp(self):
        clipper.CONFIG.write_text((clipper.ROOT / "config.example.toml").read_text())
        clipper.load_cfg()

    def test_resolve_dir(self):
        self.assertEqual(clipper.resolve_dir(""), clipper.DEFAULT_SESSIONS)
        self.assertEqual(clipper.resolve_dir("rec"), HOME.resolve() / "rec")
        self.assertEqual(clipper.resolve_dir("~/x"), Path.home() / "x")

    def test_check_dir_refuses_missing_drive(self):
        self.assertIn("is the drive connected", clipper.check_dir(HOME / "no-drive" / "clipperyt"))
        self.assertFalse((HOME / "no-drive").exists())  # never created on the wrong disk
        self.assertIsNone(clipper.check_dir(HOME / "drive-ok"))
        mnt = HOME / "mnt-empty"  # unmounted Linux mount point: empty dir
        mnt.mkdir()
        self.assertIn("is the drive connected", clipper.check_dir(mnt / "clipperyt", create=False))
        self.assertFalse((mnt / "clipperyt").exists())
        (HOME / "afile").write_text("x")
        self.assertIn("is a file", clipper.check_dir(HOME / "afile"))

    def test_record_refuses_unavailable_folder(self):
        clipper.SESSIONS = HOME / "gone" / "rec"
        with self.assertRaises(clipper.StorageUnavailable):
            clipper.record_once({"stream": {"channels": {"twitch": "x"}, "record_from": "twitch"}}, "_w")
        clipper.load_cfg()

    def test_move_copy_and_rename(self):
        src_root, dst_root = HOME / "old-a", HOME / "new-a"
        src_root.mkdir(); dst_root.mkdir()
        for copy in (True, False):
            sid = f"20990101-000{int(copy)}-mv"
            src = make_session(src_root, sid)
            st = {"done": 0, "total": 0}
            dst = mover.move_session(src, dst_root, st, copy=copy)
            self.assertFalse(src.exists())
            self.assertEqual((dst / "stream.ts").stat().st_size, 300_000)
            self.assertEqual(dst.parent, dst_root / "x")  # filed under its channel
            self.assertEqual(st["done"], st["total"])
            self.assertFalse(list(dst_root.glob("*/.moving-*")))
    def test_channel_layout_and_lookup(self):
        a = clipper.new_session_dir("Some Streamer")
        b = clipper.new_session_dir("Some Streamer")
        self.assertEqual(a.parent, clipper.SESSIONS / "some-streamer")
        self.assertTrue(b.name.endswith("-2"))
        (a / "meta.json").write_text("{}")
        flat = make_session(clipper.SESSIONS, "20990103-0000-flat")  # pre-channel-folder layout
        self.assertEqual(clipper.find_session(a.name), a)
        self.assertEqual(clipper.find_session(flat.name), flat)
        found = {d.name for d in clipper.iter_sessions(clipper.SESSIONS)}
        self.assertTrue({a.name, flat.name} <= found)
        self.assertIsNone(clipper.find_session("../etc"))

    def test_move_refuses_overwrite(self):
        src_root, dst_root = HOME / "old-b", HOME / "new-b"
        src_root.mkdir(); dst_root.mkdir()
        with self.assertRaises(RuntimeError):  # refuses to overwrite
            make_session(dst_root / "x", "20990101-0009-mv")
            make_session(src_root, "20990101-0009-mv")
            mover.move_session(src_root / "20990101-0009-mv", dst_root, {"done": 0}, copy=True)


class StorageApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        clipper.CONFIG.write_text((clipper.ROOT / "config.example.toml").read_text())
        clipper.load_cfg()
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_change_folder_list_move_serve(self):
        p = self.port
        old = make_session(clipper.DEFAULT_SESSIONS, "20990102-0000-old")
        code, r = call(p, "/api/settings", {"values": {"storage.sessions_dir": str(HOME / "nodrive" / "rec")}})
        self.assertEqual(code, 400, r)
        drive = HOME / "bigdrive"
        drive.mkdir()
        code, r = call(p, "/api/settings", {"values": {"storage.sessions_dir": str(drive / "rec")}})
        self.assertEqual(code, 200, r)
        self.assertEqual(clipper.SESSIONS, drive / "rec")
        # second change: the first folder goes into also_scan so its sessions stay listed
        make_session(drive / "rec", "20990102-0001-mid")
        code, r = call(p, "/api/settings", {"values": {"storage.sessions_dir": str(drive / "rec2")}})
        self.assertEqual(code, 200, r)
        self.assertIn(str(drive / "rec"), clipper.load_cfg()["storage"]["also_scan"])
        ids = {s["id"]: s for s in call(p, "/api/sessions")[1]}
        self.assertFalse(ids["20990102-0000-old"]["in_recordings_folder"])
        self.assertIn("20990102-0001-mid", ids)
        code, r = call(p, "/api/sessions/move-all", {})
        self.assertEqual(code, 200, r)
        for _ in range(50):
            if all((drive / "rec2" / "x" / s / "meta.json").is_file() for s in ("20990102-0000-old", "20990102-0001-mid")):
                break
            time.sleep(0.1)
        self.assertTrue((drive / "rec2" / "x" / "20990102-0000-old" / "stream.ts").exists())
        self.assertFalse(old.exists())
        ids = {s["id"]: s for s in call(p, "/api/sessions")[1]}
        self.assertTrue(ids["20990102-0000-old"]["in_recordings_folder"])
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{p}/files/20990102-0000-old/clips/01_x/short.mp4") as r:
            self.assertEqual(r.read(), b"mp4")
        disks = call(p, "/api/system")[1]["disks"]
        self.assertEqual(disks[0]["label"], "recordings")


if __name__ == "__main__":
    unittest.main()
