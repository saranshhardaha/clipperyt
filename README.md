# clipperyt

Records a YouTube, Twitch or Kick live stream and its chat overnight, finds the moments where chat blows up, and cuts each one into a captioned 9:16 Short. You don't need an account or an API key, and it's free to run.

```
streamlink ──► stream.ts ─┐
chat (no login) ► chat.tsv ┴► spikes.py ► ffmpeg cut ► whisper.cpp (Hindi) ► 1080x1920 short.mp4
```

## Setup (once)

```bash
cd ~/app/clipperyt
./setup.sh            # macOS: brew ffmpeg(+libass) streamlink yt-dlp whisper-cpp · venv · ~550 MB model
./setup_linux.sh      # Debian/Ubuntu/Proxmox LXC: apt · Python ≥3.11 · builds whisper.cpp (CUDA if nvcc) · model
```

Add `--service` to either one to also start the dashboard automatically (systemd on Linux, a LaunchAgent on macOS). Both end with `clipper.py doctor`, which checks every tool. You can run it again any time.

## Run: the dashboard

```bash
.venv/bin/python server.py        # http://<machine-ip>:8787
```

- **Jobs:** each job runs on its own, and as many as you like can run at once.
  - **Watch channels:** fill in the YouTube, Twitch and Kick names (any of them) and pick which one to record video from. The job waits for the stream to go live, records it and its chat, makes the clips, then waits for the next stream.
  - **One link:** a live or upcoming stream gets recorded until it ends, then clipped. A finished YouTube or Twitch video gets downloaded with its chat replay, then clipped.
  - Each job card shows live stats: time recorded, GB, bitrate, chat per minute with a 30-minute sparkline, messages per platform, unique chatters, and moments found so far. It also shows clipping progress and the job's log.
  - Tick **autostart** and the job starts again whenever the dashboard starts, for example after a reboot.
  - **Stop** still clips what was recorded. **Kill** stops at once, without clipping.
- **Sessions:** every recording with its length, chat, chatters, spikes, clips and size.
  - Open a session to play or download each clip, its raw cut and its captions, or to list spikes with the current settings.
  - **Re-make clips** after you change the settings.
  - **Free disk** deletes the full recording but keeps the clips.
- **Settings:** every option in `config.toml` as a form, with help text. **Advanced** is the raw TOML editor. Saved settings apply to each job's next stream or clip run.
- **System:** free disk, totals, CPU load, the encoder in use, and which tools are installed (ffmpeg plus libass, streamlink, yt-dlp, whisper, the model).

Clipping (cut, then whisper, then render) is heavy, so only `Clip jobs at once` streams get clipped at the same time (Settings → Limits, default 1). Others wait in line as `queued`. Recording is never limited.

Jobs run as separate processes. Restarting the dashboard never cuts a recording, and a job that crashes doesn't affect the others.

Protect the dashboard with a token: `CLIPPER_TOKEN=some-long-secret .venv/bin/python server.py`, then open `/?token=some-long-secret` once (a cookie keeps you logged in). `install_service.sh` generates a token for you. Without a token, anyone on the network can control it, so keep it on your LAN or Tailscale and never port-forward it.

### Where sessions are stored

Each recording goes in a folder per channel:

```
<recordings folder>/
  somegamer/
    20261119-2300-somegamer/        stream.ts, chat_*.tsv, clips/, clips.md
    20261120-2310-somegamer/
  othergamer/
    20261119-2130-othergamer-vod/
```

Sessions from before channel folders existed are tagged **UNSORTED**. **Sort into channel folders** (Sessions page) files them, and on the same drive that's instant. The Sessions page can also be filtered by channel.

### Recordings on another drive (Settings → Storage)

Set **Recordings folder** to a folder on the bigger drive, e.g. `/Volumes/BigDrive/clipperyt` (Mac) or `/mnt/media/clipperyt` (Linux). New recordings and their clips are written there directly.

- **Moving old sessions:** sessions already in the old folder stay listed and are marked **OLD FOLDER**. Move them with **Move N to recordings folder** (on the Sessions page), or with **Move to recordings folder** inside a session.
  - On another drive, files are copied, checked, and only then deleted from the old place. On the same drive the move is instant.
  - Sessions being recorded or clipped are skipped.
- **Drive not connected:** jobs never fall back to the internal disk. A watch job shows "is the drive connected?" and retries, and starts recording on the drive as soon as it's back. The System tab shows each drive's free space and flags a missing one.
- **Linux mount points:** create the folder on the mounted drive once, from Settings or with `mkdir`. When the drive isn't mounted, that folder is missing, so the app knows not to record there.

To move everything (config, jobs and sessions), set `CLIPPER_HOME=/some/disk` instead.

### Output shape (Settings → Render)

| `format` | size | what you get |
|---|---|---|
| `9:16-blur` (default) | 1080x1920 | the **whole** 16:9 frame across the middle, with a blurred copy filling the top and bottom |
| `9:16-bars` | 1080x1920 | the **whole** 16:9 frame across the middle, with black top and bottom |
| `16:9` | 1920x1080 | the original shape |
| `9:16-crop` | 1080x1920 | a centre crop that fills the screen but cuts off the sides |

In both 9:16 modes, captions go just under the picture and the credit line goes in the top band.

## Update

```bash
./update.sh                  # git pull → python packages → reload the dashboard → restart idle jobs
./update.sh --upgrade-tools  # macOS: also brew upgrade streamlink + yt-dlp (do this when a site stops working)
./update.sh --keep-jobs      # don't restart waiting jobs
```

It never interrupts a recording:
- The dashboard is reloaded through systemd, the LaunchAgent, or a manually started `server.py`.
- Jobs that are only waiting for a stream are restarted on the new code.
- Jobs that are recording or clipping keep running. Restart them from the dashboard after the stream.

It stops without changing anything if you have local edits to tracked files.

## Run: from a terminal

```bash
.venv/bin/python clipper.py run "https://www.youtube.com/watch?v=VIDEO_ID"   # one stream or video
./run.sh                                  # watch the default channels in config.toml, loop
.venv/bin/python clipper.py status        # stopped / waiting / live / processing
.venv/bin/python clipper.py doctor        # check tools
```
A terminal run shows up in the dashboard too, as a "terminal run" job you can stop.

Keep the Mac plugged in with the lid open. Each job runs under `caffeinate` so the Mac doesn't sleep, but closing the lid still puts it to sleep.

When you wake up, open `sessions/<date>/clips.md`. It lists every clip with its time in the stream, its spike score and what chat was saying. Each clip folder holds:

| file | what |
|---|---|
| `short.mp4` / `captioned.mp4` | 9:16 or 16:9 (see Output shape) with captions and a credit line, ready to post |
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
- **YouTube chat errors:** update yt-dlp (`brew upgrade yt-dlp`). Live chat reads YouTube's popout endpoint directly; if YouTube changes it, fix `bootstrap()` in `chat_youtube.py`.
- **No captions or credit line:** your ffmpeg lacks libass (Homebrew's plain `ffmpeg` 8+ doesn't include it). `setup.sh` installs `ffmpeg-full`, which clipperyt uses automatically. The System tab and `clipper.py doctor` both show whether libass is there. Until it is, clips still render, just without captions.
- **Logs:** each job's log is in the dashboard (or `jobs/<id>/job.log`). Inside each session there are also `recorder.log` and `chat_<platform>.log`.

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
