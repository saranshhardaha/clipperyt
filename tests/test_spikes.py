import unittest

import spikes

CFG = {"bucket_seconds": 10, "baseline_seconds": 300, "multiplier": 3.0, "min_messages": 8,
       "warmup_seconds": 0, "merge_seconds": 60, "hype_words": [], "hype_weight": 0.5,
       "clip_requests": True, "clip_min_users": 3, "clip_window_seconds": 20, "clip_boost": 1.0}


def chat_at(rows):  # [(seconds, author, text)] → TSV-style rows, recording starts at 1_000_000 ms
    return sorted((1_000_000 + int(t * 1000), a, x) for t, a, x in rows)


def background(minutes=10):  # 1 boring message every 5 s
    return [(t, f"bg{t % 7}", "ok") for t in range(0, minutes * 60, 5)]


class ClipRequests(unittest.TestCase):
    def find(self, rows, **cfg):
        return spikes.find_clip_requests(chat_at(rows), 1_000_000, {**CFG, **cfg})

    def test_three_users_fire_without_spike(self):
        ms = self.find(background() + [(300, "a", "CLIP IT"), (305, "b", "bhai clip karo"), (312, "c", "!clip")])
        self.assertEqual(len(ms), 1)
        m = ms[0]
        self.assertEqual((m.onset, m.requests, m.kind), (300.0, 3, "request"))
        self.assertEqual(m.score, 3.0)

    def test_same_user_counts_once(self):
        self.assertEqual(self.find([(300, "a", "clip"), (301, "a", "clip"), (302, "a", "clip it"), (303, "b", "clip")]), [])

    def test_two_users_or_too_spread_dont_fire(self):
        self.assertEqual(self.find([(300, "a", "clip"), (305, "b", "clip")]), [])
        self.assertEqual(self.find([(300, "a", "clip"), (325, "b", "clip"), (350, "c", "clip")]), [])

    def test_whole_words_only(self):
        rows = [(300, "a", "eclipse"), (301, "b", "clips are old"), (302, "c", "videoclip"), (303, "d", "clipboard")]
        self.assertEqual(self.find(rows), [])
        self.assertEqual(len(self.find([(300, "a", "Clipped!"), (301, "b", "CLIP THAT"), (302, "c", "clip kro yaar")])), 1)

    def test_configurable(self):
        rows = [(300, "a", "clip"), (301, "b", "clip")]
        self.assertEqual(len(self.find(rows, clip_min_users=2)), 1)
        self.assertEqual(self.find(rows + [(302, "c", "clip")], clip_requests=False), [])
        self.assertEqual(len(self.find([(300, "a", "save this"), (301, "b", "save this"), (302, "c", "SAVE THIS")],
                                       clip_phrases=["save this"])), 1)

    def test_long_cluster_is_one_moment(self):
        rows = [(300 + i * 8, f"u{i}", "clip") for i in range(12)]  # 90 s of steady requests
        ms = self.find(rows)
        self.assertEqual(len(ms), 1)
        self.assertEqual(ms[0].requests, 12)

    def test_vod_offsets(self):  # past streams: rec_start 0, timestamps are ms offsets
        ms = spikes.find_clip_requests([(60_000, "a", "clip"), (61_000, "b", "clip"), (62_000, "c", "clip")], 0, CFG)
        self.assertEqual(ms[0].onset, 60.0)


class Merge(unittest.TestCase):
    def test_request_boosts_overlapping_spike(self):
        rows = background() + [(400 + i * 0.5, f"h{i}", "OMG") for i in range(40)]  # spike at 400 s
        rows += [(405, "a", "clip it"), (406, "b", "clip"), (407, "c", "clip that")]
        chat = chat_at(rows)
        plain = spikes.find_moments(chat, 1_000_000, CFG)
        allm = spikes.find_all(chat, 1_000_000, CFG)
        self.assertEqual(len(plain), 1)
        self.assertEqual(len(allm), 1)
        self.assertEqual(allm[0].kind, "spike+request")
        self.assertEqual(allm[0].requests, 3)
        self.assertAlmostEqual(allm[0].score, plain[0].score + 3.0, places=2)
        self.assertTrue(allm[0].top[0].startswith("clip requested ×3"))

    def test_separate_request_kept(self):
        rows = background() + [(400 + i * 0.5, f"h{i}", "OMG") for i in range(40)]
        rows += [(500, "a", "clip"), (501, "b", "clip"), (502, "c", "clip")]  # 100 s later, chat calm
        kinds = [m.kind for m in spikes.find_all(chat_at(rows), 1_000_000, CFG)]
        self.assertEqual(kinds, ["spike", "request"])


if __name__ == "__main__":
    unittest.main()
