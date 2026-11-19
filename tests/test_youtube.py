"""YouTube posting against a fake Google: device sign-in, refresh, chunked resumable upload,
scheduling into slots, unschedule, and the dashboard routes."""
import json
import threading
import time
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tests._home import HOME
import clipper
import review
import youtube
import server
from tests.test_server import call

G = {"polls": 0, "uploads": {}, "meta": [], "deleted": [], "refreshes": 0}
CFG = {"client_id": "cid.apps.googleusercontent.com", "client_secret": "sec", "privacy": "public",
       "slots_per_day": 2, "slot_gap_hours": 3, "window_start": "10:00", "window_end": "22:00",
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
        for n in ("01_00-01-00", "02_00-05-00"):
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

        # Schedule: private + publishAt at the next free slot, listed as scheduled
        slot = call(self.port, "/api/youtube")[1]["next_slot"]["iso"]
        code, r = call(self.port, f"/api/sessions/{sid}/clips/02_00-05-00/schedule", {})
        self.assertEqual(code, 200, r)
        self.assertEqual(r["publish_at"], slot)
        self.assertEqual(self.wait(f"{sid}/02_00-05-00")["status"], "done")
        self.assertEqual(G["meta"][1]["status"]["privacyStatus"], "private")
        self.assertEqual(G["meta"][1]["status"]["publishAt"], slot)
        self.assertEqual(G["meta"][1]["snippet"]["title"], "Bhai ne police ko chakma de diya")  # AI title used
        st = call(self.port, "/api/youtube")[1]
        self.assertEqual([x["name"] for x in st["scheduled"]], ["02_00-05-00"])
        self.assertNotEqual(st["next_slot"]["iso"], slot)                # that slot is taken now

        # Unschedule: the video is deleted on YouTube, the clip is back in Kept
        code, r = call(self.port, f"/api/sessions/{sid}/clips/02_00-05-00/unschedule", {})
        self.assertEqual(code, 200, r)
        self.assertEqual(G["deleted"], ["vid-s2"])
        rv = review.get(self.clips[1])
        self.assertEqual(rv["state"], "kept")
        self.assertNotIn("video_id", rv)
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


if __name__ == "__main__":
    unittest.main()
