# clipperyt — notes for Claude Code

Records a YouTube / Twitch / Kick live stream and its chat with no accounts or API keys, finds chat spikes, and turns each one into a captioned 9:16 Short. The streamer is an Indian GTA streamer, so chat and speech are Hindi/Hinglish plus English. Python 3.12 + stdlib where possible. Runs on macOS (dev Mac) and Linux (Proxmox LXC, NVIDIA GPU optional). Public repo: github.com/saranshhardaha/clipperyt.

## Files
- `clipper.py`: CLI and orchestration.
  - `job <id>`: run one dashboard job from `jobs/<id>/job.json`. The kind is `watch` (channels, loops), `url` (one live stream or past video) or `process` (re-clip a session). State, the stop file and the log are per job in `jobs/<id>/`.
  - `watch`: loop on the default channels in config.toml. `run <url>`: one stream. Both are terminal runs and use `HOME/state.json`.
  - `process <session>` / `spikes <session>` / `status` / `doctor` (tool check, run by the setup scripts)
  - `HOME` = `$CLIPPER_HOME` or the repo, and holds config.toml, sessions/, jobs/, logs/ and locks/. `ROOT` = the code, models/ and bin/.
  - Session folders are `YYYYMMDD-HHMM-<channel-slug>[-vod][-N]` and always unique (`new_session_dir`). Recording starts inside `<recordings>/<channel>/` (a session folder with a `.waiting` marker, hidden until meta.json exists), so no folder ever moves to another parent (NAS shares refuse that).
  - `processing_slot(n)`: flock on `locks/process-N.lock` so at most `[limits] max_parallel_processing` sessions are clipped at once. Recording is never limited.
  - PATH gets the venv bin, `bin/` and Homebrew's keg-only `ffmpeg-full` (plain Homebrew ffmpeg 8+ has no libass).
- `chat_youtube.py`: live chat via YouTube's chat-popout innertube endpoint (`youtubei/v1/live_chat/get_live_chat`), stdlib only. `actions_to_rows()` is shared with replay parsing.
- `chat_twitch.py`: anonymous IRC (justinfanNNNNN).
- `chat_kick.py`: Kick's public Pusher websocket. Unofficial and can break.
- `spikes.py`: pure `find_moments(chat, rec_start_ms, cfg)` with no I/O.
- `render.py`: cutting, whisper.cpp captions, ASS subs (captions + credit) and the render.
  - `render.format` takes 4 values: `9:16-blur` and `9:16-bars` keep the whole 16:9 frame, plus `16:9` and `9:16-crop`. Legacy `vertical`/`mode` keys still map. See `filter_graph()`.
  - With no libass, `render()` drops the subs and warns instead of failing.
  - Encoder `auto` = videotoolbox → nvenc → libx264.
  - `cut_wallclock()` cuts by wall-clock time through `timeline.tsv` (epoch_ms → file bytes, sampled every 1 s), so gaps from skipped Twitch ads or stalls don't desync clips.
    - `container_of()` picks how to slice. MPEG-TS is cut at a 188-byte packet. Fragmented MP4 (Twitch Enhanced Broadcasting / HEVC; streamlink still names it `.ts`) is cut at a `moof` boundary, with the init header (ftyp+moov) put in front.
    - If either fails, it falls back to a plain `-ss` cut of the whole file. Before this, fMP4 clips failed with "Invalid data found when processing input".
- `server.py`: stdlib dashboard and JSON API on :8787 (`CLIPPER_TOKEN`).
  - It starts each job detached (`start_new_session`, plus `caffeinate -is` on macOS), and jobs survive dashboard restarts. Jobs with `autostart` start on boot.
  - Routes: `/api/jobs`, `/api/sessions`, `/api/settings` and `/api/system`.
- Layout: `<recordings>/<channel-slug>/<session-id>/`, where the id is unique across all folders. Flat `<root>/<id>` sessions from before still work (`iter_sessions`, `find_session`), and the dashboard tags them UNSORTED; a move files them under their channel.
- Signals: `graceful_signals()` re-enables SIGINT. A dashboard started in the background passes SIG_IGN down, and then "Stop" silently did nothing.
- `update.sh`: git pull --ff-only, pip, reload the dashboard (systemd / LaunchAgent / manual), restart jobs that are only `waiting`, then `doctor`. `--upgrade-tools` runs brew upgrade on macOS. It's bash 3.2-safe.
- Errors: `H.safely()` turns any handler exception into a JSON 500 instead of a dropped connection (seen as `net::ERR_EMPTY_RESPONSE` on Linux when the app folder belonged to root). `write_problems()` feeds a warning banner.
- Recorder: `recorder_for()`. `auto` means YouTube → yt-dlp (live from now, muxed HLS into one growing `.ts`) and Twitch/Kick → streamlink. While waiting, `recorder_problem()` copies the recorder's error into the job log and state. For YouTube with streamlink, "No playable streams" plus `youtube_is_live()` switches to yt-dlp. After the recorder gives up, watch jobs pause `retry_seconds`.
- YouTube bot check: `youtube_cookies` (a Netscape cookies.txt) or `cookies_from_browser`. These go to every yt-dlp call (`YTDLP_AUTH`, passed to chat_youtube via env `CLIPPER_YTDLP_AUTH`) and to streamlink as `--http-cookie`. `probe()` turns the bot-check stderr into `BOT_HELP`.
- Storage: `[storage] sessions_dir` is where new recordings go (`clipper.SESSIONS`, applied by `load_cfg()`). `also_scan` lists older folders, auto-filled when the folder changes.
  - `session_roots()` / `find_session(sid)` search every folder. Always look sessions up by id through them; never use `SESSIONS / sid`.
  - `check_dir(p, create)`: at record time a custom folder must already exist, so an unmounted drive never gets written to the internal disk. Watch jobs retry on `StorageUnavailable`.
  - `mover.py` moves sessions in a server thread: rename on the same drive, otherwise copy to `.moving-<id>`, verify sizes, rename, then delete the source.
- `dashboard.html`: the single-page UI, vanilla JS with no CDN and system fonts only (it must work offline on the LAN). Tabs: Jobs, Clips, Sessions, Settings, System.
  - Design is a control room. Status colours carry meaning: tally red = on air (only for recording), standby amber = waiting/queued, cue teal = clipping and clip requests. Primary buttons are neutral, there are no ALL-CAPS tags, and status is shown as a dot plus words (`SAY` map).
  - Jobs never move: fixed sections by type (Watching channels / One links / Fix and re-clip / Terminal runs), A–Z by name. The status chips only count and filter (saved in localStorage). Each job card has a tally strip by status, and a live mono timecode that ticks every second.
- `settings_schema.py`: `SCHEMA` lists every editable config key with its type, label and help. `dump_toml()` is a TOML writer that keeps unknown keys.
- `stats.py`: incremental chat readers, live and session stats, and the tool and system checks.
- `config.example.toml` is tracked. `config.toml` is gitignored (it may hold `twitch_oauth_token`) and is auto-created from the example. `[stream]` holds only the defaults; dashboard jobs carry their own channels, and everything else is global (one config for all jobs, as the user chose).
- `setup.sh` (brew, plus `ffmpeg-full` if there's no libass), `setup_linux.sh` (apt, Python ≥3.11 or the tomli backport, root without sudo works, builds whisper.cpp into bin/). Both take `--service`.
  - `deploy/install_service.sh` sets up systemd (Linux) or a LaunchAgent (macOS) and generates `.dashboard_token`.
- Tests: `python -m unittest discover tests` (unit, ~25 s) plus offline e2e with fake streamlink, yt-dlp and chat.
  - `python tests/e2e/run_e2e.py` (~4 min): 2 parallel watch jobs, a VOD, a live One-link job, a dashboard restart mid-recording, slot serialization.
  - `python tests/e2e/run_hourly.py` (~2 min): clipping while live, then the end-of-stream pass without duplicates.

- Clip requests: `spikes.find_clip_requests()` fires when ≥ `clip_min_users` different chatters match `clip_phrases` (whole words) within `clip_window_seconds`. `merge_requests()` folds a request into an overlapping spike (score += `clip_boost` × users, `kind="spike+request"`) or keeps it on its own. `find_all()` = spikes + requests, used everywhere.
- Clips: each clip folder has `moment.json` (the dashboard prefers it over `spikes.json` order). `_add_clips()` cuts only moments without a clip near their start, appends to clips.md and spikes.json, and backs the "Add clip-request clips" button (`requests` job mode / CLI) and the windowed passes.
- Fix/Redo/Delete: `make(n, m, folder=…)` redoes a clip in place and keeps its start. `moment.json` is written *before* cutting, so failed clips stay fixable; older clips fall back to `spikes.json` or the folder time (`moment_for_clip`). `failed_clips()` lists folders without a final video. `delete_clip()` also records the start in `.deleted_clips`, which `_add_clips()` skips. The job mode is `redo` with `clips` (none = all failed).
- Live passes: while recording, every `process_every_minutes` a detached `clipper.py window <session> <since> <until>` clips `[since, until)`, leaving a margin of merge + length seconds. `.clipped_until` in the session records progress, and the end-of-stream `cmd_process(after_live=True)` only clips from there. `max_clips` applies per window.
- Drive at boot: `wait_for_storage()` (`[storage] wait_for_drive_seconds`) is used by server autostart (in a thread) and `cmd_run`. The systemd unit waits for `remote-fs.target`.
- `/api/clips` and the Clips tab list every clip across sessions.

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
- Built and tested with fakes: recording, Twitch/YouTube/Kick chat, spikes, wall-clock cutting, captions/ASS, all 4 render formats, multi-job dashboard, settings form, stats, `run <url>`, YouTube replay via yt-dlp.
- **Real-stream check, in progress:** `run https://youtu.be/qSyo5d-dVzA` (session 20261119-0120) recorded with real YouTube live chat flowing into `chat_youtube.tsv`, so the stdlib chat reader works live.
- The earlier real runs (20261119-0112 / -0115) hit `chat-downloader` "Unable to parse initial video data". That's fixed in 2bb2777.
- The dev Mac's Homebrew ffmpeg 9 has **no libass**. Run `setup.sh` to get `ffmpeg-full`; until then clips render without captions or credit.
- Next steps the user discussed:
  1. ~~Clip-request trigger~~ built 2026-11-19, with an "Add clip-request clips" button.
  2. Tune `multiplier` / `stream_delay_seconds` after real runs.
- Twitch VOD chat still uses `chat-downloader` (best effort, may be broken too).
- Verified 2026-11-19: unit + e2e pass on macOS (arm64), Debian 12 amd64 and arm64 (Docker), and Python 3.10 + tomli imports. On arm64 + gcc 12 the native whisper.cpp build fails, so setup_linux.sh falls back to `GGML_NATIVE=OFF`.
- `spikes.py`: `min_hist = min(base_n, 60/b)`. Before, a `baseline_seconds` under 60 could never fire.
