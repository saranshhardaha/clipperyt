"""Post clips to YouTube as Shorts — only when the user presses Post or Schedule.

Standard library only. Sign-in uses Google's device flow ("enter this code at google.com/device"),
which works from a phone even when the dashboard runs on a headless Linux box. The refresh token is
kept in <data>/youtube-token.json (owner-only). Uploads are resumable and chunked. A scheduled clip
is uploaded right away as private with status.publishAt, so YouTube publishes it at that time even
if this machine is off.
"""
from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

OAUTH = os.environ.get("CLIPPER_GOOGLE_OAUTH", "https://oauth2.googleapis.com")  # tests point these at a fake
API = os.environ.get("CLIPPER_GOOGLE_API", "https://www.googleapis.com")
SCOPE = "https://www.googleapis.com/auth/youtube"  # the device flow allows this one (not youtube.upload)
CHUNK = 8 << 20


class YouTubeError(RuntimeError):
    pass


# ---------------------------------------------------------------- token storage

def token_path(home: Path) -> Path:
    return home / "youtube-token.json"


def load_token(home: Path) -> dict | None:
    try:
        return json.loads(token_path(home).read_text())
    except Exception:
        return None


def save_token(home: Path, tok: dict) -> None:
    p = token_path(home)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(tok, f)
    os.chmod(tmp, 0o600)
    tmp.replace(p)


def forget(home: Path) -> None:
    token_path(home).unlink(missing_ok=True)


# ---------------------------------------------------------------- HTTP

def _req(method: str, url: str, data: bytes | None = None, headers: dict | None = None,
         timeout: int = 60) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:  # incl. 308 "Resume Incomplete" (urllib won't follow it for PUT)
        return e.code, dict(e.headers or {}), e.read()
    except (urllib.error.URLError, TimeoutError) as e:
        raise YouTubeError(f"Couldn't reach Google: {getattr(e, 'reason', e)}") from None


def _form(url: str, fields: dict) -> tuple[int, dict]:
    code, _, body = _req("POST", url, urllib.parse.urlencode(fields).encode(),
                         {"Content-Type": "application/x-www-form-urlencoded"})
    try:
        return code, json.loads(body or b"{}")
    except json.JSONDecodeError:
        return code, {}


def _api_error(body: bytes) -> tuple[str, str]:
    try:
        err = json.loads(body).get("error", {})
        reason = (err.get("errors") or [{}])[0].get("reason", "")
        return reason, err.get("message", "")
    except Exception:
        return "", body[:200].decode("utf-8", "replace")


def explain(code: int, body: bytes) -> str:
    reason, msg = _api_error(body)
    if reason in ("quotaExceeded", "uploadLimitExceeded", "dailyLimitExceeded"):
        return "YouTube's daily upload limit is reached. Try again tomorrow."
    if code == 401 or reason in ("authError", "unauthorized"):
        return "YouTube sign-in expired. Reconnect YouTube in Settings → YouTube."
    if reason == "youtubeSignupRequired":
        return "That Google account has no YouTube channel yet. Create one, then reconnect."
    if code == 403 and "insufficient" in msg.lower():
        return "The YouTube connection is missing upload permission. Reconnect YouTube in Settings → YouTube."
    return f"YouTube error {code}: {msg or reason or 'no details'}"


# ---------------------------------------------------------------- sign-in (device flow)

def start_device_flow(cfg: dict) -> dict:
    """→ {device_code, user_code, verification_url, expires_in, interval}"""
    cid = str(cfg.get("client_id") or "").strip()
    if not cid or not str(cfg.get("client_secret") or "").strip():
        raise YouTubeError("Add the OAuth client ID and secret in Settings → YouTube first.")
    code, body = _form(f"{OAUTH}/device/code", {"client_id": cid, "scope": SCOPE})
    if code != 200:
        if body.get("error") == "invalid_client":
            raise YouTubeError("Google doesn't accept that client ID. It must be a "
                               "“TVs and Limited Input devices” OAuth client.")
        raise YouTubeError(f"Google sign-in error: {body.get('error_description') or body.get('error') or code}")
    body.setdefault("verification_url", body.get("verification_uri", "https://www.google.com/device"))
    return body


def poll_device_flow(cfg: dict, device_code: str) -> dict | None:
    """One poll. → token dict when approved, None while pending. Raises on denial / expiry."""
    code, body = _form(f"{OAUTH}/token", {
        "client_id": cfg["client_id"], "client_secret": cfg["client_secret"], "device_code": device_code,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
    if code == 200 and body.get("access_token"):
        return {"access_token": body["access_token"], "refresh_token": body.get("refresh_token"),
                "expires_at": time.time() + int(body.get("expires_in", 3600)) - 60, "scope": body.get("scope")}
    err = body.get("error")
    if err in ("authorization_pending", "slow_down"):
        return None
    if err == "access_denied":
        raise YouTubeError("Sign-in was declined on the Google page.")
    if err in ("expired_token", "invalid_grant"):
        raise YouTubeError("The sign-in code expired. Press Connect again.")
    raise YouTubeError(f"Google sign-in error: {body.get('error_description') or err or code}")


def access_token(cfg: dict, home: Path) -> str:
    tok = load_token(home)
    if not tok or not tok.get("refresh_token"):
        raise YouTubeError("YouTube isn't connected. Connect it in Settings → YouTube.")
    if tok.get("access_token") and tok.get("expires_at", 0) > time.time():
        return tok["access_token"]
    code, body = _form(f"{OAUTH}/token", {"client_id": cfg.get("client_id", ""), "client_secret": cfg.get("client_secret", ""),
                                          "refresh_token": tok["refresh_token"], "grant_type": "refresh_token"})
    if code != 200 or not body.get("access_token"):
        if body.get("error") == "invalid_grant":
            raise YouTubeError("YouTube sign-in expired or was revoked. Reconnect YouTube in Settings → YouTube. "
                               "(If this happens every 7 days, set the Google app to “In production”.)")
        raise YouTubeError(f"Couldn't refresh the YouTube sign-in: {body.get('error_description') or body.get('error') or code}")
    tok.update(access_token=body["access_token"], expires_at=time.time() + int(body.get("expires_in", 3600)) - 60)
    save_token(home, tok)
    return tok["access_token"]


def channel_info(cfg: dict, home: Path) -> dict:
    code, _, body = _req("GET", f"{API}/youtube/v3/channels?part=snippet&mine=true",
                         headers={"Authorization": f"Bearer {access_token(cfg, home)}"})
    if code != 200:
        raise YouTubeError(explain(code, body))
    items = json.loads(body).get("items") or []
    if not items:
        raise YouTubeError("That Google account has no YouTube channel yet. Create one, then reconnect.")
    return {"channel_id": items[0]["id"], "channel_title": items[0]["snippet"]["title"]}


# ---------------------------------------------------------------- upload

def upload(cfg: dict, home: Path, video: Path, title: str, description: str, tags: list[str],
           publish_at: str | None = None, progress=lambda done, total: None) -> dict:
    """Resumable upload. publish_at (UTC ISO) → private now, published by YouTube at that time.
    → {"id", "url", "privacy", "publish_at"}"""
    total = video.stat().st_size
    privacy = "private" if publish_at else str(cfg.get("privacy") or "public")
    meta = {"snippet": {"title": title.strip()[:100] or "Clip", "description": description[:4900],
                        "tags": [t[:30] for t in tags][:15], "categoryId": str(cfg.get("category_id") or "20")},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": bool(cfg.get("made_for_kids", False))}}
    if publish_at:
        meta["status"]["publishAt"] = publish_at
    tok = access_token(cfg, home)
    code, headers, body = _req(
        "POST", f"{API}/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status&notifySubscribers=true",
        json.dumps(meta).encode(), {"Authorization": f"Bearer {tok}", "Content-Type": "application/json; charset=UTF-8",
                                    "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(total)})
    loc = {k.lower(): v for k, v in headers.items()}.get("location")
    if code != 200 or not loc:
        raise YouTubeError(explain(code, body))
    sent = 0
    with open(video, "rb") as f:
        while True:
            f.seek(sent)
            chunk = f.read(CHUNK)
            end = sent + len(chunk) - 1
            code, headers, body = _req("PUT", loc, chunk, {
                "Authorization": f"Bearer {access_token(cfg, home)}", "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {sent}-{end}/{total}" if chunk else f"bytes */{total}"}, timeout=300)
            if code in (200, 201):
                vid = json.loads(body)["id"]
                progress(total, total)
                return {"id": vid, "url": f"https://www.youtube.com/shorts/{vid}", "privacy": privacy,
                        "publish_at": publish_at}
            if code != 308:
                raise YouTubeError(explain(code, body))
            rng = {k.lower(): v for k, v in headers.items()}.get("range")  # "bytes=0-12345"
            sent = int(rng.rsplit("-", 1)[1]) + 1 if rng else end + 1
            progress(sent, total)


def delete_video(cfg: dict, home: Path, video_id: str) -> None:
    code, _, body = _req("DELETE", f"{API}/youtube/v3/videos?id={urllib.parse.quote(video_id)}",
                         headers={"Authorization": f"Bearer {access_token(cfg, home)}"})
    if code not in (200, 204, 404):
        raise YouTubeError(explain(code, body))


# ---------------------------------------------------------------- scheduling

def _tz(cfg: dict):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(str(cfg.get("timezone") or "Asia/Kolkata"))
    except Exception:
        return datetime.now().astimezone().tzinfo


def _hm(s: str, default: tuple[int, int]) -> tuple[int, int]:
    try:
        h, m = str(s).split(":")
        return max(0, min(23, int(h))), max(0, min(59, int(m)))
    except Exception:
        return default


def _jitter(t: datetime, spread: int) -> timedelta:
    """0..spread random minutes, seeded by the slot itself: the time shown as "next slot"
    is the time a clip then gets, and re-asking never moves it."""
    return timedelta(minutes=random.Random(t.strftime("%Y%m%d%H%M")).randint(0, spread)) if spread > 0 else timedelta()


def next_slot(cfg: dict, taken: list[str], now: datetime | None = None, posted: list[str] = ()) -> str:
    """Next free publish time (UTC ISO 'Z'): inside the daily window, at least slot_gap_hours
    apart, at most slots_per_day per local day (counting `posted`, the times of posts already
    made), at least 15 minutes from now. random_minutes adds 0..N minutes to each slot, so posts
    land at odd minutes and the gaps vary between slot_gap_hours and slot_gap_hours + N."""
    tz = _tz(cfg)
    now = (now or datetime.now(timezone.utc)).astimezone(tz)
    gap = timedelta(hours=max(0.25, float(cfg.get("slot_gap_hours", 3))))
    per_day = max(1, int(cfg.get("slots_per_day", 4)))
    spread = max(0, int(cfg.get("random_minutes", 60)))
    (h0, m0), (h1, m1) = _hm(cfg.get("window_start", "10:00"), (10, 0)), _hm(cfg.get("window_end", "22:00"), (22, 0))

    def parse(xs):
        out = []
        for t in xs:
            try:
                out.append(datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(tz))
            except ValueError:
                pass
        return out
    busy, done = parse(taken), parse(posted)

    def free(t: datetime) -> bool:
        return all(abs((t - b).total_seconds()) >= gap.total_seconds() * 0.99 for b in busy)
    earliest = (now + timedelta(minutes=15)).replace(second=0, microsecond=0) + timedelta(minutes=1)
    for day in range(0, 120):
        d = (now + timedelta(days=day)).date()
        start = datetime(d.year, d.month, d.day, h0, m0, tzinfo=tz)
        end = datetime(d.year, d.month, d.day, h1, m1, tzinfo=tz)
        if sum(1 for b in busy + done if b.date() == d) >= per_day:
            continue
        # candidates: the window opening, now, and one gap after each scheduled post
        cands = sorted({max(c, start, earliest) for c in [start] + [b + gap for b in busy]})
        for c in cands:
            if c > end:
                break
            for t in (c + _jitter(c, spread), c):  # the random time, else the plain one if that's all that fits
                if t <= end and free(t):
                    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    raise YouTubeError("No free posting slot in the next 120 days. Widen the window or add slots per day.")


def local_label(cfg: dict, iso: str) -> str:
    t = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(_tz(cfg))
    return t.strftime("%a %d %b, %H:%M")


def local_day(cfg: dict, iso: str, now: datetime | None = None) -> str:
    """'Today' / 'Tomorrow' / 'Sat 22 Nov' in the posting time zone."""
    tz = _tz(cfg)
    d = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(tz).date()
    today = (now or datetime.now(timezone.utc)).astimezone(tz).date()
    if d == today:
        return "Today"
    if d == today + timedelta(days=1):
        return "Tomorrow"
    return d.strftime("%a %d %b")


def local_time(cfg: dict, iso: str) -> str:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(_tz(cfg)).strftime("%H:%M")
