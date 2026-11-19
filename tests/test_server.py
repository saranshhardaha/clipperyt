import json
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from tests._home import HOME
import clipper
import settings_schema
import stats

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

import server


def call(port, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


class Settings(unittest.TestCase):
    def test_example_roundtrip_keeps_unknown_keys(self):
        raw = tomllib.loads((clipper.ROOT / "config.example.toml").read_text())
        raw["stream"]["options"]["kick_chatroom_id"] = 123
        raw["spikes"]["my_extra"] = "keep me"
        raw["custom"] = {"a": [1, 2], "b": {"c": True}}
        again = tomllib.loads(settings_schema.dump_toml(raw))
        self.assertEqual(again, raw)

    def test_every_schema_key_exists_in_example(self):
        raw = tomllib.loads((clipper.ROOT / "config.example.toml").read_text())
        missing = [f["key"] for f in settings_schema.SCHEMA
                   if not f.get("optional") and settings_schema.get_path(raw, f["key"]) is None]
        self.assertEqual(missing, [])

    def test_apply_values_types_and_errors(self):
        raw = tomllib.loads((clipper.ROOT / "config.example.toml").read_text())
        errs = settings_schema.apply_values(raw, {
            "spikes.multiplier": "4.5", "clip.max_clips": "10", "captions.enabled": False,
            "spikes.hype_words": "omg, lol\nbhai", "render.format": "9:16-bars",
            "stream.options.kick_chatroom_id": ""})
        self.assertEqual(errs, [])
        self.assertEqual(raw["spikes"]["multiplier"], 4.5)
        self.assertEqual(raw["clip"]["max_clips"], 10)
        self.assertEqual(raw["spikes"]["hype_words"], ["omg", "lol", "bhai"])
        self.assertNotIn("vertical", raw["render"])
        errs = settings_schema.apply_values(raw, {"render.format": "4:3", "clip.max_clips": "2.5",
                                                  "spikes.multiplier": "abc", "nope.x": 1})
        self.assertEqual(len(errs), 4, errs)


class Stats(unittest.TestCase):
    def test_chat_tail_incremental(self):
        d = HOME / "sessions" / "20990101-0000-stats"
        d.mkdir(parents=True, exist_ok=True)
        f = d / "chat_youtube.tsv"
        f.write_text("1000\ta\thi\n2000\tb\tyo\n3000\ta\tpartial")
        t = stats.tail_for(f)
        self.assertEqual((len(t.rows), len(t.authors)), (2, 2))
        with open(f, "a") as fh:
            fh.write(" line\n4000\tc\tz\n")
        t = stats.tail_for(f)
        self.assertEqual(len(t.rows), 4)
        self.assertEqual(t.rows[2][2], "partial line")
        s = stats.session_stats(d)
        self.assertEqual((s["chat"], s["chatters"]), (4, 3))

    def test_per_minute(self):
        rows = [(i * 30_000, "a", "x") for i in range(10)]  # 2 per minute for 5 min
        self.assertEqual(stats.per_minute(rows, 0, 300_000, 5), [2, 2, 2, 2, 2])


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        clipper.load_cfg()
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_job_validation(self):
        p = self.port
        self.assertEqual(call(p, "/api/jobs", {"kind": "watch", "channels": {}, "start": False})[0], 400)
        code, r = call(p, "/api/jobs", {"kind": "watch", "channels": {"twitch": "x"},
                                        "record_from": "youtube", "start": False})
        self.assertEqual((code, r["job"]["record_from"]), (200, "twitch"))  # falls back, no error
        call(p, f"/api/jobs/{r['job']['id']}/delete", {})
        self.assertEqual(call(p, "/api/jobs", {"kind": "url", "url": "https://example.com/v", "start": False})[0], 400)
        code, r = call(p, "/api/jobs", {"kind": "url", "url": "https://youtu.be/abc", "start": False})
        self.assertEqual(code, 200, r)
        jid = r["job"]["id"]
        code, js = call(p, "/api/jobs")
        self.assertIn(jid, [j["id"] for j in js])
        self.assertEqual(call(p, f"/api/jobs/{jid}/delete", {})[1]["result"], "deleted")
        self.assertEqual(call(p, "/api/jobs/..%2Fetc/start", {})[0], 404)

    def test_settings_api(self):
        p = self.port
        code, s = call(p, "/api/settings")
        self.assertEqual(code, 200)
        self.assertIn("render.format", s["values"])
        code, r = call(p, "/api/settings", {"values": {"render.format": "16:9", "spikes.multiplier": 2.5}})
        self.assertEqual(code, 200, r)
        cfg = clipper.load_cfg()
        self.assertEqual(cfg["render"]["format"], "16:9")
        self.assertEqual(cfg["spikes"]["multiplier"], 2.5)
        self.assertEqual(call(p, "/api/settings", {"values": {"render.format": "x"}})[0], 400)
        self.assertTrue((HOME / "config.toml.bak").exists())

    def test_handler_errors_return_json(self):
        orig = server.create_job
        server.create_job = lambda data: (_ for _ in ()).throw(PermissionError(13, "Permission denied", "/x/jobs"))
        try:
            code, r = call(self.port, "/api/jobs", {"kind": "url", "url": "https://youtu.be/x"})
        finally:
            server.create_job = orig
        self.assertEqual(code, 500)
        self.assertIn("PermissionError", r["error"])
        self.assertIn("chown", r["error"])

    def test_cookie_upload(self):
        import stat
        p = self.port
        self.assertEqual(call(p, "/api/cookies", {"text": "hello"})[0], 400)
        txt = ("# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tabc\n"
               ".youtube.com\tTRUE\t/\tTRUE\t0\tPREF\tx\n.google.com\tTRUE\t/\tTRUE\t0\tNID\tn\n")
        code, r = call(p, "/api/cookies", {"text": txt})
        self.assertEqual(code, 200, r)
        self.assertEqual((r["cookies"]["youtube_cookies"], r["cookies"]["logged_in"]), (2, True))
        f = HOME / "youtube-cookies.txt"
        self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600)
        cfg = clipper.load_cfg()
        self.assertEqual(clipper.ytdlp_cookie_args(cfg["stream"]), ["--cookies", str(f.resolve())])
        self.assertTrue(call(p, "/api/settings")[1]["cookies"]["set"])
        self.assertFalse(call(p, "/api/cookies/remove", {})[1]["cookies"]["set"])
        self.assertFalse(f.exists())

    def test_done_jobs_leave_the_list(self):
        sess = clipper.channel_dir(clipper.SESSIONS, "donechan") / "20990107-0000-donechan"
        sess.mkdir(parents=True)
        (sess / "meta.json").write_text("{}")
        for jid, status in (("done-1", "done"), ("fail-1", "failed")):
            clipper.save_job({"id": jid, "kind": "url", "name": jid, "url": "https://youtu.be/x"})
            d = clipper.job_dir(jid)
            (d / "state.json").write_text(json.dumps({"status": status, "pid": 999999, "session": sess.name}))
            (d / "job.log").write_text(f"log of {jid}\n")
        ids = [j["id"] for j in call(self.port, "/api/jobs")[1]]
        self.assertNotIn("done-1", ids)
        self.assertIn("fail-1", ids)
        self.assertFalse(clipper.job_dir("done-1").exists())
        self.assertIn("log of done-1", (sess / "job.log").read_text())
        call(self.port, "/api/jobs/fail-1/delete", {})

    def test_review_states_and_attention(self):
        sess = clipper.channel_dir(clipper.SESSIONS, "revchan") / "20990110-0000-revchan"
        for name, video in (("01_00-00-10", True), ("02_00-00-40", True), ("03_00-01-10", False)):
            c = sess / "clips" / name
            c.mkdir(parents=True)
            if video:
                (c / "short.mp4").write_bytes(b"x")
        (sess / "meta.json").write_text(json.dumps({"channel": "revchan", "platform": "twitch"}))
        (sess / "stream.ts").write_bytes(b"x")
        p = self.port
        code, r = call(p, "/api/clips?channel=revchan")
        self.assertEqual(r["counts"], {"new": 2, "kept": 0, "skipped": 0, "posted": 0})
        self.assertEqual([c["name"] for c in r["clips"]], ["01_00-00-10", "02_00-00-40"])  # stream order
        self.assertEqual(call(p, f"/api/sessions/{sess.name}/clips/01_00-00-10/review", {"state": "nope"})[0], 400)
        code, r = call(p, f"/api/sessions/{sess.name}/clips/01_00-00-10/review", {"state": "kept", "title": "My title"})
        self.assertEqual((code, r["state"], r["title"]), (200, "kept", "My title"))
        r = call(p, "/api/clips?channel=revchan&state=new")[1]
        self.assertEqual([c["name"] for c in r["clips"]], ["02_00-00-40"])
        self.assertEqual(r["counts"]["kept"], 1)
        a = call(p, "/api/attention")[1]
        fix = [i for i in a["items"] if sess.name in i["text"]]
        self.assertEqual(len(fix), 1)                                       # the failed clip
        self.assertEqual(fix[0]["action"]["post"], f"/api/sessions/{sess.name}/redo")
        self.assertGreaterEqual(a["new_clips"], 1)
        call(p, f"/api/sessions/{sess.name}/clips/01_00-00-10/review", {"state": "new"})
        self.assertEqual(call(p, "/api/clips?channel=revchan")[1]["counts"]["new"], 2)

    def test_files_no_traversal(self):
        self.assertEqual(call(self.port, "/files/../config.toml")[0], 404)


if __name__ == "__main__":
    unittest.main()
