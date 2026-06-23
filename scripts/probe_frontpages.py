#!/usr/bin/env python3
"""
RECON PROBE — more NON-ISRAELI Middle East newspaper front pages.
=================================================================
This is a one-off TESTER that runs on GitHub's servers (a datacenter IP that,
unlike a laptop, is what actually reaches these CDNs). It tries FIVE different
ways to obtain a real, current front-page cover for Arab / Gulf / Egyptian /
Levantine / Iranian / Turkish papers, and records which ones genuinely return
an image — together with the exact URL so the winner can be baked into
scripts/fetch_frontpages.py.

Israeli papers are intentionally excluded (we already carry Haaretz; the goal
here is to broaden the *rest* of the region).

Sources probed
--------------
  1. frontpages.com         — per-country MENA pages (scrape → cover image URLs)
  2. Kiosko (img.kiosko.net) — scrape MENA country indices → (geo, slug) covers
  3. Freedom Forum TFP       — old CDN codes + the new frontpages.freedomforum.org
  4. Per-paper e-papers      — each paper's OWN site (Arab News, Gulf Times, …)
  5. PressReader (i.prcdn.co)— public cover thumbnails, keyed by numeric cid

It writes state/probe_results.json. Because several sources are HTML pages whose
structure we don't know yet, the probe is reconnaissance-oriented: for those it
records the candidate image URLs it discovered (not just pass/fail) so the
winners can be turned into stable, deterministic production URLs.

You don't need to read or edit this file. It's run from the
"Probe front-page sources" workflow (workflow_dispatch).
"""
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
TIMEOUT = 20
MIN_BYTES = 15000          # a real cover scan is tens–hundreds of KB
MAX_IMGS_PER_PAGE = 30     # cap test-downloads per scraped page
PAUSE = 0.25               # be polite between requests

today = datetime.now(timezone.utc).date()
DATES = [today, today - timedelta(days=1)]

# Non-Israeli MENA countries we care about, with the slug frontpages.com and the
# country code Kiosko/PressReader tend to use.
COUNTRIES = [
    # (display,           fp.com slug,            iso2, kiosko-geo guesses)
    ("Saudi Arabia",      "saudi-arabia",         "sa", ["asi", "afr"]),
    ("United Arab Emirates", "uae",               "ae", ["asi"]),
    ("Qatar",             "qatar",                "qa", ["asi"]),
    ("Kuwait",            "kuwait",               "kw", ["asi"]),
    ("Bahrain",           "bahrain",              "bh", ["asi"]),
    ("Oman",              "oman",                 "om", ["asi"]),
    ("Egypt",             "egypt",                "eg", ["afr"]),
    ("Jordan",            "jordan",               "jo", ["asi"]),
    ("Lebanon",           "lebanon",              "lb", ["asi"]),
    ("Iraq",              "iraq",                 "iq", ["asi"]),
    ("Iran",              "iran",                 "ir", ["asi"]),
    ("Turkey",            "turkey",               "tr", ["eur", "asi"]),
    ("Syria",             "syria",                "sy", ["asi"]),
    ("Yemen",             "yemen",                "ye", ["asi"]),
    ("Palestine",         "palestine",            "ps", ["asi"]),
    ("Morocco",           "morocco",              "ma", ["afr"]),
    ("Tunisia",           "tunisia",              "tn", ["afr"]),
    ("Algeria",           "algeria",              "dz", ["afr"]),
]

IMG_RE = re.compile(
    r'https?://[^\s"\'<>()]+?\.(?:jpg|jpeg|png|webp)(?:\?[^\s"\'<>()]*)?', re.I)
OG_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\']og:image["\'][^>]+content=["\']([^"\']+)',
    re.I)
CID_RE = re.compile(r'(?:i\.prcdn\.co/img\?cid=|[?&]cid=)(\d+)', re.I)
# filenames that are clearly NOT a front-page scan
JUNK_IMG = re.compile(r'(logo|sprite|icon|favicon|avatar|flag|placeholder|'
                      r'banner|ad[s_-]|pixel|blank|default|share|social)', re.I)


def referer_for(url: str):
    if "kiosko.net" in url:
        return "https://en.kiosko.net/"
    if "freedomforum.org" in url:
        return "https://frontpages.freedomforum.org/"
    if "prcdn.co" in url:
        return "https://www.pressreader.com/"
    if "frontpages.com" in url:
        return "https://www.frontpages.com/"
    return None


def get(session, url, as_image=False):
    """Return (ok, info, content_or_text). For images, ok means a real cover."""
    headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    ref = referer_for(url)
    if ref:
        headers["Referer"] = ref
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
    ok = r.status_code == 200
    return ok, f"{r.status_code} {ct or '?'} {len(r.text)}c", r.text


def extract_images(html, base_host_hint=""):
    """Pull candidate cover-image URLs out of an HTML page, best first."""
    urls = []
    for m in OG_RE.finditer(html):
        urls.append(m.group(1))
    urls += IMG_RE.findall(html)
    # de-dupe, drop obvious junk, keep order
    seen, out = set(), []
    for u in urls:
        u = u.replace("&amp;", "&").strip()
        if u in seen or JUNK_IMG.search(u):
            continue
        seen.add(u)
        out.append(u)
    return out


# ---------------------------------------------------------------------------
# 1. frontpages.com — per-country MENA listing pages
# ---------------------------------------------------------------------------
def probe_frontpages_com(session):
    print("\n=== 1. frontpages.com ===")
    out = []
    for disp, slug, iso2, _ in COUNTRIES:
        page = f"https://www.frontpages.com/{slug}-newspapers/"
        ok, info, html = get(session, page)
        print(f"  [{disp}] {page} -> {info}")
        entry = {"country": disp, "page": page, "page_info": info,
                 "candidates": [], "covers_ok": []}
        if ok and html:
            imgs = extract_images(html)
            entry["candidates"] = imgs[:MAX_IMGS_PER_PAGE]
            for iu in imgs[:MAX_IMGS_PER_PAGE]:
                iok, iinfo, _ = get(session, iu, as_image=True)
                if iok:
                    entry["covers_ok"].append({"url": iu, "info": iinfo})
                    print(f"      OK cover {iu} ({iinfo})")
                time.sleep(PAUSE)
        out.append(entry)
        time.sleep(PAUSE)
    return out


# ---------------------------------------------------------------------------
# 2. Kiosko — scrape MENA country indices, then test the .750 covers
# ---------------------------------------------------------------------------
KIOSKO_IMG = re.compile(
    r'img\.kiosko\.net/(\d{4})/(\d{2})/(\d{2})/([a-z]+)/([a-z0-9_\-]+)\.\d+\.jpg',
    re.I)


def probe_kiosko(session):
    print("\n=== 2. Kiosko ===")
    out = []
    index_urls = []
    for disp, slug, iso2, geos in COUNTRIES:
        index_urls.append((disp, f"https://en.kiosko.net/{iso2}/"))
        for g in geos:
            index_urls.append((disp, f"https://en.kiosko.net/{g}/geo/{iso2}.html"))

    found = {}   # (geo, slug) -> country
    for disp, idx in index_urls:
        ok, info, html = get(session, idx)
        print(f"  index [{disp}] {idx} -> {info}")
        if ok and html:
            for m in KIOSKO_IMG.finditer(html):
                geo, pslug = m.group(4), m.group(5)
                found.setdefault((geo, pslug), disp)
        time.sleep(PAUSE)

    print(f"  discovered {len(found)} (geo,slug) pairs; testing covers…")
    for (geo, pslug), disp in sorted(found.items()):
        hit = None
        for d in DATES:
            u = f"https://img.kiosko.net/{d:%Y/%m/%d}/{geo}/{pslug}.750.jpg"
            iok, iinfo, _ = get(session, u, as_image=True)
            if iok:
                hit = {"country": disp, "geo": geo, "slug": pslug,
                       "url": u, "date": d.isoformat(), "info": iinfo}
                break
            time.sleep(PAUSE)
        if hit:
            print(f"      OK {disp:14s} {geo}/{pslug}")
            out.append(hit)
    return out


# ---------------------------------------------------------------------------
# 3. Freedom Forum — old CDN candidate codes + the new TFP site
# ---------------------------------------------------------------------------
FF_CANDIDATE_CODES = [
    # UAE
    "UAE_TN", "UAE_GN", "UAE_KT", "UAE_GT",
    # Qatar
    "QAT_GT", "QAT_PEN", "QAT_QT",
    # Saudi Arabia
    "SAU_AN", "SAU_SG", "KSA_AN",
    # Egypt
    "EGY_AH", "EGY_ET", "EGY_DNE",
    # Jordan
    "JOR_JT", "JOR_AD",
    # Lebanon
    "LEB_DS", "LEB_OLJ",
    # Kuwait / Bahrain / Oman
    "KUW_KT", "KUW_AT", "BAH_GDN", "OMA_TO", "OMA_ODO",
    # Iran / Iraq / Turkey
    "IRN_TT", "IRQ_BG", "TUR_DS", "TUR_HDN", "TUR_DN",
]


def probe_freedomforum(session):
    print("\n=== 3. Freedom Forum ===")
    out = {"old_cdn": [], "new_tfp": {}}
    # 3a. old CDN, keyed by day-of-month
    for code in FF_CANDIDATE_CODES:
        hit = None
        for d in DATES:
            u = f"https://cdn.freedomforum.org/dfp/jpg{d.day}/lg/{code}.jpg"
            iok, iinfo, _ = get(session, u, as_image=True)
            if iok:
                hit = {"code": code, "url": u, "date": d.isoformat(), "info": iinfo}
                break
            time.sleep(PAUSE)
        if hit:
            print(f"      OK old-cdn {code}")
            out["old_cdn"].append(hit)

    # 3b. new TFP — look for a JSON listing of papers + image URLs
    for probe in [
        "https://frontpages.freedomforum.org/",
        "https://frontpages.freedomforum.org/api/papers",
        "https://frontpages.freedomforum.org/data/papers.json",
    ]:
        ok, info, body = get(session, probe)
        print(f"  new-tfp {probe} -> {info}")
        out["new_tfp"][probe] = {"info": info}
        if ok and body and ("freedomforum" in probe):
            # record any cover URLs + any country/MENA hints we can see
            imgs = extract_images(body)
            mena = [u for u in imgs if re.search(
                r'(uae|qat|sau|ksa|egy|jor|leb|kuw|bah|oma|irn|irq|tur)', u, re.I)]
            out["new_tfp"][probe]["sample_imgs"] = imgs[:20]
            out["new_tfp"][probe]["mena_imgs"] = mena[:20]
        time.sleep(PAUSE)
    return out


# ---------------------------------------------------------------------------
# 4. Per-paper e-papers (each paper's OWN site)
# ---------------------------------------------------------------------------
EPAPERS = [
    ("Arab News", "Saudi Arabia", "https://www.arabnews.com/issuepdf"),
    ("Saudi Gazette", "Saudi Arabia", "https://saudigazette.com.sa/"),
    ("Gulf Times", "Qatar", "https://www.gulf-times.com/pdfs"),
    ("Gulf Times (epaper)", "Qatar", "https://epaper.gulf-times.com/"),
    ("The Peninsula", "Qatar", "https://thepeninsulaqatar.com/epaper"),
    ("Qatar Tribune", "Qatar", "https://www.qatar-tribune.com/PDF"),
    ("Khaleej Times", "UAE", "https://epaper.khaleejtimes.com/"),
    ("Gulf News (epaper)", "UAE", "https://gulfnews.com/epaper"),
    ("Oman Daily Observer", "Oman", "https://www.omanobserver.om/epaper/"),
    ("Times of Oman", "Oman", "https://timesofoman.com/epaper"),
    ("Kuwait Times", "Kuwait", "https://www.kuwaittimes.com/epaper"),
    ("The Jordan Times", "Jordan", "https://jordantimes.com/"),
    ("Daily News Egypt", "Egypt", "https://www.dailynewsegypt.com/"),
    ("Tehran Times", "Iran", "https://www.tehrantimes.com/"),
    ("Daily Sabah", "Turkey", "https://www.dailysabah.com/"),
    ("Hurriyet Daily News", "Turkey", "https://www.hurriyetdailynews.com/"),
]


def probe_epapers(session):
    print("\n=== 4. Per-paper e-papers ===")
    out = []
    for name, country, page in EPAPERS:
        ok, info, html = get(session, page)
        print(f"  [{name}] {page} -> {info}")
        entry = {"name": name, "country": country, "page": page,
                 "page_info": info, "candidates": [], "covers_ok": []}
        if ok and html:
            imgs = extract_images(html)
            # also surface PDFs that may be the cover
            pdfs = re.findall(
                r'https?://[^\s"\'<>()]+?\.pdf(?:\?[^\s"\'<>()]*)?', html, re.I)
            entry["candidates"] = imgs[:MAX_IMGS_PER_PAGE]
            entry["pdfs"] = list(dict.fromkeys(pdfs))[:10]
            for iu in imgs[:MAX_IMGS_PER_PAGE]:
                iok, iinfo, _ = get(session, iu, as_image=True)
                if iok:
                    entry["covers_ok"].append({"url": iu, "info": iinfo})
                    print(f"      OK cover {iu} ({iinfo})")
                time.sleep(PAUSE)
        out.append(entry)
        time.sleep(PAUSE)
    return out


# ---------------------------------------------------------------------------
# 5. PressReader — discover cid from the public publication page, test i.prcdn.co
# ---------------------------------------------------------------------------
PR_SLUGS = [
    ("Gulf News", "UAE", "gulf-news"),
    ("Khaleej Times", "UAE", "khaleej-times"),
    ("Gulf Times", "Qatar", "gulf-times"),
    ("The Peninsula", "Qatar", "the-peninsula"),
    ("Arab News", "Saudi Arabia", "arab-news"),
    ("Saudi Gazette", "Saudi Arabia", "saudi-gazette"),
    ("The Jordan Times", "Jordan", "the-jordan-times"),
    ("Kuwait Times", "Kuwait", "kuwait-times"),
    ("Oman Daily Observer", "Oman", "oman-daily-observer"),
    ("Times of Oman", "Oman", "times-of-oman"),
    ("Daily Sabah", "Turkey", "daily-sabah"),
    ("Tehran Times", "Iran", "tehran-times"),
]


def probe_pressreader(session):
    print("\n=== 5. PressReader ===")
    out = []
    for name, country, slug in PR_SLUGS:
        page = f"https://www.pressreader.com/newspapers/n/{slug}"
        ok, info, html = get(session, page)
        cids = []
        if ok and html:
            cids = sorted(set(CID_RE.findall(html)), key=int)
        print(f"  [{name}] {page} -> {info}  cids={cids[:5]}")
        entry = {"name": name, "country": country, "page": page,
                 "page_info": info, "cids": cids[:5], "cover_ok": None}
        for cid in cids[:3]:
            u = f"https://i.prcdn.co/img?cid={cid}&page=1&width=600"
            iok, iinfo, _ = get(session, u, as_image=True)
            if iok:
                entry["cover_ok"] = {"cid": cid, "url": u, "info": iinfo}
                print(f"      OK cover cid={cid} ({iinfo})")
                break
            time.sleep(PAUSE)
        out.append(entry)
        time.sleep(PAUSE)
    return out


def main():
    session = requests.Session()
    report = {
        "ran": datetime.now(timezone.utc).isoformat(),
        "date": today.isoformat(),
        "note": "Recon probe for NON-ISRAELI Middle East front pages.",
        "frontpages_com": [],
        "kiosko": [],
        "freedomforum": {},
        "epapers": [],
        "pressreader": [],
    }
    for label, fn, key in [
        ("frontpages.com", probe_frontpages_com, "frontpages_com"),
        ("kiosko", probe_kiosko, "kiosko"),
        ("freedomforum", probe_freedomforum, "freedomforum"),
        ("epapers", probe_epapers, "epapers"),
        ("pressreader", probe_pressreader, "pressreader"),
    ]:
        try:
            report[key] = fn(session)
        except Exception as e:
            print(f"!! {label} crashed: {type(e).__name__}: {e}", file=sys.stderr)
            report[key] = {"error": f"{type(e).__name__}: {e}"}

    # ---- summary ----
    fp_ok = sum(len(c["covers_ok"]) for c in report["frontpages_com"]
                if isinstance(c, dict))
    kiosko_ok = len(report["kiosko"]) if isinstance(report["kiosko"], list) else 0
    ff_ok = (len(report["freedomforum"].get("old_cdn", []))
             if isinstance(report["freedomforum"], dict) else 0)
    ep_ok = sum(len(c["covers_ok"]) for c in report["epapers"]
                if isinstance(c, dict))
    pr_ok = sum(1 for c in report["pressreader"]
                if isinstance(c, dict) and c.get("cover_ok"))
    report["summary"] = {
        "frontpages_com_covers": fp_ok,
        "kiosko_covers": kiosko_ok,
        "freedomforum_old_cdn": ff_ok,
        "epaper_covers": ep_ok,
        "pressreader_covers": pr_ok,
    }

    out_path = Path(__file__).parent.parent / "state" / "probe_results.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print("\n================  SUMMARY  ================")
    for k, v in report["summary"].items():
        print(f"  {k:24s}: {v}")
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
