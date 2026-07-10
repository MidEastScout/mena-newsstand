#!/usr/bin/env python3
"""One-off diagnostic, run from GitHub Actions (the IP that matters).

Kiosko's constructed image URLs (img.kiosko.net/Y/m/d/geo/slug.750.jpg) have
returned 404 for every date since 2026-07-07, freezing four covers on Monday's
edition. This script figures out what still works for those papers:

  1. Kiosko HTML paper pages  -> do they still exist? what image URL do they
     embed now (og:image / img.kiosko.net patterns)? does that image download?
  2. Kiosko URL variants      -> alternate hosts (img2/en5...) and sizes.
  3. Freedom Forum            -> candidate codes for the four papers, plus
     code discovery by scraping the TFP gallery.
  4. PressReader (i.prcdn.co) -> public cover thumbnails keyed by numeric cid,
     discovered from each publication page.

Prints a compact verdict per paper. No files are written.
"""
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
TIMEOUT = 20
MIN_BYTES = 15000
PAUSE = 0.3

today = datetime.now(timezone.utc).date()
DATES = [today, today - timedelta(days=1)]

PAPERS = [
    # (label, kiosko_country_path, kiosko_geo, kiosko_slug)
    ("daily_mail",      "uk", "uk", "daily_mail"),
    ("the_independent", "uk", "uk", "the_independent"),
    ("die_welt",        "de", "de", "die_welt"),
    ("hurriyet",        "tr", "tr", "hurriyet"),
    ("wsj_control",     "us", "us", "wsj"),
]

FF_GUESSES = {
    "daily_mail":      ["UK_DM", "UK_DMAIL", "UK_MAIL", "GBR_DM", "ENG_DM"],
    "the_independent": ["UK_IND", "UK_INDY", "UK_INDEP", "GBR_IND"],
    "die_welt":        ["GER_WELT", "DEU_WELT", "GER_DW", "GER_W"],
    "hurriyet":        ["TUR_HUR", "TUR_HURR", "TUR_H", "TUR_HDN"],
}

PR_SLUGS = {
    "daily_mail":      ["daily-mail"],
    "the_independent": ["the-independent", "independent"],
    "die_welt":        ["die-welt", "welt"],
    "hurriyet":        ["hurriyet", "hurriyet-daily-news"],
}

IMG_RE = re.compile(
    r'https?://[^\s"\'<>()]+?\.(?:jpg|jpeg|png|webp)(?:\?[^\s"\'<>()]*)?', re.I)
OG_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\']og:image["\'][^>]+content=["\']([^"\']+)',
    re.I)
CID_RE = re.compile(r'(?:i\.prcdn\.co/img\?cid=|[?&]cid=)(\d+)', re.I)


def get(session, url, as_image=False, referer=None):
    headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    if referer:
        headers["Referer"] = referer
    headers["Accept"] = ("image/avif,image/webp,image/*,*/*" if as_image
                         else "text/html,application/xhtml+xml,*/*")
    try:
        r = session.get(url, headers=headers, timeout=TIMEOUT)
    except Exception as e:
        return False, f"ERR {type(e).__name__}: {str(e)[:80]}", None
    ct = r.headers.get("Content-Type", "")
    if as_image:
        ok = (r.status_code == 200 and ct.startswith("image/")
              and len(r.content) >= MIN_BYTES)
        return ok, f"{r.status_code} {ct or '?'} {len(r.content)}B", r.content
    return r.status_code == 200, f"{r.status_code} {ct or '?'} {len(r.text)}c", r.text


def main():
    s = requests.Session()

    print("=== 1. Kiosko HTML paper pages ===")
    for label, cpath, geo, slug in PAPERS:
        for host in ("en.kiosko.net", "en5.kiosko.net"):
            page = f"https://{host}/{cpath}/np/{slug}.html"
            ok, info, html = get(s, page, referer="https://en.kiosko.net/")
            print(f"  [{label}] {page} -> {info}")
            if not (ok and html):
                time.sleep(PAUSE)
                continue
            cands = []
            for m in OG_RE.finditer(html):
                cands.append(m.group(1))
            cands += [u for u in IMG_RE.findall(html) if "kiosko" in u]
            seen = set()
            cands = [u for u in cands if not (u in seen or seen.add(u))]
            print(f"      embedded image candidates ({len(cands)}):")
            for u in cands[:8]:
                iok, iinfo, _ = get(s, u, as_image=True,
                                    referer=page)
                print(f"        {'OK ' if iok else '-- '}{u} ({iinfo})")
                time.sleep(PAUSE)
            break  # first host that answered is enough
        time.sleep(PAUSE)

    print("\n=== 2. Kiosko constructed URL variants ===")
    for label, cpath, geo, slug in PAPERS[:2]:   # two papers is representative
        for d in DATES:
            for host in ("img.kiosko.net", "img2.kiosko.net"):
                for size in ("750", "1500", "200"):
                    u = f"https://{host}/{d:%Y/%m/%d}/{geo}/{slug}.{size}.jpg"
                    iok, iinfo, _ = get(s, u, as_image=True,
                                        referer="https://en.kiosko.net/")
                    print(f"  {'OK ' if iok else '-- '}{u} ({iinfo})")
                    time.sleep(PAUSE)

    print("\n=== 3a. Freedom Forum candidate codes ===")
    for label, codes in FF_GUESSES.items():
        for code in codes:
            hit = None
            for d in DATES:
                u = f"https://cdn.freedomforum.org/dfp/jpg{d.day}/lg/{code}.jpg"
                iok, iinfo, _ = get(
                    s, u, as_image=True,
                    referer="https://www.freedomforum.org/todaysfrontpages/")
                if iok:
                    hit = (u, iinfo)
                    break
                time.sleep(PAUSE)
            print(f"  [{label}] {code}: {'OK ' + hit[0] if hit else 'no'}")

    print("\n=== 3b. Freedom Forum gallery discovery ===")
    for probe in ("https://frontpages.freedomforum.org/",
                  "https://frontpages.freedomforum.org/gallery"):
        ok, info, html = get(s, probe)
        print(f"  {probe} -> {info}")
        if ok and html:
            codes = sorted(set(re.findall(
                r'cdn\.freedomforum\.org/dfp/jpg\d+/\w+/([A-Za-z0-9_]+)\.jpg',
                html)))
            print(f"      codes seen ({len(codes)}): {codes[:60]}")
        time.sleep(PAUSE)

    print("\n=== 4. PressReader ===")
    for label, slugs in PR_SLUGS.items():
        for slug in slugs:
            page = f"https://www.pressreader.com/newspapers/n/{slug}"
            ok, info, html = get(s, page,
                                 referer="https://www.pressreader.com/")
            cids = sorted(set(CID_RE.findall(html or "")), key=int) if html else []
            print(f"  [{label}] {page} -> {info} cids={cids[:5]}")
            for cid in cids[:2]:
                u = f"https://i.prcdn.co/img?cid={cid}&page=1&width=600"
                iok, iinfo, _ = get(s, u, as_image=True,
                                    referer="https://www.pressreader.com/")
                print(f"      {'OK ' if iok else '-- '}{u} ({iinfo})")
                time.sleep(PAUSE)
            time.sleep(PAUSE)

    print("\ndone")


if __name__ == "__main__":
    sys.exit(main())
