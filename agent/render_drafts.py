#!/usr/bin/env python3
"""Stage 3.5 — Render drafts.json as a self-contained, RTL-correct HTML page.

The Windows console renders Hebrew left-to-right, garbling it. This turns the
drafts produced by draft_posts.py into a single HTML file you can double-click:
each draft shows in correct right-to-left layout, with a one-click copy button.
No web server, no internet, no dependencies — the data is baked into the file.

draft_posts.py calls write_html() automatically, so you usually don't run this
directly. To re-render an existing drafts.json:

  py agent\\render_drafts.py
"""
import argparse
import html
import json
import sys
from datetime import datetime
from pathlib import Path

from config import DRAFTS_HTML_PATH, DRAFTS_PATH, ensure_utf8_console

PAGE_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  font-family: "Segoe UI", system-ui, Arial, sans-serif;
  max-width: 820px; margin: 0 auto; padding: 24px 16px 64px;
  background: #f4f5f7; color: #1a1a1a; line-height: 1.6;
}
@media (prefers-color-scheme: dark) {
  body { background: #16181c; color: #e8e8e8; }
  .card { background: #20242b !important; border-color: #2c313a !important; }
  .post { background: #161a20 !important; border-color: #2c313a !important; }
  .chip { background: #2a2f38 !important; color: #cfd4dc !important; }
}
header h1 { margin: 0 0 4px; font-size: 22px; }
header .sub { color: #6b7280; font-size: 13px; }
.banner {
  margin: 16px 0 24px; padding: 12px 16px; border-radius: 10px;
  background: #fff4d6; border: 1px solid #f0d488; color: #6b5400;
  font-weight: 600; font-size: 14px;
}
.card {
  background: #fff; border: 1px solid #e3e6ea; border-radius: 14px;
  padding: 18px 20px; margin: 0 0 18px; box-shadow: 0 1px 3px rgba(0,0,0,.05);
}
.card-top {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  margin-bottom: 12px; font-size: 13px; color: #6b7280;
}
.badge {
  font-weight: 700; padding: 2px 10px; border-radius: 999px; font-size: 12px;
  color: #fff;
}
.post {
  /* dir=auto + plaintext: each line picks its own direction, so Hebrew is RTL
     and embedded English/numbers/links still read correctly. */
  unicode-bidi: plaintext;
  white-space: pre-wrap; word-wrap: break-word;
  font-size: 18px; line-height: 1.7;
  background: #fafbfc; border: 1px solid #eceff2; border-radius: 10px;
  padding: 16px; margin: 0 0 12px;
}
.copy-btn {
  cursor: pointer; border: 0; border-radius: 8px; padding: 8px 16px;
  background: #2563eb; color: #fff; font-size: 14px; font-weight: 600;
}
.copy-btn:active { transform: translateY(1px); }
.meta { font-size: 13px; color: #4b5563; margin-top: 14px; }
.meta .label { color: #9aa1ab; }
.chips { margin-top: 6px; }
.chip {
  display: inline-block; background: #eef1f5; color: #374151;
  border-radius: 999px; padding: 2px 10px; margin: 2px 4px 2px 0; font-size: 12px;
}
.flags { margin-top: 10px; }
.flag {
  display: inline-block; background: #fde8e8; color: #9b1c1c;
  border: 1px solid #f5b5b5; border-radius: 8px;
  padding: 3px 10px; margin: 2px 4px 2px 0; font-size: 12px; font-weight: 600;
}
.empty { text-align: center; color: #9aa1ab; padding: 40px; }
"""

COPY_JS = """
function copyDraft(id){
  var el = document.getElementById(id);
  navigator.clipboard.writeText(el.innerText).then(function(){
    var b = document.querySelector('[data-for="'+id+'"]');
    var old = b.textContent; b.textContent = '\\u2713 \\u05d4\\u05d5\\u05e2\\u05ea\\u05e7';
    setTimeout(function(){ b.textContent = old; }, 1500);
  });
}
"""


def _conf_color(conf) -> str:
    if not isinstance(conf, (int, float)):
        return "#6b7280"
    if conf >= 0.8:
        return "#15803d"
    if conf >= 0.6:
        return "#b45309"
    return "#b91c1c"


def _draft_card(i: int, d: dict) -> str:
    e = html.escape
    pid = f"post{i}"
    conf = d.get("confidence")
    conf_s = f"{conf:.0%}" if isinstance(conf, (int, float)) else "?"
    post = e(d.get("post_text", "").strip())

    meta = []
    if d.get("source_headline"):
        meta.append(f'<div class="meta"><span class="label">מבוסס על:</span> '
                    f'{e(d["source_headline"])}</div>')
    if d.get("attribution"):
        meta.append(f'<div class="meta"><span class="label">ייחוס מקור:</span> '
                    f'{e(d["attribution"])}</div>')
    facts = d.get("relays_facts") or []
    if facts:
        chips = "".join(f'<span class="chip">{e(f)}</span>' for f in facts)
        meta.append(f'<div class="meta"><span class="label">עובדות שמועברות '
                    f'(לבדיקה):</span><div class="chips">{chips}</div></div>')
    flags = d.get("review_flags") or []
    flags_html = ""
    if flags:
        items = "".join(f'<span class="flag">⚠ {e(f)}</span>' for f in flags)
        flags_html = f'<div class="flags">{items}</div>'

    return f"""
  <div class="card">
    <div class="card-top">
      <span class="badge" style="background:{_conf_color(conf)}">ביטחון {conf_s}</span>
      <span>טיוטה {i}</span>
      <span>· סטטוס: {e(str(d.get("status", "pending")))}</span>
    </div>
    <div class="post" id="{pid}" dir="auto">{post}</div>
    <button class="copy-btn" data-for="{pid}" onclick="copyDraft('{pid}')">העתק</button>
    {"".join(meta)}
    {flags_html}
  </div>"""


def build_html(payload: dict) -> str:
    drafts = payload.get("drafts", [])
    gen = payload.get("generated_at", "")[:16].replace("T", " ")
    author = html.escape(str(payload.get("author", "")))
    model = html.escape(str(payload.get("model", "")))
    cards = ("".join(_draft_card(i, d) for i, d in enumerate(drafts, 1))
             if drafts else '<div class="empty">אין טיוטות. הרץ קודם את draft_posts.py</div>')
    return f"""<!doctype html>
<html lang="he" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>טיוטות לבדיקה</title>
<style>{PAGE_CSS}</style>
</head>
<body>
  <header>
    <h1>טיוטות לבדיקה</h1>
    <div class="sub">{author} · נוצר {gen} · מודל {model} · {len(drafts)} טיוטות</div>
  </header>
  <div class="banner">⚠ שום דבר לא נשלח. זו תצוגה לבדיקה בלבד — העתק והדבק ידנית מה שתרצה לפרסם.</div>
  {cards}
<script>{COPY_JS}</script>
</body>
</html>"""


def write_html(payload: dict, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(build_html(payload), encoding="utf-8")
    return out_path


def main() -> None:
    ensure_utf8_console()
    ap = argparse.ArgumentParser(description="Render drafts.json as an RTL HTML page.")
    ap.add_argument("--input", default=str(DRAFTS_PATH))
    ap.add_argument("--out", default=str(DRAFTS_HTML_PATH))
    args = ap.parse_args()

    in_path = Path(args.input)
    if not in_path.is_file():
        sys.exit(f"ERROR: {in_path} not found. Run draft_posts.py first.")
    payload = json.loads(in_path.read_text(encoding="utf-8"))
    out = write_html(payload, Path(args.out))
    print(f"Wrote {out}")
    print("Double-click it (or open in any browser) to read the drafts in correct Hebrew.")


if __name__ == "__main__":
    main()
