import unittest

from tests._home import HOME  # noqa: F401
import clipper

P = clipper.parse_target


class Parse(unittest.TestCase):
    def check(self, text, kind, **kw):
        r = P(text)
        self.assertEqual(r["kind"], kind, (text, r))
        for k, v in kw.items():
            self.assertEqual(r.get(k), v, (text, r))
        self.assertTrue(r["label"] or kind == "error")
        return r

    def test_youtube(self):
        self.check("https://www.youtube.com/@IShowSpeed", "watch", platform="youtube", channel="IShowSpeed")
        self.check("youtube.com/@IShowSpeed/live", "watch", channel="IShowSpeed")
        self.check("https://m.youtube.com/@TechnoGamerzOfficial/streams", "watch", channel="TechnoGamerzOfficial")
        self.check("https://www.youtube.com/channel/UCX6OQ3DkcsbYNE6H8uQQuVA", "watch",
                   channel="UCX6OQ3DkcsbYNE6H8uQQuVA")
        self.check("https://www.youtube.com/c/TotalGaming", "watch", channel="TotalGaming")
        for u in ("https://www.youtube.com/watch?v=qSyo5d-dVzA&t=10s", "https://youtu.be/qSyo5d-dVzA",
                  "https://www.youtube.com/live/qSyo5d-dVzA?si=x", "youtube.com/shorts/qSyo5d-dVzA"):
            self.check(u, "url", platform="youtube", url="https://www.youtube.com/watch?v=qSyo5d-dVzA")
        self.check("https://www.youtube.com/feed/subscriptions", "error")

    def test_twitch(self):
        self.check("https://www.twitch.tv/ohnepixel", "watch", platform="twitch", channel="ohnepixel")
        self.check("twitch.tv/OhnePixel/videos", "watch", channel="ohnepixel")
        self.check("https://www.twitch.tv/videos/2291837465", "url", url="https://www.twitch.tv/videos/2291837465")
        self.check("https://www.twitch.tv/ohnepixel/clip/AbcDef", "error")
        self.check("https://clips.twitch.tv/AbcDef", "error")
        self.check("https://www.twitch.tv/directory", "error")

    def test_kick(self):
        self.check("https://kick.com/xqc", "watch", platform="kick", channel="xqc")
        self.check("https://kick.com/xqc/videos/0b1c2d", "error")
        self.check("https://kick.com/video/0b1c2d", "error")

    def test_names_and_junk(self):
        self.check("@IShowSpeed", "ask", name="IShowSpeed", guess="youtube")
        self.check("ohnepixel", "ask", name="ohnepixel", guess=None)
        self.check("https://example.com/x", "error")
        self.check("hello world !!", "error")
        self.check("", "error")

    def test_channel_id_urls(self):
        self.assertEqual(clipper.stream_url("youtube", "UCX6OQ3DkcsbYNE6H8uQQuVA"),
                         "https://www.youtube.com/channel/UCX6OQ3DkcsbYNE6H8uQQuVA/live")
        self.assertEqual(clipper.stream_url("youtube", "IShowSpeed"), "https://www.youtube.com/@IShowSpeed/live")


if __name__ == "__main__":
    unittest.main()
