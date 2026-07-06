#!/usr/bin/env python3
"""Send the hourly "top stories" web-push notification.

Runs inside the GitHub Action after build_topstories.py. It:

  1. Reads the freshly built top_stories.json.
  2. Decides whether to send, using state/push_state.json:
       • only if the set of top stories actually CHANGED since the last push
         (so users never get the same five headlines twice), and
       • not more often than PUSH_MIN_INTERVAL_MIN minutes (default 60) — the
         "hourly" cap, even though the workflow itself runs every ~30 min.
  3. Fetches every subscription from the Cloudflare Worker.
  4. Pushes a "<lead headline> / and N more top stories" notification that
     opens the site, via VAPID + pywebpush.
  5. Prunes subscriptions the push service reports as gone (410/404).
  6. Records what it sent so the next run can dedupe.

Everything is best-effort and self-disabling: if the push secrets are not
configured, or pywebpush is missing, it prints a notice and exits 0 so a
newsstand refresh is never blocked by notifications.

Environment (set as GitHub repo secrets, wired in fetch-headlines.yml):
    PUSH_API             https://mena-push.<sub>.workers.dev   (Worker base URL)
    PUSH_ADMIN_TOKEN     bearer for the Worker's /subscriptions and /prune
    VAPID_PRIVATE_KEY    base64url private key (from gen_vapid_keys.py)
    VAPID_SUBJECT        mailto:you@example.com  (contact for the push service)
Optional:
    SITE_URL             click-through target (default: the live GitHub Pages URL)
    PUSH_MIN_INTERVAL_MIN minimum minutes between pushes (default 60)
    FORCE_PUSH           "true" to bypass the change + interval gates (testing)
"""
import json
import os
import sys
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

import requests

ROOT = Path(__file__).parent.parent
TOP_STORIES = ROOT / "top_stories.json"
STATE_PATH = ROOT / "state" / "push_state.json"

DEFAULT_SITE_URL = "https://mideastscout.github.io/mena-newsstand/"
NOTIF_TAG = "mena-top-stories"
TOP_N = 5


def log(msg: str) -> None:
    print(f"[send_push] {msg}", flush=True)


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def top_stories() -> list:
    data = load_json(TOP_STORIES)
    return (data.get("stories") or [])[:TOP_N]


def signature(stories: list) -> str:
    """Stable fingerprint of the current top set — the representative URLs, in
    order. If none of the top-5 leading links move, we do not notify again."""
    joined = "|".join((s.get("rep") or {}).get("url", "") for s in stories)
    return sha256(joined.encode("utf-8")).hexdigest()


def build_payload(stories: list, site_url: str) -> dict:
    lead = stories[0]
    lead_title = (lead.get("rep") or {}).get("title", "Top stories")
    more = len(stories) - 1
    if more > 0:
        body = f"and {more} more top {'story' if more == 1 else 'stories'} across the Middle East"
    else:
        body = "Tap to read the latest across the Middle East"
    return {
        "title": lead_title,
        "body": body,
        "url": site_url,
        "tag": NOTIF_TAG,
        "stories": [
            {
                "title": (s.get("rep") or {}).get("title", ""),
                "url": (s.get("rep") or {}).get("url", site_url),
            }
            for s in stories
        ],
    }


def fetch_subscriptions(api: str, token: str) -> list:
    r = requests.get(
        api.rstrip("/") + "/subscriptions",
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json().get("subscriptions", [])


def prune(api: str, token: str, endpoints: list) -> None:
    if not endpoints:
        return
    try:
        requests.post(
            api.rstrip("/") + "/prune",
            headers={"Authorization": f"Bearer {token}", "content-type": "application/json"},
            data=json.dumps({"endpoints": endpoints}),
            timeout=20,
        )
        log(f"pruned {len(endpoints)} dead subscription(s)")
    except Exception as exc:
        log(f"prune failed (non-fatal): {exc}")


def main() -> int:
    stories = top_stories()
    if not stories:
        log("no top stories available — nothing to send")
        return 0

    api = os.environ.get("PUSH_API", "").strip()
    token = os.environ.get("PUSH_ADMIN_TOKEN", "").strip()
    vapid_key = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
    vapid_subject = os.environ.get("VAPID_SUBJECT", "").strip()
    site_url = os.environ.get("SITE_URL", "").strip() or DEFAULT_SITE_URL
    force = os.environ.get("FORCE_PUSH", "").lower() == "true"
    try:
        min_interval = int(os.environ.get("PUSH_MIN_INTERVAL_MIN", "60"))
    except ValueError:
        min_interval = 60

    if not (api and token and vapid_key and vapid_subject):
        log("push not configured (need PUSH_API, PUSH_ADMIN_TOKEN, "
            "VAPID_PRIVATE_KEY, VAPID_SUBJECT) — skipping. This is expected "
            "until you finish the one-time setup in PUSH-NOTIFICATIONS.md.")
        return 0

    now = datetime.now(timezone.utc)
    sig = signature(stories)
    state = load_json(STATE_PATH)
    last_sig = state.get("last_signature")
    last_sent = state.get("last_sent_at")

    if not force:
        if sig == last_sig:
            log("top stories unchanged since last push — skipping")
            return 0
        if last_sent:
            try:
                elapsed_min = (now - datetime.fromisoformat(last_sent)).total_seconds() / 60
                if elapsed_min < min_interval:
                    log(f"last push was {elapsed_min:.0f} min ago (< {min_interval}); "
                        "holding to keep the hourly cap")
                    return 0
            except Exception:
                pass  # unparseable timestamp → treat as due

    # Import the sender lazily so a missing dependency degrades to a notice
    # instead of crashing the workflow step.
    try:
        from pywebpush import WebPushException, webpush
    except Exception as exc:
        log(f"pywebpush unavailable ({exc}) — skipping. Add 'pywebpush' to "
            "requirements.txt to enable notifications.")
        return 0

    try:
        subs = fetch_subscriptions(api, token)
    except Exception as exc:
        log(f"could not fetch subscriptions from the Worker: {exc}")
        return 0

    if not subs:
        log("no subscribers yet — recording state, nothing to send")
        save_state({"last_signature": sig, "last_sent_at": now.isoformat(),
                    "last_subscribers": 0})
        return 0

    payload = json.dumps(build_payload(stories, site_url))
    sent, dead = 0, []
    for rec in subs:
        endpoint = rec.get("endpoint")
        keys = rec.get("keys") or {}
        if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
            continue
        try:
            webpush(
                subscription_info={"endpoint": endpoint, "keys": keys},
                data=payload,
                vapid_private_key=vapid_key,
                vapid_claims={"sub": vapid_subject},  # fresh dict per send
                ttl=3600,
            )
            sent += 1
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (404, 410):
                dead.append(endpoint)  # subscription gone — prune it
            else:
                log(f"push failed for one subscriber (HTTP {status}): {exc}")
        except Exception as exc:
            log(f"unexpected push error for one subscriber: {exc}")

    prune(api, token, dead)

    save_state({
        "last_signature": sig,
        "last_sent_at": now.isoformat(),
        "last_subscribers": len(subs),
        "last_lead": (stories[0].get("rep") or {}).get("title", ""),
    })
    log(f"sent {sent}/{len(subs)} notification(s); lead: "
        f"{(stories[0].get('rep') or {}).get('title', '')!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
