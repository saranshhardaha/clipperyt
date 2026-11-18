"""Log Twitch chat to a TSV file — anonymous, read-only (no account, no token).

usage: python chat_twitch.py <channel_login> <out.tsv>

Each line: <epoch_ms>\t<author>\t<message>   (epoch_ms = time received here)
"""
import random
import socket
import sys
import time

HOST, PORT = "irc.chat.twitch.tv", 6667


def clean(s: str) -> str:
    return (s or "").replace("\t", " ").replace("\n", " ").strip()


def run(channel: str, out_path: str) -> None:
    channel = channel.lower().lstrip("#@")
    with open(out_path, "a", encoding="utf-8", buffering=1) as f:
        while True:
            try:
                sock = socket.create_connection((HOST, PORT), timeout=360)
                nick = f"justinfan{random.randint(10000, 99999)}"  # Twitch's anonymous login
                sock.sendall(f"PASS SCHMOOPIIE\r\nNICK {nick}\r\nJOIN #{channel}\r\n".encode())
                print(f"[chat-twitch] joined #{channel} as {nick}", flush=True)
                buf = b""
                while True:
                    data = sock.recv(65536)
                    if not data:
                        raise ConnectionError("server closed connection")
                    buf += data
                    *lines, buf = buf.split(b"\r\n")
                    for raw in lines:
                        line = raw.decode("utf-8", errors="replace")
                        if line.startswith("PING"):
                            sock.sendall(b"PONG :tmi.twitch.tv\r\n")
                        elif " PRIVMSG #" in line:
                            # :user!user@user.tmi.twitch.tv PRIVMSG #chan :message
                            prefix, _, rest = line.partition(" PRIVMSG #")
                            author = prefix.lstrip(":").split("!", 1)[0]
                            msg = rest.partition(" :")[2]
                            f.write(f"{int(time.time() * 1000)}\t{clean(author)}\t{clean(msg)}\n")
                        elif "RECONNECT" in line:
                            raise ConnectionError("server asked to reconnect")
            except KeyboardInterrupt:
                return
            except Exception as e:
                print(f"[chat-twitch] {e!r}; reconnecting in 5s", flush=True)
                time.sleep(5)


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2])
