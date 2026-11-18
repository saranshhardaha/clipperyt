# Multi-stream dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run many record/clip jobs at once from the dashboard, pick output shape, edit every setting in a form, see stats — on macOS and Linux.

**Architecture:** server.py launches one detached `clipper.py job <id>` process per job; each job has its own folder (`jobs/<id>/`) with job.json/state.json/.stop/job.log. A file-lock slot pool limits concurrent clip processing. The dashboard is one stdlib-served HTML page with vanilla JS.

**Tech Stack:** Python 3.11+ stdlib (tomllib, fcntl, http.server, unittest), ffmpeg, streamlink, yt-dlp, whisper.cpp.

**Spec:** `docs/superpowers/specs/2026-11-19-multi-stream-dashboard-design.md`

## Global Constraints
- Python ≥ 3.11 stdlib only for app code; `tomli` fallback allowed if 3.10 slips in.
- No LLM calls, no CDN/JS frameworks, no accounts.
- Timing rules in CLAUDE.md unchanged (`rec_start_ms`, `-vod` offsets, `timeline.tsv`).
- Restarting server.py never kills a job (`start_new_session=True`).
- Streamer credit stays (burned-in + clips.md links).
- macOS + Linux: `fcntl`, `os.killpg`, `caffeinate` only when `sys.platform == "darwin"`.

## Review Focus
- Two jobs going live in the same minute for the same channel → distinct session folders (`…-slug`, `…-slug-2`).
- Dashboard restarted while jobs run → jobs still listed with correct live status (pid check).
- Config saved from form with unknown keys (e.g. `kick_chatroom_id`) → keys preserved.
- Job whose process died (crash/kill -9) → shows `stopped`/`failed`, can be restarted, never stuck "live".
- Legacy config without `render.format` (`vertical=false` or `mode="crop"`) → same output as before.

---

### Task 1: Data dir, per-job state, unique sessions, slots (clipper.py)
**Files:** Modify `clipper.py`; Test `tests/test_jobs.py`
**Interfaces (produces):**
- `HOME: Path` (env `CLIPPER_HOME`, default ROOT); `SESSIONS = HOME/"sessions"`, `JOBS = HOME/"jobs"`, `LOCKS = HOME/"locks"`.
- `slug(s: str) -> str`; `new_session_dir(channel: str, suffix: str = "") -> Path`.
- `job_paths(jid) -> dict` / `load_job(jid) -> dict` / `save_job(job: dict)`; `read_state(path=None) -> dict`.
- `processing_slot(n: int)` context manager (flock on `LOCKS/process-{i}.lock`, sets state `queued` while waiting).
- `cmd_job(jid)` runs kind `watch` (loop) or `url` (once); CLI `clipper.py job <id>`.
- [ ] Tests: slug, session uniqueness, slot exclusivity (2 processes, n=1 serialize), legacy `read_state` with dead pid → stopped.
- [ ] Implement; run `python -m unittest tests.test_jobs -v` → PASS. Commit.

### Task 2: Render formats (render.py)
**Files:** Modify `render.py`, `clipper.py` (final filename), `config.example.toml`; Test `tests/test_render.py`
**Interfaces:** `out_format(cfg) -> str` in {"16:9","9:16-blur","9:16-bars","9:16-crop"}; `filter_graph(fmt, sub) -> str`; `frame_size(fmt) -> (w,h)`.
- [ ] Tests: legacy mapping (`vertical=false`→16:9, `mode=crop`→9:16-crop, default→9:16-blur); real ffmpeg render of 2 s testsrc2 in each format → probe size 1080x1920 / 1920x1080; bars output has black top rows.
- [ ] Implement; tests PASS. Commit.

### Task 3: Server — jobs API, settings schema + TOML writer, stats
**Files:** Modify `server.py`; Create `settings_schema.py`, `stats.py`; Test `tests/test_server.py`
**Interfaces:** `settings_schema.SCHEMA: list[dict]` (section,key,type∈{str,int,float,bool,enum,list,secret},label,help,options?); `settings_schema.dump_toml(cfg) -> str`; `stats.ChatTail` (incremental counts per minute, unique authors); `stats.job_stats(job, state)`, `stats.session_stats(dir)`, `stats.system_stats()`.
Routes: `GET/POST /api/jobs`, `POST /api/jobs/<id>/{start,stop,kill,delete}`, `GET /api/jobs/<id>/log`, `GET/POST /api/settings`, `GET /api/system`, existing sessions/files/config routes kept; legacy `/api/start|stop` removed from UI.
- [ ] Tests: dump_toml round-trips example config incl. unknown keys; POST /api/jobs validates kind/url; settings POST rejects bad enum; stats on synthetic chat.
- [ ] Implement; tests PASS. Commit.

### Task 4: Dashboard UI
**Files:** Modify `server.py` (PAGE → `dashboard.html` served from disk)
- Tabs: Jobs · Sessions · Settings · System. Jobs: add form (watch channels+record_from / url, autostart), cards with status pill, stats, sparkline (inline SVG), log drawer, start/stop/kill/delete. Sessions: table + stats + clip grid with correct aspect. Settings: generated from schema, Advanced raw TOML. System: disk, totals, encoder, tool checks.
- [ ] Verify in built-in browser against fake data dir (desktop + mobile width). Commit.

### Task 5: Setup + services (Mac + Linux)
**Files:** `setup.sh`, `setup_linux.sh`, `requirements.txt`, Create `deploy/install_service.sh`, `deploy/com.clipperyt.dashboard.plist.in`, modify `deploy/clipperyt.service`, `deploy/README-systemd.md`
- Linux: no-sudo-as-root, Python ≥3.11 check (try python3.12/3.11, else tomli), dirs incl. jobs/locks, `bash -n` clean. macOS: same dirs; `doctor` step printing tool status.
- [ ] `bash -n` both scripts; run `setup.sh` checks portion on Mac. Commit.

### Task 6: Offline e2e with fakes
**Files:** Create `tests/e2e/run_e2e.sh`, `tests/e2e/fakes/{streamlink,yt-dlp}`, fake chat loggers
- Copy repo to scratch, `CLIPPER_HOME` there, start server on spare port, create 2 watch jobs + 1 url job through API, wait for clips, assert both sessions distinct, each has clips in configured format, slots serialized.
- [ ] Run → PASS. Commit.

### Task 7: Docs
- README dashboard section, CLAUDE.md files/status. Commit.
