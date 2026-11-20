"""YouTube posting against a fake Google: device sign-in, refresh, chunked resumable upload,
scheduling into slots, unschedule, and the dashboard routes."""
import json
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tests._home import HOME
import clipper
import review
import youtube
import server
from tests.test_server import call

G = {"polls": 0, "uploads": {}, "meta": [], "deleted": [], "refreshes": 0}
CFG = {"client_id": "cid.apps.googleusercontent.com", "client_secret": "sec", "privacy": "public",
       "slots_per_day": 2, "slot_gap_hours": 3, "random_minutes": 0, "window_start": "10:00", "window_end": "22:00",
       "timezone": "Asia/Kolkata", "category_id": "20", "made_for_kids": False}


class FakeGoogle(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def out(self, code, obj=None, headers=None):
        data = json.dumps(obj).encode() if obj is not None else b""
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def authed(self):
        return self.headers.get("Authorization") == "Bearer at-1"

    def do_POST(self):
        raw = self.body()
        if self.path == "/device/code":
            f = dict(x.split("=", 1) for x in raw.decode().split("&"))
            if f["client_id"] != CFG["client_id"]:
                return self.out(401, {"error": "invalid_client"})
            return self.out(200, {"device_code": "dev-1", "user_code": "ABCD-EFGH", "expires_in": 1800,
                                  "interval": 1, "verification_url": "https://www.google.com/device"})
        if self.path == "/token":
            f = dict(x.split("=", 1) for x in raw.decode().split("&"))
            if f["grant_type"] == "refresh_token":
                G["refreshes"] += 1
                if f["refresh_token"] != "rt-1":
                    return self.out(400, {"error": "invalid_grant"})
                return self.out(200, {"access_token": "at-1", "expires_in": 3600})
            G["polls"] += 1
            if G["polls"] < 2:
                return self.out(428, {"error": "authorization_pending"})
            return self.out(200, {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600,
                                  "scope": youtube.SCOPE})
        if self.path.startswith("/upload/youtube/v3/videos?uploadType=resumable"):
            if not self.authed():
                return self.out(401, {"error": {"code": 401, "message": "bad"}})
            sid = f"s{len(G['uploads']) + 1}"
            G["uploads"][sid] = bytearray()
            G["meta"].append(json.loads(raw))
            return self.out(200, None, {"Location": f"http://127.0.0.1:{self.server.server_address[1]}/session/{sid}"})
        self.out(404, {})

    def do_PUT(self):
        sid = self.path.rsplit("/", 1)[1]
        data = self.body()
        rng = self.headers["Content-Range"]  # bytes a-b/total
        a, rest = rng.split(" ")[1].split("-")
        b, total = rest.split("/")
        assert int(a) == len(G["uploads"][sid]), (a, len(G["uploads"][sid]))
        G["uploads"][sid] += data
        if len(G["uploads"][sid]) < int(total):
            return self.out(308, None, {"Range": f"bytes=0-{len(G['uploads'][sid]) - 1}"})
        self.out(200, {"id": f"vid-{sid}"})

    def do_GET(self):
        if self.path.startswith("/youtube/v3/channels") and self.authed():
            return self.out(200, {"items": [{"id": "UCxyz", "snippet": {"title": "Clips Daily"}}]})
        self.out(404, {})

    def do_DELETE(self):
        G["deleted"].append(self.path.split("id=")[1])
        self.out(204)


class Slots(unittest.TestCase):
    def test_next_slot_window_gap_and_daily_cap(self):
        now = datetime(2026, 11, 20, 3, 0, tzinfo=timezone.utc)  # 08:30 IST
        first = youtube.next_slot(CFG, [], now)
        self.assertEqual(first, "2026-11-20T04:30:00Z")          # 10:00 IST
        second = youtube.next_slot(CFG, [first], now)
        self.assertEqual(second, "2026-11-20T07:30:00Z")         # 13:00 IST, 3 h later
        third = youtube.next_slot(CFG, [first, second], now)     # 2 per day → next day 10:00
        self.assertEqual(third, "2026-11-21T04:30:00Z")
        late = datetime(2026, 11, 20, 16, 40, tzinfo=timezone.utc)  # 22:10 IST: today's window is over
        self.assertEqual(youtube.next_slot(CFG, [], late), "2026-11-21T04:30:00Z")
        self.assertEqual(youtube.local_label(CFG, first), "Fri 20 Nov, 10:00")
        # posts already made today count toward the daily cap
        self.assertEqual(youtube.next_slot(CFG, [], now, posted=["2026-11-20T02:00:00Z", "2026-11-20T02:30:00Z"]),
                         "2026-11-21T04:30:00Z")
        self.assertEqual(youtube.local_day(CFG, first, now), "Today")
        self.assertEqual(youtube.local_day(CFG, third, now), "Tomorrow")

    def test_random_minutes_and_gaps(self):
        cfg = {**CFG, "slots_per_day": 4, "random_minutes": 60}
        now = datetime(2026, 11, 20, 3, 0, tzinfo=timezone.utc)
        taken = []
        for _ in range(16):
            t = youtube.next_slot(cfg, taken, now)
            self.assertEqual(t, youtube.next_slot(cfg, taken, now))  # what's shown is what a clip gets
            taken.append(t)
        ts = [datetime.fromisoformat(t.replace("Z", "+00:00")) + timedelta(hours=5, minutes=30) for t in taken]  # IST
        for t in ts:
            self.assertTrue((10, 0) <= (t.hour, t.minute) <= (22, 0), t)
        days = {}
        for t in ts:
            days.setdefault(t.date(), []).append(t)
        self.assertTrue(all(len(v) <= 4 for v in days.values()))
        gaps = [(b - a).total_seconds() / 3600 for v in days.values() for a, b in zip(v, v[1:])]
        self.assertTrue(gaps and all(3 <= g <= 4 for g in gaps), gaps)
        self.assertGreater(len({round(g, 2) for g in gaps}), 1)        # the gaps differ
        self.assertTrue(any(t.minute not in (0, 30) for t in ts))      # not on round times


class Reschedule(unittest.TestCase):
    def test_reslot_follows_new_settings_and_keeps_order(self):
        from unittest import mock
        now = datetime(2026, 11, 20, 3, 0, tzinfo=timezone.utc)            # 08:30 IST
        q = {"paused": False, "history": [], "items": [
            {"key": "s/a", "at": "2026-11-20T04:30:00Z", "status": "waiting"},
            {"key": "s/b", "at": "2026-11-20T07:30:00Z", "status": "waiting"},
            {"key": "s/c", "at": "2026-11-20T05:00:00Z", "status": "uploading"},   # keeps its time
            {"key": "s/d", "at": "2026-11-21T04:30:00Z", "status": "waiting"}]}
        cfg = {**CFG, "slots_per_day": 3, "slot_gap_hours": 2, "window_start": "16:00", "random_minutes": 0}
        with mock.patch.object(server, "yt_cfg", return_value=cfg), \
                mock.patch.object(server, "scheduled_clips", return_value=[]), \
                mock.patch.object(server, "save_queue") as saved:
            msg = server.reslot(q, now)
        self.assertTrue(saved.called)
        at = {i["key"]: i["at"] for i in q["items"]}
        self.assertEqual(at["s/c"], "2026-11-20T05:00:00Z")
        # 16:00 and 18:00 IST; the uploading clip is the day's third post, so d moves to tomorrow 16:00
        self.assertEqual([at["s/a"], at["s/b"], at["s/d"]],
                         ["2026-11-20T10:30:00Z", "2026-11-20T12:30:00Z", "2026-11-21T10:30:00Z"])
        self.assertIn("Rescheduled 3 clips", msg)
        with mock.patch.object(server, "yt_cfg", return_value=cfg), \
                mock.patch.object(server, "scheduled_clips", return_value=[]), mock.patch.object(server, "save_queue"):
            self.assertEqual(server.reslot(q, now), "Times already match the settings")


class OldConfig(unittest.TestCase):
    def test_missing_youtube_section_shows_defaults(self):
        import settings_schema
        v = settings_schema.flatten({"stream": {}})          # config.toml made before [youtube] existed
        self.assertEqual(v["youtube.slots_per_day"], 4)
        self.assertEqual(v["youtube.window_start"], "10:00")
        self.assertEqual(v["youtube.client_id"], "")
        # and that form saves as-is
        settings_schema.apply_values({"stream": {}}, {k: v[k] for k in v if k.startswith("youtube.")})


class Posting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.g = ThreadingHTTPServer(("127.0.0.1", 0), FakeGoogle)
        threading.Thread(target=cls.g.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{cls.g.server_address[1]}"
        youtube.OAUTH, youtube.API, youtube.CHUNK = base, base, 1000
        server.yt_cfg = lambda: CFG
        threading.Thread(target=server.yt_worker, daemon=True).start()
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.s = HOME / "sessions" / "speedy" / "20990110-0000-speedy"
        cls.clips = []
        for n in ("01_00-01-00", "02_00-05-00", "03_00-09-00"):
            c = cls.s / "clips" / n
            c.mkdir(parents=True)
            (c / "short.mp4").write_bytes(bytes(range(256)) * 10)   # 2560 bytes → 3 chunks
            (c / "titles.json").write_text(json.dumps({"titles": ["Bhai ne police ko chakma de diya"],
                                                       "hashtags": ["GTA5"], "description": "desc #GTA5"}))
            cls.clips.append(c)
        (cls.s / "meta.json").write_text(json.dumps({"platform": "youtube", "channel": "SpeedyGamer",
                                                     "source_url": "https://youtu.be/abc"}))

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.g.shutdown()
        youtube.forget(HOME)
        server.queue_path().unlink(missing_ok=True)
        import shutil
        shutil.rmtree(HOME / clipper.POST_BACKUP, ignore_errors=True)

    def wait(self, key, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            u = server.UPLOADS.get(key, {})
            if u.get("status") in ("done", "error"):
                return u
            time.sleep(0.05)
        self.fail(f"upload {key} didn't finish: {server.UPLOADS.get(key)}")

    def test_flow(self):
        sid = self.s.name
        # not connected yet → posting is refused with a pointer to Settings
        code, r = call(self.port, f"/api/sessions/{sid}/clips/01_00-01-00/post", {})
        self.assertEqual(code, 400)
        self.assertIn("Connect YouTube", r["error"])

        # device sign-in: code shown, poll thread stores the token and the channel
        code, r = call(self.port, "/api/youtube/connect", {})
        self.assertEqual(code, 200, r)
        self.assertEqual(r["pending"]["user_code"], "ABCD-EFGH")
        end = time.time() + 15
        while time.time() < end and not call(self.port, "/api/youtube")[1]["connected"]:
            time.sleep(0.2)
        st = call(self.port, "/api/youtube")[1]
        self.assertTrue(st["connected"])
        self.assertEqual(st["channel"], "Clips Daily")
        self.assertIsNone(st["pending"])
        self.assertEqual(youtube.token_path(HOME).stat().st_mode & 0o777, 0o600)

        # Switch channel: a new code while the old channel stays connected; Cancel keeps the old one
        code, r = call(self.port, "/api/youtube/connect", {})
        self.assertEqual(code, 200, r)
        self.assertTrue(r["connected"])
        self.assertEqual(r["pending"]["user_code"], "ABCD-EFGH")
        call(self.port, "/api/youtube/connect/cancel", {})
        st = call(self.port, "/api/youtube")[1]
        self.assertIsNone(st["pending"])
        self.assertTrue(st["connected"])

        # Post now: chunked upload (308s) with the AI title, public, Gaming
        code, r = call(self.port, f"/api/sessions/{sid}/clips/01_00-01-00/post", {"title": "My own title"})
        self.assertEqual(code, 200, r)
        u = self.wait(f"{sid}/01_00-01-00")
        self.assertEqual(u["status"], "done", u)
        self.assertEqual(bytes(G["uploads"]["s1"]), (self.clips[0] / "short.mp4").read_bytes())
        m = G["meta"][0]
        self.assertEqual(m["snippet"]["title"], "My own title")
        self.assertEqual(m["snippet"]["categoryId"], "20")
        self.assertIn("#Shorts", m["snippet"]["description"])
        self.assertEqual(m["status"], {"privacyStatus": "public", "selfDeclaredMadeForKids": False})
        rv = review.get(self.clips[0])
        self.assertEqual((rv["state"], rv["video_id"], rv["url"]), ("posted", "vid-s1", "https://www.youtube.com/shorts/vid-s1"))
        # can't post the same clip twice
        self.assertEqual(call(self.port, f"/api/sessions/{sid}/clips/01_00-01-00/post", {})[0], 400)

        # Schedule: the clip goes on the list with the next free time; nothing is uploaded yet
        slot = call(self.port, "/api/youtube")[1]["next_slot"]["iso"]
        code, r = call(self.port, f"/api/sessions/{sid}/clips/02_00-05-00/schedule", {"title": "Queued title"})
        self.assertEqual(code, 200, r)
        self.assertEqual(r["at"], slot)
        self.assertEqual(review.get(self.clips[1])["state"], "kept")
        self.assertEqual(call(self.port, f"/api/sessions/{sid}/clips/03_00-09-00/schedule", {})[0], 200)
        q = call(self.port, "/api/youtube")[1]["queue"]
        self.assertEqual([i["name"] for i in q], ["02_00-05-00", "03_00-09-00"])
        self.assertGreater(q[1]["at"], q[0]["at"])
        self.assertEqual(q[0]["title"], "Queued title")
        # each row shows the streamer, the AI titles and the description that will be posted
        self.assertEqual(q[0]["streamer"], {"name": "SpeedyGamer", "platforms": ["YouTube"]})
        self.assertEqual(q[0]["suggested"], ["Bhai ne police ko chakma de diya"])
        self.assertEqual((q[0]["ai_description"], q[0]["custom_description"]), ("desc #GTA5", False))
        self.assertIn("@SpeedyGamer", q[0]["description"])                     # credit added to the AI text
        self.assertIn("desc #GTA5", q[0]["description"])
        self.assertIn("#Shorts", q[0]["description"])
        key2 = f"{sid}/02_00-05-00"
        self.assertEqual(call(self.port, "/api/youtube/queue",
                              {"action": "description", "key": key2, "description": "Full chase, no cuts"})[0], 200)
        q = call(self.port, "/api/youtube")[1]["queue"]
        self.assertTrue(q[0]["custom_description"])
        d = server._clip_title_desc(self.s, self.clips[1], None)[1]
        self.assertTrue(d.startswith("Clip from @SpeedyGamer"), d)              # credit can't be edited away
        self.assertIn("Full chase, no cuts", d)
        call(self.port, "/api/youtube/queue", {"action": "description", "key": key2, "description": ""})
        self.assertFalse(call(self.port, "/api/youtube")[1]["queue"][0]["custom_description"])
        self.assertEqual(len(G["uploads"]), 1)                                  # only the Post now one
        self.assertEqual(call(self.port, f"/api/sessions/{sid}/clips/02_00-05-00/schedule", {})[0], 400)

        # reorder swaps the times
        call(self.port, "/api/youtube/queue", {"action": "move", "key": f"{sid}/03_00-09-00", "dir": -1})
        q2 = call(self.port, "/api/youtube")[1]["queue"]
        self.assertEqual([i["name"] for i in q2], ["03_00-09-00", "02_00-05-00"])
        self.assertEqual([i["at"] for i in q2], [q[0]["at"], q[1]["at"]])
        call(self.port, "/api/youtube/queue", {"action": "move", "key": f"{sid}/03_00-09-00", "dir": 1})

        # paused: nothing goes up even when the time has come
        far = datetime.fromisoformat(q[1]["at"].replace("Z", "+00:00")) + timedelta(hours=1)
        call(self.port, "/api/youtube/queue", {"action": "pause", "paused": True})
        server.yt_tick(far)
        self.assertEqual(len(G["uploads"]), 1)
        call(self.port, "/api/youtube/queue", {"action": "pause", "paused": False})

        # both times passed (machine was off): the first goes up, the other gets a new later time
        server.yt_tick(far)
        self.assertEqual(self.wait(f"{sid}/02_00-05-00")["status"], "done")
        self.assertEqual(G["meta"][1]["snippet"]["title"], "Queued title")
        self.assertEqual(G["meta"][1]["status"]["privacyStatus"], "public")
        st = call(self.port, "/api/youtube")[1]
        self.assertEqual([i["name"] for i in st["queue"]], ["03_00-09-00"])
        self.assertGreater(st["queue"][0]["at"], far.strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.assertEqual(st["history"][0]["key"], f"{sid}/02_00-05-00")
        self.assertEqual(review.get(self.clips[1])["state"], "posted")

        # Unschedule a listed clip: it just leaves the list
        code, r = call(self.port, f"/api/sessions/{sid}/clips/03_00-09-00/unschedule", {})
        self.assertEqual(code, 200, r)
        self.assertEqual(call(self.port, "/api/youtube")[1]["queue"], [])
        self.assertEqual(G["deleted"], [])

        # a clip uploaded with a publish time: Unschedule deletes it on YouTube
        review.put(self.clips[2], "posted", url="u", video_id="vid-old", publish_at="2999-01-01T00:00:00Z")
        self.assertEqual([x["name"] for x in call(self.port, "/api/youtube")[1]["scheduled"]], ["03_00-09-00"])
        code, r = call(self.port, f"/api/sessions/{sid}/clips/03_00-09-00/unschedule", {})
        self.assertEqual(code, 200, r)
        self.assertEqual(G["deleted"], ["vid-old"])
        self.assertEqual(review.get(self.clips[2])["state"], "kept")
        self.assertEqual(call(self.port, "/api/youtube")[1]["scheduled"], [])

        # an expired access token is refreshed with the refresh token
        tok = youtube.load_token(HOME)
        tok["expires_at"] = 0
        youtube.save_token(HOME, tok)
        before = G["refreshes"]
        self.assertEqual(youtube.access_token(CFG, HOME), "at-1")
        self.assertEqual(G["refreshes"], before + 1)

        # a failed upload shows in Needs attention, Disconnect forgets the token
        server.UPLOADS["x/01_00-00-01"] = {"status": "error", "error": "boom"}
        self.assertTrue(any("boom" in i["text"] for i in call(self.port, "/api/attention")[1]["items"]))
        server.UPLOADS.pop("x/01_00-00-01")
        call(self.port, "/api/youtube/disconnect", {})
        self.assertFalse(call(self.port, "/api/youtube")[1]["connected"])
        self.assertFalse(youtube.token_path(HOME).exists())

    def test_zz_scheduled_clip_is_backed_up(self):  # after test_flow (it counts uploads)
        import shutil
        youtube.save_token(HOME, {"access_token": "at-1", "refresh_token": "rt-1",
                                  "expires_at": time.time() + 3600, "channel_title": "Clips Daily"})
        s = HOME / "sessions" / "speedy" / "20990111-0000-speedy"
        c = s / "clips" / "01_00-02-00"
        c.mkdir(parents=True)
        body = bytes(range(255, -1, -1)) * 5
        (c / "short.mp4").write_bytes(body)
        (s / "meta.json").write_text(json.dumps({"platform": "youtube", "channel": "SpeedyGamer",
                                                 "source_url": "https://youtu.be/abc"}))
        key = f"{s.name}/01_00-02-00"
        code, r = call(self.port, f"/api/sessions/{s.name}/clips/01_00-02-00/schedule", {"title": "Saved copy"})
        self.assertEqual(code, 200, r)
        b = clipper.backup_clip_dir(s.name, "01_00-02-00")
        self.assertEqual((b / "short.mp4").read_bytes(), body)                 # a real copy, with the meta
        self.assertNotEqual((b / "short.mp4").stat().st_ino, (c / "short.mp4").stat().st_ino)
        self.assertTrue((b.parent.parent / "meta.json").exists())
        self.assertEqual(clipper.locked_clips(s), {"01_00-02-00": "scheduled"})

        # the clip folder deleted by hand: the next tick puts it back from the copy
        shutil.rmtree(c)
        server.yt_tick(datetime.now(timezone.utc))                   # not due yet
        self.assertEqual((c / "short.mp4").read_bytes(), body)

        # the whole session gone (drive unplugged, deleted outside the app): it still posts, from the copy
        shutil.rmtree(s)
        at = next(i["at"] for i in server.load_queue()["items"] if i["key"] == key)
        server.yt_tick(datetime.fromisoformat(at.replace("Z", "+00:00")) + timedelta(minutes=1))
        self.assertEqual(self.wait(key)["status"], "done")
        sent = G["uploads"][f"s{len(G['uploads'])}"]
        self.assertEqual(bytes(sent), body)
        self.assertEqual(G["meta"][-1]["snippet"]["title"], "Saved copy")
        self.assertIn("@SpeedyGamer", G["meta"][-1]["snippet"]["description"])
        self.assertFalse(b.exists())                                           # posted: copy removed
        self.assertFalse(b.parent.parent.exists())
        self.assertEqual(server.load_queue()["history"][0]["key"], key)

        # copies nothing needs any more are cleaned up
        orphan = clipper.backup_clip_dir("20990112-0000-speedy", "01_00-00-05")
        orphan.mkdir(parents=True)
        (orphan / "short.mp4").write_bytes(b"x")
        self.assertEqual(server.purge_backups(), 1)
        self.assertFalse(orphan.exists())
        youtube.forget(HOME)


if __name__ == "__main__":
    unittest.main()
