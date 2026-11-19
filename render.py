"""Cut clips, transcribe them with whisper.cpp, render captioned Shorts."""
from __future__ import annotations

import json
import re
import sys
import subprocess
from pathlib import Path


_ENCODER: str | None = None


def pick_encoder(want: str = "auto") -> str:
    """'auto' → Apple VideoToolbox, else NVIDIA NVENC, else libx264 (CPU)."""
    global _ENCODER
    if want and want != "auto":
        return want
    if _ENCODER is None:
        encs = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                              capture_output=True, text=True).stdout
        _ENCODER = "libx264"
        for e in ("h264_videotoolbox", "h264_nvenc"):
            if e in encs and subprocess.run(  # listed ≠ usable (e.g. no GPU) → test it
                    ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=256x256:d=0.1",
                     "-c:v", e, "-f", "null", "-"], capture_output=True).returncode == 0:
                _ENCODER = e
                break
    return _ENCODER


FORMATS = ("16:9", "9:16-blur", "9:16-bars", "9:16-crop")


def out_format(cfg: dict) -> str:
    """[render] format, or the old vertical/mode keys if format isn't set."""
    f = cfg.get("format")
    if f in FORMATS:
        return f
    if not cfg.get("vertical", True):
        return "16:9"
    return "9:16-crop" if cfg.get("mode") == "crop" else "9:16-blur"


def is_vertical(cfg: dict) -> bool:
    return out_format(cfg) != "16:9"


def frame_size(fmt: str) -> tuple[int, int]:
    return (1920, 1080) if fmt == "16:9" else (1080, 1920)


def filter_graph(fmt: str, sub: str = "") -> str:
    """ffmpeg -filter_complex for each output shape. `sub` = ",ass=subs.ass" or "".

    16:9       whole frame, letterboxed to 1920x1080 if the source isn't 16:9
    9:16-blur  whole 16:9 frame across the middle, blurred copy fills top/bottom
    9:16-bars  whole 16:9 frame across the middle, black top/bottom
    9:16-crop  centre of the frame cropped to fill 1080x1920 (loses the sides)
    """
    w, h = frame_size(fmt)
    fit = f"scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2"
    fill = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
    if fmt == "9:16-blur":
        return ("[0:v]split[a][b];"
                f"[a]{fill},boxblur=20:4,eq=brightness=-0.08[bg];"
                f"[b]{fit}[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1{sub}[v]")
    if fmt == "9:16-crop":
        return f"[0:v]{fill},setsar=1{sub}[v]"
    # 16:9 and 9:16-bars: fit inside, pad with black
    return f"[0:v]{fit},pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1{sub}[v]"


def pick_font(want: str = "auto") -> str:
    if want and want != "auto":
        return want
    return "Kohinoor Devanagari" if sys.platform == "darwin" else "Noto Sans Devanagari"


def run(cmd: list[str], cwd: str | Path | None = None) -> None:
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        tail = "\n".join((r.stderr or r.stdout).strip().splitlines()[-15:])
        raise RuntimeError(f"{cmd[0]} failed:\n{tail}")


def cut(stream: Path, start: float, length: float, out: Path) -> None:
    """Fast cut, no re-encode. Starts on the keyframe at/before `start`."""
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-ss", f"{max(0.0, start):.2f}", "-i", str(stream), "-t", f"{length:.2f}",
         "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
         "-avoid_negative_ts", "make_zero", str(out)])


def container_of(stream: Path) -> str:
    """'ts' (MPEG-TS), 'mp4' (fragmented MP4 — Twitch Enhanced Broadcasting / HEVC, some
    YouTube) or 'other', from the first bytes. Byte-slicing is only valid per container."""
    with open(stream, "rb") as f:
        head = f.read(4096)
    if len(head) > 376 and head[0] == head[188] == head[376] == 0x47:
        return "ts"
    if any(b in head[:64] for b in (b"ftyp", b"styp", b"moof", b"moov")):
        return "mp4"
    return "other"


def _box_at(f, pos: int, kind: bytes) -> int | None:
    """Size of a `kind` box starting at file offset pos, if one is really there."""
    f.seek(pos)
    hdr = f.read(8)
    if len(hdr) < 8 or hdr[4:8] != kind:
        return None
    size = int.from_bytes(hdr[:4], "big")
    return size if size >= 8 else None


def _moof_near(f, pos: int, total: int, forward: bool) -> int | None:
    """Offset of a real moof box (followed by mdat) at/before pos, or at/after pos if forward."""
    win = 8 << 20
    cur = pos
    while 0 <= cur <= total:
        lo = cur if forward else max(0, cur - win)
        f.seek(lo)
        buf = f.read(win + 8)
        idxs = []
        i = buf.find(b"moof")
        while i != -1:
            idxs.append(lo + i - 4)
            i = buf.find(b"moof", i + 1)
        for box in (idxs if forward else reversed(idxs)):
            if box < 0 or (forward and box < pos) or (not forward and box > pos):
                continue
            size = _box_at(f, box, b"moof")
            if size and _box_at(f, box + size, b"mdat"):
                return box
        if forward:
            if lo + win >= total:
                return None
            cur = lo + win
        else:
            if lo == 0:
                return None
            cur = lo
    return None


def _copy_range(src, dst, start: int, end: int) -> None:
    src.seek(start)
    left = end - start
    while left > 0:
        chunk = src.read(min(left, 8 << 20))
        if not chunk:
            break
        dst.write(chunk)
        left -= len(chunk)


def cut_wallclock(stream: Path, timeline: Path, wall_start: float, length: float, out: Path) -> None:
    """Cut by real time using timeline.tsv (wall clock → bytes written so far).

    If parts of the stream never reached the file (Twitch ads skipped, network
    stalls), plain `-ss` seeking drifts out of sync with chat. Mapping wall-clock
    time to byte offsets doesn't: a gap just means the size didn't grow.

    MPEG-TS can be sliced at any 188-byte packet. Fragmented MP4 is sliced at a
    moof (fragment) boundary with the file's init header (ftyp+moov) put in front.
    If slicing still fails, fall back to a plain -ss cut of the whole file.
    """
    pts = []
    for line in timeline.read_text().splitlines():
        a, _, b = line.partition("\t")
        if a.isdigit() and b.isdigit():
            pts.append((int(a) / 1000, int(b)))
    total = stream.stat().st_size
    if not pts:
        raise RuntimeError("timeline.tsv is empty")
    before = [b for t, b in pts if t <= wall_start]
    start_b = before[-1] if before else 0          # segment boundary → starts on a keyframe
    after = [b for t, b in pts if t >= wall_start + length]
    end_b = min(after[0] if after else total, total)
    if end_b - start_b < 188 * 100:
        raise RuntimeError("nothing recorded at that time (inside a skipped ad?)")
    kind = container_of(stream)
    piece = out.with_suffix(".part.mp4" if kind == "mp4" else ".part.ts")
    try:
        with open(stream, "rb") as src, open(piece, "wb") as dst:
            if kind == "mp4":
                first = _moof_near(src, 0, total, forward=True)
                if first is None:
                    raise RuntimeError("no fragments (moof) in the recording")
                s0 = _moof_near(src, max(start_b, first), total, forward=False) or first
                e0 = _moof_near(src, end_b, total, forward=True) or total
                _copy_range(src, dst, 0, first)        # ftyp + moov (init segment)
                _copy_range(src, dst, s0, e0)
            else:
                start_b -= start_b % 188               # align to MPEG-TS packets
                _copy_range(src, dst, start_b, end_b)
        cut(piece, 0, length, out)
    except Exception as e:
        # last resort: seek in the whole file (exact unless the recording has gaps before this point)
        t0 = pts[0][0]
        print(f"    ! byte-slice cut failed ({kind}: {str(e).splitlines()[-1][:120]}) — "
              f"falling back to a plain seek", flush=True)
        cut(stream, max(0.0, wall_start - t0), length, out)
    finally:
        piece.unlink(missing_ok=True)


_WHISPER_HELP: dict[str, str] = {}


def _whisper_supports(wbin: str, flag: str) -> bool:
    """Does this whisper-cli build know `flag`? (setup_linux.sh builds its own; old builds lack some.)"""
    if wbin not in _WHISPER_HELP:
        try:
            r = subprocess.run([wbin, "--help"], capture_output=True, text=True, timeout=30)
            _WHISPER_HELP[wbin] = r.stdout + r.stderr
        except (OSError, subprocess.TimeoutExpired):
            _WHISPER_HELP[wbin] = ""
    return flag in _WHISPER_HELP[wbin]


def dtw_preset(model: Path) -> str | None:
    """whisper.cpp --dtw preset for a ggml model file name (token-level timestamps)."""
    n = model.name.lower()
    for key, preset in (("large-v3-turbo", "large.v3.turbo"), ("large-v3", "large.v3"), ("large-v2", "large.v2"),
                        ("large-v1", "large.v1"), ("medium.en", "medium.en"), ("medium", "medium"),
                        ("small.en", "small.en"), ("small", "small"), ("base.en", "base.en"), ("base", "base"),
                        ("tiny.en", "tiny.en"), ("tiny", "tiny")):
        if key in n:
            return preset
    return None


def _srt_time(t: float) -> str:
    ms = int(round(max(0.0, t) * 1000))
    return f"{ms // 3600000:02d}:{ms % 3600000 // 60000:02d}:{ms % 60000 // 1000:02d},{ms % 1000:03d}"


def words_to_cues(words: list[tuple[str, float]], max_words: int = 5, max_chars: int = 28,
                  gap: float = 1.0, hold: float = 0.6) -> list[tuple[float, float, str]]:
    """Short captions from timed words: each cue starts at its first word and ends just after its
    last word (or when the next cue starts) — never shown during the silence before speech."""
    cues: list[list] = []
    for w, t in words:
        if not w:
            continue
        last = cues[-1] if cues else None
        new = (last is None or len(last[2]) >= max_words or len(" ".join(last[2]) + " " + w) > max_chars
               or t - last[1] > gap or re.search(r"[.!?।,]$", last[2][-1]))
        if new:
            cues.append([t, t, [w]])
        else:
            last[1] = t
            last[2].append(w)
    # a lone word right after a cue (e.g. "है") joins it instead of flashing on its own
    merged: list[list] = []
    for c in cues:
        p = merged[-1] if merged else None
        if (p and len(c[2]) == 1 and c[0] - p[1] < 0.6 and not re.search(r"[.!?।]$", p[2][-1])
                and len(" ".join(p[2] + c[2])) <= max_chars + 6):
            p[1] = c[1]
            p[2] += c[2]
        else:
            merged.append(c)
    cues = merged
    out = []
    for i, (a, b, ws) in enumerate(cues):
        start = max(0.0, a - 0.05)
        end = b + hold
        if i + 1 < len(cues):
            end = min(end, cues[i + 1][0] - 0.05)
        out.append((start, max(end, start + 0.4), " ".join(ws)))
    return out


def silences(wav: Path, noise_db: int = -35, min_len: float = 0.4) -> list[tuple[float, float]]:
    """Silent stretches (start, end) via ffmpeg silencedetect."""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(wav), "-af",
                        f"silencedetect=n={noise_db}dB:d={min_len}", "-f", "null", "-"],
                       capture_output=True, text=True)
    out, start = [], None
    for m in re.finditer(r"silence_(start|end): (-?[\d.]+)", r.stderr):
        if m[1] == "start":
            start = float(m[2])
        elif start is not None:
            out.append((max(0.0, start), float(m[2])))
            start = None
    if start is not None:
        out.append((start, 1e9))
    return out


def trim_to_speech(segs: list[tuple[float, float, str]], quiet: list[tuple[float, float]]) -> list[tuple[float, float, str]]:
    """Fallback when word times aren't available: don't start (or end) a caption inside silence."""
    out = []
    for a, b, text in segs:
        for qa, qb in quiet:
            # whisper often starts a segment a moment before the silence it then sits in
            if qa - 0.3 <= a < qb < b:
                a = qb
            elif qa <= a < qb:
                a = min(qb, b)
            if qa < b <= qb:
                b = max(qa, a)
        if b - a >= 0.2:
            out.append((a, b, text))
    return out


def transcribe(clip: Path, out_srt: Path, cfg: dict, root: Path) -> bool:
    """whisper.cpp → .srt with captions timed to the words (not to whisper's long segments,
    which start during the silence before speech). Returns False if whisper isn't set up."""
    model = Path(cfg["model"])
    model = model if model.is_absolute() else root / model
    if not model.exists():
        print(f"    ! whisper model not found at {model} — skipping captions")
        return False
    wav = out_srt.with_suffix(".wav")
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(clip),
         "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)])
    wbin = cfg.get("whisper_bin", "whisper-cli")
    if wbin == "whisper-cli" and (root / "bin" / "whisper-cli").exists():
        wbin = str(root / "bin" / "whisper-cli")  # built by setup_linux.sh
    base = out_srt.with_suffix("")
    js = base.with_suffix(".json")
    cmd = [wbin, "-m", str(model), "-f", str(wav), "-l", cfg.get("language", "hi"), "-ojf", "-of", str(base), "-np"]
    preset = dtw_preset(model)
    use_dtw = bool(preset) and _whisper_supports(wbin, "--dtw")
    if use_dtw:
        cmd += ["--dtw", preset]
        if _whisper_supports(wbin, "--no-flash-attn"):
            cmd.append("-nfa")  # DTW timestamps are empty with flash attention on
    if cfg.get("translate"):
        cmd.append("-tr")
    try:
        run(cmd)
        data = json.loads(js.read_text(encoding="utf-8", errors="replace"))
        segs = data.get("transcription", [])
        words: list[tuple[str, float]] = []
        for seg in segs:
            cur = None
            for t in seg.get("tokens", []):
                txt = t.get("text", "")
                if txt.startswith("[_") or txt.startswith("<|"):
                    continue
                when = t.get("t_dtw", -1)
                if txt.startswith(" ") or cur is None:
                    cur = [txt.strip(), when / 100 if when is not None and when >= 0 else None]
                    words.append(cur)
                else:
                    cur[0] += txt
        timed = words and all(w[1] is not None for w in words)
        if timed:
            cues = words_to_cues([(w, t) for w, t in words])
            how = "word-timed"
        else:  # no DTW: whisper's segments, trimmed so they don't start in silence
            cues = trim_to_speech([(s["offsets"]["from"] / 1000, s["offsets"]["to"] / 1000, s["text"].strip())
                                   for s in segs if s.get("text", "").strip()], silences(wav))
            how = "segment-timed, trimmed to speech"
        out_srt.write_text("".join(f"{i}\n{_srt_time(a)} --> {_srt_time(b)}\n{text}\n\n"
                                   for i, (a, b, text) in enumerate(cues, 1)), encoding="utf-8")
        print(f"    captions: {len(cues)} cues ({how})", flush=True)
    finally:
        wav.unlink(missing_ok=True)
        js.unlink(missing_ok=True)
    return out_srt.exists()


def _ass_time(t: str | float) -> str:
    if isinstance(t, str):  # "HH:MM:SS,mmm"
        h, m, rest = t.strip().split(":")
        sec, ms = rest.split(",")
        t = int(h) * 3600 + int(m) * 60 + int(sec) + int(ms) / 1000
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs % 360000 // 6000:02d}:{cs % 6000 // 100:02d}.{cs % 100:02d}"


def build_subs(speech_srt: Path | None, credit: str | None, length: float, out: Path,
               cfg: dict) -> bool:
    """Speech captions (bottom) + small credit line (top) as one .ass file."""
    fmt = out_format(cfg)
    vertical = fmt != "16:9"
    w, h = frame_size(fmt)
    font = pick_font(cfg.get("font", "auto"))
    fs = int(cfg.get("font_size", 64))
    if fmt in ("9:16-blur", "9:16-bars"):
        # 16:9 video fills the middle band (1080x608); captions hang just below it
        cap_align, cap_margin = 8, (h + w * 9 // 16) // 2 + 40
    elif fmt == "9:16-crop":
        cap_align, cap_margin = 2, 320
    else:
        cap_align, cap_margin = 2, 60
    events: list[str] = []
    if credit:
        events.append(f"Dialogue: 0,{_ass_time(0)},{_ass_time(length)},Credit,,0,0,0,,{credit}")
    if speech_srt and speech_srt.exists():
        for blk in re.split(r"\n\s*\n", speech_srt.read_text(encoding="utf-8").strip()):
            lines = blk.strip().splitlines()
            if len(lines) >= 3 and "-->" in lines[1]:
                text = " ".join(lines[2:]).strip().replace("{", "(").replace("}", ")")
                if text and not re.fullmatch(r"[\[\(].*[\]\)]", text):  # drop "[Music]" etc.
                    a, b = lines[1].split("-->")
                    events.append(f"Dialogue: 1,{_ass_time(a)},{_ass_time(b)},Caption,,0,0,0,,{text}")
    if not events:
        return False
    out.write_text(f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{font},{fs},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,{max(3, fs // 14)},0,{cap_align},60,60,{cap_margin},1
Style: Credit,{font},{max(28, fs // 2)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,3,8,0,8,40,40,{120 if vertical else 30},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
""" + "\n".join(events) + "\n", encoding="utf-8")
    return True


_HAS_ASS: bool | None = None


def has_ass() -> bool:
    """Can this ffmpeg burn in .ass subtitles? (needs libass; stock Homebrew ffmpeg 8+ lacks it)"""
    global _HAS_ASS
    if _HAS_ASS is None:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
        _HAS_ASS = bool(re.search(r"^\s*\S+\s+ass\s", out, re.M))
    return _HAS_ASS


def render(clip: Path, subs: Path | None, out: Path, cfg: dict) -> None:
    """Captioned version in the configured shape (see filter_graph)."""
    if subs and not has_ass():
        print("    ! ffmpeg has no libass ('ass' filter) — rendering without captions/credit. "
              "Run setup.sh to install an ffmpeg with libass.", flush=True)
        subs = None
    sub = f",ass={subs.name}" if subs else ""
    vf = filter_graph(out_format(cfg), sub)

    enc = pick_encoder(cfg.get("encoder", "auto"))
    if "videotoolbox" in enc:
        venc = ["-c:v", enc, "-b:v", "10M"]
    elif "nvenc" in enc:
        venc = ["-c:v", enc, "-preset", "p5", "-cq", "21", "-b:v", "0"]
    else:
        venc = ["-c:v", enc, "-preset", "veryfast", "-crf", "20"]
    # run inside the clip folder so the subtitles path needs no escaping
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", clip.name,
         "-filter_complex", vf, "-map", "[v]", "-map", "0:a:0?",
         *venc, "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
         "-movflags", "+faststart", out.name], cwd=clip.parent)

