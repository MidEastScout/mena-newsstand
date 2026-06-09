#!/usr/bin/env python3
"""Downloads today's newspaper front-page images server-side and saves them
under frontpages/ so the static site can serve them from its own origin.

Why server-side: Kiosko (and most cover CDNs) block hotlinking by checking the
Referer header, so loading their images directly from the browser fails for all
but a few papers. Fetching them here — from the GitHub Actions runner — and
committing the results sidesteps that entirely. Everything is then served from
GitHub Pages, with no CORS / Referer / hotlink issues.

Writes frontpages/manifest.json describing which papers have a current image.

Source kinds:
  ("ff", CODE)             -> Freedom Forum CDN (keyed by day-of-month)
  ("kiosko", GEO, SLUG)    -> Kiosko CDN (keyed by full date)
  ("frontpages", SLUG)     -> frontpages.com page; og:image extracted from HTML
                              (URL contains a daily random hash, can't predict it)
"""
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

PAPERS = [
    {"id": "asharq", "name": "Asharq Al-Awsat", "loc": "Pan-Arab / Saudi",
     "lang": "ar", "site": "https://aawsat.com",
     "src": [("kiosko", "uk", "asharq_al_awsat"),
             ("kiosko", "asi", "asharq_al_awsat")]},
    {"id": "al_ahram", "name": "Al-Ahram", "loc": "Egypt", "lang": "ar",
     "site": "https://www.ahram.org.eg",
     "src": [("kiosko", "eg", "al_ahram")]},
    {"id": "haaretz", "name": "Haaretz", "loc": "Israel", "lang": "en",
     "site": "https://www.haaretz.com",
     "src": [("ff", "ISR_HA"), ("kiosko", "il", "haaretz")]},
    {"id": "al_quds", "name": "Al-Quds Al-Arabi", "loc": "Pan-Arab / UK",
     "lang": "ar", "site": "https://www.alquds.co.uk",
     "src": [("frontpages", "al-quds"),
             ("kiosko", "uk", "alquds"), ("kiosko", "uk", "al_quds")]},
    {"id": "arab_news", "name": "Arab News", "loc": "Saudi Arabia", "lang": "en",
     "site": "https://www.arabnews.com",
     "src": [("ff", "SAU_AN"), ("frontpages", "arab-news"),
             ("kiosko", "asi", "arab_news")]},
    {"id": "al_anba", "name": "Al-Anba", "loc": "Kuwait", "lang": "ar",
     "site": "https://www.alanba.com.kw",
     "src": [("kiosko", "asi", "al_anba"), ("kiosko", "asi", "al_anbaa"),
             ("kiosko", "kw", "al_anba")]},
    {"id": "the_national", "name": "The National", "loc": "UAE", "lang": "en",
     "site": "https://www.thenationalnews.com",
     "src": [("ff", "UAE_TN"), ("kiosko", "asi", "the_national")]},
    {"id": "annahar", "name": "An-Nahar", "loc": "Lebanon", "lang": "ar",
     "site": "https://www.annahar.com",
     "src": [("kiosko", "asi", "nahar"), ("kiosko", "asi", "an_nahar"),
             ("kiosko", "lb", "nahar")]},
    {"id": "hurriyet", "name": "Hürriyet", "loc": "Turkey", "lang": "tr",
     "site": "https://www.hurriyetdailynews.com",
     "src": [("kiosko", "tr", "hurriyet")]},
    {"id": "tehran_times", "name": "Tehran Times", "loc": "Iran", "lang": "en",
     "site": "https://www.tehrantimes.com",
     "src": [("kiosko", "ir", "tehran_times")]},
    {"id": "nyt", "name": "New York Times", "loc": "USA", "lang": "en",
     "site": "https://www.nytimes.com",
     "src": [("ff", "NY_NYT"), ("kiosko", "us", "newyork_times")]},
    {"id": "wsj", "name": "Wall Street Journal", "loc": "USA", "lang": "en",
     "site": "https://www.wsj.com",
     "src": [("ff", "WSJ"), ("kiosko", "us", "wsj")]},
    {"id": "ft", "name": "Financial Times", "loc": "UK", "lang": "en",
     "site": "https://www.ft.com",
     "src": [("frontpages", "financial-times"),
             ("kiosko", "uk", "ft_uk"), ("kiosko", "us", "ft_us")]},
    {"id": "guardian", "name": "The Guardian", "loc": "UK", "lang": "en",
     "site": "https://www.theguardian.com",
     "src": [("kiosko", "uk", "guardian"), ("kiosko", "uk", "observer")]},
    {"id": "lemonde", "name": "Le Monde", "loc": "France", "lang": "fr",
     "site": "https://www.lemonde.fr",
     "src": [("frontpages", "le-monde"), ("kiosko", "fr", "lemonde")]},
]

OUT_DIR = Path(__file__).parent.parent / "frontpages"
MIN_BYTES = 12000
TIMEOUT = 25
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

OG_RE = re.compile(
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
    re.I)
OG_RE2 = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
    re.I)


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
    if "frontpages.com" in url:
        return "https://www.frontpages.com/"
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


def fetch_frontpages_cover(session: requests.Session, slug: str):
    """Fetch paper page on frontpages.com, extract og:image URL, download it."""
    page_url = f"https://www.frontpages.com/{slug}/"
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,*/*",
        "Referer": "https://www.frontpages.com/",
    }
    try:
        r = session.get(page_url, headers=headers, timeout=TIMEOUT)
    except Exception as exc:
        print(f"      ! {page_url} -> {exc}", file=sys.stderr)
        return None, None
    if r.status_code != 200:
        print(f"      - {page_url} -> {r.status_code}")
        return None, None
    img_url = next(
        (m for rx in (OG_RE, OG_RE2) for m in rx.findall(r.text)),
        None,
    )
    if not img_url:
        print(f"      - {page_url} -> no og:image found")
        return None, None
    data = try_download(session, img_url)
    return data, img_url


def main():
    OUT_DIR.mkdir(exist_ok=True)
    today = datetime.now(timezone.utc).date()
    dates = [today, today - timedelta(days=1)]
    session = requests.Session()
    manifest = {"updated": datetime.now(timezone.utc).isoformat(), "papers": []}

    for p in PAPERS:
        dest = OUT_DIR / f"{p['id']}.jpg"
        got = used_url = used_date = None

        for d in dates:
            for src in p["src"]:
                if src[0] == "frontpages":
                    data, img_url = fetch_frontpages_cover(session, src[1])
                    if data:
                        got, used_url, used_date = data, img_url, d.isoformat()
                        break
                else:
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
            ok = dest.exists()
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
