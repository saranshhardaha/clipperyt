# clipperyt — notes for Claude Code

Records a YouTube / Twitch / Kick live stream and its chat with no accounts or API keys, finds chat spikes, and turns each one into a captioned 9:16 Short. The streamer is an Indian GTA streamer, so chat and speech are Hindi/Hinglish plus English. Python 3.12 + stdlib where possible. Runs on macOS (dev Mac) and Linux (Proxmox LXC, NVIDIA GPU optional). Public repo: github.com/saranshhardaha/clipperyt.

## Files
- `clipper.py`: CLI and orchestration.
  - `job <id>`: run one dashboard job from `jobs/<id>/job.json`. The kind is `watch` (channels, loops), `url` (one live stream or past video) or `process` (re-clip a session). State, the stop file and the log are per job in `jobs/<id>/`.
  - `watch`: loop on the default channels in config.toml. `run <url>`: one stream. Both are terminal runs and use `HOME/state.json`.
  - `process <session>` / `spikes <session>` / `status` / `doctor` (tool check, run by the setup scripts)
  - `HOME` = `$CLIPPER_HOME` or the repo, and holds config.toml, sessions/, jobs/, logs/ and locks/. `ROOT` = the code, models/ and bin/.
  - Session folders are `YYYYMMDD-HHMM-<channel-slug>[-vod][-N]` and always unique (`new_session_dir`). Recording waits in `sessions/_waiting-<job>`, which is renamed onto the reserved empty folder.
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
- `server.py`: stdlib dashboard and JSON API on :8787 (`CLIPPER_TOKEN`).
  - It starts each job detached (`start_new_session`, plus `caffeinate -is` on macOS), and jobs survive dashboard restarts. Jobs with `autostart` start on boot.
  - Routes: `/api/jobs`, `/api/sessions`, `/api/settings` and `/api/system`.
- `dashboard.html`: the single-page UI, vanilla JS with no CDN. Tabs: Jobs, Sessions, Settings, System.
- `settings_schema.py`: `SCHEMA` lists every editable config key with its type, label and help. `dump_toml()` is a TOML writer that keeps unknown keys.
- `stats.py`: incremental chat readers, live and session stats, and the tool and system checks.
- `config.example.toml` is tracked. `config.toml` is gitignored (it may hold `twitch_oauth_token`) and is auto-created from the example. `[stream]` holds only the defaults; dashboard jobs carry their own channels, and everything else is global (one config for all jobs, as the user chose).
- `setup.sh` (brew, plus `ffmpeg-full` if there's no libass), `setup_linux.sh` (apt, Python ≥3.11 or the tomli backport, root without sudo works, builds whisper.cpp into bin/). Both take `--service`.
  - `deploy/install_service.sh` sets up systemd (Linux) or a LaunchAgent (macOS) and generates `.dashboard_token`.
- Tests: `python -m unittest discover tests` (unit, ~10 s) and `python tests/e2e/run_e2e.py` (offline, ~3 min: 2 parallel live jobs, 1 VOD, a dashboard restart mid-recording, slot serialization, using fake streamlink, yt-dlp and chat).

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
  1. **Clip-request trigger:** a moment fires when ≥3 *distinct* users say a clip phrase ("clip", "clip it", "clip that", "clipped", "clip karo", "!clip") within 20 s, even without a volume spike. Count each user once, boost the score, and mark it in clips.md. Configurable. Proposed but not built yet.
  2. Tune `multiplier` / `stream_delay_seconds` after real runs.
- Twitch VOD chat still uses `chat-downloader` (best effort, may be broken too).
- Verified 2026-11-19: unit + e2e pass on macOS (arm64), Debian 12 amd64 and arm64 (Docker), and Python 3.10 + tomli imports. On arm64 + gcc 12 the native whisper.cpp build fails, so setup_linux.sh falls back to `GGML_NATIVE=OFF`.
- `spikes.py`: `min_hist = min(base_n, 60/b)`. Before, a `baseline_seconds` under 60 could never fire.
