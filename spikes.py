"""Find moments where chat suddenly gets loud.

Chat is counted in fixed buckets (10 s by default). A bucket is a spike when
its weighted message count is >= multiplier x the average of the previous
baseline window (5 min by default). Short hype messages ("OMG", "😂😂", "bhai")
count a bit extra. Spikes close together are merged into one moment.

Clip requests: when >= clip_min_users different chatters say a clip phrase
("clip", "clip it", "clip karo", "!clip" …) within clip_window_seconds, that is a
moment too, even if chat isn't louder. A request overlapping a spike boosts it.
"""
from __future__ import annotations

import re
from collections import Counter, deque
from dataclasses import dataclass, field

DEFAULT_CLIP_PHRASES = ["clip", "clip it", "clip that", "clip this", "clipped", "clip karo",
                        "clip kro", "clip kar do", "!clip"]


@dataclass
class Moment:
    onset: float           # seconds into the recording where chat started reacting
    peak: float            # seconds into the recording of the loudest bucket
    end: float
    score: float           # peak weighted count / baseline
    messages: int          # messages inside the moment
    top: list[str] = field(default_factory=list)  # most repeated messages
    requests: int = 0      # different chatters who asked for a clip
    kind: str = "spike"    # "spike" | "request" | "spike+request"

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


# ---------------------------------------------------------------- clip requests

def clip_matcher(phrases: list[str]):
    """Whole-word, case-insensitive match for any phrase ("clip" ≠ "eclipse" / "clips")."""
    parts = []
    for p in phrases or DEFAULT_CLIP_PHRASES:
        p = _norm(str(p))
        if p:
            parts.append(r"\s+".join(re.escape(w) for w in p.split()))
    if not parts:
        return lambda text: False
    rx = re.compile(r"(?<![\w!])(?:" + "|".join(sorted(parts, key=len, reverse=True)) + r")(?!\w)")
    return lambda text: bool(rx.search(_norm(text)))


def find_clip_requests(chat: list[tuple[int, str, str]], rec_start_ms: int, cfg: dict) -> list[Moment]:
    """Moments where >= clip_min_users different chatters asked for a clip within clip_window_seconds."""
    if not cfg.get("clip_requests", True):
        return []
    need = max(2, int(cfg.get("clip_min_users", 3)))
    win = float(cfg.get("clip_window_seconds", 20))
    boost = float(cfg.get("clip_boost", 1.0))
    is_req = clip_matcher(cfg.get("clip_phrases") or DEFAULT_CLIP_PHRASES)
    reqs = sorted(((ts - rec_start_ms) / 1000.0, author, text) for ts, author, text in chat
                  if ts >= rec_start_ms and is_req(text))
    # clusters: requests chained by gaps <= window
    clusters: list[list[tuple[float, str, str]]] = []
    for r in reqs:
        if clusters and r[0] - clusters[-1][-1][0] <= win:
            clusters[-1].append(r)
        else:
            clusters.append([r])
    out = []
    for cl in clusters:
        # first point where `need` different people asked within one window
        q: deque = deque()
        fired_at = onset = None
        for r in cl:
            q.append(r)
            while q and r[0] - q[0][0] > win:
                q.popleft()
            if len({a for _, a, _ in q}) >= need:
                fired_at, onset = r[0], q[0][0]
                break
        if fired_at is None:
            continue
        users = len({a for _, a, _ in cl})
        common = Counter(_norm(t) for _, _, t in cl).most_common(5)
        out.append(Moment(onset=onset, peak=fired_at, end=cl[-1][0], score=round(boost * users, 2),
                          messages=len(cl), top=[f"{t} ×{c}" if c > 1 else t for t, c in common],
                          requests=users, kind="request"))
    return out


def merge_requests(moments: list[Moment], requests: list[Moment], cfg: dict) -> list[Moment]:
    """Fold each request into a spike it overlaps (score boosted), else keep it as its own moment."""
    merge = float(cfg.get("merge_seconds", 60))
    boost = float(cfg.get("clip_boost", 1.0))
    out = list(moments)
    for r in requests:
        hit = next((m for m in out if m.kind != "request"
                    and m.onset - merge <= r.onset <= m.end + merge), None)
        if hit:
            hit.requests = max(hit.requests, r.requests)
            hit.kind = "spike+request"
            hit.score = round(hit.score + boost * r.requests, 2)
            hit.onset = min(hit.onset, r.onset)
            hit.top = [f"clip requested ×{r.requests}"] + [t for t in hit.top if not t.startswith("clip requested")]
        else:
            out.append(r)
    return sorted(out, key=lambda m: m.onset)


def find_all(chat: list[tuple[int, str, str]], rec_start_ms: int, cfg: dict) -> list[Moment]:
    """Spikes + clip requests, merged."""
    return merge_requests(find_moments(chat, rec_start_ms, cfg), find_clip_requests(chat, rec_start_ms, cfg), cfg)
