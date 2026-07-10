#!/usr/bin/env python3
"""Downloads today's newspaper front-page images server-side and saves them
under frontpages/ so the static site can serve them from its own origin.

Why server-side: Kiosko and Freedom Forum block hotlinking by checking the
Referer header, so loading their images directly from the browser fails.
Fetching them here — from the GitHub Actions runner — and committing the results
sidesteps that entirely. Everything is then served from GitHub Pages.

Only papers whose covers are actually reachable from a datacenter IP are listed
(verified with scripts/probe_frontpages.py). Titles that block datacenter IPs or
aren't carried by either source are omitted rather than shown as permanent
"not available" placeholders.

Writes frontpages/manifest.json describing which papers have a current image.

Source kinds (all probe-confirmed reachable from a datacenter IP):
  ("ff", CODE)            -> Freedom Forum CDN (keyed by day-of-month)
  ("kiosko", GEO, SLUG)   -> Kiosko CDN (keyed by full date)
  ("kioskopage", CC, SLUG)-> Kiosko's HTML paper page, scraped for the current
                             cover URL. Only accepts an image dated the day
                             being fetched, so a frozen/stale Kiosko can never
                             smuggle in an old edition — but if Kiosko ever
                             changes its image URL scheme again this kind
                             adapts automatically where the constructed
                             ("kiosko", ...) URLs would 404 forever.
  ("paperboy", CC, SLUG)  -> thepaperboy.com CDN (keyed by full date):
                             cdn.thepaperboy.com/frontpages/CC/YYYYMMDD/SLUG.jpg
                             (added 2026-07-10 when Kiosko froze site-wide; the
                             _lg variant is fetched first, ~230 KB)
  ("gztpage", SLUG)       -> gzt.com/gazeteler/SLUG (Turkish front-page
                             aggregator), scraped for img.piri.net scans whose
                             URL path carries the edition date. Same
                             date-verification rule as kioskopage: only an
                             image dated the day being fetched is accepted.
  ("gulftimes",)          -> Gulf Times' own CDN: dated page-1 JPEG of the
                             main section (gulf-times.com/pdf/Y/m/d/main-Ymd-N.jpeg)
  ("sgpdf",)              -> Saudi Gazette's own dated PDF; page 1 is rendered to
                             JPEG locally (needs pypdfium2; best-effort)

Fill-only mode (FRONTPAGES_FILL_ONLY=true)
------------------------------------------
Newspapers don't all reach the upstream CDNs at the same hour: Gulf titles are
scanned before dawn UTC, but US papers (Freedom Forum) and several European /
Turkish titles (Kiosko) aren't posted until hours later. A single morning grab
therefore captures the early risers on today's date and falls back to
*yesterday* for the late ones — and they stay a day behind until the next fixed
run.

To close that gap the workflow re-runs this script opportunistically on its
normal ~30-min cycle with FRONTPAGES_FILL_ONLY=true. In that mode any paper
whose committed cover is already today's edition is kept as-is (no request),
and only the stragglers are retried — so each title upgrades to today within
one cycle of appearing upstream, and once every cover is current the script
makes no network calls at all. The fixed morning/afternoon runs still do a full
refresh (FILL_ONLY unset/false) to catch late re-plates.
"""
import json
import os
import re
import shutil
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

PAPERS = [
    # ——— Middle East ———
    # Freedom Forum receives no weekend scans for the UAE titles (probed
    # 2026-07-05: both codes serve Friday's edition all weekend, back to normal
    # Monday). no_print here reflects upstream scan availability, not the print
    # schedule, so weekends don't raise false "dead source" warnings.
    {"id": "the_national", "name": "The National", "loc": "UAE", "lang": "en",
     "site": "https://www.thenationalnews.com", "no_print": {5, 6},
     "src": [("ff", "UAE_TN"), ("kiosko", "asi", "the_national")]},
    {"id": "gulf_news", "name": "Gulf News", "loc": "UAE", "lang": "en",
     "site": "https://gulfnews.com", "no_print": {5, 6},
     "src": [("ff", "UAE_GN")]},
    {"id": "gulf_times", "name": "Gulf Times", "loc": "Qatar", "lang": "en",
     "site": "https://www.gulf-times.com", "src": [("gulftimes",)]},
    {"id": "saudi_gazette", "name": "Saudi Gazette", "loc": "Saudi Arabia", "lang": "en",
     "site": "https://saudigazette.com.sa", "src": [("sgpdf",)]},
    {"id": "kuwait_times", "name": "Kuwait Times", "loc": "Kuwait", "lang": "en",
     "site": "https://www.kuwaittimes.com", "src": [("ff", "KUW_KT")]},
    {"id": "daily_sabah", "name": "Daily Sabah", "loc": "Turkey", "lang": "en",
     "site": "https://www.dailysabah.com", "src": [("ff", "TUR_DS")]},
    # Kiosko froze site-wide on 2026-07-06 (its pages kept serving Monday's
    # editions; every constructed URL for later dates 404s), so gzt.com is the
    # primary now (probed 2026-07-10: serves today's Hürriyet scan to a
    # datacenter IP). Kiosko stays as fallback and self-heals if it resumes.
    {"id": "hurriyet", "name": "Hürriyet", "loc": "Turkey", "lang": "tr",
     "site": "https://www.hurriyet.com.tr",
     "src": [("gztpage", "hurriyet"),
             ("kiosko", "tr", "hurriyet"),
             ("kioskopage", "tr", "hurriyet")]},

    # ——— United States (Freedom Forum — all probe-confirmed) ———
    {"id": "nyt", "name": "New York Times", "loc": "USA", "lang": "en",
     "site": "https://www.nytimes.com",
     "src": [("ff", "NY_NYT"), ("kiosko", "us", "newyork_times")]},
    # WSJ has no Sunday print edition; USA Today prints no weekend editions at
    # all (the Friday paper is the weekend edition) — those aren't stale covers,
    # there is simply no newer edition to fetch. no_print lists the weekdays
    # (Mon=0..Sun=6) with no NEW edition expected, so the staleness warning
    # counts only genuinely missed print days.
    {"id": "wsj", "name": "Wall Street Journal", "loc": "USA", "lang": "en",
     "site": "https://www.wsj.com", "no_print": {6},
     "src": [("ff", "WSJ"), ("kiosko", "us", "wsj")]},
    {"id": "usa_today", "name": "USA Today", "loc": "USA", "lang": "en",
     "site": "https://www.usatoday.com", "no_print": {5, 6},
     "src": [("ff", "USAT"), ("kiosko", "us", "usa_today")]},

    # ——— Europe ———
    # UK titles moved to thepaperboy.com when Kiosko froze on 2026-07-06
    # (probed 2026-07-10: paperboy serves today's editions from a datacenter IP
    # as deterministic dated URLs). Kiosko stays as fallback for redundancy.
    {"id": "the_independent", "name": "The Independent", "loc": "UK", "lang": "en",
     "site": "https://www.independent.co.uk",
     "src": [("paperboy", "uk", "the_independent"),
             ("kiosko", "uk", "the_independent"),
             ("kioskopage", "uk", "the_independent")]},
    {"id": "daily_mail", "name": "Daily Mail", "loc": "UK", "lang": "en",
     "site": "https://www.dailymail.co.uk",
     "src": [("paperboy", "uk", "daily_mail"),
             ("kiosko", "uk", "daily_mail"),
             ("kioskopage", "uk", "daily_mail")]},
    # No reachable replacement source found for Die Welt (probed 2026-07-10:
    # paperboy has no German titles, epaper.welt.de is a JS app, kiosk
    # storefronts 404/time out). Until Kiosko resumes this cover stays on its
    # last edition — honestly date-labelled on the site, tracked by the
    # auto-opened staleness issue, and self-healing via kioskopage the moment
    # Kiosko publishes again or changes its URL scheme.
    {"id": "die_welt", "name": "Die Welt", "loc": "Germany", "lang": "de",
     "site": "https://www.welt.de",
     "src": [("kiosko", "de", "die_welt"), ("kioskopage", "de", "die_welt")]},
]

OUT_DIR = Path(__file__).parent.parent / "frontpages"
# Small grid thumbnails live here. The Front Pages grid shows each cover at
# ~180px wide, so serving the full 0.5–0.7 MB scans there made the tab load
# several megabytes of images. These ~360px JPEGs (tens of KB each) load the
# grid fast; the full-resolution cover is still used in the click-to-open modal.
THUMB_DIR = OUT_DIR / "thumbs"
THUMB_WIDTH = 360       # 2x the ~180px display width for sharpness on retina
THUMB_QUALITY = 72
# Dated copies of each day's covers live here so the site can show history.
# index.json maps each available date -> the paper ids captured that day, plus
# a "papers" lookup for display metadata.
ARCHIVE_DIR = OUT_DIR / "archive"
ARCHIVE_RETENTION_DAYS = 365   # keep ~1 year, then prune the oldest days
MIN_BYTES = 12000
TIMEOUT = 25
# How many days back to try for each cover (today first). Widening past
# yesterday lets a title recover the most recent edition it can actually reach
# when the last day or two are missing upstream, instead of freezing on a much
# older carried-forward image. used_date is always stamped with the real
# calendar date of the edition fetched, so covers stay honestly labelled.
WINDOW_DAYS = max(2, int(os.environ.get("FRONTPAGES_WINDOW_DAYS", "3")))
# Covers that have missed this many EXPECTED editions (per-paper no_print days
# excluded) are flagged as a workflow warning, so a chronic refresh failure
# surfaces in the Actions log instead of going unnoticed — without weekends or
# a single holiday raising false alarms.
STALE_WARN_DAYS = int(os.environ.get("FRONTPAGES_STALE_WARN_DAYS", "2"))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


# Scraped-page results, one fetch per page per run (shared across the
# WINDOW_DAYS date attempts): {page_url: [cover image urls found]}.
_KIOSKO_PAGE_CACHE = {}
_GZT_PAGE_CACHE = {}

# gzt.com hosts each day's front-page scan on img.piri.net with the edition
# date in the path (no zero-padding): .../piri/upload/3/2026/7/10/<hash>.jpg
_PIRI_IMG_RE = re.compile(
    r'https?://img\.piri\.net/piri/upload/\d+/\d{4}/\d{1,2}/\d{1,2}/'
    r'[a-f0-9\-]+\.jpe?g', re.I)


def gzt_page_covers(session, slug: str) -> list:
    """Scrape gzt.com/gazeteler/<slug> for dated img.piri.net cover scans."""
    page = f"https://www.gzt.com/gazeteler/{slug}"
    if page in _GZT_PAGE_CACHE:
        return _GZT_PAGE_CACHE[page]
    urls = []
    try:
        r = session.get(page, headers={"User-Agent": UA,
                                       "Referer": "https://www.gzt.com/"},
                        timeout=TIMEOUT)
        if r.status_code == 200:
            urls = list(dict.fromkeys(_PIRI_IMG_RE.findall(r.text)))
    except Exception as exc:
        print(f"      ! {page} -> {exc}", file=sys.stderr)
    _GZT_PAGE_CACHE[page] = urls
    return urls

_KIOSKO_IMG_RE = re.compile(
    r'https?://img\.kiosko\.net/\d{4}/\d{2}/\d{2}/[a-z]+/[a-z0-9_\-]+\.\d+\.jpg',
    re.I)


def kiosko_page_covers(session, cc: str, slug: str) -> list:
    """Scrape en.kiosko.net/<cc>/np/<slug>.html for cover image URLs (og:image
    plus any img.kiosko.net references), deduped, best first."""
    page = f"https://en.kiosko.net/{cc}/np/{slug}.html"
    if page in _KIOSKO_PAGE_CACHE:
        return _KIOSKO_PAGE_CACHE[page]
    urls = []
    try:
        r = session.get(page, headers={"User-Agent": UA,
                                       "Referer": "https://en.kiosko.net/"},
                        timeout=TIMEOUT)
        if r.status_code == 200:
            og = re.findall(r'<meta[^>]+(?:property|name)=["\']og:image["\']'
                            r'[^>]+content=["\']([^"\']+)', r.text, re.I)
            urls = [u for u in og if "kiosko.net" in u]
            urls += _KIOSKO_IMG_RE.findall(r.text)
            urls = list(dict.fromkeys(urls))
    except Exception as exc:
        print(f"      ! {page} -> {exc}", file=sys.stderr)
    _KIOSKO_PAGE_CACHE[page] = urls
    return urls


def candidate_urls(session, src, d) -> list:
    """The URL(s) to try for one source on date d, in priority order."""
    kind = src[0]
    if kind == "ff":
        return [f"https://cdn.freedomforum.org/dfp/jpg{d.day}/lg/{src[1]}.jpg"]
    if kind == "kiosko":
        return [f"https://img.kiosko.net/{d:%Y/%m/%d}/{src[1]}/{src[2]}.750.jpg"]
    if kind == "kioskopage":
        # Only covers actually dated d are eligible — the date in the image URL
        # path is the edition date, so a frozen page (still advertising an old
        # edition) yields nothing here and the fetch falls through to the next
        # source/date instead of re-downloading a stale cover as "today's".
        out = []
        for u in kiosko_page_covers(session, src[1], src[2]):
            if f"{d:%Y/%m/%d}" in u:
                # Prefer the full-size scan over whatever thumbnail size the
                # page embeds (.300/.200); keep the as-found URL as backup.
                big = re.sub(r'\.\d+\.jpg$', '.750.jpg', u)
                if big != u:
                    out.append(big)
                out.append(u)
        return list(dict.fromkeys(out))
    if kind == "paperboy":
        base = f"https://cdn.thepaperboy.com/frontpages/{src[1]}/{d:%Y%m%d}/{src[2]}"
        return [f"{base}_lg.jpg", f"{base}.jpg"]
    if kind == "gztpage":
        # The date filter (unpadded, slash-delimited) is what guarantees the
        # scan matched is the edition of day d and not an older one.
        return [u for u in gzt_page_covers(session, src[1])
                if f"/{d.year}/{d.month}/{d.day}/" in u]
    if kind == "gulftimes":
        # Gulf Times posts the main section's page 1 as a dated JPEG; the trailing
        # number is the edition, usually 1 but occasionally a later re-plate (2/3).
        return [f"https://www.gulf-times.com/pdf/{d:%Y/%m/%d}/main-{d:%Y%m%d}-{e}.jpeg"
                for e in (1, 2, 3)]
    if kind == "sgpdf":
        return [f"https://www.saudigazette.com.sa/uploads/pdf/{d:%Y/%m/%d}/sg-{d:%Y%m%d}.pdf"]
    raise ValueError(src)


def referer_for(url: str):
    if "kiosko.net" in url:
        return "https://en.kiosko.net/"
    if "freedomforum.org" in url:
        return "https://www.freedomforum.org/todaysfrontpages/"
    if "thepaperboy.com" in url:
        return "https://www.thepaperboy.com/"
    if "piri.net" in url:
        return "https://www.gzt.com/"
    if "gulf-times.com" in url:
        return "https://www.gulf-times.com/"
    if "saudigazette.com.sa" in url:
        return "https://www.saudigazette.com.sa/"
    return None


def pdf_first_page_to_jpeg(pdf_bytes: bytes):
    """Render page 1 of a PDF to JPEG bytes. Best-effort: returns None if
    pypdfium2 is unavailable or rendering fails, so the paper is simply skipped
    rather than breaking the whole run."""
    try:
        import io
        import pypdfium2 as pdfium
    except Exception as exc:
        print(f"      ! pypdfium2 unavailable, skipping PDF cover: {exc}",
              file=sys.stderr)
        return None
    try:
        pdf = pdfium.PdfDocument(pdf_bytes)
        pil = pdf[0].render(scale=2.0).to_pil().convert("RGB")
        buf = io.BytesIO()
        pil.save(buf, "JPEG", quality=85, optimize=True)
        data = buf.getvalue()
        return data if len(data) >= MIN_BYTES else None
    except Exception as exc:
        print(f"      ! PDF render failed: {exc}", file=sys.stderr)
        return None


def _is_image_bytes(data: bytes) -> bool:
    """True for JPEG/PNG/WebP magic bytes. Trusting Content-Type instead broke
    on CDNs that serve real covers as application/octet-stream (thepaperboy),
    and sniffing also rejects HTML error pages served as 200s."""
    return (data[:3] == b"\xff\xd8\xff"
            or data[:8] == b"\x89PNG\r\n\x1a\n"
            or (data[:4] == b"RIFF" and data[8:12] == b"WEBP"))


def fetch_cover(session: requests.Session, url: str):
    """Download a cover URL and return image bytes. PDFs are rendered to JPEG
    (page 1). Returns None if it isn't a real cover."""
    is_pdf = url.lower().split("?")[0].endswith(".pdf")
    headers = {"User-Agent": UA,
               "Accept": "*/*" if is_pdf else "image/avif,image/webp,image/*,*/*"}
    ref = referer_for(url)
    if ref:
        headers["Referer"] = ref
    try:
        r = session.get(url, headers=headers, timeout=TIMEOUT)
    except Exception as exc:
        print(f"      ! {url} -> {exc}", file=sys.stderr)
        return None
    ct = r.headers.get("Content-Type", "")
    if r.status_code != 200:
        print(f"      - {url} -> {r.status_code} {ct or '?'} {len(r.content)}B")
        return None
    if is_pdf or ct == "application/pdf":
        img = pdf_first_page_to_jpeg(r.content)
        if not img:
            print(f"      - {url} -> PDF unusable ({len(r.content)}B)")
        return img
    if _is_image_bytes(r.content) and len(r.content) >= MIN_BYTES:
        return r.content
    print(f"      - {url} -> {r.status_code} {ct or '?'} {len(r.content)}B")
    return None


def make_thumb(src_path: Path, thumb_path: Path) -> bool:
    """Write a small, web-optimised JPEG thumbnail of src_path. Best-effort: a
    failure (or missing Pillow) never aborts the run — the grid just falls back
    to the full image for that paper."""
    try:
        from PIL import Image
    except Exception as exc:
        print(f"      ! Pillow unavailable, skipping thumbnails: {exc}", file=sys.stderr)
        return False
    try:
        with Image.open(src_path) as im:
            im = im.convert("RGB")
            w, h = im.size
            if w > THUMB_WIDTH:
                im = im.resize((THUMB_WIDTH, round(h * THUMB_WIDTH / w)), Image.LANCZOS)
            thumb_path.parent.mkdir(parents=True, exist_ok=True)
            im.save(thumb_path, "JPEG", quality=THUMB_QUALITY, optimize=True, progressive=True)
        return True
    except Exception as exc:
        print(f"      ! thumbnail failed for {src_path.name}: {exc}", file=sys.stderr)
        return False


def archive_today(today: date, manifest: dict) -> None:
    """Save a dated copy of today's available covers under archive/<date>/ and
    update archive/index.json, then prune anything older than the retention
    window. Only papers that actually have an image today are archived.

    Idempotent per day: a later run (e.g. the afternoon refresh) overwrites the
    same day's folder with the newest editions.
    """
    today_str = today.isoformat()
    ok_ids = [p["id"] for p in manifest["papers"] if p["ok"]]

    index_path = ARCHIVE_DIR / "index.json"
    try:
        idx = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception:
        idx = {}
    idx.setdefault("papers", {})
    idx.setdefault("dates", {})

    # Refresh display metadata for every paper we currently track.
    for p in PAPERS:
        idx["papers"][p["id"]] = {
            "name": p["name"], "loc": p["loc"], "lang": p["lang"], "site": p["site"],
        }

    archived = []
    if ok_ids:
        day_dir = ARCHIVE_DIR / today_str
        day_dir.mkdir(parents=True, exist_ok=True)
        for pid in ok_ids:
            src_img = OUT_DIR / f"{pid}.jpg"
            if src_img.exists():
                shutil.copy2(src_img, day_dir / f"{pid}.jpg")
                archived.append(pid)
        if archived:
            idx["dates"][today_str] = archived
            print(f"  archived {len(archived)} covers under archive/{today_str}/")

    # Prune days older than the retention window (from disk and the index).
    cutoff = today - timedelta(days=ARCHIVE_RETENTION_DAYS)
    for d in list(idx["dates"].keys()):
        try:
            dd = date.fromisoformat(d)
        except ValueError:
            continue
        if dd < cutoff:
            del idx["dates"][d]
            folder = ARCHIVE_DIR / d
            if folder.exists():
                shutil.rmtree(folder, ignore_errors=True)
            print(f"  pruned archive/{d}/ (older than {ARCHIVE_RETENTION_DAYS} days)")

    idx["updated"] = datetime.now(timezone.utc).isoformat()
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  archive index: {len(idx['dates'])} day(s) available")


def _stale_days(today: date, used_date):
    """How many days behind `today` the edition labelled `used_date` is (0 = today,
    None if there's no dated cover)."""
    try:
        return (today - date.fromisoformat(used_date)).days
    except Exception:
        return None


def _missed_print_days(today: date, used_date, no_print) -> int | None:
    """How many EXPECTED editions have been missed since used_date — i.e. days in
    (used_date, today] that are not in the paper's no_print weekday set. A WSJ
    Friday cover on a Sunday misses nothing (Sat 4-Jul was a holiday, Sun has no
    print); the same gap on Tue-Thu misses two — that's the real staleness."""
    try:
        d = date.fromisoformat(used_date)
    except Exception:
        return None
    missed, cur = 0, d + timedelta(days=1)
    while cur <= today:
        if cur.weekday() not in (no_print or ()):
            missed += 1
        cur += timedelta(days=1)
    return missed


def load_prev_manifest() -> dict:
    """Return {id: entry} from the committed manifest, or {} if unreadable."""
    try:
        data = json.loads((OUT_DIR / "manifest.json").read_text(encoding="utf-8"))
        return {p["id"]: p for p in data.get("papers", [])}
    except Exception:
        return {}


def main():
    OUT_DIR.mkdir(exist_ok=True)
    today = datetime.now(timezone.utc).date()
    today_str = today.isoformat()
    dates = [today - timedelta(days=k) for k in range(WINDOW_DAYS)]   # today, then progressively older
    session = requests.Session()
    manifest = {"updated": datetime.now(timezone.utc).isoformat(), "papers": []}

    # Opportunistic catch-up: when asked to fill only, papers already showing
    # today's edition are kept untouched and only the stragglers are retried.
    fill_only = os.environ.get("FRONTPAGES_FILL_ONLY", "").lower() == "true"
    prev = load_prev_manifest()

    for p in PAPERS:
        dest = OUT_DIR / f"{p['id']}.jpg"

        # In fill-only mode, leave a cover that's already current alone — no
        # request, no churn. Carry its manifest entry forward verbatim.
        prev_entry = prev.get(p["id"])
        if (fill_only and prev_entry and prev_entry.get("ok")
                and prev_entry.get("date") == today_str and dest.exists()):
            print(f"  = {p['name']}: already current ({today_str}), skipped")
            manifest["papers"].append({
                "id": p["id"], "name": p["name"], "loc": p["loc"], "lang": p["lang"],
                "site": p["site"], "ok": True, "src": prev_entry.get("src"),
                "date": today_str, "stale_days": 0, "missed_editions": 0,
            })
            continue

        got = used_url = used_date = None
        for d in dates:
            for src in p["src"]:
                for url in candidate_urls(session, src, d):
                    data = fetch_cover(session, url)
                    if data:
                        got, used_url, used_date = data, url, d.isoformat()
                        break
                if got:
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
            # We kept an existing cover but downloaded nothing this run (today's
            # edition isn't up yet, or a transient CDN blip). Preserve the date
            # and src that image already carried rather than nulling them out —
            # otherwise the kept cover would lose its date label.
            if ok and prev_entry:
                used_date = prev_entry.get("date")
                used_url = prev_entry.get("src")

        # Build (or refresh) the grid thumbnail for any paper that has a cover.
        if ok and dest.exists():
            make_thumb(dest, THUMB_DIR / f"{p['id']}.jpg")

        manifest["papers"].append({
            "id": p["id"], "name": p["name"], "loc": p["loc"], "lang": p["lang"],
            "site": p["site"], "ok": ok, "src": used_url, "date": used_date,
            "stale_days": _stale_days(today, used_date),
            "missed_editions": _missed_print_days(today, used_date, p.get("no_print")),
        })

    # Prune covers for papers that were removed from PAPERS, so the repo and the
    # live site never keep showing a paper after it's dropped from the list.
    keep = {p["id"] for p in PAPERS}
    for img in OUT_DIR.glob("*.jpg"):
        if img.stem not in keep:
            img.unlink()
            print(f"  - pruned stale cover {img.name}")
    if THUMB_DIR.exists():
        for img in THUMB_DIR.glob("*.jpg"):
            if img.stem not in keep:
                img.unlink()
                print(f"  - pruned stale thumb {img.name}")

    (OUT_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    n_ok = sum(1 for x in manifest["papers"] if x["ok"])
    print(f"\nFront pages: {n_ok}/{len(PAPERS)} available")

    # Surface chronic refresh failures. Staleness is measured in MISSED EXPECTED
    # EDITIONS, not calendar days — a WSJ Friday cover on a Sunday has missed
    # nothing (no Sunday print), so weekends and single holidays don't cry wolf.
    # A cover that has genuinely missed 2+ print days means its source (Freedom
    # Forum code / Kiosko slug) is likely dead and needs re-probing
    # (scripts/probe_frontpages.py / probe_sources.py). The ::warning:: makes
    # that visible in the Actions log every run.
    stale = sorted(
        ((x["id"], x["missed_editions"]) for x in manifest["papers"]
         if isinstance(x.get("missed_editions"), int)
         and x["missed_editions"] >= STALE_WARN_DAYS),
        key=lambda t: -t[1])
    missing = [x["id"] for x in manifest["papers"] if not x["ok"]]
    if stale:
        listed = ", ".join(f"{pid} ({n} editions behind)" for pid, n in stale)
        print(f"::warning title=Stale front pages::{len(stale)} cover(s) have missed "
              f"≥{STALE_WARN_DAYS} expected editions — source likely dead, "
              f"re-probe needed: {listed}")
    if missing:
        print(f"::warning title=Missing front pages::no image for: {', '.join(missing)}")

    # Keep a dated copy of today's covers for the in-site archive.
    archive_today(today, manifest)


if __name__ == "__main__":
    main()
