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

- **Adding a job:** press **New job** and paste anything: a channel (`youtube.com/@name`, `youtube.com/channel/UC…`, `twitch.tv/name`, `kick.com/name`), a stream or video link (`youtube.com/watch?v=…`, `youtu.be/…`, `/live/…`, `/shorts/…`, `twitch.tv/videos/…`), or just a name.
  - The box says what you'll get before you add it.
  - Channels become chips, so you can add the same streamer's Twitch and Kick too and their chat is combined. The first chip records the video; click **Use for video** on another to switch.
  - A bare name asks which platform it's on.
  - Things that can't be clipped are explained, for example Twitch clips, and past Kick videos because Kick doesn't keep their chat.
  - **Edit** on a watch job opens the same box, prefilled, so you can change its channels, which one records the video, or its name. A running job picks up the change from its next stream.
- **Jobs:** each job runs on its own, and as many as you like can run at once.
  - **Watch channels:** fill in the YouTube, Twitch and Kick names (any of them) and pick which one to record video from. The job waits for the stream to go live, records it and its chat, makes the clips, then waits for the next stream.
  - **One link:** a live or upcoming stream gets recorded until it ends, then clipped. A finished YouTube or Twitch video gets downloaded with its chat replay, then clipped.
  - Each job card shows live stats: time recorded, GB, bitrate, chat per minute with a 30-minute sparkline, messages per platform, unique chatters, and moments found so far. It also shows clipping progress and the job's log.
  - Tick **autostart** and the job starts again whenever the dashboard starts, for example after a reboot.
  - **Stop** still clips what was recorded. **Kill** stops at once, without clipping.
  - Finished one-link and fix/re-clip jobs leave the list by themselves. The session keeps the clips, and the job's log is added to the session's `job.log`. Failed ones stay until you press **Clear failed**.
- **Review:** your morning inbox. The dashboard opens here when there are new clips and nothing is on air, and the tab shows how many are new.
  - **One by one:** a big vertical player. Pick an AI title or type one, copy the title and description, then **Skip**, **Download** or **Keep**. **Mark posted** works on any clip, in the grid and on the session page too.
  - Keys on a computer: ← → move, K keep, S skip, D download, P posted, Space play. On a phone, swipe left or right on the video. Every change has **Undo**.
  - Pressing **Keep** fetches AI titles if you've added an OpenRouter key, so you don't pay for clips you skip.
  - The New, Kept, Posted and Skipped filters are saved per clip (`review.json`), so they're the same on every device. **Grid** shows all clips at once.
- **Getting started:** a short checklist on the Jobs page until the first channel, the tools and the first recording are in place. Each step ticks itself off, and you can hide it.
- **Confirmations** use the dashboard's own dialog: the dangerous button is red, and Cancel is the default. Buttons show a spinner while their action runs, so nothing gets started twice.
- **Needs attention:** one strip at the top of every tab, listing failed jobs, YouTube blocking a recorder, failed clips, a missing or full drive, missing tools and folders the dashboard can't write to. Each item has the button that fixes it (Fix clips, Upload cookies, How to fix…).
- **Sessions:** every recording with its length, chat, chatters, spikes, clips and size.
  - Open a session to play or download each clip, its raw cut and its captions, or to list spikes with the current settings.
  - **Timeline:** the whole stream's chat as bars, with moments marked (teal = clip request) and clips as bands (dashed = made by hand, red = failed).
    - Click a clip to jump to it and play it.
    - Click anywhere else to **make a clip there**. The confirmation says where it starts and how long it runs, and it's cut with the current settings.
    - This is for moments detection missed, for example during the first minute, while chat is still building its baseline.
    - A clip made by hand at a spot you deleted brings it back. Clicking a spot that already has a clip doesn't duplicate it.
  - The main actions sit on the page (Fix failed clips, Re-make clips, Add clip-request clips). The rarer ones are under **More**: list moments, move, delete recording, delete session.
  - **Re-make clips** redoes the session with the current settings.
    - A moment whose clip start is within half a clip length of an existing clip **overwrites that clip**. It keeps the same number, and the folder is renamed if the start moved.
    - New moments become new clips.
    - Clips that no moment matches any more are kept. Deleted clips stay deleted.
    - Length, delay, lead, output shape and captions all follow the new settings. **Redo** on a single clip does the same for that clip.
  - **Add clip-request clips** finds moments where 3 or more chatters asked for a clip and cuts only the ones without a clip yet. Existing clips stay.
  - **Fix N failed clips**, or **Fix** on a failed clip, cuts, captions and renders it again in place. **Redo** does the same for a working clip, for example after editing `speech.srt` or changing settings.
  - **Delete** (on the session page and in Review's grid) moves a clip to the session's `.trash` folder and drops its clips.md row.
    - The toast has **Undo**.
    - Trashed clips are deleted for good after 7 days.
    - While a clip is deleted, "Add clip-request clips" doesn't bring it back.
  - CLI: `clipper.py redo <session> [clip…]`. With no clip names it redoes every failed clip.
  - **Free disk** deletes the full recording but keeps the clips.
- **Settings:** grouped into **Detection · Clips & captions · Output · Titles (AI) · Channels & recording · Storage & limits · Config file** (a dropdown on a phone).
  - Each section shows the settings you actually change. Technical ones (bucket and baseline seconds, whisper paths, encoder, quality…) are under **Advanced** inside it.
  - Settings that differ from the default get a dot and a **Reset**, and so does their section in the list.
  - **Detection preview:** while you edit detection settings, it shows what they'd find on a session you pick, for example "2 clips with these settings (+1 vs saved)", with the times. Nothing is saved or re-made until you save.
  - **Config file** edits `config.toml` directly. Saved settings apply to each job's next stream or clip run.
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
- **After a reboot:** autostart jobs and One-link jobs wait up to `wait_for_drive_seconds` (default 300) for the recordings folder, because NAS and USB drives often mount after the dashboard starts. The systemd service also waits for mounts.
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

## Captions

Captions are timed to the words, not to whisper's long blocks, which used to start during the silence before someone spoke.
- whisper.cpp gives per-word times (`--dtw`, flash attention off).
- The app builds short 2–5 word captions, each shown from its first word to just after its last.
- With a model or whisper build that can't do word times, each block is trimmed so it doesn't start inside silence.

## Titles for Shorts (AI via OpenRouter)

**Titles** on a clip, in its session or the Clips tab, asks an AI model through OpenRouter for 5 title options and hashtags, using the transcript and what chat said.
- The default model is `x-ai/grok-4.7` (Grok). Any OpenRouter model works, and Settings has a picker with prices.
- You also get a description with the streamer credit and stream link. The app writes that part itself, not the model.
- Each line has a Copy button.
- Add your key from openrouter.ai/keys in **Settings → Titles (AI)**.
- It only runs when you press the button, so recording and clipping never call it and overnight runs stay free.
- Results and their cost are saved with the clip. **Suggest again** asks for new ones.

## Post to YouTube (one click, or scheduled)

Clips can go straight to your channel as Shorts. Nothing is posted unless you press a button.
- **Post now** uploads the clip with the title you picked (or the first AI suggestion), the description and #Shorts.
- **Schedule for …** uploads it right away as private and YouTube makes it public at the next free slot. Slots come from **Settings → YouTube**: daily window (default 10:00–22:00), hours between posts and posts per day, in your time zone. Your machine can be off when it goes public.
- **Unschedule** deletes the private video from YouTube and puts the clip back in Kept. Scheduled clips are listed in Settings → YouTube.
- A failed upload shows in Needs attention.

One-time setup (about 10 minutes, steps also shown in Settings → YouTube):
1. In console.cloud.google.com create a project and enable **YouTube Data API v3**.
2. OAuth consent screen: External, add your Google account as a test user, then **Publish app** ("In production"). In Testing, Google ends the sign-in after 7 days.
3. Credentials → OAuth client ID of type **TVs and Limited Input devices**. Paste its ID and secret in Settings → YouTube and save.
4. Press **Connect YouTube**, open google.com/device on any phone and enter the code. Google warns the app isn't verified; that's expected for your own app.

The sign-in is stored in `youtube-token.json` next to config.toml (owner-only, gitignored). YouTube limits how many videos a channel can upload per day.

## Clip requests

A moment also fires when at least 3 *different* chatters type a clip phrase within 20 s, even if chat isn't louder than usual.
- Phrases: `clip`, `clip it`, `clip that`, `clipped`, `clip karo`, `!clip`…
- Each chatter counts once. Matches are whole words, so "eclipse" and "clips" don't count.
- If a chat spike happens at the same moment, the request boosts that spike's score. Otherwise it becomes a moment of its own.
- Either way the clip is tagged **CLIP REQUEST ×N** in the dashboard and **clip requested ×N** in clips.md.
- Settings → Spike detection has the switch, phrases, number of chatters, time window and boost.
- `clipper.py requests <session>` does the same as the **Add clip-request clips** button.

## Long streams: clipping while live

Every `process_every_minutes` (default 60) during a recording, a background pass clips the newest part. Recording keeps going while it runs.
- When the stream ends, only the part not clipped yet is processed, so nothing is clipped twice.
- `max_clips` applies to each pass, so a 24-hour stream isn't limited to 25 clips in total.
- Set it to 0 to clip only at the end.

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
- **YouTube asks to sign in ("confirm you're not a bot"):** YouTube does this to some IPs, sometimes for hours.
  - Symptoms: a **One link** job fails with that message, or a watch job never goes live while the channel is live (streamlink then reports "No playable streams").
  - Fix:
    1. Log in to YouTube in a browser. A spare account is best.
    2. Export the cookies as `cookies.txt` with a "cookies.txt" browser extension.
    3. Upload it in **Settings → Stream defaults → YouTube cookies → Upload cookies.txt**. The dashboard stores it as an owner-only file and switches it on. You can also put the file in the app folder and set the path yourself.
  - yt-dlp and streamlink both use it. `cookies*.txt` is gitignored. The file is your login, so keep it private.
- **A job says "waiting" although the stream is live:** check the job card or log for a `recorder:` line. That's the recorder's own error, for example YouTube's "No playable streams" or "not a bot".
  - With `recorder = "auto"` (the default), YouTube records with yt-dlp, which works with cookies, and Twitch and Kick use streamlink.
  - Older configs say `"streamlink"`. For YouTube the job then switches to yt-dlp by itself when streamlink is blocked but yt-dlp sees the stream live.
  - To check by hand on the server:
    ```bash
    .venv/bin/yt-dlp --cookies youtube-cookies.txt -F "<url>"
    ```
    That should list formats. If it doesn't, run `./update.sh` (installs deno) and re-export your cookies.
- **Creating a job fails on Linux (`net::ERR_EMPTY_RESPONSE`, or "PermissionError"):** the dashboard's user can't write to the app folder. This usually means setup ran with `sudo`. Fix it with `sudo chown -R <user> /path/to/clipperyt`. The current `setup_linux.sh` and `install_service.sh` do this for you, and the dashboard warns about it on the Jobs and System tabs.
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
