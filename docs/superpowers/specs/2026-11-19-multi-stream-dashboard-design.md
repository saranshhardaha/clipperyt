# Multi-stream dashboard — design (approved 2026-11-19)

## Intent
Run several streams at once from one dashboard (live channels and finished
videos), choose the output shape, edit every setting from a form, and see
stats. Must work on the dev Mac and on the Linux box (Proxmox LXC), and the
setup scripts must install everything needed on both.

## Goals
- Many jobs at once. A job is either **watch** (channel(s): wait → record →
  clip → repeat) or **url** (one live stream or past video, then done).
- Output shape, one global setting `render.format`:
  `16:9` · `9:16-blur` (whole 16:9 frame, blurred fill) · `9:16-bars`
  (whole 16:9 frame, black bars) · `9:16-crop` (old centre crop).
- Every config.toml key editable as a typed form field. Raw TOML editor kept
  as "Advanced".
- Stats per job (time, GB, bitrate, chat per platform, msgs/min sparkline,
  spikes so far), per session (duration, chat msgs, unique chatters, spikes,
  clips, size) and system (disk, totals, encoder, tool/model health).

## Non-goals / anti-goals
- No per-job settings overrides (user picked "one global config", option A).
- No LLM, no accounts, no external services, no JS frameworks or CDN: the
  page must work offline on a LAN. Stdlib Python only.
- Never break a running recording: restarting the dashboard must not kill jobs.
- Don't break the timing rules in CLAUDE.md.

## Approaches considered
1. **One detached process per job, launched by server.py** — chosen. Reuses
   `record_once`/`cmd_process`; crash isolation; jobs survive dashboard restarts.
2. One watcher process, one thread per stream — signal handling and global
   `STOP`/state get messy; one bug kills every stream.
3. systemd unit per stream — Linux only, nothing on the Mac.

## Design
- **Data dir**: `CLIPPER_HOME` env (default = repo dir). Holds `config.toml`,
  `sessions/`, `jobs/`, `logs/`, `locks/`. Lets tests run in isolation.
- **Jobs**: `jobs/<id>/job.json` = `{id, kind, name, channels, record_from, url,
  autostart, created}`; `jobs/<id>/state.json`, `.stop`, `job.log`. Server
  starts `clipper.py job <id>` with `start_new_session=True` (+ `caffeinate -is`
  on macOS). State/stop paths are per job (module globals swapped by `cmd_job`).
- **Unique sessions**: `YYYYMMDD-HHMM-<channel-slug>` (+`-2`, … on collision);
  per-job waiting dir `sessions/_waiting-<id>`. Session regex widened.
- **Processing slots**: `[limits] max_parallel_processing` (default 1).
  `fcntl.flock` on `locks/process-N.lock` (Mac + Linux). Job shows `queued`.
- **Legacy CLI** (`watch`, `run`, `process`, `spikes`, `status`) keeps working.
- **Autostart**: jobs flagged `autostart` are started when server.py boots
  (replaces the curl ExecStartPost trick for systemd/launchd).
- **Settings**: a schema in server.py lists every key with type/label/help/
  options. Saving writes TOML via a small serializer (keeps unknown keys,
  writes `.bak`).
- **Stats**: incremental chat-file readers cached by offset; live spikes via
  `spikes.find_moments` at most every 30 s per job.
- **Setup**: Python ≥3.11 needed (`tomllib`) — Linux script installs a new
  enough python or `tomli` fallback; handles root-without-sudo in LXC;
  `deploy/install_service.sh` installs systemd (Linux) or a LaunchAgent (macOS).
- **Tests**: offline, repo copied to a scratch dir with fake `streamlink`,
  `yt-dlp` and chat loggers; two jobs in parallel.

## Builder's calls
- Recording cap dropped: the recorder process is itself the "wait for live"
  loop, so capping it would need a separate liveness probe. Disk/bitrate stats
  instead.
- Old `[render] vertical/mode` still read when `format` is missing.
