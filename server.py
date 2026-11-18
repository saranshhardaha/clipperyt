#!/usr/bin/env python3
"""clipperyt dashboard — check status, start/stop, edit config, watch clips.

  python server.py                       → http://0.0.0.0:8787
  CLIPPER_TOKEN=secret python server.py  → requires ?token=secret once (then a cookie)
  CLIPPER_PORT=9000 / CLIPPER_HOST=127.0.0.1 to change where it listens

Standard library only. The watcher runs as a separate process, so restarting
this server never interrupts a recording.
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import signal
import subprocess
import sys
import threading
import time
import tomllib
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import clipper

ROOT = clipper.ROOT
SESSIONS = clipper.SESSIONS
LOGS = ROOT / "logs"
TOKEN = os.environ.get("CLIPPER_TOKEN", "")
PY = sys.executable
SESSION_RE = re.compile(r"^\d{8}-\d{4}[\w-]*$")
jobs: dict[str, subprocess.Popen] = {}  # session → running `process` job


# ------------------------------------------------------------------ actions

def watcher_log() -> Path | None:
    files = sorted(LOGS.glob("watch-*.log")) if LOGS.exists() else []
    return files[-1] if files else None


def start_watcher() -> str:
    if clipper.read_state()["status"] != "stopped":
        return "already running"
    LOGS.mkdir(exist_ok=True)
    logf = open(LOGS / f"watch-{time.strftime('%Y%m%d-%H%M%S')}.log", "a")
    cmd = [PY, "-u", str(ROOT / "clipper.py"), "watch"]
    if sys.platform == "darwin":
        cmd = ["caffeinate", "-is"] + cmd
    # own session: survives server restarts, gets its own Ctrl-C
    p = subprocess.Popen(cmd, cwd=ROOT, stdout=logf, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    clipper.STATE.write_text(json.dumps({"status": "starting", "pid": p.pid, "since": int(time.time())}))
    threading.Thread(target=p.wait, daemon=True).start()  # reap when it exits
    return "started"


def stop_watcher(force: bool = False) -> str:
    st = clipper.read_state()
    if st["status"] == "stopped":
        return "not running"
    if st["status"] == "processing" and not force:
        clipper.STOPFILE.touch()  # let it finish the clips, then exit
        return "will stop after the current clips are done"
    try:
        # SIGINT = graceful: stops recording, still clips what was recorded
        os.killpg(os.getpgid(st["pid"]), signal.SIGKILL if force else signal.SIGINT)
    except OSError as e:
        return f"error: {e}"
    return "killed" if force else "stopping — clipping what was recorded"


def run_process(session: str) -> str:
    if session in jobs and jobs[session].poll() is None:
        return "already processing"
    jobs[session] = subprocess.Popen(
        [PY, "-u", str(ROOT / "clipper.py"), "process", session], cwd=ROOT,
        stdout=open(SESSIONS / session / "process.log", "w"), stderr=subprocess.STDOUT)
    return "processing started"


# ------------------------------------------------------------------ data

def tail(path: Path | None, n: int = 80) -> str:
    if not path or not path.exists():
        return ""
    with open(path, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 64_000))
        return "\n".join(f.read().decode("utf-8", "replace").splitlines()[-n:])


def count_lines(p: Path) -> int:
    with open(p, "rb") as f:
        return sum(1 for _ in f)


def status() -> dict:
    st = clipper.read_state()
    out = {"state": st, "log": tail(watcher_log(), 60)}
    if st.get("status") == "live" and st.get("session"):
        sess = SESSIONS / st["session"]
        ts = sess / "stream.ts"
        out["live"] = {
            "minutes": round((time.time() * 1000 - st["rec_start_ms"]) / 60000, 1),
            "gb": round(ts.stat().st_size / 1e9, 2) if ts.exists() else 0,
            "chat": {f.stem.replace("chat_", ""): count_lines(f) for f in sorted(sess.glob("chat*.tsv"))},
        }
    try:
        du = os.statvfs(ROOT)
        out["disk_free_gb"] = round(du.f_bavail * du.f_frsize / 1e9, 1)
    except OSError:
        pass
    return out


def sessions() -> list[dict]:
    res = []
    for d in sorted(SESSIONS.glob("*"), reverse=True) if SESSIONS.exists() else []:
        if not d.is_dir() or not SESSION_RE.match(d.name):
            continue
        meta = {}
        try:
            meta = json.loads((d / "meta.json").read_text())
        except Exception:
            pass
        ts = d / "stream.ts"
        job = jobs.get(d.name)
        res.append({
            "id": d.name, "channel": meta.get("channel", "?"), "platform": meta.get("platform", "?"),
            "gb": round(ts.stat().st_size / 1e9, 2) if ts.exists() else 0,
            "clips": len(list((d / "clips").glob("*/short.mp4"))) + len(list((d / "clips").glob("*/captioned.mp4"))),
            "processing": bool(job and job.poll() is None),
        })
    return res


def session_detail(sid: str) -> dict:
    d = SESSIONS / sid
    spikes_ = json.loads((d / "spikes.json").read_text()) if (d / "spikes.json").exists() else []
    clips = []
    for n, c in enumerate(sorted((d / "clips").glob("*/")) if (d / "clips").exists() else []):
        final = next((c / f for f in ("short.mp4", "captioned.mp4") if (c / f).exists()), None)
        m = spikes_[n] if n < len(spikes_) else {}
        clips.append({
            "name": c.name, "video": f"/files/{sid}/clips/{c.name}/{final.name}" if final else None,
            "raw": f"/files/{sid}/clips/{c.name}/raw.mp4" if (c / "raw.mp4").exists() else None,
            "score": m.get("score"), "top": m.get("top", []),
            "speech": (c / "speech.srt").read_text(encoding="utf-8", errors="replace")[:2000]
            if (c / "speech.srt").exists() else "",
        })
    job = jobs.get(sid)
    return {"id": sid, "clips": clips, "processing": bool(job and job.poll() is None),
            "process_log": tail(d / "process.log", 30)}


# ------------------------------------------------------------------ http

class H(BaseHTTPRequestHandler):
    server_version = "clipperyt"

    def log_message(self, fmt, *args):  # quieter
        if not self.path.startswith(("/api/status", "/files/")):
            sys.stderr.write(f"[http] {self.address_string()} {fmt % args}\n")

    # -- helpers
    def authed(self) -> bool:
        if not TOKEN:
            return True
        q = parse_qs(urlparse(self.path).query)
        if q.get("token", [""])[0] == TOKEN:
            return True
        c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        return "clipper" in c and c["clipper"].value == TOKEN

    def send(self, code: int, body: bytes | str | dict | list, ctype: str = "application/json",
             extra: dict | None = None) -> None:
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith(("text", "application/json")) else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if TOKEN and parse_qs(urlparse(self.path).query).get("token", [""])[0] == TOKEN:
            self.send_header("Set-Cookie", f"clipper={TOKEN}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    # -- routes
    def do_GET(self):
        if not self.authed():
            return self.send(401, "Unauthorized — open /?token=YOUR_TOKEN once", "text/plain")
        path = urlparse(self.path).path
        if path == "/":
            return self.send(200, PAGE, "text/html")
        if path == "/api/status":
            return self.send(200, status())
        if path == "/api/sessions":
            return self.send(200, sessions())
        if m := re.fullmatch(r"/api/sessions/([\w-]+)", path):
            if not SESSION_RE.match(m[1]) or not (SESSIONS / m[1]).is_dir():
                return self.send(404, {"error": "no such session"})
            return self.send(200, session_detail(m[1]))
        if m := re.fullmatch(r"/api/sessions/([\w-]+)/spikes", path):
            out = subprocess.run([PY, str(ROOT / "clipper.py"), "spikes", m[1]], cwd=ROOT,
                                 capture_output=True, text=True, timeout=120)
            return self.send(200, {"output": out.stdout + out.stderr})
        if path == "/api/config":
            return self.send(200, {"toml": (ROOT / "config.toml").read_text(encoding="utf-8")})
        if path.startswith("/files/"):
            return self.file(unquote(path[len("/files/"):]))
        self.send(404, {"error": "not found"})

    do_HEAD = do_GET

    def do_POST(self):
        if not self.authed():
            return self.send(401, {"error": "unauthorized"})
        path = urlparse(self.path).path
        if path == "/api/start":
            return self.send(200, {"result": start_watcher()})
        if path == "/api/stop":
            force = parse_qs(urlparse(self.path).query).get("force", ["0"])[0] == "1"
            return self.send(200, {"result": stop_watcher(force)})
        if m := re.fullmatch(r"/api/sessions/([\w-]+)/process", path):
            if not SESSION_RE.match(m[1]) or not (SESSIONS / m[1]).is_dir():
                return self.send(404, {"error": "no such session"})
            return self.send(200, {"result": run_process(m[1])})
        if path == "/api/config":
            text = json.loads(self.body() or b"{}").get("toml", "")
            try:
                tomllib.loads(text)
            except tomllib.TOMLDecodeError as e:
                return self.send(400, {"error": f"invalid TOML: {e}"})
            (ROOT / "config.toml").with_suffix(".toml.bak").write_text(
                (ROOT / "config.toml").read_text(encoding="utf-8"), encoding="utf-8")
            (ROOT / "config.toml").write_text(text, encoding="utf-8")
            return self.send(200, {"result": "saved — applies from the next stream"})
        self.send(404, {"error": "not found"})

    def file(self, rel: str) -> None:
        p = (SESSIONS / rel).resolve()
        if not str(p).startswith(str(SESSIONS.resolve()) + os.sep) or not p.is_file():
            return self.send(404, {"error": "not found"})
        size = p.stat().st_size
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        start, end = 0, size - 1
        rng = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if rng:  # browsers need Range requests to scrub video
            if rng[1]:
                start, end = int(rng[1]), int(rng[2]) if rng[2] else size - 1
            else:
                start = size - int(rng[2])
            end = min(end, size - 1)
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if "download" in self.path:
            self.send_header("Content-Disposition", f'attachment; filename="{p.parent.name}-{p.name}"')
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(p, "rb") as f:
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = f.read(min(left, 1 << 20))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>clipperyt</title>
<style>
:root{--bg:#0f1115;--card:#181b22;--line:#2a2f3a;--fg:#e8eaf0;--mut:#8b93a7;--acc:#7c9cff;--ok:#3ecf8e;--warn:#f5a524;--bad:#f2555a}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--fg)}
main{max-width:980px;margin:0 auto;padding:16px}h1{font-size:18px;margin:4px 0 14px}h2{font-size:15px;margin:0 0 10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:14px}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
button{background:#232838;color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:7px 12px;font:inherit;cursor:pointer}
button.pri{background:var(--acc);color:#0b1020;border-color:var(--acc);font-weight:600}button.bad{border-color:var(--bad);color:var(--bad)}
.pill{display:inline-block;padding:3px 10px;border-radius:99px;font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.stopped{background:#2a2f3a;color:var(--mut)}.waiting{background:#2b2a1a;color:var(--warn)}.live{background:#3a1518;color:#ff6b6f}
.processing,.starting{background:#16253f;color:var(--acc)}.idle{background:#2a2f3a;color:var(--mut)}
.mut{color:var(--mut)}pre{background:#0b0d11;border:1px solid var(--line);border-radius:8px;padding:10px;max-height:260px;overflow:auto;font-size:12px;white-space:pre-wrap;margin:8px 0 0}
table{width:100%;border-collapse:collapse}td,th{padding:7px 6px;border-bottom:1px solid var(--line);text-align:left}th{color:var(--mut);font-weight:500}
tr.sel{background:#1e2330}tr[data-id]{cursor:pointer}
.clips{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px}
.clip{background:#11141a;border:1px solid var(--line);border-radius:10px;padding:8px}
.clip video{width:100%;aspect-ratio:9/16;background:#000;border-radius:6px}
.clip .t{font-size:12px;color:var(--mut);margin-top:6px}
textarea{width:100%;min-height:320px;background:#0b0d11;color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:10px;font:12px/1.5 ui-monospace,Menlo,monospace}
.stat{display:inline-block;margin-right:18px}.stat b{font-size:18px;display:block}
</style></head><body><main>
<h1>clipperyt</h1>

<div class="card">
  <div class="row" style="justify-content:space-between">
    <div class="row"><span id="pill" class="pill stopped">…</span><span id="what" class="mut"></span></div>
    <div class="row">
      <button class="pri" onclick="act('/api/start')">Start watching</button>
      <button onclick="if(confirm('Stop? It will still clip what was recorded.'))act('/api/stop')">Stop</button>
      <button class="bad" onclick="if(confirm('Kill immediately, no clipping?'))act('/api/stop?force=1')">Kill</button>
    </div>
  </div>
  <div id="stats" style="margin-top:12px"></div>
  <details><summary class="mut" style="margin-top:8px;cursor:pointer">Log</summary><pre id="log"></pre></details>
</div>

<div class="card"><h2>Sessions</h2><table><thead><tr><th>Session</th><th>Channel</th><th>Size</th><th>Clips</th><th></th></tr></thead>
<tbody id="sessions"></tbody></table></div>

<div class="card" id="detail" hidden>
  <div class="row" style="justify-content:space-between"><h2 id="dtitle"></h2>
    <div class="row"><button onclick="spk()">List spikes</button><button class="pri" onclick="proc()">Re-make clips</button></div></div>
  <pre id="dlog" hidden></pre>
  <div class="clips" id="clips" style="margin-top:10px"></div>
</div>

<div class="card"><details><summary style="cursor:pointer"><b>Config</b> <span class="mut">config.toml</span></summary>
  <textarea id="cfg" spellcheck="false"></textarea>
  <div class="row" style="margin-top:8px"><button class="pri" onclick="saveCfg()">Save</button><span id="cfgmsg" class="mut"></span></div>
</details></div>
</main>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let cur=null;
async function j(u,o){const r=await fetch(u,o);return r.json()}
async function act(u){const r=await j(u,{method:'POST'});toast(r.result||r.error);refresh()}
function toast(m){$('what').textContent=m;setTimeout(refresh,1500)}
function ago(s){const m=Math.round((Date.now()/1000-s)/60);return m<1?'just now':m<60?m+' min':Math.floor(m/60)+'h '+m%60+'m'}
async function refresh(){
  const s=await j('/api/status'),st=s.state;
  $('pill').className='pill '+st.status;$('pill').textContent=st.status;
  $('what').textContent=st.status==='waiting'?'waiting for '+st.url+' · '+ago(st.since):
    st.status==='live'?'recording @'+st.channel+' on '+st.platform:
    st.status==='processing'?'making '+st.clips+' clips for '+st.session:'';
  let h='';
  if(s.live){h+=`<span class="stat"><b>${s.live.minutes} min</b><span class="mut">recorded</span></span><span class="stat"><b>${s.live.gb} GB</b><span class="mut">on disk</span></span>`;
    for(const[k,v]of Object.entries(s.live.chat))h+=`<span class="stat"><b>${v}</b><span class="mut">${esc(k)} chat</span></span>`}
  if(s.disk_free_gb!=null)h+=`<span class="stat"><b>${s.disk_free_gb} GB</b><span class="mut">free</span></span>`;
  $('stats').innerHTML=h;$('log').textContent=s.log||'(no log yet)';
  const ss=await j('/api/sessions');
  $('sessions').innerHTML=ss.map(x=>`<tr data-id="${x.id}" class="${x.id===cur?'sel':''}" onclick="open_('${x.id}')">
    <td>${x.id}</td><td>${esc(x.channel)} <span class="mut">${esc(x.platform)}</span></td><td>${x.gb} GB</td>
    <td>${x.processing?'<span class="pill processing">working</span>':x.clips}</td><td class="mut">›</td></tr>`).join('')||'<tr><td colspan=5 class="mut">No sessions yet</td></tr>';
  if(cur)loadDetail(false);
}
async function open_(id){cur=id;$('detail').hidden=false;$('dlog').hidden=true;await loadDetail(true);$('detail').scrollIntoView({behavior:'smooth'})}
async function loadDetail(full){
  const d=await j('/api/sessions/'+cur);
  $('dtitle').textContent=cur+(d.processing?' — working…':'');
  if(d.processing||!full&&!$('dlog').hidden){$('dlog').hidden=false;$('dlog').textContent=d.process_log}
  const sig=JSON.stringify(d.clips.map(c=>c.video));
  if(!full&&$('clips').dataset.sig===sig)return;
  $('clips').dataset.sig=sig;
  $('clips').innerHTML=d.clips.map(c=>`<div class="clip">
    ${c.video?`<video src="${c.video}" controls preload="metadata" playsinline></video>`:'<div class="mut">no video</div>'}
    <div><b>${esc(c.name)}</b> ${c.score!=null?`<span class="mut">· score ${c.score}</span>`:''}</div>
    <div class="t">${esc((c.top||[]).slice(0,3).join(' · '))}</div>
    <div class="row" style="margin-top:6px">${c.video?`<a href="${c.video}?download=1"><button>Download</button></a>`:''}
    ${c.raw?`<a href="${c.raw}?download=1"><button>Raw</button></a>`:''}</div></div>`).join('')||'<div class="mut">No clips yet.</div>';
}
async function proc(){const r=await j('/api/sessions/'+cur+'/process',{method:'POST'});$('dlog').hidden=false;$('dlog').textContent=r.result;setTimeout(()=>loadDetail(false),1500)}
async function spk(){$('dlog').hidden=false;$('dlog').textContent='…';const r=await j('/api/sessions/'+cur+'/spikes');$('dlog').textContent=r.output}
async function loadCfg(){$('cfg').value=(await j('/api/config')).toml}
async function saveCfg(){const r=await j('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({toml:$('cfg').value})});$('cfgmsg').textContent=r.result||r.error}
refresh();loadCfg();setInterval(refresh,5000);
</script></body></html>"""


def main() -> None:
    host = os.environ.get("CLIPPER_HOST", "0.0.0.0")
    port = int(os.environ.get("CLIPPER_PORT", "8787"))
    SESSIONS.mkdir(exist_ok=True)
    srv = ThreadingHTTPServer((host, port), H)
    print(f"clipperyt dashboard on http://{host}:{port}"
          + ("  (token required: add ?token=… once)" if TOKEN else "  (no token — keep it on your LAN/Tailscale)"),
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
