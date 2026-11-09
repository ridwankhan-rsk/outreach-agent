"""
portal.py — a tiny local web page for the agent.  Open it with:  python cli.py portal

What it does:
  * upload a new resume PDF (it replaces the one attached to future emails; old ones are kept)
  * show agent status: paused?, sends today, leads by status, the latest replies

Security (it runs on your own machine, but websites in your browser can still try to reach
localhost, so it is locked down):
  * listens on 127.0.0.1 only, never on the network
  * every request needs a private token (stored in ~/.outreach-agent/portal_token, 0600);
    `cli.py portal` opens the page with the token already in the URL
  * the Host header must be localhost (blocks DNS-rebinding tricks)
  * uploads must be sent with a custom header + application/pdf, which another website cannot do
  * reply text from other people is only ever inserted with textContent, never as HTML
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import config
import db
import guard
import resume

log = logging.getLogger("outreach.portal")


# --------------------------------------------------------------------------- #
# Token + URL
# --------------------------------------------------------------------------- #

def get_token() -> str:
    path = config.DATA_DIR / "portal_token"
    if path.exists():
        return path.read_text().strip()
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(24)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)
    return token


def url() -> str:
    return f"http://{config.PORTAL_HOST}:{config.PORTAL_PORT}/?t={get_token()}"


# --------------------------------------------------------------------------- #
# Data shown on the page
# --------------------------------------------------------------------------- #

def _state() -> dict:
    with db.connect() as conn:
        leads = [
            {"campaign": r["campaign"], "status": r["status"], "n": r["c"]}
            for r in conn.execute(
                "SELECT campaign,status,COUNT(*) c FROM leads GROUP BY 1,2 ORDER BY 1,2")
        ]
        recent = [
            {
                "who": r["name"] or r["sender"], "org": r["org"] or "",
                "category": r["classification"], "at": r["received_at"],
                "text": (r["body"] or r["snippet"] or "")[:200],
            }
            for r in conn.execute(
                "SELECT r.*, l.name, l.org FROM replies r LEFT JOIN leads l ON l.id=r.lead_id "
                "WHERE r.classification IN ('needs_response','decline','stop_request') "
                "ORDER BY r.received_at DESC LIMIT 5")
        ]
        queued = [
            {"id": r["id"], "campaign": r["campaign"], "org": r["org"] or "", "name": r["name"] or "",
             "email": r["email"], "source": r["source"], "send_after": r["eligible_at"],
             "note": json.loads(r["data_json"]).get("note", "")[:200]}
            for r in conn.execute(
                "SELECT * FROM leads WHERE status='new' ORDER BY eligible_at, added_at LIMIT 60")
        ]
        return {
            "queued": queued,
            "resume": resume.info(),
            "max_bytes": config.RESUME_MAX_BYTES,
            "paused": guard.is_paused(),
            "sent_today": db.sends_since(conn, db.local_midnight_epoch()),
            "daily_limit": guard.daily_limit(conn),
            "leads": leads,
            "replies": recent,
            "campaigns_attaching": sorted(
                n for n, c in guard.load_campaigns(only_enabled=False).items() if c.get("attach_resume")),
        }


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    server_version = "Outreach"

    def log_message(self, *args):          # silence default logging: URLs contain the token
        pass

    # ---- helpers ----------------------------------------------------------- #
    def _reply(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._reply(code, json.dumps(obj).encode(), "application/json")

    def _authorised(self) -> bool:
        port = self.server.server_address[1]
        if self.headers.get("Host", "") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return False
        given = self.headers.get("X-Token") or parse_qs(urlparse(self.path).query).get("t", [""])[0]
        return hmac.compare_digest(given.encode(), get_token().encode())

    # ---- routes ------------------------------------------------------------ #
    def do_GET(self):
        if not self._authorised():
            return self._reply(403, b"Forbidden. Open the portal with: python cli.py portal",
                               "text/plain")
        path = urlparse(self.path).path
        if path == "/":
            return self._reply(200, PAGE.encode(), "text/html; charset=utf-8", {
                "Content-Security-Policy":
                    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                    "connect-src 'self'; frame-ancestors 'none'"})
        if path == "/api/state":
            return self._json(200, _state())
        if path == "/resume.pdf":
            cur = resume.current_path()
            if not cur:
                return self._reply(404, b"No resume uploaded yet.", "text/plain")
            return self._reply(200, cur.read_bytes(), "application/pdf",
                               {"Content-Disposition": 'inline; filename="resume.pdf"'})
        if path.startswith("/history/"):
            name = path[len("/history/"):]
            if resume.HISTORY_NAME.match(name) and (resume.history_dir() / name).exists():
                return self._reply(200, (resume.history_dir() / name).read_bytes(),
                                   "application/pdf", {"Content-Disposition": f'inline; filename="{name}"'})
        return self._reply(404, b"Not found", "text/plain")

    def _remove_lead(self):
        """Take a queued lead out of the send queue (only leads not yet emailed)."""
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            return self._json(415, {"error": "Send JSON."})
        try:
            lead_id = int(json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", "0")), 1024)))["id"])
        except (ValueError, KeyError, TypeError):
            return self._json(400, {"error": "Bad request."})
        with db.connect() as conn:
            cur = conn.execute(
                "UPDATE leads SET status='removed', status_reason='removed in portal' "
                "WHERE id=? AND status='new'", (lead_id,))
        return self._json(200, {"ok": True, "removed": cur.rowcount})

    def do_POST(self):
        path = urlparse(self.path).path
        if self._authorised() and path == "/remove":
            return self._remove_lead()
        if not self._authorised() or path != "/upload":
            return self._json(403, {"error": "forbidden"})
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/pdf":
            return self._json(415, {"error": "Send the file as application/pdf."})
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > config.RESUME_MAX_BYTES:
            return self._json(413, {"error": f"File must be 1 byte to "
                                              f"{config.RESUME_MAX_BYTES // (1024 * 1024)} MB."})
        try:
            info = resume.save(self.rfile.read(length))
        except resume.ResumeError as exc:
            return self._json(400, {"error": str(exc)})
        log.info("resume uploaded via portal (%d bytes)", info["size"])
        return self._json(200, {"ok": True, "resume": info})


# --------------------------------------------------------------------------- #
# Server lifecycle
# --------------------------------------------------------------------------- #

def make_server(port: int | None = None) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((config.PORTAL_HOST, config.PORTAL_PORT if port is None else port), Handler)


def start_in_background():
    """Called by the daemon so the portal is available whenever the agent is running."""
    try:
        srv = make_server()
    except OSError as exc:
        log.warning("portal not started (%s). Is another copy running?", exc)
        return None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("portal ready on http://%s:%s (open it with: python cli.py portal)",
             config.PORTAL_HOST, config.PORTAL_PORT)
    return srv


# --------------------------------------------------------------------------- #
# The page (plain HTML + a little JS; all dynamic text goes in via textContent)
# --------------------------------------------------------------------------- #

PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Outreach Portal</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--text:#1c2024;--muted:#6b7280;--line:#e3e6ea;--accent:#2563eb;--ok:#15803d;--bad:#b91c1c;--warn:#b45309}
@media (prefers-color-scheme:dark){:root{--bg:#111316;--card:#1a1d21;--text:#e8eaed;--muted:#9aa0a8;--line:#2b3036;--accent:#6ea0ff;--ok:#4ade80;--bad:#f87171;--warn:#fbbf24}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:720px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:22px;margin:0 0 4px}.sub{color:var(--muted);margin:0 0 20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin-bottom:16px}
h2{font-size:15px;margin:0 0 10px}
.row{display:flex;justify-content:space-between;gap:12px;padding:6px 0;border-top:1px solid var(--line)}
.row:first-of-type{border-top:0}.muted{color:var(--muted)}
.drop{display:block;border:2px dashed var(--line);border-radius:10px;padding:18px;text-align:center;margin-top:12px;cursor:pointer}
.drop.over{border-color:var(--accent)}
button,.btn{background:var(--accent);color:#fff;border:0;border-radius:8px;padding:8px 14px;font:inherit;cursor:pointer;text-decoration:none;display:inline-block}
.row button{align-self:flex-start;white-space:nowrap;padding:5px 12px;font-size:13px}
a{color:var(--accent)}
.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}
.pill{font-size:12px;padding:1px 8px;border:1px solid var(--line);border-radius:99px;color:var(--muted)}
</style></head><body><main>
<h1>Outreach Portal</h1><p class="sub">Local only. Nothing here is reachable from other computers.</p>

<section class="card"><h2>Resume</h2>
  <div id="resume"></div>
  <label class="drop" id="drop">Drop a PDF here or click to choose one
    <input id="file" type="file" accept="application/pdf" hidden></label>
  <p id="msg" class="muted" style="margin:8px 0 0"></p>
  <p class="muted" style="margin:8px 0 0;font-size:13px">A new upload is attached to emails sent from now on. Emails already sent are unchanged. Older versions are kept below.</p>
</section>

<section class="card"><h2>Queued leads</h2>
  <p class="muted" style="margin:0 0 8px;font-size:13px">Waiting to be emailed. Leads found automatically wait out a grace period first; remove any you don't want contacted.</p>
  <div id="queue" class="muted">Nothing queued.</div></section>
<section class="card"><h2>Previous versions</h2><div id="history" class="muted">None yet.</div></section>
<section class="card"><h2>Agent</h2><div id="agent"></div></section>
<section class="card"><h2>Latest replies</h2><div id="replies" class="muted">None yet.</div></section>
</main>
<script>
const token = new URLSearchParams(location.search).get('t') || '';
const $ = id => document.getElementById(id);
const el = (tag, text, cls) => { const e = document.createElement(tag); if (text !== undefined) e.textContent = text; if (cls) e.className = cls; return e; };
const fmtTime = s => new Date(s * 1000).toLocaleString([], {dateStyle:'medium', timeStyle:'short'});
const fmtSize = b => b > 1e6 ? (b/1e6).toFixed(1) + ' MB' : Math.round(b/1e3) + ' KB';
const row = (k, v, cls) => { const r = el('div', undefined, 'row'); r.append(el('span', k, 'muted'), el('span', v, cls)); return r; };

async function load() {
  const r = await fetch('/api/state', {headers: {'X-Token': token}});
  if (!r.ok) { $('msg').textContent = 'Could not load (is the token in the URL?)'; return; }
  const s = await r.json();
  maxBytes = s.max_bytes;

  const rs = $('resume'); rs.replaceChildren();
  if (s.resume) {
    rs.append(row('Current file', fmtSize(s.resume.size) + ' (id ' + s.resume.sha + ')'));
    rs.append(row('Uploaded', fmtTime(s.resume.uploaded_at)));
    const v = el('div', undefined, 'row'); const a = el('a', 'View current resume'); a.href = '/resume.pdf?t=' + encodeURIComponent(token); a.target = '_blank'; v.append(el('span', '', 'muted'), a); rs.append(v);
  } else {
    rs.append(el('p', 'No resume uploaded yet. Campaigns that attach a resume will not send until you upload one.', 'warn'));
  }
  rs.append(row('Attached by campaigns', s.campaigns_attaching.length ? s.campaigns_attaching.join(', ') : 'none (set attach_resume: true in a campaign file)'));

  const qd = $('queue'); qd.replaceChildren();
  if (!s.queued.length) qd.textContent = 'Nothing queued.';
  s.queued.forEach(x => {
    const box = el('div', undefined, 'row'); const left = el('div');
    left.append(el('div', (x.org || x.email) + (x.name ? ' · ' + x.name : '') + '  (' + x.campaign + ')'),
                el('div', x.email, 'muted'), el('div', x.note, 'muted'),
                el('div', x.send_after * 1000 > Date.now() ? 'sends after ' + fmtTime(x.send_after) : 'ready to send', 'muted'));
    const b = el('button', 'Remove'); b.onclick = async () => {
      b.disabled = true;
      await fetch('/remove', {method: 'POST', headers: {'X-Token': token, 'Content-Type': 'application/json'}, body: JSON.stringify({id: x.id})});
      load(); };
    box.append(left, b); qd.append(box); });

  const h = $('history'); h.replaceChildren();
  const hist = (s.resume && s.resume.history) || [];
  if (!hist.length) h.textContent = 'None yet.';
  hist.forEach(x => { const a = el('a', fmtTime(x.saved_at)); a.href = '/history/' + x.name + '?t=' + encodeURIComponent(token); a.target = '_blank'; const r = el('div', undefined, 'row'); r.append(a, el('span', fmtSize(x.size), 'muted')); h.append(r); });

  const ag = $('agent'); ag.replaceChildren();
  ag.append(row('State', s.paused ? 'PAUSED: ' + s.paused : 'active', s.paused ? 'bad' : 'ok'));
  ag.append(row('Sent today', s.sent_today + ' / ' + s.daily_limit));
  s.leads.forEach(l => ag.append(row(l.campaign + ' · ' + l.status, String(l.n))));

  const rp = $('replies'); rp.replaceChildren();
  if (!s.replies.length) rp.textContent = 'None yet.';
  s.replies.forEach(x => { const box = el('div', undefined, 'row'); const left = el('div'); left.append(el('div', x.who + (x.org ? ' (' + x.org + ')' : '')), el('div', x.text, 'muted')); box.append(left, el('span', x.category.replace('_', ' '), 'pill')); rp.append(box); });
}

let maxBytes = 5 * 1024 * 1024;
async function upload(file) {
  if (file.size > maxBytes) { $('msg').className = 'bad'; $('msg').textContent = 'That file is ' + fmtSize(file.size) + '; the limit is ' + fmtSize(maxBytes) + '.'; return; }
  $('msg').className = 'muted'; $('msg').textContent = 'Uploading...';
  const r = await fetch('/upload', {method: 'POST', headers: {'X-Token': token, 'Content-Type': 'application/pdf'}, body: file});
  const j = await r.json();
  if (r.ok) { $('msg').className = 'ok'; $('msg').textContent = 'Saved. New emails will attach this version.'; load(); }
  else { $('msg').className = 'bad'; $('msg').textContent = j.error || 'Upload failed.'; }
}
$('file').addEventListener('change', e => e.target.files[0] && upload(e.target.files[0]));
const drop = $('drop');
['dragover','dragenter'].forEach(t => drop.addEventListener(t, e => { e.preventDefault(); drop.classList.add('over'); }));
['dragleave','drop'].forEach(t => drop.addEventListener(t, e => { e.preventDefault(); drop.classList.remove('over'); }));
drop.addEventListener('drop', e => e.dataTransfer.files[0] && upload(e.dataTransfer.files[0]));
load(); setInterval(load, 15000);
</script></body></html>
"""
