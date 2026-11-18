"""Find moments where chat suddenly gets loud.

Chat is counted in fixed buckets (10 s by default). A bucket is a spike when
its weighted message count is >= multiplier x the average of the previous
baseline window (5 min by default). Short hype messages ("OMG", "😂😂", "bhai")
count a bit extra. Spikes close together are merged into one moment.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field


@dataclass
class Moment:
    onset: float           # seconds into the recording where chat started reacting
    peak: float            # seconds into the recording of the loudest bucket
    end: float
    score: float           # peak weighted count / baseline
    messages: int          # messages inside the moment
    top: list[str] = field(default_factory=list)  # most repeated messages

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def load_chat(path: str) -> list[tuple[int, str, str]]:
    rows = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t", 2)
            if len(parts) == 3 and parts[0].isdigit():
                rows.append((int(parts[0]), parts[1], parts[2]))
    rows.sort(key=lambda r: r[0])
    return rows


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _is_hype(text: str, hype: set[str]) -> bool:
    t = _norm(text)
    if not t:
        return False
    if t in hype:
        return True
    words = re.findall(r"\w+|[^\w\s]", t)
    # short message made of hype words / emoji, e.g. "OMG OMG", "😂😂😂", "bhai kya"
    return len(t) <= 30 and bool(words) and sum(w in hype for w in words) >= max(1, len(words) // 2)


def find_moments(chat: list[tuple[int, str, str]], rec_start_ms: int, cfg: dict) -> list[Moment]:
    b = float(cfg.get("bucket_seconds", 10))
    base_n = max(1, int(cfg.get("baseline_seconds", 300) / b))
    mult = float(cfg.get("multiplier", 3.0))
    min_msgs = int(cfg.get("min_messages", 8))
    warmup = float(cfg.get("warmup_seconds", 120))
    merge = float(cfg.get("merge_seconds", 60))
    hype = {_norm(w) for w in cfg.get("hype_words", [])}
    hw = float(cfg.get("hype_weight", 0.5))

    # messages relative to recording start; drop anything before it
    msgs = [((ts - rec_start_ms) / 1000.0, text) for ts, _, text in chat if ts >= rec_start_ms]
    if not msgs:
        return []

    n = int(msgs[-1][0] // b) + 1
    count = [0] * n
    weight = [0.0] * n
    texts: list[list[str]] = [[] for _ in range(n)]
    for t, text in msgs:
        i = int(t // b)
        count[i] += 1
        weight[i] += 1 + (hw if _is_hype(text, hype) else 0)
        texts[i].append(text)

    # rolling baseline over the previous base_n buckets (needs 1 minute of history,
    # or the whole baseline window if that's shorter — else nothing could ever fire)
    min_hist = max(1, min(base_n, int(60 / b)))
    hits: list[tuple[int, float]] = []
    running = 0.0
    for i in range(n):
        if i > 0:
            running += weight[i - 1]
            if i - 1 - base_n >= 0:
                running -= weight[i - 1 - base_n]
        hist = min(i, base_n)
        if i * b < warmup or hist < min_hist:
            continue
        avg = running / hist
        if count[i] >= min_msgs and weight[i] >= mult * max(avg, 0.5):
            hits.append((i, weight[i] / max(avg, 0.5)))

    # merge nearby spike buckets into moments
    moments: list[Moment] = []
    group: list[tuple[int, float]] = []

    def flush() -> None:
        if not group:
            return
        first, last = group[0][0], group[-1][0]
        peak_i, peak_score = max(group, key=lambda g: g[1])
        all_txt = [t for i in range(first, last + 1) for t in texts[i]]
        common = Counter(_norm(t) for t in all_txt if t.strip()).most_common(5)
        moments.append(Moment(
            onset=first * b, peak=peak_i * b, end=(last + 1) * b,
            score=round(peak_score, 2),
            messages=sum(count[first:last + 1]),
            top=[f"{t} ×{c}" if c > 1 else t for t, c in common],
        ))

    for h in hits:
        if group and (h[0] - group[-1][0]) * b > merge:
            flush()
            group = []
        group.append(h)
    flush()
    return moments


def fmt(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"
