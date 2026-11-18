# clipperyt — notes for Claude Code

Records a YouTube / Twitch / Kick live stream and its chat with no accounts or API keys, finds chat spikes, and turns each one into a captioned 9:16 Short. The streamer is an Indian GTA streamer, so chat and speech are Hindi/Hinglish plus English. Python 3.12 + stdlib where possible. Runs on macOS (dev Mac) and Linux (Proxmox LXC, NVIDIA GPU optional). Public repo: github.com/saranshhardaha/clipperyt.

## Files
- `clipper.py`: CLI and orchestration.
  - `watch`: loop on the channels in config.toml
  - `run <url>`: one stream. If live, record and clip. If finished, download the video + chat replay, then clip.
  - `process <session>` / `spikes <session>` / `status`
- `chat_youtube.py`: live chat via YouTube's chat-popout innertube endpoint (`youtubei/v1/live_chat/get_live_chat`), stdlib only. `actions_to_rows()` is shared with replay parsing.
- `chat_twitch.py`: anonymous IRC (justinfanNNNNN).
- `chat_kick.py`: Kick's public Pusher websocket. Unofficial and can break.
- `spikes.py`: pure `find_moments(chat, rec_start_ms, cfg)` with no I/O.
- `render.py`: cutting, whisper.cpp captions, ASS subs (captions + credit), and the 1080x1920 render. Encoder `auto` = videotoolbox → nvenc → libx264.
  - `cut_wallclock()` cuts by wall-clock time through `timeline.tsv` (epoch_ms → file bytes, sampled every 1 s), so gaps from skipped Twitch ads or stalls don't desync clips.
- `server.py`: stdlib dashboard + JSON API on :8787 (`CLIPPER_TOKEN`). Starts the watcher detached and talks to it only through `state.json` and `.stop`.
- `config.example.toml` is tracked. `config.toml` is gitignored (it may hold `twitch_oauth_token`) and is auto-created from the example.
- `setup.sh` (brew), `setup_linux.sh` (apt, builds whisper.cpp into bin/), `deploy/` (systemd).

## Timing rules (don't break)
- Live sessions: `meta.rec_start_ms` = the moment the first video bytes hit disk, by local clock.
  - Chat lines are `epoch_ms<TAB>author<TAB>text`, with real epoch times. YouTube uses YouTube's `timestampUsec`; Twitch and Kick use local receive time.
  - Clip start = spike onset − `stream_delay_seconds` − `lead_seconds`.
- Past-stream (`-vod`) sessions: `rec_start_ms = 0`, and chat "epoch_ms" holds **ms offset from video start**. No timeline.tsv, so cuts use plain `-ss`.
- Each platform writes `chat_<platform>.tsv`, and spikes merge every `chat*.tsv` (simulcast).

## Conventions
- Don't call any LLM at runtime. Overnight runs must be free and offline-capable.
- Keep the streamer credit (burned-in line + clips.md links).
- Test offline with fakes: a synthetic `stream.ts` (ffmpeg testsrc2), synthetic chat TSV + meta.json, and a fake `streamlink`/`yt-dlp` on PATH. YouTube can't be reached from the sandbox that wrote this.

## Status (2026-11-19)
- Built and tested with fakes: recording, Twitch/YouTube/Kick chat, spikes, wall-clock cutting, captions/ASS, vertical render, dashboard, `run <url>`, YouTube replay via yt-dlp.
- **Not yet verified on real streams.** The first real run (`run https://www.youtube.com/watch?v=VZqAPdUvqnQ`, sessions 20261119-0112 / -0115) hit `chat-downloader` "Unable to parse initial video data". That's fixed in commit 2bb2777 by the stdlib YouTube chat reader, which still needs a real-stream check.
- Next steps the user discussed:
  1. **Clip-request trigger:** a moment fires when ≥3 *distinct* users say a clip phrase ("clip", "clip it", "clip that", "clipped", "clip karo", "!clip") within 20 s, even without a volume spike. Count each user once, boost the score, and mark it in clips.md. Configurable. Proposed but not built yet.
  2. Tune `multiplier` / `stream_delay_seconds` after real runs.
- Twitch VOD chat still uses `chat-downloader` (best effort, may be broken too).
