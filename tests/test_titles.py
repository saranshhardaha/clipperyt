import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tests._home import HOME
import clipper
import titles

SEEN = {}


class FakeXAI(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        SEEN.update(path=self.path, auth=self.headers.get("Authorization"), body=body)
        if self.headers.get("Authorization") != "Bearer good-key":
            self.send_response(401); self.end_headers(); self.wfile.write(b'{"error":"bad key"}'); return
        if self.headers.get("X-Title") != "clipperyt" or "HTTP-Referer" in self.headers:
            self.send_response(400); self.end_headers(); return
        text = '```json\n{"titles": ["Bhai ne police ko chakma de diya \U0001F602", "Insane GTA escape", "t3", "t4", "t5", "t6"], "hashtags": ["#GTA5", "GTA RP", "gaming"]}\n```'
        out = {"id": "gen-1", "model": body["model"], "choices": [{"message": {"role": "assistant", "content": text}}],
               "usage": {"prompt_tokens": 300, "completion_tokens": 80, "cost": 0.00108}}
        data = json.dumps(out).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)


class Titles(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeXAI)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        titles.API_BASE = f"http://127.0.0.1:{cls.srv.server_address[1]}/api/v1"
        cls.s = HOME / "sessions" / "speedy" / "20990109-0000-speedy"
        cls.clip = cls.s / "clips" / "01_00-01-00"
        cls.clip.mkdir(parents=True)
        (cls.s / "meta.json").write_text(json.dumps({"platform": "youtube", "channel": "SpeedyGamer",
                                                     "title": "GTA RP night", "source_url": "https://youtu.be/abc"}))
        (cls.clip / "moment.json").write_text(json.dumps({"top": ["clip requested ×4", "omg ×20"], "requests": 4}))
        (cls.clip / "speech.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nbhaago police aa gayi\n\n")

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_suggest(self):
        out = titles.suggest(self.s, self.clip, {"openrouter_api_key": "good-key", "language": "hinglish"})
        self.assertEqual(SEEN["path"], "/api/v1/chat/completions")
        self.assertEqual(SEEN["body"]["model"], "x-ai/grok-4.7")          # default model
        self.assertEqual(out["cost"], 0.00108)
        sent = json.dumps(SEEN["body"]["messages"], ensure_ascii=False)
        self.assertIn("bhaago police aa gayi", sent)                 # transcript goes in
        self.assertIn("omg ×20", sent)                               # chat goes in
        self.assertNotIn("clip requested", sent.split("what_chat_said")[1].split("]")[0])
        self.assertEqual(len(out["titles"]), 5)
        self.assertEqual(out["hashtags"], ["GTA5", "GTARP", "gaming"])
        self.assertIn("@SpeedyGamer", out["description"])           # credit is ours, always there
        self.assertIn("https://youtu.be/abc", out["description"])
        self.assertIn("#Shorts", out["description"])
        self.assertEqual(titles.cached(self.clip)["titles"], out["titles"])

    def test_errors(self):
        with self.assertRaisesRegex(titles.TitleError, "OpenRouter API key"):
            titles.suggest(self.s, self.clip, {"openrouter_api_key": ""})
        with self.assertRaisesRegex(titles.TitleError, "rejected the API key"):
            titles.suggest(self.s, self.clip, {"openrouter_api_key": "wrong"})


if __name__ == "__main__":
    unittest.main()
