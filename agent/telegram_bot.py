#!/usr/bin/env python3
"""Stage 5 — Telegram review + publish bot.

Each draft arrives in your private Telegram chat with tappable buttons:
  ✓ Approve   ✗ Reject   ✏️ Edit   📤 Send

- Editing: tap ✏️, then reply with the new text.
- Sending: tap 📤 -> the bot asks you to confirm -> on confirm it posts the
  draft to your Telegram channel and marks it "sent".

Nothing is ever posted without your explicit Send + Confirm. Only you (the
configured owner) can command the bot; everyone else is ignored.

Pure Python over the official Telegram Bot API (long polling with requests) —
no extra dependencies, no Node, no ban risk.

Setup (one time):
  1. In Telegram, message @BotFather -> /newbot -> copy the token.
  2. Put it where the agent can read it (PowerShell):
       $env:TELEGRAM_BOT_TOKEN = "123456:ABC..."
     or add TELEGRAM_BOT_TOKEN=... to a .env file next to this script.
  3. Run:  py agent\\telegram_bot.py
     Message your bot once; it replies with your numeric ID. Set it:
       $env:TELEGRAM_OWNER_ID = "<that number>"   (then restart the bot)
  4. Create a channel, add your bot as an admin, then in the chat send:
       /setchannel @yourchannelname
  5. Generate drafts (py agent\\draft_posts.py), then send /list in Telegram.

Usage:
  py agent\\telegram_bot.py
"""
import argparse
import html
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from config import (DRAFTS_PATH, FEEDBACK_PATH, TELEGRAM_STATE_PATH,
                    ensure_utf8_console, load_dotenv)

API_BASE = "https://api.telegram.org/bot{token}/{method}"
VALID_STATUS = {"pending", "approved", "rejected", "sent"}
STATUS_LABEL = {"pending": "⏳ ממתינה", "approved": "✅ אושרה",
                "rejected": "🚫 נדחתה", "sent": "📤 נשלחה"}

_session = None
_token = ""
# Runtime state: who may command, where to publish, edit conversation state.
STATE = {"owner_id": None, "channel": None, "offset": 0, "awaiting_edit": {}}


# --------------------------------------------------------------------------- #
# Telegram API
# --------------------------------------------------------------------------- #
def api(method: str, **params):
    """Call a Bot API method; return the 'result', raise on API error."""
    global _session
    if _session is None:
        import requests
        _session = requests.Session()
    url = API_BASE.format(token=_token, method=method)
    timeout = params.get("timeout", 0) + 10
    resp = _session.post(url, json=params, timeout=timeout)
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API {method} failed: {data.get('description')}")
    return data.get("result")


def keyboard(rows):
    return {"inline_keyboard": [
        [{"text": t, "callback_data": cb} for t, cb in row] for row in rows]}


def send_message(chat_id, text, buttons=None):
    params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True}
    if buttons is not None:
        params["reply_markup"] = buttons
    return api("sendMessage", **params)


def edit_message(chat_id, message_id, text, buttons=None):
    params = {"chat_id": chat_id, "message_id": message_id, "text": text,
              "parse_mode": "HTML", "disable_web_page_preview": True}
    if buttons is not None:
        params["reply_markup"] = buttons
    try:
        return api("editMessageText", **params)
    except RuntimeError as err:
        if "not modified" in str(err):
            return None
        raise


def answer_callback(callback_id, text=None):
    params = {"callback_query_id": callback_id}
    if text:
        params["text"] = text
    try:
        api("answerCallbackQuery", **params)
    except RuntimeError:
        pass


# --------------------------------------------------------------------------- #
# Drafts store (shares drafts.json / feedback.json with the other stages)
# --------------------------------------------------------------------------- #
def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_drafts():
    if not DRAFTS_PATH.is_file():
        return None
    return json.loads(DRAFTS_PATH.read_text(encoding="utf-8"))


def save_drafts(payload):
    DRAFTS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                           encoding="utf-8")


def log_feedback(entry):
    if FEEDBACK_PATH.is_file():
        log = json.loads(FEEDBACK_PATH.read_text(encoding="utf-8"))
    else:
        log = {"schema": "mena-agent/feedback@1", "entries": []}
    log["entries"].append(entry)
    FEEDBACK_PATH.write_text(json.dumps(log, ensure_ascii=False, indent=1),
                             encoding="utf-8")


def update_draft(idx, status=None, text=None):
    """Apply a status change and/or edit to draft idx; persist + log. Returns draft."""
    payload = read_drafts()
    if not payload:
        return None
    drafts = payload.get("drafts", [])
    if not 0 <= idx < len(drafts):
        return None
    draft = drafts[idx]
    original = (draft.get("post_text") or "").strip()
    edited = False
    if text is not None:
        new_text = text.strip()
        edited = bool(new_text) and new_text != original
        draft["final_text"] = new_text or original
        draft["edited"] = draft.get("edited", False) or edited
    if status is not None:
        draft["status"] = status
        draft["reviewed_at"] = now_iso()
        if status == "sent":
            draft["sent_at"] = now_iso()
    draft.setdefault("final_text", original)
    save_drafts(payload)
    log_feedback({"ts": now_iso(), "index": idx, "channel": "telegram",
                  "source_headline": draft.get("source_headline", ""),
                  "status": draft.get("status"), "edited": edited,
                  "original_text": original, "final_text": draft.get("final_text")})
    return draft


def counts(payload):
    c = {"pending": 0, "approved": 0, "rejected": 0, "sent": 0}
    for d in payload.get("drafts", []):
        c[d.get("status", "pending")] = c.get(d.get("status", "pending"), 0) + 1
    return c


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def draft_text(idx, draft):
    e = html.escape
    conf = draft.get("confidence")
    conf_s = f"{conf:.0%}" if isinstance(conf, (int, float)) else "?"
    status = draft.get("status", "pending")
    body = e((draft.get("final_text") or draft.get("post_text") or "").strip())
    lines = [f"<b>טיוטה {idx + 1}</b> · ביטחון {conf_s} · {STATUS_LABEL.get(status, status)}",
             "", body, ""]
    if draft.get("source_headline"):
        lines.append(f"<i>מבוסס על:</i> {e(draft['source_headline'])}")
    if draft.get("attribution"):
        lines.append(f"<i>מקור:</i> {e(draft['attribution'])}")
    flags = draft.get("review_flags") or []
    if flags:
        lines.append("⚠ " + ", ".join(e(f) for f in flags))
    if draft.get("edited"):
        lines.append("✏️ <i>נערך</i>")
    return "\n".join(lines)


def draft_buttons(idx, status="pending"):
    if status == "sent":
        return keyboard([])
    return keyboard([
        [("✓ אשר", f"ap:{idx}"), ("✗ דחה", f"rj:{idx}")],
        [("✏️ ערוך", f"ed:{idx}"), ("📤 שלח", f"sn:{idx}")],
    ])


def confirm_buttons(idx):
    return keyboard([[("✅ אשר שליחה", f"cf:{idx}"), ("✖️ ביטול", f"cx:{idx}")]])


# --------------------------------------------------------------------------- #
# State persistence
# --------------------------------------------------------------------------- #
def load_state():
    if TELEGRAM_STATE_PATH.is_file():
        saved = json.loads(TELEGRAM_STATE_PATH.read_text(encoding="utf-8"))
        STATE["channel"] = saved.get("channel")


def save_state():
    TELEGRAM_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    TELEGRAM_STATE_PATH.write_text(
        json.dumps({"channel": STATE["channel"]}, ensure_ascii=False, indent=1),
        encoding="utf-8")


def is_owner(user_id):
    return STATE["owner_id"] is not None and user_id == STATE["owner_id"]


# --------------------------------------------------------------------------- #
# Update handling
# --------------------------------------------------------------------------- #
HELP = (
    "🤖 <b>בוט בדיקת טיוטות</b>\n\n"
    "/list — הצג את הטיוטות הנוכחיות עם כפתורים\n"
    "/setchannel @name — הגדר את הערוץ לפרסום (הוסף אותי כמנהל קודם)\n"
    "/status — סיכום: ממתינות / אושרו / נדחו / נשלחו\n"
    "/whoami — הצג את מזהה המשתמש והערוץ\n"
    "/help — עזרה\n\n"
    "לכל טיוטה: ✓ אשר · ✗ דחה · ✏️ ערוך · 📤 שלח.\n"
    "לעריכה: לחץ ✏️ ואז שלח את הטקסט החדש.\n"
    "<b>שום דבר לא נשלח ללא לחיצה על שלח ואז אישור.</b>")


def post_draft(chat_id, idx, draft):
    send_message(chat_id, draft_text(idx, draft),
                 draft_buttons(idx, draft.get("status", "pending")))


def cmd_list(chat_id):
    payload = read_drafts()
    if not payload or not payload.get("drafts"):
        send_message(chat_id, "אין טיוטות. הרץ קודם: py agent\\draft_posts.py")
        return
    c = counts(payload)
    send_message(chat_id, f"📋 {len(payload['drafts'])} טיוטות · "
                 f"ממתינות {c['pending']} · אושרו {c['approved']} · "
                 f"נדחו {c['rejected']} · נשלחו {c['sent']}")
    for idx, draft in enumerate(payload["drafts"]):
        post_draft(chat_id, idx, draft)


def handle_message(msg):
    chat_id = msg["chat"]["id"]
    user_id = msg.get("from", {}).get("id")
    text = (msg.get("text") or "").strip()

    # Setup mode: owner not configured yet — help the user discover their ID.
    if STATE["owner_id"] is None:
        send_message(chat_id,
                     f"המזהה שלך הוא <code>{user_id}</code>.\n"
                     "הגדר אותו והפעל מחדש:\n"
                     "<code>$env:TELEGRAM_OWNER_ID = \"" + str(user_id) + "\"</code>")
        return
    if not is_owner(user_id):
        return  # ignore everyone but the owner

    # If we're waiting for edited text, consume this message as the new text.
    pending = STATE["awaiting_edit"].pop(user_id, None)
    if pending is not None and not text.startswith("/"):
        idx, message_id = pending
        draft = update_draft(idx, text=text)
        if draft:
            edit_message(chat_id, message_id, draft_text(idx, draft),
                         draft_buttons(idx, draft.get("status", "pending")))
            send_message(chat_id, f"✏️ טיוטה {idx + 1} עודכנה.")
        return

    if text in ("/start", "/help"):
        send_message(chat_id, HELP)
    elif text == "/list":
        cmd_list(chat_id)
    elif text == "/status":
        payload = read_drafts() or {"drafts": []}
        c = counts(payload)
        send_message(chat_id, f"ממתינות {c['pending']} · אושרו {c['approved']} · "
                     f"נדחו {c['rejected']} · נשלחו {c['sent']}")
    elif text == "/whoami":
        send_message(chat_id, f"מזהה: <code>{user_id}</code>\n"
                     f"ערוץ פרסום: {html.escape(STATE['channel'] or '(לא הוגדר)')}")
    elif text.startswith("/setchannel"):
        parts = text.split(maxsplit=1)
        if len(parts) == 2 and parts[1].strip():
            STATE["channel"] = parts[1].strip()
            save_state()
            send_message(chat_id, f"✅ ערוץ הפרסום: {html.escape(STATE['channel'])}\n"
                         "ודא שהוספת אותי כמנהל עם הרשאת פרסום.")
        else:
            send_message(chat_id, "שימוש: /setchannel @yourchannelname")
    elif text.startswith("/"):
        send_message(chat_id, "פקודה לא מוכרת. /help")


def handle_callback(cb):
    user_id = cb.get("from", {}).get("id")
    cb_id = cb["id"]
    message = cb.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")
    data = cb.get("data", "")

    if not is_owner(user_id):
        answer_callback(cb_id, "לא מורשה")
        return

    action, _, raw_idx = data.partition(":")
    try:
        idx = int(raw_idx)
    except ValueError:
        answer_callback(cb_id)
        return

    if action == "ap":
        draft = update_draft(idx, status="approved")
        answer_callback(cb_id, "אושרה ✅")
        if draft:
            edit_message(chat_id, message_id, draft_text(idx, draft), draft_buttons(idx, "approved"))
    elif action == "rj":
        draft = update_draft(idx, status="rejected")
        answer_callback(cb_id, "נדחתה 🚫")
        if draft:
            edit_message(chat_id, message_id, draft_text(idx, draft), draft_buttons(idx, "rejected"))
    elif action == "ed":
        STATE["awaiting_edit"][user_id] = (idx, message_id)
        answer_callback(cb_id)
        send_message(chat_id, f"✏️ שלח עכשיו את הטקסט החדש לטיוטה {idx + 1} (או /list לביטול).")
    elif action == "sn":
        if not STATE["channel"]:
            answer_callback(cb_id, "אין ערוץ. /setchannel קודם", )
            send_message(chat_id, "לא הוגדר ערוץ פרסום. השתמש ב: /setchannel @yourchannel")
            return
        payload = read_drafts()
        draft = payload["drafts"][idx] if payload and idx < len(payload["drafts"]) else None
        answer_callback(cb_id)
        preview = html.escape((draft.get("final_text") or draft.get("post_text") or "").strip()) if draft else ""
        edit_message(chat_id, message_id,
                     f"📤 לפרסם את טיוטה {idx + 1} ל־{html.escape(STATE['channel'])}?\n\n{preview}",
                     confirm_buttons(idx))
    elif action == "cf":
        payload = read_drafts()
        draft = payload["drafts"][idx] if payload and idx < len(payload["drafts"]) else None
        if not draft:
            answer_callback(cb_id, "לא נמצאה")
            return
        if draft.get("status") == "sent":
            answer_callback(cb_id, "כבר נשלחה")
            return
        body = (draft.get("final_text") or draft.get("post_text") or "").strip()
        try:
            send_message(STATE["channel"], body)
        except RuntimeError as err:
            answer_callback(cb_id, "שגיאת שליחה")
            send_message(chat_id, f"❌ השליחה נכשלה: {html.escape(str(err))}\n"
                         "ודא שהבוט מנהל בערוץ עם הרשאת פרסום.")
            return
        draft = update_draft(idx, status="sent")
        answer_callback(cb_id, "נשלח ✅")
        edit_message(chat_id, message_id,
                     draft_text(idx, draft) + f"\n\n📤 <i>נשלח ל־{html.escape(STATE['channel'])}</i>",
                     draft_buttons(idx, "sent"))
    elif action == "cx":
        payload = read_drafts()
        draft = payload["drafts"][idx] if payload and idx < len(payload["drafts"]) else None
        answer_callback(cb_id, "בוטל")
        if draft:
            edit_message(chat_id, message_id, draft_text(idx, draft),
                         draft_buttons(idx, draft.get("status", "pending")))


def handle_update(update):
    if "callback_query" in update:
        handle_callback(update["callback_query"])
    elif "message" in update and update["message"].get("text") is not None:
        handle_message(update["message"])


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #
def drain_backlog():
    """Skip past updates received while the bot was offline (avoid replays)."""
    result = api("getUpdates", offset=-1, timeout=0)
    if result:
        STATE["offset"] = result[-1]["update_id"] + 1


def main():
    ensure_utf8_console()
    ap = argparse.ArgumentParser(description="Telegram review + publish bot.")
    ap.add_argument("--owner", type=int, default=None, help="Owner Telegram user id")
    args = ap.parse_args()

    global _token
    load_dotenv()
    _token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not _token:
        sys.exit("ERROR: TELEGRAM_BOT_TOKEN not set.\n"
                 "  Get one from @BotFather, then in PowerShell:\n"
                 '    $env:TELEGRAM_BOT_TOKEN = "123456:ABC..."\n'
                 "  or add TELEGRAM_BOT_TOKEN=... to a .env file next to this script.")

    owner_env = args.owner or os.environ.get("TELEGRAM_OWNER_ID", "").strip()
    STATE["owner_id"] = int(owner_env) if owner_env else None
    load_state()

    me = api("getMe")
    print(f"Bot            : @{me.get('username')} (id {me.get('id')})")
    print(f"Owner          : {STATE['owner_id'] or '(not set — message the bot to learn your ID)'}")
    print(f"Publish channel: {STATE['channel'] or '(not set — use /setchannel @name)'}")
    print("Listening. Press Ctrl+C to stop.")

    drain_backlog()
    if STATE["owner_id"]:
        try:
            send_message(STATE["owner_id"],
                         "🤖 הבוט פעיל. שלח /list כדי לראות את הטיוטות.")
        except RuntimeError:
            pass  # owner hasn't messaged the bot yet; can't initiate

    backoff = 2
    while True:
        try:
            updates = api("getUpdates", offset=STATE["offset"], timeout=25)
            backoff = 2
            for update in updates:
                STATE["offset"] = update["update_id"] + 1
                try:
                    handle_update(update)
                except Exception as exc:  # one bad update must not kill the bot
                    print(f"  ! error handling update: {exc}", file=sys.stderr)
        except KeyboardInterrupt:
            print("\nStopped.")
            return
        except Exception as exc:
            print(f"  ! poll error: {exc}; retrying in {backoff}s", file=sys.stderr)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)


if __name__ == "__main__":
    main()
