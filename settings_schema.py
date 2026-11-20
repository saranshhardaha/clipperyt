"""Every config.toml setting the dashboard can edit, plus a small TOML writer.

The stdlib can read TOML (tomllib) but not write it, so `dump_toml` writes the
subset config.toml uses: tables, strings, numbers, booleans and lists. Keys the
schema doesn't know (e.g. ones added by hand) are kept, not dropped.
"""
from __future__ import annotations

import json
import math

# type: str | secret | int | float | bool | enum | list
SCHEMA: list[dict] = [
    # ---- stream (defaults for `clipper.py watch` and the dashboard's new-job form)
    {"key": "stream.record_from", "type": "enum", "options": ["youtube", "twitch", "kick"],
     "label": "Default platform to record", "help": "Pre-filled in the new-job form; also used by `clipper.py watch`."},
    {"key": "stream.channels.youtube", "type": "str", "label": "Default YouTube handle",
     "help": "youtube.com/@<handle>, without @. Empty if they don't stream there."},
    {"key": "stream.channels.twitch", "type": "str", "label": "Default Twitch login", "help": "twitch.tv/<login>"},
    {"key": "stream.channels.kick", "type": "str", "label": "Default Kick slug", "help": "kick.com/<slug>"},
    {"key": "stream.options.recorder", "type": "enum", "options": ["auto", "streamlink", "yt-dlp"],
     "labels": {"auto": "auto (YouTube → yt-dlp, Twitch/Kick → streamlink)"},
     "label": "Recorder", "help": "auto is best: yt-dlp gets past YouTube's bot check with cookies; streamlink skips Twitch ads."},
    {"key": "stream.options.quality", "type": "str", "label": "Quality",
     "help": "best, 1080p60, 720p … (streamlink names)."},
    {"key": "stream.options.retry_seconds", "type": "int", "min": 5, "label": "Live check every (s)",
     "help": "Seconds between \"is it live yet?\" checks while waiting."},
    {"key": "stream.options.twitch_disable_ads", "type": "bool", "label": "Skip Twitch ad segments",
     "help": "The ad isn't recorded; the stream underneath it is lost either way for logged-out viewers."},
    {"key": "stream.options.twitch_oauth_token", "type": "secret", "label": "Twitch OAuth token (optional)",
     "help": "From an account with Turbo or a sub → no ads at all. twitch.tv → DevTools → Cookies → auth-token."},
    {"key": "stream.options.youtube_cookies", "type": "str", "label": "YouTube cookies file (optional)",
     "help": "Path to a cookies.txt exported from a browser logged in to YouTube (a spare account is best). "
             "Needed when YouTube says \"confirm you're not a bot\". Relative paths are inside the data folder."},
    {"key": "stream.options.cookies_from_browser", "type": "enum", "options": ["", "chrome", "firefox", "safari", "brave", "edge"],
     "labels": {"": "off"}, "label": "…or read cookies from a browser",
     "help": "Mac only, for yt-dlp (not streamlink). Chrome may show a Keychain prompt; Safari needs Full Disk Access. "
             "A cookies file is more reliable for overnight runs."},
    {"key": "stream.options.kick_chatroom_id", "type": "int", "optional": True, "label": "Kick chatroom id (optional)",
     "help": "Only if the automatic lookup gets blocked by Cloudflare (see README)."},

    # ---- spikes
    {"key": "spikes.bucket_seconds", "type": "int", "min": 1, "label": "Bucket (s)", "help": "Chat is counted in buckets this long."},
    {"key": "spikes.baseline_seconds", "type": "int", "min": 10, "label": "Baseline window (s)",
     "help": "Each bucket is compared with the average of this many previous seconds."},
    {"key": "spikes.multiplier", "type": "float", "min": 1, "label": "Spike multiplier",
     "help": "Spike = bucket ≥ this × baseline. Higher = fewer, bigger moments."},
    {"key": "spikes.min_messages", "type": "int", "min": 0, "label": "Min messages per bucket",
     "help": "Ignores \"spikes\" in a dead chat (2 msgs vs 0.5 avg)."},
    {"key": "spikes.warmup_seconds", "type": "int", "min": 0, "label": "Warm-up (s)",
     "help": "Skip the chat flood at the start of the stream."},
    {"key": "spikes.merge_seconds", "type": "int", "min": 0, "label": "Merge spikes closer than (s)"},
    {"key": "spikes.hype_words", "type": "list", "label": "Hype words",
     "help": "Short messages made of these count extra. Comma or newline separated."},
    {"key": "spikes.hype_weight", "type": "float", "min": 0, "label": "Hype weight",
     "help": "Extra weight per hype message (0.5 → counts as 1.5)."},

    {"key": "spikes.clip_requests", "type": "bool", "label": "Clip requests",
     "help": "Also make a moment when enough different chatters ask for a clip, even if chat isn't louder."},
    {"key": "spikes.clip_phrases", "type": "list", "label": "Clip phrases",
     "help": "Whole words/phrases, any case. \"clip\" already covers \"clip it\", \"bhai clip karo\"…; \"eclipse\"/\"clips\" don't count."},
    {"key": "spikes.clip_min_users", "type": "int", "min": 2, "label": "Different chatters needed",
     "help": "Each chatter counts once, however often they type it."},
    {"key": "spikes.clip_window_seconds", "type": "int", "min": 5, "label": "…within (s)"},
    {"key": "spikes.clip_boost", "type": "float", "min": 0, "label": "Clip-request boost",
     "help": "Score added per requesting chatter. A request during a chat spike adds to that spike's score."},

    # ---- clip
    {"key": "clip.stream_delay_seconds", "type": "float", "min": 0, "label": "Stream delay (s)",
     "help": "Viewers see the stream this late, so chat reacts late."},
    {"key": "clip.lead_seconds", "type": "float", "min": 0, "label": "Lead (s)",
     "help": "Start the clip this long before the delay-corrected spike."},
    {"key": "clip.length_seconds", "type": "float", "min": 5, "label": "Clip length (s)"},
    {"key": "clip.max_clips", "type": "int", "min": 1, "label": "Max clips per stream (per hour while live)",
     "help": "Keeps the top N by score. With hourly clipping it applies to each hour, so a 24-hour stream isn't capped at N."},
    {"key": "clip.process_every_minutes", "type": "int", "min": 0, "label": "Clip while live every (min)",
     "help": "Long streams: make clips for the last hour every N minutes while recording continues. 0 = only when the stream ends."},

    # ---- captions
    {"key": "captions.enabled", "type": "bool", "label": "Captions"},
    {"key": "captions.whisper_bin", "type": "str", "label": "whisper.cpp binary"},
    {"key": "captions.model", "type": "str", "label": "Whisper model", "help": "Path relative to the app folder, or absolute."},
    {"key": "captions.language", "type": "enum", "options": ["auto", "hi", "en"], "label": "Language",
     "help": "auto detects per clip (Hindi or English)."},
    {"key": "captions.translate", "type": "bool", "label": "Translate to English",
     "help": "Needs a non-turbo model, e.g. ggml-medium.bin."},

    # ---- render
    {"key": "render.format", "type": "enum", "options": ["9:16-blur", "9:16-bars", "9:16-facecam", "16:9", "9:16-crop"],
     "labels": {"9:16-blur": "9:16 · whole 16:9 frame, blurred fill",
                "9:16-bars": "9:16 · whole 16:9 frame, black bars",
                "9:16-facecam": "9:16 · facecam on top, gameplay below",
                "16:9": "16:9 · original shape",
                "9:16-crop": "9:16 · centre crop (cuts the sides)"},
     "label": "Output shape", "help": "Both 9:16 fill modes keep the full 16:9 picture; nothing is cropped. "
     "Facecam on top needs the camera's place marked once per channel: Facecam box on a session page."},
    {"key": "render.facecam_height", "type": "float", "min": 0.2, "max": 0.6, "label": "Facecam height",
     "help": "Share of the Short the camera takes on top, 0.2–0.6 (0.4 = 40%). Only for facecam on top."},
    {"key": "render.facecam_game", "type": "enum", "options": ["fill", "whole"],
     "labels": {"fill": "Fill: crop the gameplay to fill the space (bigger, cuts the sides)",
                "whole": "Whole: the full gameplay frame, blurred fill around it"},
     "label": "Gameplay under the facecam", "help": "Only for facecam on top."},
    {"key": "render.encoder", "type": "enum", "options": ["auto", "h264_videotoolbox", "h264_nvenc", "libx264"],
     "label": "Encoder", "help": "auto = Mac hardware → NVIDIA GPU → CPU."},
    {"key": "render.font_size", "type": "int", "min": 16, "label": "Caption size (px on 1080 wide)"},
    {"key": "render.font", "type": "str", "label": "Font",
     "help": "auto = Kohinoor Devanagari (Mac) / Noto Sans Devanagari (Linux)."},
    {"key": "render.credit", "type": "bool", "label": "Burn in streamer credit",
     "help": "Small \"@channel · Platform\" line at the top. Keep it on."},

    # ---- storage
    {"key": "storage.sessions_dir", "type": "str", "label": "Recordings folder",
     "help": "Where new recordings + clips go, e.g. /Volumes/BigDrive/clipperyt (Mac) or /mnt/media/clipperyt "
             "(Linux). Empty = the app's sessions/ folder. The folder above it must already exist, so an unplugged "
             "drive never silently fills the internal disk. Existing sessions stay put — move them from Sessions."},
    {"key": "storage.also_scan", "type": "list", "label": "Also list sessions from",
     "help": "Older recordings folders, still shown in Sessions. Filled in automatically when you change the "
             "recordings folder; remove one once it's empty."},

    {"key": "storage.wait_for_drive_seconds", "type": "int", "min": 0, "label": "Wait for the drive (s)",
     "help": "After a reboot, autostart jobs and One-link jobs wait up to this long for the recordings folder "
             "(NAS/USB drives often mount late). Watch jobs keep retrying after that anyway."},

    # ---- titles (AI via OpenRouter) — only used when you press "Titles" on a clip
    {"key": "titles.openrouter_api_key", "type": "secret", "label": "OpenRouter API key",
     "help": "From openrouter.ai/keys. Used only when you press Titles on a clip; recording and clipping never "
             "call it. The clip's transcript and chat lines are sent to the model you pick."},
    {"key": "titles.model", "type": "str", "label": "Model",
     "help": "Any OpenRouter model id. x-ai/grok-4.7 (Grok 4.7) is the default; cheaper models work too. "
             "Start typing to pick from OpenRouter's list."},
    {"key": "titles.language", "type": "enum", "options": ["hinglish", "english", "hindi"],
     "labels": {"hinglish": "Hinglish (Latin script)", "english": "English", "hindi": "Hindi (Devanagari)"},
     "label": "Title language"},
    {"key": "titles.style", "type": "str", "label": "Style notes (optional)",
     "help": "e.g. \"mention GTA RP\", \"no emojis\", \"always add the streamer's name\"."},

    # ---- youtube (posting) — only when you press Post or Schedule
    {"key": "youtube.client_id", "type": "str", "label": "OAuth client ID",
     "help": "From Google Cloud → Credentials → OAuth client of type “TVs and Limited Input devices”."},
    {"key": "youtube.client_secret", "type": "secret", "label": "OAuth client secret"},
    {"key": "youtube.privacy", "type": "enum", "options": ["public", "unlisted", "private"], "label": "Post now as",
     "help": "Scheduled posts are uploaded private and YouTube makes them public at the slot time."},
    {"key": "youtube.slots_per_day", "type": "int", "min": 1, "label": "Scheduled posts per day"},
    {"key": "youtube.slot_gap_hours", "type": "float", "min": 0.25, "label": "Hours between scheduled posts"},
    {"key": "youtube.random_minutes", "type": "int", "min": 0, "label": "Random extra minutes per post",
     "help": "Each scheduled post gets 0 to this many extra minutes at random, so posts don't land on round times and the gap varies (3 h + up to 60 min = 3–4 h). 0 = exact times."},
    {"key": "youtube.window_start", "type": "str", "label": "Post from (HH:MM)"},
    {"key": "youtube.window_end", "type": "str", "label": "Post until (HH:MM)"},
    {"key": "youtube.timezone", "type": "str", "label": "Time zone", "help": "e.g. Asia/Kolkata"},
    {"key": "youtube.category_id", "type": "str", "label": "Category id", "help": "20 = Gaming."},
    {"key": "youtube.made_for_kids", "type": "bool", "label": "Made for kids", "help": "Leave off for stream clips."},

    # ---- limits
    {"key": "limits.max_parallel_processing", "type": "int", "min": 1, "label": "Clip jobs at once",
     "help": "How many streams can be cut + transcribed + rendered at the same time. 1 is safest for whisper."},
]

# How the Settings page groups fields (what people change first), and which are "advanced"
GROUPS = [
    ("detection", "Detection", "What counts as a moment worth clipping."),
    ("clips", "Clips & captions", "Where clips start, how long they are, and their captions."),
    ("output", "Output", "The shape and look of the finished Shorts."),
    ("titles", "Titles (AI)", "Title suggestions through OpenRouter, only when you press Titles on a clip."),
    ("youtube", "YouTube", "Post clips as Shorts with one click, or schedule them into time slots."),
    ("channels", "Channels & recording", "Default channels, YouTube cookies, and how streams are recorded."),
    ("storage", "Storage & limits", "Where recordings go and how much runs at once."),
    ("file", "Config file", "Edit config.toml directly."),
]
GROUP_OF = {"spikes": "detection", "clip": "clips", "captions": "clips", "render": "output", "titles": "titles",
            "stream": "channels", "storage": "storage", "limits": "storage", "youtube": "youtube"}
ADVANCED = {"spikes.bucket_seconds", "spikes.baseline_seconds", "spikes.warmup_seconds", "spikes.merge_seconds",
            "spikes.hype_weight", "spikes.clip_boost", "captions.whisper_bin", "captions.model", "render.encoder",
            "render.font", "stream.options.quality", "stream.options.retry_seconds", "stream.options.kick_chatroom_id",
            "stream.options.cookies_from_browser", "stream.options.twitch_oauth_token", "storage.also_scan",
            "storage.wait_for_drive_seconds", "stream.record_from", "youtube.category_id", "youtube.made_for_kids",
            "youtube.timezone"}
for _f in SCHEMA:
    _f["group"] = GROUP_OF[_f["key"].split(".")[0]]
    _f["adv"] = _f["key"] in ADVANCED


def defaults() -> dict:
    """Schema key → value shipped in config.example.toml (for "changed from default" + Reset)."""
    from pathlib import Path
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib
    raw = tomllib.loads((Path(__file__).resolve().parent / "config.example.toml").read_text(encoding="utf-8"))
    return {f["key"]: get_path(raw, f["key"]) for f in SCHEMA}


SECTION_TITLES = {"stream": "Stream defaults", "spikes": "Spike detection", "clip": "Clips",
                  "captions": "Captions", "render": "Render", "storage": "Storage", "titles": "Titles (AI)", "youtube": "YouTube", "limits": "Limits"}
BY_KEY = {f["key"]: f for f in SCHEMA}
LEGACY = {"render.vertical", "render.mode"}  # superseded by render.format


def get_path(d: dict, key: str):
    for part in key.split("."):
        if not isinstance(d, dict) or part not in d:
            return None
        d = d[part]
    return d


def set_path(d: dict, key: str, value) -> None:
    *head, last = key.split(".")
    for part in head:
        d = d.setdefault(part, {})
    if value is None:
        d.pop(last, None)
    else:
        d[last] = value


def flatten(cfg: dict) -> dict:
    """Schema key → current value (raw TOML structure, not the flattened runtime cfg)."""
    out = {f["key"]: get_path(cfg, f["key"]) for f in SCHEMA}
    # a config.toml from before a section existed (e.g. [youtube]) shows the shipped defaults,
    # so the form can be saved without filling in every new field by hand
    defs = defaults()
    for f in SCHEMA:
        if out[f["key"]] is None and not f.get("optional") and f["key"] != "render.format":
            out[f["key"]] = defs.get(f["key"])
    if out.get("render.format") is None:  # show what the old keys mean
        import render
        out["render.format"] = render.out_format(cfg.get("render", {}))
    return out


def coerce(field: dict, v):
    """Form value → typed TOML value. Raises ValueError with a readable message."""
    t, label = field["type"], field.get("label", field["key"])
    if field.get("optional") and (v is None or v == ""):
        return None
    if t in ("str", "secret"):
        return str(v or "").strip()
    if t == "bool":
        if isinstance(v, str):
            return v.lower() in ("1", "true", "on", "yes")
        return bool(v)
    if t == "enum":
        if v not in field["options"]:
            raise ValueError(f"{label}: pick one of {', '.join(field['options'])}")
        return v
    if t == "list":
        if isinstance(v, str):
            v = [x.strip() for x in v.replace("\n", ",").split(",")]
        return [str(x) for x in v if str(x).strip()]
    if t in ("int", "float"):
        try:
            n = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"{label}: needs a number") from None
        if not math.isfinite(n):
            raise ValueError(f"{label}: needs a number")
        if "min" in field and n < field["min"]:
            raise ValueError(f"{label}: must be ≥ {field['min']}")
        if "max" in field and n > field["max"]:
            raise ValueError(f"{label}: must be ≤ {field['max']}")
        if t == "int":
            if n != int(n):
                raise ValueError(f"{label}: needs a whole number")
            return int(n)
        return n
    raise ValueError(f"unknown type {t}")


def apply_values(cfg: dict, values: dict) -> list[str]:
    """Merge form values into the raw TOML dict in place. Returns error messages."""
    errors = []
    for key, v in values.items():
        field = BY_KEY.get(key)
        if not field:
            errors.append(f"unknown setting {key}")
            continue
        try:
            set_path(cfg, key, coerce(field, v))
        except ValueError as e:
            errors.append(str(e))
    if "render.format" in values:
        for k in LEGACY:
            set_path(cfg, k, None)
    return errors


# ---------------------------------------------------------------- TOML writer

def _val(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(int(v)) + ".0" if v == int(v) and abs(v) < 1e15 else repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)  # JSON string escapes are valid TOML basic strings
    if isinstance(v, list):
        return "[" + ", ".join(_val(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_key(k)} = {_val(x)}" for k, x in v.items()) + " }"
    raise TypeError(f"can't write {type(v).__name__} to TOML")


def _key(k: str) -> str:
    return k if k.replace("_", "").replace("-", "").isalnum() and k.isascii() else json.dumps(k)


def _order(prefix: str, keys) -> list[str]:
    known = [f["key"].split(".")[-1] for f in SCHEMA if f["key"].rsplit(".", 1)[0] == prefix]
    sub = sorted({f["key"][len(prefix) + 1:].split(".")[0] for f in SCHEMA
                  if f["key"].startswith(prefix + ".") and f["key"].count(".") > prefix.count(".") + 1})
    rank = {k: i for i, k in enumerate(known + sub)}
    return sorted(keys, key=lambda k: (rank.get(k, 10_000), k))


def _table(prefix: str, d: dict, out: list[str]) -> None:
    scalars = [k for k in d if not isinstance(d[k], dict)]
    tables = [k for k in d if isinstance(d[k], dict)]
    if scalars or not tables:
        if out:
            out.append("")
        title = SECTION_TITLES.get(prefix)
        if title:
            out.append(f"# ---- {title}")
        out.append(f"[{prefix}]")
        for k in _order(prefix, scalars):
            f = BY_KEY.get(f"{prefix}.{k}")
            if f and f.get("help"):
                out.append(f"# {f['help']}")
            out.append(f"{_key(k)} = {_val(d[k])}")
    for k in _order(prefix, tables):
        _table(f"{prefix}.{_key(k)}", d[k], out)


def dump_toml(cfg: dict) -> str:
    out: list[str] = ["# clipperyt config — edited from the dashboard (Settings).",
                      "# Comments here are regenerated; a .bak copy of the previous file is kept."]
    top_scalars = {k: v for k, v in cfg.items() if not isinstance(v, dict)}
    for k, v in top_scalars.items():
        out.append(f"{_key(k)} = {_val(v)}")
    order = list(SECTION_TITLES)
    for k in sorted((k for k, v in cfg.items() if isinstance(v, dict)),
                    key=lambda k: (order.index(k) if k in order else 99, k)):
        _table(_key(k), cfg[k], out)
    return "\n".join(out) + "\n"
