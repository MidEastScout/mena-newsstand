#!/usr/bin/env python3
"""One-off diagnostic, run from GitHub Actions (the IP that matters).

Round 1 findings: Kiosko is frozen site-wide (its pages still embed the
2026-07-06 editions), guessed Freedom Forum codes don't exist, PressReader
serves a JS shell. Round 2 hunts for working replacement sources:

  1. frontpages.com per-paper pages -> extract + test cover image URLs
  2. thepaperboy.com per-paper pages -> extract + test cover image URLs
  3. Freedom Forum gallery/API discovery with retries (429 backoff)

Prints raw candidates so a stable production URL pattern can be derived.
No files are written.
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
PAUSE = 0.4

today = datetime.now(timezone.utc).date()

IMG_RE = re.compile(
    r'https?://[^\s"\'<>()]+?\.(?:jpg|jpeg|png|webp)(?:\?[^\s"\'<>()]*)?', re.I)
OG_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\']og:image["\'][^>]+content=["\']([^"\']+)',
    re.I)
SRC_RE = re.compile(r'<img[^>]+(?:src|data-src)=["\']([^"\']+)["\']', re.I)
JUNK_IMG = re.compile(r'(logo|sprite|icon|favicon|avatar|flag|placeholder|'
                      r'banner|pixel|blank|default|share|social|button)', re.I)


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


def candidates(html):
    urls = [m.group(1) for m in OG_RE.finditer(html)]
    urls += [m.group(1) for m in SRC_RE.finditer(html)]
    urls += IMG_RE.findall(html)
    seen, out = set(), []
    for u in urls:
        u = u.replace("&amp;", "&").strip()
        if u.startswith("//"):
            u = "https:" + u
        if u in seen or JUNK_IMG.search(u) or not u.startswith("http"):
            continue
        seen.add(u)
        out.append(u)
    return out


def probe_page(session, label, page, referer, max_test=6):
    ok, info, html = get(session, page, referer=referer)
    print(f"  [{label}] {page} -> {info}")
    if not (ok and html):
        return
    cands = candidates(html)
    print(f"      {len(cands)} image candidate(s):")
    for u in cands[:max_test]:
        iok, iinfo, _ = get(session, u, as_image=True, referer=page)
        print(f"        {'OK ' if iok else '-- '}{u} ({iinfo})")
        time.sleep(PAUSE)
    # show any date-looking strings near cover URLs, to judge freshness
    dates = sorted(set(re.findall(r'20\d{2}[-/]?\d{2}[-/]?\d{2}', html)))[-6:]
    print(f"      date strings on page: {dates}")


def main():
    s = requests.Session()

    print("=== 1. frontpages.com ===")
    for label, slug in [
        ("daily_mail", "daily-mail"),
        ("the_independent", "the-independent"),
        ("die_welt", "die-welt"),
        ("hurriyet", "hurriyet"),
        ("nyt_control", "the-new-york-times"),
    ]:
        probe_page(s, label, f"https://www.frontpages.com/{slug}/",
                   "https://www.frontpages.com/")
        time.sleep(PAUSE)

    print("\n=== 2. thepaperboy.com ===")
    for label, path in [
        ("daily_mail", "uk/daily-mail/front-pages-today.cfm"),
        ("the_independent", "uk/the-independent/front-pages-today.cfm"),
        ("die_welt", "germany/die-welt/front-pages-today.cfm"),
        ("hurriyet", "turkey/hurriyet/front-pages-today.cfm"),
    ]:
        probe_page(s, label, f"https://www.thepaperboy.com/{path}",
                   "https://www.thepaperboy.com/")
        time.sleep(PAUSE)

    print("\n=== 3. Freedom Forum discovery (with backoff) ===")
    for probe in (
        "https://frontpages.freedomforum.org/",
        "https://frontpages.freedomforum.org/gallery",
        "https://www.freedomforum.org/todaysfrontpages/",
        "https://cdn.freedomforum.org/dfp/json/papers.json",
    ):
        for attempt in (1, 2):
            ok, info, body = get(s, probe)
            print(f"  {probe} -> {info}")
            if "429" not in info:
                break
            time.sleep(8)
        if ok and body:
            codes = sorted(set(re.findall(
                r'(?:cdn\.freedomforum\.org/dfp/jpg\d+/\w+/|"paperId"\s*:\s*")'
                r'([A-Za-z0-9_]+)', body)))
            if codes:
                print(f"      codes seen ({len(codes)}): {codes[:80]}")
        time.sleep(PAUSE)

    print("\ndone")


if __name__ == "__main__":
    sys.exit(main())
