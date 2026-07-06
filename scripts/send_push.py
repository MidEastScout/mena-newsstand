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


def _story_headline(s: dict) -> str:
    """The clearest one-line label for a story: the neutral LLM summary when the
    clustering produced one, otherwise the representative outlet's English
    headline."""
    return (s.get("summary") or (s.get("rep") or {}).get("title") or "").strip()


def _coverage(s: dict) -> str:
    """A short '· 11 outlets' tag conveying how widely the story is carried — the
    'most-covered' signal, shown inline so each line says why it's a top story."""
    n = s.get("outlets")
    return f" · {n} outlets" if isinstance(n, int) and n > 1 else ""


def build_payload(stories: list, site_url: str) -> dict:
    lead = stories[0]
    lead_title = _story_headline(lead) or "Top stories across the Middle East"
    # Body names the OTHER top stories (not a vague "and 4 more"), each with how
    # many outlets carry it, so the notification itself is a scannable digest that
    # opens the site on tap. Numbered from 2 (the lead is the title = story 1).
    lines = []
    for i, s in enumerate(stories[1:5], start=2):
        h = _story_headline(s)
        if h:
            lines.append(f"{i}. {h}{_coverage(s)}")
    body = "\n".join(lines) if lines else "Tap to read the latest across the Middle East"
    return {
        "title": f"{lead_title}{_coverage(lead)}",
        "body": body,
        "url": site_url,
        "tag": NOTIF_TAG,
        "stories": [
            {
                "title": _story_headline(s),
                "url": (s.get("rep") or {}).get("url", site_url),
                "outlets": s.get("outlets"),
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


def _endpoint_key(endpoint: str) -> str:
    """A stable per-subscriber key for the timing state (never store raw
    endpoints, which are long and effectively identifiers)."""
    return sha256(endpoint.encode("utf-8")).hexdigest()


def _interval_min(rec: dict, default_min: float) -> float:
    """This subscriber's chosen minutes-between-notifications, clamped to a sane
    window. The practical floor is the site's refresh cadence (~30 min): a value
    below it just means 'every refresh'."""
    try:
        v = float(rec.get("interval"))
    except (TypeError, ValueError):
        return default_min
    return max(15.0, min(v, 10080.0))   # 15 min .. 1 week


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
        default_interval = float(os.environ.get("PUSH_DEFAULT_INTERVAL_MIN", "30"))
    except ValueError:
        default_interval = 30.0

    if not (api and token and vapid_key and vapid_subject):
        log("push not configured (need PUSH_API, PUSH_ADMIN_TOKEN, "
            "VAPID_PRIVATE_KEY, VAPID_SUBJECT) — skipping. This is expected "
            "until you finish the one-time setup in PUSH-NOTIFICATIONS.md.")
        return 0

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

    now = datetime.now(timezone.utc)
    sig = signature(stories)
    prev_subs = (load_json(STATE_PATH).get("subs") or {})

    if not subs:
        log("no subscribers yet — recording state, nothing to send")
        save_state({"last_signature": sig, "updated": now.isoformat(),
                    "subscribers": 0, "subs": {}})
        return 0

    # Each subscriber is gated on THEIR OWN interval and THEIR OWN last-seen story
    # set — so timing is per person and nobody gets the same five headlines twice.
    payload = json.dumps(build_payload(stories, site_url))
    sent = held = 0
    dead = []
    new_subs = {}
    for rec in subs:
        endpoint = rec.get("endpoint")
        keys = rec.get("keys") or {}
        if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
            continue
        key = _endpoint_key(endpoint)
        prev = prev_subs.get(key, {})
        interval = _interval_min(rec, default_interval)

        due = force
        if not force:
            changed = sig != prev.get("last_sig")
            elapsed_ok = True
            ls = prev.get("last_sent_at")
            if ls:
                try:
                    elapsed_ok = (now - datetime.fromisoformat(ls)).total_seconds() / 60 >= interval
                except Exception:
                    elapsed_ok = True  # unparseable → treat as due
            due = changed and elapsed_ok

        if not due:
            new_subs[key] = prev          # carry state forward unchanged
            held += 1
            continue
        try:
            webpush(
                subscription_info={"endpoint": endpoint, "keys": keys},
                data=payload,
                vapid_private_key=vapid_key,
                vapid_claims={"sub": vapid_subject},  # fresh dict per send
                ttl=int(interval * 60),
            )
            sent += 1
            new_subs[key] = {"last_sent_at": now.isoformat(), "last_sig": sig,
                             "interval": interval}
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (404, 410):
                dead.append(endpoint)     # subscription gone — prune, drop its state
            else:
                new_subs[key] = prev      # transient failure — keep state, retry next run
                log(f"push failed for one subscriber (HTTP {status}): {exc}")
        except Exception as exc:
            new_subs[key] = prev
            log(f"unexpected push error for one subscriber: {exc}")

    prune(api, token, dead)

    save_state({
        "last_signature": sig,
        "updated": now.isoformat(),
        "subscribers": len(subs),
        "subs": new_subs,
    })
    log(f"sent {sent}, held {held}, pruned {len(dead)} "
        f"(of {len(subs)} subscribers); lead: "
        f"{(stories[0].get('rep') or {}).get('title', '')!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
