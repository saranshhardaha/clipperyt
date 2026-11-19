"""Suggest YouTube Shorts titles for a clip through OpenRouter — only when the user presses the button.

Never called by recording/clipping jobs: overnight runs stay free and offline.
Sends the channel, stream title, what chat said and the clip's transcript to the chosen
model (default x-ai/grok-4.7) via OpenRouter's chat completions API, and caches the
answer in clips/<clip>/titles.json.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

API_BASE = os.environ.get("CLIPPER_AI_BASE", "https://openrouter.ai/api/v1")  # tests point this at a fake
DEFAULT_MODEL = "x-ai/grok-4.7"
NAMES = {"youtube": "YouTube", "twitch": "Twitch", "kick": "Kick"}
LANG = {"hinglish": "Hinglish (Hindi words in Latin script, mixed with English) — how Indian gaming fans write",
        "english": "English", "hindi": "Hindi in Devanagari script"}


class TitleError(RuntimeError):
    pass


def _transcript(clip: Path, limit: int = 1500) -> str:
    srt = clip / "speech.srt"
    if not srt.exists():
        return ""
    lines = [l.strip() for l in srt.read_text(encoding="utf-8", errors="replace").splitlines()
             if l.strip() and not l.strip().isdigit() and "-->" not in l]
    return " ".join(lines)[:limit]


def credit_description(meta: dict) -> str:
    """The part of the description we always write ourselves (credit + link), never the model."""
    chans = meta.get("chat_from") or {meta.get("platform", ""): meta.get("channel", "")}
    links = [meta["source_url"]] if meta.get("source_url") else [
        {"youtube": f"https://www.youtube.com/@{c}", "twitch": f"https://www.twitch.tv/{c}",
         "kick": f"https://kick.com/{c}"}.get(p, "") for p, c in chans.items()]
    who = meta.get("channel", "the streamer").lstrip("@")
    return f"Clip from @{who}'s stream — watch the full stream: {' '.join(l for l in links if l)}"


def prompt(meta: dict, moment: dict, transcript: str, cfg: dict) -> list[dict]:
    lang = LANG.get(cfg.get("language", "hinglish"), cfg.get("language", "hinglish"))
    said = [t for t in moment.get("top", []) if not t.startswith("clip requested")][:5]
    facts = {
        "streamer": meta.get("channel"), "platform": NAMES.get(meta.get("platform", ""), meta.get("platform")),
        "stream_title": meta.get("title"), "what_chat_said": said,
        "chatters_asked_for_a_clip": moment.get("requests", 0),
        "what_the_streamer_says_in_the_clip": transcript or "(no transcript)",
    }
    system = ("You write titles for YouTube Shorts made from live-stream clips. Titles must be accurate to "
              "what happens in the clip (use the transcript and chat), punchy, under 70 characters, no "
              "clickbait lies, no hashtags inside the title, at most one emoji. Never invent names or events. "
              "Reply with JSON only: {\"titles\": [5 strings], \"hashtags\": [3-6 strings without #]}.")
    user = (f"Write the titles in {lang}." + (f" Style notes: {cfg['style']}" if cfg.get("style") else "")
            + "\n\nClip facts:\n" + json.dumps(facts, ensure_ascii=False, indent=1))
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _post(path: str, body: dict, key: str, timeout: int = 90) -> dict:
    # app name only — no site/repo URL, so nothing identifies the user beyond their own key
    req = urllib.request.Request(f"{API_BASE}{path}", data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                          "X-Title": "clipperyt"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error", {}).get("message", "")
        except Exception:
            msg = ""
        if e.code == 401:
            raise TitleError("OpenRouter rejected the API key. Check it in Settings → Titles.") from None
        if e.code == 402:
            raise TitleError("Your OpenRouter account is out of credits. Add credits at openrouter.ai, then try again.") from None
        if e.code == 429:
            raise TitleError("OpenRouter is rate limiting. Try again in a minute.") from None
        if e.code == 400 and "model" in msg.lower():
            raise TitleError(f"OpenRouter doesn't know the model “{body.get('model')}”. Pick one in Settings → Titles.") from None
        if e.code in (502, 503):
            raise TitleError("That model is unavailable on OpenRouter right now. Try again, or pick another model.") from None
        raise TitleError(f"OpenRouter error {e.code}: {msg[:200] or 'no details'}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise TitleError(f"Couldn't reach OpenRouter: {getattr(e, 'reason', e)}") from None
    if isinstance(data.get("error"), dict):  # failures after the provider accepted come back as 200
        raise TitleError(f"OpenRouter: {data['error'].get('message', 'the model failed')}")
    return data


def _reply_text(resp: dict) -> str:
    try:
        return resp["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError):
        return ""


def _parse(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise TitleError("The model didn't return titles. Try again.")
    try:
        data = json.loads(m[0])
    except json.JSONDecodeError:
        raise TitleError("The model's answer wasn't valid JSON. Try again.") from None
    titles = [str(t).strip().strip('"') for t in data.get("titles", []) if str(t).strip()][:5]
    tags = [re.sub(r"[^\w]", "", str(h).lstrip("#")) for h in data.get("hashtags", [])][:6]
    if not titles:
        raise TitleError("The model didn't return titles. Try again.")
    return {"titles": titles, "hashtags": [t for t in tags if t]}


def suggest(session: Path, clip: Path, cfg: dict) -> dict:
    """Ask the model for titles for one clip; cache in clip/titles.json."""
    key = str(cfg.get("openrouter_api_key") or os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if not key:
        raise TitleError("Add your OpenRouter API key in Settings → Titles first.")
    meta = json.loads((session / "meta.json").read_text())
    try:
        moment = json.loads((clip / "moment.json").read_text())
    except Exception:
        moment = {}
    model = str(cfg.get("model") or DEFAULT_MODEL).strip()
    resp = _post("/chat/completions", {"model": model, "messages": prompt(meta, moment, _transcript(clip), cfg)}, key)
    out = _parse(_reply_text(resp))
    tags = " ".join("#" + t for t in out["hashtags"] + ["Shorts"])
    usage = resp.get("usage") or {}
    out.update(description=f"{credit_description(meta)}\n\n{tags}", model=resp.get("model") or model,
               cost=usage.get("cost"), made=int(time.time()))
    (clip / "titles.json").write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


_models_cache: dict = {}


def models() -> list[dict]:
    """OpenRouter's public model list (cached an hour) for the model picker."""
    if _models_cache.get("t", 0) > time.time() - 3600:
        return _models_cache["list"]
    try:
        with urllib.request.urlopen(urllib.request.Request(f"{API_BASE}/models", headers={"X-Title": "clipperyt"}),
                                    timeout=30) as r:
            data = json.loads(r.read()).get("data", [])
    except Exception as e:
        raise TitleError(f"Couldn't load OpenRouter's model list: {getattr(e, 'reason', e)}") from None
    out = []
    for m in data:
        p = m.get("pricing") or {}
        try:
            pin, pout = float(p.get("prompt") or 0) * 1e6, float(p.get("completion") or 0) * 1e6
        except ValueError:
            pin = pout = 0.0
        out.append({"id": m.get("id"), "name": m.get("name") or m.get("id"), "in": round(pin, 3), "out": round(pout, 3)})
    out.sort(key=lambda m: m["id"])
    _models_cache.update(t=time.time(), list=out)
    return out


def cached(clip: Path) -> dict | None:
    try:
        return json.loads((clip / "titles.json").read_text())
    except Exception:
        return None
