#!/usr/bin/env python3
"""One-off diagnostic, run from GitHub Actions (the IP that matters).

Round 2 found cdn.thepaperboy.com serving TODAY's UK covers via a clean dated
URL (Content-Type: application/octet-stream, so bytes must be sniffed).
Round 3:
  1. confirm the paperboy bytes are real JPEGs (magic bytes + size)
  2. look for die_welt / hurriyet under paperboy country paths
  3. probe gazeteoku.com (TR aggregator) for Hürriyet
  4. probe ikiosk.de (Axel Springer's shop) for Die Welt's cover
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
PAUSE = 0.35

today = datetime.now(timezone.utc).date()
Y = f"{today:%Y%m%d}"

IMG_RE = re.compile(
    r'https?://[^\s"\'<>()]+?\.(?:jpg|jpeg|png|webp)(?:\?[^\s"\'<>()]*)?', re.I)
OG_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\']og:image["\'][^>]+content=["\']([^"\']+)',
    re.I)
SRC_RE = re.compile(r'<img[^>]+(?:src|data-src)=["\']([^"\']+)["\']', re.I)


def sniff(data: bytes) -> str:
    if not data:
        return "empty"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:5] == b"<!DOC" or data[:5] == b"<html":
        return "html"
    return "other:" + data[:8].hex()


def get(session, url, referer=None, as_image=False):
    headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
               "Accept": ("image/avif,image/webp,image/*,*/*" if as_image
                          else "text/html,application/xhtml+xml,*/*")}
    if referer:
        headers["Referer"] = referer
    try:
        r = session.get(url, headers=headers, timeout=TIMEOUT)
    except Exception as e:
        return None, f"ERR {type(e).__name__}: {str(e)[:70]}"
    return r, f"{r.status_code} {r.headers.get('Content-Type','?')} {len(r.content)}B"


def test_image(session, url, referer=None):
    r, info = get(session, url, referer=referer, as_image=True)
    kind = sniff(r.content) if r is not None else "-"
    good = (r is not None and r.status_code == 200
            and kind in ("jpeg", "png", "webp") and len(r.content) >= 15000)
    print(f"    {'OK ' if good else '-- '}{url} ({info}, sniff={kind})")
    time.sleep(PAUSE)
    return good


def scrape_and_test(session, label, page, referer, keep=None, max_test=8):
    r, info = get(session, page, referer=referer)
    print(f"  [{label}] {page} -> {info}")
    if r is None or r.status_code != 200:
        return
    html = r.text
    urls = [m.group(1) for m in OG_RE.finditer(html)]
    urls += [m.group(1) for m in SRC_RE.finditer(html)]
    urls += IMG_RE.findall(html)
    seen, out = set(), []
    for u in urls:
        u = u.replace("&amp;", "&").strip()
        if u.startswith("//"):
            u = "https:" + u
        if not u.startswith("http") or u in seen:
            continue
        if keep and not re.search(keep, u, re.I):
            continue
        seen.add(u)
        out.append(u)
    print(f"      {len(out)} candidate(s) kept:")
    for u in out[:max_test]:
        test_image(session, u, referer=page)


def main():
    s = requests.Session()

    print(f"=== 1. paperboy UK byte-sniff (date {Y}) ===")
    for slug in ("daily_mail", "the_independent"):
        for suffix in ("", "_lg"):
            test_image(
                s, f"https://cdn.thepaperboy.com/frontpages/uk/{Y}/{slug}{suffix}.jpg",
                referer="https://www.thepaperboy.com/")

    print("\n=== 2. paperboy country-path guesses for die_welt / hurriyet ===")
    for country in ("germany", "de", "turkey", "tr", "world"):
        for slug in ("die_welt", "die-welt", "welt", "hurriyet"):
            test_image(
                s, f"https://cdn.thepaperboy.com/frontpages/{country}/{Y}/{slug}.jpg",
                referer="https://www.thepaperboy.com/")

    print("\n=== 3. gazeteoku.com for Hürriyet ===")
    scrape_and_test(s, "hurriyet", "https://www.gazeteoku.com/gazete/hurriyet",
                    "https://www.gazeteoku.com/", keep=r"hurriyet|manset|gazete")

    print("\n=== 4. ikiosk.de for Die Welt ===")
    scrape_and_test(s, "die_welt", "https://www.ikiosk.de/de/epaper/die-welt.html",
                    "https://www.ikiosk.de/", keep=r"welt|cover|issue")

    print("\ndone")


if __name__ == "__main__":
    sys.exit(main())
