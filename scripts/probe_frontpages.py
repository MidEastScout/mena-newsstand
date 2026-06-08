#!/usr/bin/env python3
"""One-off diagnostic: probe many candidate cover URLs per paper and report
which actually return a real image FROM THE GITHUB ACTIONS RUNNER (the only
network where the cover CDNs answer). Run this via the workflow, read the log,
then bake the winners into fetch_frontpages.py. Safe to delete afterwards.
"""
import sys
from datetime import datetime, timedelta, timezone

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
MIN_BYTES = 12000
TIMEOUT = 25

# Papers still failing, with generous candidate identifiers to try.
#   kiosko geos to try, kiosko slugs to try, freedom-forum codes to try.
PAPERS = [
    {"name": "Asharq Al-Awsat",
     "kiosko_geo": ["uk", "asi", "sa"],
     "kiosko_slug": ["asharq_al_awsat", "asharq", "asharqalawsat", "alsharq_alawsat"],
     "ff": ["SAU_AAA", "UK_AAA", "ASHARQ", "SAU_SA"]},
    {"name": "Al-Ahram",
     "kiosko_geo": ["eg", "asi"],
     "kiosko_slug": ["al_ahram", "alahram", "ahram"],
     "ff": ["EGY_AA", "EGY_AH", "EG_AA"]},
    {"name": "Al-Quds Al-Arabi",
     "kiosko_geo": ["uk", "asi"],
     "kiosko_slug": ["alquds", "al_quds", "al_quds_al_arabi", "alqudsalarabi", "quds"],
     "ff": ["UK_QUDS", "PSE_QA"]},
    {"name": "Arab News",
     "kiosko_geo": ["asi", "sa", "uk"],
     "kiosko_slug": ["arab_news", "arabnews"],
     "ff": ["SAU_AN", "SAU_ARN", "SA_AN", "SAUDI_AN"]},
    {"name": "Al-Anba",
     "kiosko_geo": ["asi", "kw"],
     "kiosko_slug": ["al_anba", "al_anbaa", "alanba", "anba"],
     "ff": ["KWT_AA", "KUW_AA"]},
    {"name": "An-Nahar",
     "kiosko_geo": ["asi", "lb"],
     "kiosko_slug": ["nahar", "an_nahar", "annahar", "al_nahar"],
     "ff": ["LBN_AN", "LEB_AN"]},
    {"name": "Tehran Times",
     "kiosko_geo": ["ir", "asi"],
     "kiosko_slug": ["tehran_times", "tehrantimes"],
     "ff": ["IRN_TT", "IR_TT"]},
    {"name": "Financial Times",
     "kiosko_geo": ["uk", "us"],
     "kiosko_slug": ["ft_uk", "ft_us", "ft", "financial_times"],
     "ff": ["UK_FT", "FT", "USA_FT", "ENG_FT"]},
    {"name": "Le Monde",
     "kiosko_geo": ["fr"],
     "kiosko_slug": ["lemonde", "le_monde"],
     "ff": ["FRA_LM", "FR_LM", "FRANCE_LM"]},
]

# Sizes to try for kiosko (some papers only expose smaller sizes publicly).
KIOSKO_SIZES = ["750", "550", "wide", ""]


def kiosko_urls(geo, slug, d):
    for size in KIOSKO_SIZES:
        suffix = f".{size}.jpg" if size else ".jpg"
        yield f"https://img.kiosko.net/{d:%Y/%m/%d}/{geo}/{slug}{suffix}"


def ff_urls(code, d):
    # old CDN (day-of-month keyed) — proven to work for ISR_HA/NY_NYT/WSJ
    yield f"https://cdn.freedomforum.org/dfp/jpg{d.day}/lg/{code}.jpg"


def referer_for(url):
    if "kiosko.net" in url:
        return "https://en.kiosko.net/"
    if "freedomforum.org" in url:
        return "https://www.freedomforum.org/todaysfrontpages/"
    return None


def probe(session, url):
    headers = {"User-Agent": UA, "Accept": "image/avif,image/webp,image/*,*/*"}
    ref = referer_for(url)
    if ref:
        headers["Referer"] = ref
    try:
        r = session.get(url, headers=headers, timeout=TIMEOUT)
    except Exception as exc:
        return f"ERR {exc}"
    ct = r.headers.get("Content-Type", "")
    n = len(r.content)
    ok = r.status_code == 200 and ct.startswith("image/") and n >= MIN_BYTES
    return f"{'OK ' if ok else '   '} {r.status_code} {ct or '?':<12} {n:>8}B", ok


def main():
    session = requests.Session()
    today = datetime.now(timezone.utc).date()
    dates = [today, today - timedelta(days=1)]
    for p in PAPERS:
        print(f"\n=== {p['name']} ===")
        winners = []
        for d in dates:
            cands = []
            for geo in p["kiosko_geo"]:
                for slug in p["kiosko_slug"]:
                    cands.extend(kiosko_urls(geo, slug, d))
            for code in p["ff"]:
                cands.extend(ff_urls(code, d))
            for url in cands:
                res = probe(session, url)
                line, ok = res if isinstance(res, tuple) else (res, False)
                print(f"  {line}  {url}")
                if ok:
                    winners.append(url)
            if winners:
                break
        print(f"  --> WINNERS: {winners or 'none'}")


if __name__ == "__main__":
    main()
