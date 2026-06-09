#!/usr/bin/env python3
"""Downloads today's newspaper front-page images server-side and saves them
under frontpages/ so the static site can serve them from its own origin.

Why server-side: Kiosko and Freedom Forum block hotlinking by checking the
Referer header, so loading their images directly from the browser fails.
Fetching them here — from the GitHub Actions runner — and committing the results
sidesteps that entirely. Everything is then served from GitHub Pages.

Only papers whose covers are actually reachable from a datacenter IP are listed.
Many MENA dailies (Asharq, Al-Ahram, Al-Quds, Arab News, Al-Anba, An-Nahar,
Tehran Times) aren't carried by Kiosko/Freedom Forum or block our requests, and
paywalled titles (FT, Le Monde) are hotlink-protected — so they're omitted
rather than shown as permanent "not available" placeholders.

Writes frontpages/manifest.json describing which papers have a current image.

Source kinds:
  ("ff", CODE)          -> Freedom Forum CDN (keyed by day-of-month)
  ("kiosko", GEO, SLUG) -> Kiosko CDN (keyed by full date)
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

PAPERS = [
    {"id": "haaretz", "name": "Haaretz", "loc": "Israel", "lang": "en",
     "site": "https://www.haaretz.com",
     "src": [("ff", "ISR_HA"), ("kiosko", "il", "haaretz")]},
    {"id": "the_national", "name": "The National", "loc": "UAE", "lang": "en",
     "site": "https://www.thenationalnews.com",
     "src": [("ff", "UAE_TN"), ("kiosko", "asi", "the_national")]},
    {"id": "hurriyet", "name": "Hürriyet", "loc": "Turkey", "lang": "tr",
     "site": "https://www.hurriyetdailynews.com",
     "src": [("kiosko", "tr", "hurriyet")]},
    {"id": "nyt", "name": "New York Times", "loc": "USA", "lang": "en",
     "site": "https://www.nytimes.com",
     "src": [("ff", "NY_NYT"), ("kiosko", "us", "newyork_times")]},
    {"id": "wsj", "name": "Wall Street Journal", "loc": "USA", "lang": "en",
     "site": "https://www.wsj.com",
     "src": [("ff", "WSJ"), ("kiosko", "us", "wsj")]},
    {"id": "guardian", "name": "The Guardian", "loc": "UK", "lang": "en",
     "site": "https://www.theguardian.com",
     "src": [("kiosko", "uk", "guardian"), ("kiosko", "uk", "observer")]},
]

OUT_DIR = Path(__file__).parent.parent / "frontpages"
MIN_BYTES = 12000
TIMEOUT = 25
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def candidate_url(src, d) -> str:
    if src[0] == "ff":
        return f"https://cdn.freedomforum.org/dfp/jpg{d.day}/lg/{src[1]}.jpg"
    if src[0] == "kiosko":
        return f"https://img.kiosko.net/{d:%Y/%m/%d}/{src[1]}/{src[2]}.750.jpg"
    raise ValueError(src)


def referer_for(url: str):
    if "kiosko.net" in url:
        return "https://en.kiosko.net/"
    if "freedomforum.org" in url:
        return "https://www.freedomforum.org/todaysfrontpages/"
    return None


def try_download(session: requests.Session, url: str):
    headers = {"User-Agent": UA, "Accept": "image/avif,image/webp,image/*,*/*"}
    ref = referer_for(url)
    if ref:
        headers["Referer"] = ref
    try:
        r = session.get(url, headers=headers, timeout=TIMEOUT)
    except Exception as exc:
        print(f"      ! {url} -> {exc}", file=sys.stderr)
        return None
    ct = r.headers.get("Content-Type", "")
    if r.status_code == 200 and ct.startswith("image/") and len(r.content) >= MIN_BYTES:
        return r.content
    print(f"      - {url} -> {r.status_code} {ct or '?'} {len(r.content)}B")
    return None


def main():
    OUT_DIR.mkdir(exist_ok=True)
    today = datetime.now(timezone.utc).date()
    dates = [today, today - timedelta(days=1)]   # today, then yesterday fallback
    session = requests.Session()
    manifest = {"updated": datetime.now(timezone.utc).isoformat(), "papers": []}

    for p in PAPERS:
        dest = OUT_DIR / f"{p['id']}.jpg"
        got = used_url = used_date = None
        for d in dates:
            for src in p["src"]:
                url = candidate_url(src, d)
                data = try_download(session, url)
                if data:
                    got, used_url, used_date = data, url, d.isoformat()
                    break
            if got:
                break

        if got:
            dest.write_bytes(got)
            print(f"  + {p['name']}: {len(got)}B from {used_url}")
            ok = True
        else:
            ok = dest.exists()   # keep yesterday's image if today's failed
            print(f"  x {p['name']}: {'kept previous image' if ok else 'no image'}",
                  file=sys.stderr)

        manifest["papers"].append({
            "id": p["id"], "name": p["name"], "loc": p["loc"], "lang": p["lang"],
            "site": p["site"], "ok": ok, "src": used_url, "date": used_date,
        })

    (OUT_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    n_ok = sum(1 for x in manifest["papers"] if x["ok"])
    print(f"\nFront pages: {n_ok}/{len(PAPERS)} available")


if __name__ == "__main__":
    main()
