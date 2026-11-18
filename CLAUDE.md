# clipperyt — notes for Claude Code

Python 3.12 on macOS (Apple Silicon). Clips a YouTube, Twitch or Kick live stream wherever chat spikes.

- `clipper.py`: CLI with `watch`, `process <session>` and `spikes <session>`. It also handles recording and orchestration.
- `chat_twitch.py`: logs Twitch chat through anonymous IRC (justinfan).
- `chat_youtube.py`: YouTube live chat via the popout innertube endpoint (stdlib only, YouTube send timestamps). Past-stream replay uses yt-dlp `--sub-langs live_chat` (`clipper.youtube_replay`). `chat_kick.py`: logs Kick chat through Kick's public Pusher websocket, which is unofficial.
- `spikes.py`: pure function `find_moments(chat, rec_start_ms, cfg)`. Has no I/O, so it's easy to test.
- `render.py`: ffmpeg cut, whisper.cpp captions, an ASS subtitle file (captions + credit), and the 1080x1920 render.
- `server.py`: dashboard plus JSON API, standard library only. It starts the watcher detached (`start_new_session`) and talks to it only through `state.json` (status, pid) and `.stop`. Stop means SIGINT to the process group; while processing it touches `.stop` instead.
- `setup.sh` (macOS/brew) and `setup_linux.sh` (apt, builds whisper.cpp into bin/). `deploy/` holds the systemd unit.
- `config.toml`: every tunable setting. Session layout is `sessions/<YYYYmmdd-HHMM>/{stream.ts, chat_<platform>.tsv, meta.json, spikes.json, clips.md, clips/NN_HH-MM-SS/}`.

Rules:
- Each platform writes its own `chat_<platform>.tsv`, and spikes merge every `chat*.tsv`. Lines are `epoch_ms<TAB>author<TAB>text`, stamped with the **local receive time**. meta.json `rec_start_ms` uses the same clock, so don't switch to server timestamps.
- Don't call any LLM at runtime. The overnight job has to run offline and free.
- Test spike logic offline: write a synthetic chat.tsv and meta.json, then run `clipper.py spikes`.
- Credit the streamer: the burned-in credit line and `clips.md` must stay.
