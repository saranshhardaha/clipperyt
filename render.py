"""Cut clips, transcribe them with whisper.cpp, render captioned Shorts."""
from __future__ import annotations

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


def transcribe(clip: Path, out_srt: Path, cfg: dict, root: Path) -> bool:
    """whisper.cpp → .srt. Returns False (and leaves no srt) if whisper isn't set up."""
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
    cmd = [wbin, "-m", str(model), "-f", str(wav),
           "-l", cfg.get("language", "hi"), "-osrt", "-of", str(out_srt.with_suffix("")),
           "-np", "-ml", "42", "-sow"]
    if cfg.get("translate"):
        cmd.append("-tr")
    try:
        run(cmd)
    finally:
        wav.unlink(missing_ok=True)
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

