# clipperyt

Records a YouTube, Twitch or Kick live stream and its chat overnight, finds the moments where chat blows up, and cuts each one into a captioned 9:16 Short. You don't need an account or an API key, and it's free to run.

```
streamlink ──► stream.ts ─┐
chat (no login) ► chat.tsv ┴► spikes.py ► ffmpeg cut ► whisper.cpp (Hindi) ► 1080x1920 short.mp4
```

## Setup (once, macOS)

```bash
cd ~/app/clipperyt
./setup.sh          # brew: ffmpeg streamlink yt-dlp whisper-cpp · python venv · ~550 MB whisper model
```

Edit `config.toml` (setup creates it from `config.example.toml`; it's gitignored so tokens never get committed):

```toml
[stream]
record_from = "youtube"   # which platform's video to record

[stream.channels]         # fill in every platform they're live on
youtube = "SomeGamer"     # youtube.com/@SomeGamer
twitch  = "somegamer"     # twitch.tv/somegamer   ("" if not on Twitch)
kick    = ""
```

Simulcasting to YouTube and Twitch at once? Fill in both. The video gets recorded once from `record_from`, and chat from both platforms is combined, so you get more chat to detect spikes from.

Captions use `language = "auto"`, so whisper detects Hindi or English for each clip. You can force it with `"hi"` or `"en"`.

## Run

There are three ways to run it:

```bash
./run.sh                          # in a terminal: waits for live → records → clips → waits again
.venv/bin/python clipper.py status   # quick check: stopped / waiting / live (minutes, GB, chat counts) / processing
.venv/bin/python server.py           # web dashboard on http://<machine-ip>:8787
```

### Dashboard (`server.py`)

From any device on your network you can:
- see the live status (minutes recorded, GB on disk, chat messages per platform, free disk space) and the log
- **Start watching**, **Stop** (it still clips what was recorded) or **Kill** (stops immediately, no clipping)
- browse sessions, play and download each Short or its raw cut, list spikes, re-make clips
- edit `config.toml`. Invalid TOML is rejected, and a `.bak` copy is kept.

It uses only the Python standard library. The watcher runs as its own process, so restarting the dashboard never cuts off a recording.

Protect it with a token: `CLIPPER_TOKEN=some-long-secret .venv/bin/python server.py`, then open `/?token=some-long-secret` once (a cookie keeps you logged in). Without a token, anyone on the network can control it, so keep it on your LAN or Tailscale and never port-forward it.

### On a Linux server (e.g. a Proxmox LXC)

```bash
./setup_linux.sh    # apt deps, venv (streamlink + yt-dlp), builds whisper.cpp (with CUDA if nvcc exists), model
```

Then follow `deploy/README-systemd.md` to start the dashboard on boot. On an NVIDIA box, captions and rendering use the GPU automatically (`encoder = "auto"`).

Keep the Mac plugged in with the lid open. `caffeinate` stops it sleeping, but closing the lid still puts it to sleep.

When you wake up, open `sessions/<date>/clips.md`. It lists every clip with its time in the stream, its spike score and what chat was saying. Each clip folder holds:

| file | what |
|---|---|
| `short.mp4` | 1080x1920 with captions and a credit line, ready to post |
| `raw.mp4` | the original 45 s cut with no edits |
| `speech.srt` | the whisper transcript, to fix typos and re-render |
| `chat.txt` | the most repeated chat messages at that moment |

## Tuning

```bash
.venv/bin/python clipper.py spikes 20261118-2300    # list spikes only (fast)
.venv/bin/python clipper.py process 20261118-2300   # rebuild all clips with the new config
```

| Problem | Change |
|---|---|
| Too many boring clips | raise `multiplier` (3 → 4) or `min_messages` |
| Missed a moment | lower `multiplier` to 2.5 |
| Clip starts after the action | raise `stream_delay_seconds` or `lead_seconds` |
| Captions in Devanagari but you want English | `translate = true` plus a non-turbo model (`ggml-medium.bin`) |
| Captions wrong or empty | GTA music and gunfire hurt accuracy. Edit `speech.srt` and re-run `process` |

## How the timing works

- **t = 0** is the moment the first video bytes land on disk.
- Every chat message gets stamped with the Mac's clock when it arrives, so chat and video share one clock.
- Viewers watch about 8 s behind live and react after the moment happens, so each clip starts at **spike − delay − 30 s**.

## When something breaks

- **Twitch ads:** Twitch inserts ads into the stream itself for viewers who aren't logged in. `twitch_disable_ads = true` (the default) keeps them out of the recording, but the gameplay during the ad is still missing. If an account with Turbo or a sub to the channel is available, put its token in `twitch_oauth_token` and there won't be any ads at all.
- **YouTube ads** are shown by the viewer's player and aren't part of the stream, so recordings don't contain them.
- **Gaps in the recording** (skipped ads, network stalls) don't break clip timing: `timeline.tsv` logs file size every second, and clips get cut by real time.
- **Kick recording fails:** run `streamlink --plugins` to check for `kick`. If it's missing, set `recorder = "yt-dlp"`.
- **Kick chat says "couldn't read chatroom id":** Kick's Cloudflare blocked the lookup. Open `https://kick.com/api/v2/channels/<slug>` in a browser, copy `"chatroom":{"id":…}` into `kick_chatroom_id`.
- **Kick chat connects but stays empty:** Kick may have moved off Pusher. Update `PUSHER_URL` in `chat_kick.py`.
- **YouTube chat errors:** `.venv/bin/pip install -U chat-downloader yt-dlp`.
- **`subtitles`/`ass` filter missing:** your ffmpeg lacks libass. `setup.sh` prints the fix.
- **Logs:** check `logs/`, plus `recorder.log` and `chat.log` inside each session.

## Cost

| | |
|---|---|
| streamlink, yt-dlp, ffmpeg, whisper.cpp, chat readers | **free** |
| APIs or accounts | **none** |
| Runtime LLM calls | **none**, so a full night costs ₹0 |
| Electricity (MacBook Air, ~8 h) | a few rupees |
| Claude Code | only for changing the code (your existing plan) |

## Disk

A 1080p60 GTA stream takes about 3–6 GB an hour. Delete old `sessions/*/stream.ts` once you've picked your clips.

## The one rule

Credit and link the streamer every time, and ask first. A credit line isn't permission, and a stream belongs to its streamer.
