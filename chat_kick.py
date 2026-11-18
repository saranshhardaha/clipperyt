"""Log Kick live chat to a TSV file — no account, read-only.

usage: python chat_kick.py <channel_slug> <out.tsv> [chatroom_id]

Uses Kick's public Pusher websocket (the same one kick.com's chat uses).
This is unofficial: if Kick changes it, update PUSHER_URL below.

Each line: <epoch_ms>\t<author>\t<message>   (epoch_ms = time received here)
"""
import asyncio
import json
import subprocess
import sys
import time

import websockets

PUSHER_URL = ("wss://ws-us2.pusher.com/app/32cbd69e4b950bf97679"
              "?protocol=7&client=js&version=8.4.0&flash=false")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


def clean(s: str) -> str:
    return (s or "").replace("\t", " ").replace("\n", " ").strip()


def lookup_chatroom_id(slug: str) -> int:
    # curl gets past Kick's Cloudflare check more reliably than python requests
    out = subprocess.run(
        ["curl", "-s", "--compressed", "-H", f"User-Agent: {UA}",
         "-H", "Accept: application/json", f"https://kick.com/api/v2/channels/{slug}"],
        capture_output=True, text=True, timeout=30,
    )
    try:
        return int(json.loads(out.stdout)["chatroom"]["id"])
    except Exception:
        raise SystemExit(
            f"[chat-kick] couldn't read chatroom id for '{slug}' (Cloudflare?). "
            f"Open https://kick.com/api/v2/channels/{slug} in a browser, find "
            f'"chatroom":{{"id":NNN}} and put it in config.toml as kick_chatroom_id.')


async def listen(chatroom_id: int, out_path: str) -> None:
    channel = f"chatrooms.{chatroom_id}.v2"
    with open(out_path, "a", encoding="utf-8", buffering=1) as f:
        while True:
            try:
                async with websockets.connect(PUSHER_URL, ping_interval=None) as ws:
                    await ws.send(json.dumps({"event": "pusher:subscribe",
                                              "data": {"auth": "", "channel": channel}}))
                    print(f"[chat-kick] subscribed to {channel}", flush=True)
                    async for raw in ws:
                        msg = json.loads(raw)
                        ev = msg.get("event", "")
                        if ev == "pusher:ping":
                            await ws.send(json.dumps({"event": "pusher:pong", "data": {}}))
                        elif ev.endswith("ChatMessageEvent"):
                            data = json.loads(msg["data"])  # Pusher double-encodes
                            author = clean((data.get("sender") or {}).get("username", "?"))
                            text = clean(data.get("content", ""))
                            f.write(f"{int(time.time() * 1000)}\t{author}\t{text}\n")
            except asyncio.CancelledError:
                return
            except Exception as e:
                print(f"[chat-kick] error: {e!r}; reconnecting in 5s", flush=True)
                await asyncio.sleep(5)


def main() -> None:
    slug, out_path = sys.argv[1], sys.argv[2]
    room = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3] else lookup_chatroom_id(slug)
    try:
        asyncio.run(listen(room, out_path))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
