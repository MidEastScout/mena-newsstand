#!/usr/bin/env python3
"""One-off diagnostic, run from GitHub Actions (the IP that matters).

Round 3 confirmed cdn.thepaperboy.com serves today's UK covers as real JPEGs
(deterministic dated URLs) but carries no German/Turkish titles. Round 4:
last hunt for Die Welt / Hürriyet covers via publisher storefronts and
aggregators, plus a paperboy no-Referer check (hotlink robustness).
No files are written.
"""
import re
import sys
import time
from datetime import datetime, timezone

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
SRC_RE = re.compile(r'<img[^>]+(?:src|data-src|data-lazy-src)=["\']([^"\']+)["\']',
                    re.I)


def sniff(data: bytes) -> str:
    if not data:
        return "empty"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return "other"


def get(session, url, referer=None, as_image=False):
    headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9,de;q=0.8,tr;q=0.7",
               "Accept": ("image/avif,image/webp,image/*,*/*" if as_image
                          else "text/html,application/xhtml+xml,*/*")}
    if referer:
        headers["Referer"] = referer
    try:
        r = session.get(url, headers=headers, timeout=TIMEOUT)
    except Exception as e:
        return None, f"ERR {type(e).__name__}: {str(e)[:70]}"
    return r, f"{r.status_code} {r.headers.get('Content-Type','?')} {len(r.content)}B"


def test_image(session, url, referer=None, min_bytes=15000):
    r, info = get(session, url, referer=referer, as_image=True)
    kind = sniff(r.content) if r is not None else "-"
    good = (r is not None and r.status_code == 200
            and kind in ("jpeg", "png", "webp") and len(r.content) >= min_bytes)
    print(f"    {'OK ' if good else '-- '}{url} ({info}, sniff={kind})")
    time.sleep(PAUSE)
    return good


def scrape_and_test(session, label, page, referer=None, max_test=10):
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
        if re.search(r'(logo|icon|favicon|sprite|placeholder|avatar|flag)', u, re.I):
            continue
        seen.add(u)
        out.append(u)
    print(f"      {len(out)} candidate(s):")
    for u in out[:max_test]:
        test_image(session, u, referer=page, min_bytes=12000)


def main():
    s = requests.Session()

    print("=== 1. paperboy without Referer (hotlink check) ===")
    test_image(s, f"https://cdn.thepaperboy.com/frontpages/uk/{Y}/daily_mail.jpg")
    test_image(s, f"https://cdn.thepaperboy.com/frontpages/uk/{Y}/the_independent_lg.jpg")

    print("\n=== 2. Hürriyet publisher/e-paper pages ===")
    for label, page in [
        ("hurriyet e-gazete", "https://www.hurriyet.com.tr/e-gazete/"),
        ("mynet gazeteler", "https://www.mynet.com/gazeteler/hurriyet"),
        ("gazeteilk sayfa", "https://www.gzt.com/gazeteler/hurriyet"),
    ]:
        scrape_and_test(s, label, page)
        time.sleep(PAUSE)

    print("\n=== 3. Die Welt publisher/storefront pages ===")
    for label, page in [
        ("epaper.welt.de", "https://epaper.welt.de/"),
        ("presseplus", "https://www.presseplus.de/die-welt"),
        ("ikiosk search", "https://www.ikiosk.de/de/search?q=welt"),
        ("united kiosk", "https://www.united-kiosk.de/epaper/die-welt/"),
    ]:
        scrape_and_test(s, label, page)
        time.sleep(PAUSE)

    print("\ndone")


if __name__ == "__main__":
    sys.exit(main())
