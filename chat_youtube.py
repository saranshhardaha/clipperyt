"""Log YouTube live chat to a TSV file — no account, read-only.

usage: python chat_youtube.py <channel_handle> <out.tsv>

Each line: <epoch_ms>\t<author>\t<message>
epoch_ms is the time *we received* the message on this machine, so it
shares a clock with the recording start time written by clipper.py.
"""
import subprocess
import sys
import time

from chat_downloader import ChatDownloader


def clean(s: str) -> str:
    return (s or "").replace("\t", " ").replace("\n", " ").strip()


def live_video_url(handle: str) -> str | None:
    """Resolve youtube.com/@handle/live to the actual live video URL."""
    try:
        out = subprocess.run(
            ["yt-dlp", "--no-warnings", "--skip-download", "--print", "webpage_url",
             f"https://www.youtube.com/@{handle}/live"],
            capture_output=True, text=True, timeout=60,
        )
        url = out.stdout.strip().splitlines()
        return url[0] if out.returncode == 0 and url else None
    except Exception:
        return None


def main() -> None:
    handle, out_path = sys.argv[1], sys.argv[2]
    url = None
    while not url:
        url = live_video_url(handle)
        if not url:
            print("[chat-yt] not live yet, retrying in 20s", flush=True)
            time.sleep(20)
    print(f"[chat-yt] reading chat of {url}", flush=True)

    with open(out_path, "a", encoding="utf-8", buffering=1) as f:
        while True:  # reconnect if the chat stream drops mid-broadcast
            try:
                chat = ChatDownloader().get_chat(url, message_groups=["messages", "superchat"])
                for msg in chat:
                    now_ms = int(time.time() * 1000)
                    author = clean((msg.get("author") or {}).get("name", "?"))
                    f.write(f"{now_ms}\t{author}\t{clean(msg.get('message', ''))}\n")
                print("[chat-yt] chat ended", flush=True)
                return
            except KeyboardInterrupt:
                return
            except Exception as e:  # network blip / YouTube change
                print(f"[chat-yt] error: {e!r}; reconnecting in 10s", flush=True)
                time.sleep(10)


if __name__ == "__main__":
    main()
