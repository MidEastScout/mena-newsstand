#!/usr/bin/env python3
"""Stage 4 — Local review dashboard: approve / edit / reject drafts.

A tiny local web app (Python standard library only — no installs, no internet)
that loads drafts.json and lets you review each draft in correct right-to-left
Hebrew. Decisions persist back to drafts.json; every edit is logged to
feedback.json so future drafts can learn from how you change them.

It binds to 127.0.0.1 only — nothing is exposed to the network, and NOTHING is
published. Approving a draft just marks it ready; Stage 5 will add the (always
manual) publish step.

Usage (PowerShell):
  py agent\\review_server.py            # opens http://127.0.0.1:8765 in your browser
  py agent\\review_server.py --port 9000
"""
import argparse
import json
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from config import DRAFTS_PATH, FEEDBACK_PATH, ensure_utf8_console

DRAFTS_FILE = DRAFTS_PATH
FEEDBACK_FILE = FEEDBACK_PATH
VALID_STATUS = {"pending", "approved", "rejected"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_drafts() -> dict:
    if not DRAFTS_FILE.is_file():
        return {"error": "drafts.json not found. Run draft_posts.py first."}
    return json.loads(DRAFTS_FILE.read_text(encoding="utf-8"))


def counts(payload: dict) -> dict:
    c = {"pending": 0, "approved": 0, "rejected": 0}
    for d in payload.get("drafts", []):
        c[d.get("status", "pending")] = c.get(d.get("status", "pending"), 0) + 1
    return c


def log_feedback(entry: dict) -> None:
    """Append a review decision to feedback.json (the feedback-loop record)."""
    if FEEDBACK_FILE.is_file():
        log = json.loads(FEEDBACK_FILE.read_text(encoding="utf-8"))
    else:
        log = {"schema": "mena-agent/feedback@1", "entries": []}
    log["entries"].append(entry)
    FEEDBACK_FILE.write_text(json.dumps(log, ensure_ascii=False, indent=1),
                             encoding="utf-8")


def apply_review(body: dict) -> dict:
    payload = read_drafts()
    if "error" in payload:
        return payload
    drafts = payload.get("drafts", [])
    try:
        idx = int(body["index"])
        draft = drafts[idx]
    except (KeyError, ValueError, IndexError, TypeError):
        return {"error": "invalid draft index"}

    status = body.get("status", "pending")
    if status not in VALID_STATUS:
        return {"error": f"invalid status '{status}'"}

    original = (draft.get("post_text") or "").strip()
    new_text = (body.get("text") if body.get("text") is not None else original).strip()
    edited = bool(new_text) and new_text != original

    draft["status"] = status
    draft["final_text"] = new_text or original
    draft["edited"] = edited
    draft["reviewed_at"] = now_iso()
    DRAFTS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                           encoding="utf-8")

    log_feedback({
        "ts": now_iso(),
        "index": idx,
        "source_headline": draft.get("source_headline", ""),
        "status": status,
        "edited": edited,
        "original_text": original,
        "final_text": new_text or original,
    })
    return {"ok": True, "draft": draft, "summary": counts(payload)}


PAGE_HTML = r"""<!doctype html>
<html lang="he" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>בדיקת טיוטות</title>
<style>
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { font-family: "Segoe UI", system-ui, Arial, sans-serif; max-width: 860px;
  margin: 0 auto; padding: 20px 16px 80px; background: #f4f5f7; color: #1a1a1a; line-height: 1.6; }
@media (prefers-color-scheme: dark) {
  body { background: #16181c; color: #e8e8e8; }
  .card, .bar { background: #20242b; border-color: #2c313a; }
  textarea, .post { background: #161a20; color: #e8e8e8; border-color: #2c313a; }
  .chip { background: #2a2f38; color: #cfd4dc; }
}
header h1 { margin: 0 0 4px; font-size: 22px; }
header .sub { color: #6b7280; font-size: 13px; }
.bar { position: sticky; top: 0; z-index: 5; background: #fff; border: 1px solid #e3e6ea;
  border-radius: 12px; padding: 10px 16px; margin: 14px 0 20px; display: flex;
  align-items: center; gap: 14px; flex-wrap: wrap; font-size: 14px; }
.count { font-weight: 700; }
.count.pending { color: #6b7280; } .count.approved { color: #15803d; } .count.rejected { color: #b91c1c; }
.banner { margin: 0 0 18px; padding: 11px 16px; border-radius: 10px; background: #fff4d6;
  border: 1px solid #f0d488; color: #6b5400; font-weight: 600; font-size: 14px; }
.card { background: #fff; border: 1px solid #e3e6ea; border-radius: 14px; padding: 16px 18px;
  margin: 0 0 16px; box-shadow: 0 1px 3px rgba(0,0,0,.05); transition: opacity .2s; }
.card.rejected { opacity: .5; }
.card.approved { border-color: #86c79b; }
.card-top { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 10px;
  font-size: 13px; color: #6b7280; }
.badge { font-weight: 700; padding: 2px 10px; border-radius: 999px; font-size: 12px; color: #fff; }
.pill { padding: 2px 10px; border-radius: 999px; font-size: 12px; font-weight: 700; }
.pill.pending { background: #e5e7eb; color: #374151; }
.pill.approved { background: #d1fae5; color: #065f46; }
.pill.rejected { background: #fee2e2; color: #991b1b; }
textarea { width: 100%; unicode-bidi: plaintext; white-space: pre-wrap; font-size: 18px;
  line-height: 1.7; font-family: inherit; background: #fafbfc; border: 1px solid #eceff2;
  border-radius: 10px; padding: 14px; resize: vertical; min-height: 90px; }
.btns { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
button { cursor: pointer; border: 0; border-radius: 8px; padding: 8px 16px; font-size: 14px;
  font-weight: 600; }
.approve { background: #16a34a; color: #fff; } .reject { background: #dc2626; color: #fff; }
.copy { background: #2563eb; color: #fff; } .reset { background: #e5e7eb; color: #374151; }
.copyall { background: #2563eb; color: #fff; margin-inline-start: auto; }
.meta { font-size: 13px; color: #4b5563; margin-top: 12px; }
.meta .label { color: #9aa1ab; }
.chip { display: inline-block; background: #eef1f5; color: #374151; border-radius: 999px;
  padding: 2px 10px; margin: 2px 4px 2px 0; font-size: 12px; }
.flag { display: inline-block; background: #fde8e8; color: #9b1c1c; border: 1px solid #f5b5b5;
  border-radius: 8px; padding: 3px 10px; margin: 2px 4px 2px 0; font-size: 12px; font-weight: 600; }
.empty { text-align: center; color: #9aa1ab; padding: 50px; }
.saved { color: #15803d; font-size: 12px; font-weight: 600; margin-inline-start: 6px; }
</style>
</head>
<body>
  <header>
    <h1>בדיקת טיוטות</h1>
    <div class="sub" id="sub"></div>
  </header>
  <div class="banner">⚠ שום דבר לא נשלח. אישור = סימון הטיוטה כמוכנה לפרסום ידני. עריכות נשמרות בעת האישור.</div>
  <div class="bar">
    <span class="count pending">ממתינות: <span id="c-pending">0</span></span>
    <span class="count approved">אושרו: <span id="c-approved">0</span></span>
    <span class="count rejected">נדחו: <span id="c-rejected">0</span></span>
    <button class="copyall" onclick="copyApproved()">העתק את כל המאושרות</button>
  </div>
  <div id="list"></div>
<script>
let DATA = null;

function esc(s){ return (s||"").replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }

async function load(){
  const r = await fetch('/api/drafts'); DATA = await r.json();
  if (DATA.error){ document.getElementById('list').innerHTML =
    '<div class="empty">'+esc(DATA.error)+'</div>'; return; }
  document.getElementById('sub').textContent =
    (DATA.author||'') + ' · נוצר ' + ((DATA.generated_at||'').slice(0,16).replace('T',' ')) +
    ' · ' + (DATA.drafts||[]).length + ' טיוטות';
  render();
}

function render(){
  const list = document.getElementById('list');
  const drafts = DATA.drafts || [];
  if (!drafts.length){ list.innerHTML = '<div class="empty">אין טיוטות. הרץ קודם draft_posts.py</div>'; return; }
  list.innerHTML = drafts.map((d,i) => card(d,i)).join('');
  updateCounts();
}

function card(d,i){
  const status = d.status || 'pending';
  const conf = (typeof d.confidence === 'number') ? Math.round(d.confidence*100)+'%' : '?';
  const confColor = (d.confidence>=0.8)?'#15803d':(d.confidence>=0.6)?'#b45309':'#b91c1c';
  const text = esc(d.final_text || d.post_text || '');
  const facts = (d.relays_facts||[]).map(f => '<span class="chip">'+esc(f)+'</span>').join('');
  const flags = (d.review_flags||[]).map(f => '<span class="flag">⚠ '+esc(f)+'</span>').join('');
  return `
  <div class="card ${status}" id="card${i}">
    <div class="card-top">
      <span class="badge" style="background:${confColor}">ביטחון ${conf}</span>
      <span>טיוטה ${i+1}</span>
      <span class="pill ${status}" id="pill${i}">${({pending:'ממתינה',approved:'אושרה',rejected:'נדחתה'})[status]}</span>
      ${d.edited ? '<span class="saved">נערך</span>' : ''}
    </div>
    <textarea id="ta${i}" dir="auto">${text}</textarea>
    <div class="btns">
      <button class="approve" onclick="decide(${i},'approved')">✓ אשר</button>
      <button class="reject" onclick="decide(${i},'rejected')">✗ דחה</button>
      <button class="copy" onclick="copyOne(${i})">העתק</button>
      <button class="reset" onclick="decide(${i},'pending')">↺ אפס</button>
      <span class="saved" id="ok${i}" style="display:none">✓ נשמר</span>
    </div>
    ${d.source_headline ? '<div class="meta"><span class="label">מבוסס על:</span> '+esc(d.source_headline)+'</div>' : ''}
    ${d.attribution ? '<div class="meta"><span class="label">ייחוס מקור:</span> '+esc(d.attribution)+'</div>' : ''}
    ${facts ? '<div class="meta"><span class="label">עובדות שמועברות (לבדיקה):</span><div>'+facts+'</div></div>' : ''}
    ${flags ? '<div class="meta">'+flags+'</div>' : ''}
  </div>`;
}

async function decide(i, status){
  const text = document.getElementById('ta'+i).value;
  const r = await fetch('/api/draft', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({index:i, status:status, text:text})});
  const res = await r.json();
  if (res.error){ alert(res.error); return; }
  DATA.drafts[i] = res.draft;
  const card = document.getElementById('card'+i);
  card.className = 'card ' + status;
  const pill = document.getElementById('pill'+i);
  pill.className = 'pill ' + status;
  pill.textContent = ({pending:'ממתינה',approved:'אושרה',rejected:'נדחתה'})[status];
  const ok = document.getElementById('ok'+i);
  ok.style.display = 'inline'; setTimeout(()=> ok.style.display='none', 1500);
  updateCounts();
}

function updateCounts(){
  const c = {pending:0, approved:0, rejected:0};
  (DATA.drafts||[]).forEach(d => c[d.status||'pending']++);
  document.getElementById('c-pending').textContent = c.pending;
  document.getElementById('c-approved').textContent = c.approved;
  document.getElementById('c-rejected').textContent = c.rejected;
}

function copyOne(i){
  navigator.clipboard.writeText(document.getElementById('ta'+i).value);
  const ok = document.getElementById('ok'+i); ok.style.display='inline';
  setTimeout(()=> ok.style.display='none', 1500);
}

function copyApproved(){
  const texts = (DATA.drafts||[]).filter(d => d.status==='approved')
    .map(d => d.final_text || d.post_text);
  if (!texts.length){ alert('אין טיוטות מאושרות'); return; }
  navigator.clipboard.writeText(texts.join('\n\n———\n\n'));
  alert('הועתקו ' + texts.length + ' טיוטות מאושרות');
}

load();
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body, ctype: str = "application/json") -> None:
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _local_only(self) -> bool:
        # Refuse requests whose Host isn't loopback, so a random website in the
        # browser can't drive this server.
        host = self.headers.get("Host", "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def do_GET(self) -> None:
        if not self._local_only():
            return self._send(403, "forbidden", "text/plain")
        if self.path == "/":
            self._send(200, PAGE_HTML, "text/html")
        elif self.path == "/api/drafts":
            self._send(200, json.dumps(read_drafts(), ensure_ascii=False))
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self) -> None:
        if not self._local_only():
            return self._send(403, "forbidden", "text/plain")
        if self.path != "/api/draft":
            return self._send(404, "not found", "text/plain")
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or "{}")
        except (ValueError, json.JSONDecodeError):
            return self._send(400, json.dumps({"error": "bad request"}))
        self._send(200, json.dumps(apply_review(body), ensure_ascii=False))

    def log_message(self, *args) -> None:
        pass  # keep the console quiet


def main() -> None:
    ensure_utf8_console()
    ap = argparse.ArgumentParser(description="Local review dashboard for drafts.json.")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="Don't auto-open the browser")
    args = ap.parse_args()

    if not DRAFTS_FILE.is_file():
        sys.exit(f"ERROR: {DRAFTS_FILE} not found. Run draft_posts.py first.")

    port, server = args.port, None
    for candidate in range(args.port, args.port + 12):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", candidate), Handler)
            port = candidate
            break
        except OSError:
            continue
    if server is None:
        sys.exit(f"ERROR: no free port in {args.port}-{args.port + 11}.")

    url = f"http://127.0.0.1:{port}/"
    print(f"Review dashboard : {url}")
    print("Approve / edit / reject your drafts there. Decisions save to drafts.json.")
    print("Press Ctrl+C to stop the server.")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.shutdown()


if __name__ == "__main__":
    main()
