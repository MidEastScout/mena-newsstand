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

Source kinds:
  ("ff", CODE)          -> Freedom Forum CDN (keyed by day-of-month)
  ("kiosko", GEO, SLUG) -> Kiosko CDN (keyed by full date)
"""
import json
import shutil
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

PAPERS = [
    # ——— Middle East ———
    {"id": "haaretz", "name": "Haaretz", "loc": "Israel", "lang": "en",
     "site": "https://www.haaretz.com",
     "src": [("ff", "ISR_HA"), ("kiosko", "il", "haaretz")]},
    {"id": "the_national", "name": "The National", "loc": "UAE", "lang": "en",
     "site": "https://www.thenationalnews.com",
     "src": [("ff", "UAE_TN"), ("kiosko", "asi", "the_national")]},

    # ——— United States (Freedom Forum — all probe-confirmed) ———
    {"id": "nyt", "name": "New York Times", "loc": "USA", "lang": "en",
     "site": "https://www.nytimes.com",
     "src": [("ff", "NY_NYT"), ("kiosko", "us", "newyork_times")]},
    {"id": "wsj", "name": "Wall Street Journal", "loc": "USA", "lang": "en",
     "site": "https://www.wsj.com",
     "src": [("ff", "WSJ"), ("kiosko", "us", "wsj")]},
    {"id": "usa_today", "name": "USA Today", "loc": "USA", "lang": "en",
     "site": "https://www.usatoday.com", "src": [("ff", "USAT")]},

    # ——— Europe (Kiosko — all probe-confirmed) ———
    {"id": "the_independent", "name": "The Independent", "loc": "UK", "lang": "en",
     "site": "https://www.independent.co.uk", "src": [("kiosko", "uk", "the_independent")]},
    {"id": "daily_mail", "name": "Daily Mail", "loc": "UK", "lang": "en",
     "site": "https://www.dailymail.co.uk", "src": [("kiosko", "uk", "daily_mail")]},
    {"id": "die_welt", "name": "Die Welt", "loc": "Germany", "lang": "de",
     "site": "https://www.welt.de", "src": [("kiosko", "de", "die_welt")]},
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

        # Build (or refresh) the grid thumbnail for any paper that has a cover.
        if ok and dest.exists():
            make_thumb(dest, THUMB_DIR / f"{p['id']}.jpg")

        manifest["papers"].append({
            "id": p["id"], "name": p["name"], "loc": p["loc"], "lang": p["lang"],
            "site": p["site"], "ok": ok, "src": used_url, "date": used_date,
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

    # Keep a dated copy of today's covers for the in-site archive.
    archive_today(today, manifest)


if __name__ == "__main__":
    main()
