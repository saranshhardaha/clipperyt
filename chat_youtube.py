"""Log YouTube live chat to a TSV file — no account, no API key, read-only.

usage: python chat_youtube.py <channel_handle | video_url> <out.tsv>

Talks to the same endpoint YouTube's chat popout uses (youtubei/v1/live_chat).
Each line: <epoch_ms>\t<author>\t<message>, where epoch_ms is YouTube's own
send time for the message (timestampUsec), so batching/polling delay doesn't
blur spikes.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


def clean(s: str) -> str:
    return (s or "").replace("\t", " ").replace("\n", " ").strip()


def http(url: str, body: dict | None = None) -> str:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode() if body is not None else None,
        headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
                 "Content-Type": "application/json", "Cookie": "CONSENT=YES+1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


# ------------------------------------------------------------- parsing (shared with VOD replay)

def runs_text(msg: dict | None) -> str:
    if not msg:
        return ""
    if "simpleText" in msg:
        return msg["simpleText"]
    out = []
    for r in msg.get("runs", []):
        if "text" in r:
            out.append(r["text"])
        elif "emoji" in r:
            e = r["emoji"]
            eid = e.get("emojiId", "")
            # standard emoji: id is the character itself; channel emotes: use :shortcut:
            out.append(eid if len(eid) <= 8 else (e.get("shortcuts") or [":emote:"])[0])
    return "".join(out)


def item_to_row(item: dict) -> tuple[int, str, str] | None:
    """addChatItemAction.item → (epoch_ms, author, text) for text + super chat messages."""
    r = (item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
         or item.get("liveChatPaidStickerRenderer"))
    if not r or "timestampUsec" not in r:
        return None
    return (int(r["timestampUsec"]) // 1000, clean(runs_text(r.get("authorName"))),
            clean(runs_text(r.get("message"))))


def actions_to_rows(actions: list[dict]) -> list[tuple[int, str, str]]:
    rows = []
    for a in actions or []:
        item = (a.get("addChatItemAction") or {}).get("item")
        if item and (row := item_to_row(item)):
            rows.append(row)
    return rows


# ------------------------------------------------------------- live polling

def video_id(handle_or_url: str) -> str | None:
    m = re.search(r"(?:v=|youtu\.be/|/live/|/shorts/)([\w-]{11})", handle_or_url)
    if m:
        return m[1]
    # channel handle → whatever is live on /@handle/live right now
    try:
        auth = json.loads(os.environ.get("CLIPPER_YTDLP_AUTH") or "[]")  # cookies, set by clipper.py
        out = subprocess.run(["yt-dlp", *auth, "--no-warnings", "--skip-download", "--print", "id",
                              (f"https://www.youtube.com/channel/{handle_or_url}/live"
                               if re.fullmatch(r"UC[\w-]{22}", handle_or_url)
                               else f"https://www.youtube.com/@{handle_or_url.lstrip('@')}/live")],
                             capture_output=True, text=True, timeout=60)
        vid = out.stdout.strip().splitlines()
        return vid[0] if out.returncode == 0 and vid else None
    except Exception:
        return None


def bootstrap(vid: str) -> tuple[str, str, str | None]:
    """→ (continuation, clientVersion, apiKey) from the chat popout page."""
    page = http(f"https://www.youtube.com/live_chat?is_popout=1&v={vid}")
    m = re.search(r'(?:window\["ytInitialData"\]|var ytInitialData)\s*=\s*(\{.+?\})\s*;\s*</script>', page, re.S)
    if not m:
        raise RuntimeError("chat not available yet (no ytInitialData) — stream not live, or chat disabled")
    data = json.loads(m[1])
    conts = data["contents"]["liveChatRenderer"]["continuations"][0]
    cont = next(v["continuation"] for v in conts.values() if isinstance(v, dict) and "continuation" in v)
    ver = re.search(r'"INNERTUBE_CONTEXT_CLIENT_VERSION":"([^"]+)"', page) or \
        re.search(r'"clientVersion":"([^"]+)"', page)
    key = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', page)
    return cont, (ver[1] if ver else "2.20241101.00.00"), (key[1] if key else None)


def poll(vid: str, out_path: str) -> None:
    cont, ver, key = bootstrap(vid)
    url = "https://www.youtube.com/youtubei/v1/live_chat/get_live_chat?prettyPrint=false"
    if key:
        url += f"&key={key}"
    seen: set[tuple] = set()
    n = 0
    print(f"[chat-yt] connected to chat of {vid}", flush=True)
    with open(out_path, "a", encoding="utf-8", buffering=1) as f:
        while True:
            data = json.loads(http(url, {"context": {"client": {"clientName": "WEB", "clientVersion": ver}},
                                         "continuation": cont}))
            lc = (data.get("continuationContents") or {}).get("liveChatContinuation")
            if not lc:
                print("[chat-yt] chat ended", flush=True)
                return
            for row in actions_to_rows(lc.get("actions", [])):
                if row in seen:
                    continue
                seen.add(row)
                f.write(f"{row[0]}\t{row[1]}\t{row[2]}\n")
                n += 1
            if len(seen) > 20000:
                seen.clear()
            nxt = (lc.get("continuations") or [{}])[0]
            c = next((v for v in nxt.values() if isinstance(v, dict) and "continuation" in v), None)
            if not c:
                print("[chat-yt] chat ended", flush=True)
                return
            cont = c["continuation"]
            # YouTube suggests a wait; cap it so busy chats stay smooth
            time.sleep(min(max(int(c.get("timeoutMs", 2000)) / 1000, 0.5), 5))


def main() -> None:
    target, out_path = sys.argv[1], sys.argv[2]
    vid = None
    while not vid:
        vid = video_id(target)
        if not vid:
            print("[chat-yt] not live yet, retrying in 20s", flush=True)
            time.sleep(20)
    fails = 0
    while True:
        try:
            poll(vid, out_path)
            return
        except KeyboardInterrupt:
            return
        except Exception as e:  # not live yet / network blip / YouTube hiccup
            fails += 1
            wait = min(60, 5 * fails)
            print(f"[chat-yt] {e!r}; retrying in {wait}s", flush=True)
            time.sleep(wait)


if __name__ == "__main__":
    main()
