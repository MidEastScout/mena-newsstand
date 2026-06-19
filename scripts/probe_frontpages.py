#!/usr/bin/env python3
"""
WHAT THIS SCRIPT DOES (plain English)
======================================
This is a one-off TESTER. The Middle-Eastern newspaper covers are unreliable,
so this tries a big list of well-known US and European papers and checks which
ones actually return a real front-page image from GitHub's servers.

It tries each paper on the two cover sources we use:
  - Freedom Forum  (best for US papers)
  - Kiosko         (best for European papers)
...for today and yesterday, trying a couple of name spellings each.

It then writes the verdict to state/probe_results.json (and prints it to the
log). Claude reads that file and bakes the winners into fetch_frontpages.py so
the site only ever lists papers whose covers genuinely work.

You don't need to read or edit this file. It's run automatically.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
TIMEOUT = 25
MIN_BYTES = 12000

today = datetime.now(timezone.utc).date()
DATES = [today, today - timedelta(days=1)]

# Each candidate lists the id/name/loc/lang we'd use on the site, plus the
# source(s) to try. A source is either:
#   ("ff", "CODE")              Freedom Forum
#   ("kiosko", "geo", "slug")   Kiosko
# Multiple sources / spellings are tried in order; first hit wins.
CANDIDATES = [
    # ——— United States (Freedom Forum) ———
    {"id": "usa_today", "name": "USA Today", "loc": "USA", "lang": "en",
     "site": "https://www.usatoday.com", "src": [("ff", "USAT")]},
    {"id": "washington_post", "name": "The Washington Post", "loc": "USA", "lang": "en",
     "site": "https://www.washingtonpost.com", "src": [("ff", "DC_WP")]},
    {"id": "la_times", "name": "Los Angeles Times", "loc": "USA", "lang": "en",
     "site": "https://www.latimes.com", "src": [("ff", "CA_LAT")]},
    {"id": "chicago_tribune", "name": "Chicago Tribune", "loc": "USA", "lang": "en",
     "site": "https://www.chicagotribune.com", "src": [("ff", "IL_CT")]},
    {"id": "boston_globe", "name": "The Boston Globe", "loc": "USA", "lang": "en",
     "site": "https://www.bostonglobe.com", "src": [("ff", "MA_BG")]},
    {"id": "ny_post", "name": "New York Post", "loc": "USA", "lang": "en",
     "site": "https://nypost.com", "src": [("ff", "NY_NYP")]},
    {"id": "newsday", "name": "Newsday", "loc": "USA", "lang": "en",
     "site": "https://www.newsday.com", "src": [("ff", "NY_ND")]},
    {"id": "denver_post", "name": "The Denver Post", "loc": "USA", "lang": "en",
     "site": "https://www.denverpost.com", "src": [("ff", "CO_DP")]},
    {"id": "sf_chronicle", "name": "San Francisco Chronicle", "loc": "USA", "lang": "en",
     "site": "https://www.sfchronicle.com", "src": [("ff", "CA_SFC")]},
    {"id": "houston_chronicle", "name": "Houston Chronicle", "loc": "USA", "lang": "en",
     "site": "https://www.houstonchronicle.com", "src": [("ff", "TX_HC")]},
    {"id": "dallas_news", "name": "The Dallas Morning News", "loc": "USA", "lang": "en",
     "site": "https://www.dallasnews.com", "src": [("ff", "TX_DMN")]},
    {"id": "star_tribune", "name": "Star Tribune", "loc": "USA", "lang": "en",
     "site": "https://www.startribune.com", "src": [("ff", "MN_ST")]},
    {"id": "philly_inquirer", "name": "The Philadelphia Inquirer", "loc": "USA", "lang": "en",
     "site": "https://www.inquirer.com", "src": [("ff", "PA_PI")]},
    {"id": "seattle_times", "name": "The Seattle Times", "loc": "USA", "lang": "en",
     "site": "https://www.seattletimes.com", "src": [("ff", "WA_ST")]},
    {"id": "ajc", "name": "Atlanta Journal-Constitution", "loc": "USA", "lang": "en",
     "site": "https://www.ajc.com", "src": [("ff", "GA_AJC")]},
    {"id": "miami_herald", "name": "Miami Herald", "loc": "USA", "lang": "en",
     "site": "https://www.miamiherald.com", "src": [("ff", "FL_MH")]},
    {"id": "arizona_republic", "name": "The Arizona Republic", "loc": "USA", "lang": "en",
     "site": "https://www.azcentral.com", "src": [("ff", "AZ_AR")]},
    {"id": "washington_times", "name": "The Washington Times", "loc": "USA", "lang": "en",
     "site": "https://www.washingtontimes.com", "src": [("ff", "DC_WT")]},

    # ——— United Kingdom (Kiosko) ———
    {"id": "the_times", "name": "The Times", "loc": "UK", "lang": "en",
     "site": "https://www.thetimes.co.uk", "src": [("kiosko", "uk", "the_times")]},
    {"id": "telegraph", "name": "The Daily Telegraph", "loc": "UK", "lang": "en",
     "site": "https://www.telegraph.co.uk",
     "src": [("kiosko", "uk", "the_daily_telegraph"), ("kiosko", "uk", "telegraph")]},
    {"id": "the_independent", "name": "The Independent", "loc": "UK", "lang": "en",
     "site": "https://www.independent.co.uk",
     "src": [("kiosko", "uk", "the_independent"), ("kiosko", "uk", "independent")]},
    {"id": "i_paper", "name": "The i Paper", "loc": "UK", "lang": "en",
     "site": "https://inews.co.uk",
     "src": [("kiosko", "uk", "i"), ("kiosko", "uk", "inews"), ("kiosko", "uk", "the_i")]},
    {"id": "daily_mail", "name": "Daily Mail", "loc": "UK", "lang": "en",
     "site": "https://www.dailymail.co.uk", "src": [("kiosko", "uk", "daily_mail")]},
    {"id": "metro_uk", "name": "Metro", "loc": "UK", "lang": "en",
     "site": "https://metro.co.uk", "src": [("kiosko", "uk", "metro")]},
    {"id": "guardian", "name": "The Guardian", "loc": "UK", "lang": "en",
     "site": "https://www.theguardian.com", "src": [("kiosko", "uk", "guardian")]},

    # ——— France (Kiosko) ———
    {"id": "le_monde", "name": "Le Monde", "loc": "France", "lang": "fr",
     "site": "https://www.lemonde.fr", "src": [("kiosko", "fr", "le_monde")]},
    {"id": "le_figaro", "name": "Le Figaro", "loc": "France", "lang": "fr",
     "site": "https://www.lefigaro.fr", "src": [("kiosko", "fr", "le_figaro")]},
    {"id": "liberation", "name": "Libération", "loc": "France", "lang": "fr",
     "site": "https://www.liberation.fr", "src": [("kiosko", "fr", "liberation")]},
    {"id": "les_echos", "name": "Les Échos", "loc": "France", "lang": "fr",
     "site": "https://www.lesechos.fr", "src": [("kiosko", "fr", "les_echos")]},
    {"id": "le_parisien", "name": "Le Parisien", "loc": "France", "lang": "fr",
     "site": "https://www.leparisien.fr",
     "src": [("kiosko", "fr", "le_parisien"), ("kiosko", "fr", "aujourd_hui_en_france")]},
    {"id": "la_croix", "name": "La Croix", "loc": "France", "lang": "fr",
     "site": "https://www.la-croix.com", "src": [("kiosko", "fr", "la_croix")]},
    {"id": "l_equipe", "name": "L'Équipe", "loc": "France", "lang": "fr",
     "site": "https://www.lequipe.fr",
     "src": [("kiosko", "fr", "l_equipe"), ("kiosko", "fr", "lequipe")]},

    # ——— Spain (Kiosko) ———
    {"id": "el_pais", "name": "El País", "loc": "Spain", "lang": "es",
     "site": "https://elpais.com", "src": [("kiosko", "es", "el_pais")]},
    {"id": "el_mundo", "name": "El Mundo", "loc": "Spain", "lang": "es",
     "site": "https://www.elmundo.es", "src": [("kiosko", "es", "el_mundo")]},
    {"id": "abc_es", "name": "ABC", "loc": "Spain", "lang": "es",
     "site": "https://www.abc.es", "src": [("kiosko", "es", "abc")]},
    {"id": "la_vanguardia", "name": "La Vanguardia", "loc": "Spain", "lang": "es",
     "site": "https://www.lavanguardia.com", "src": [("kiosko", "es", "la_vanguardia")]},
    {"id": "el_periodico", "name": "El Periódico", "loc": "Spain", "lang": "es",
     "site": "https://www.elperiodico.com", "src": [("kiosko", "es", "el_periodico")]},
    {"id": "marca", "name": "Marca", "loc": "Spain", "lang": "es",
     "site": "https://www.marca.com", "src": [("kiosko", "es", "marca")]},
    {"id": "as_es", "name": "Diario AS", "loc": "Spain", "lang": "es",
     "site": "https://as.com",
     "src": [("kiosko", "es", "diario_as"), ("kiosko", "es", "as")]},

    # ——— Italy (Kiosko) ———
    {"id": "corriere", "name": "Corriere della Sera", "loc": "Italy", "lang": "it",
     "site": "https://www.corriere.it", "src": [("kiosko", "it", "corriere_della_sera")]},
    {"id": "repubblica", "name": "La Repubblica", "loc": "Italy", "lang": "it",
     "site": "https://www.repubblica.it", "src": [("kiosko", "it", "la_repubblica")]},
    {"id": "la_stampa", "name": "La Stampa", "loc": "Italy", "lang": "it",
     "site": "https://www.lastampa.it", "src": [("kiosko", "it", "la_stampa")]},
    {"id": "il_sole", "name": "Il Sole 24 Ore", "loc": "Italy", "lang": "it",
     "site": "https://www.ilsole24ore.com", "src": [("kiosko", "it", "il_sole_24_ore")]},
    {"id": "gazzetta", "name": "La Gazzetta dello Sport", "loc": "Italy", "lang": "it",
     "site": "https://www.gazzetta.it", "src": [("kiosko", "it", "la_gazzetta_dello_sport")]},

    # ——— Germany (Kiosko) ———
    {"id": "die_welt", "name": "Die Welt", "loc": "Germany", "lang": "de",
     "site": "https://www.welt.de", "src": [("kiosko", "de", "die_welt")]},
    {"id": "faz", "name": "Frankfurter Allgemeine", "loc": "Germany", "lang": "de",
     "site": "https://www.faz.net",
     "src": [("kiosko", "de", "frankfurter_allgemeine"), ("kiosko", "de", "faz")]},
    {"id": "sueddeutsche", "name": "Süddeutsche Zeitung", "loc": "Germany", "lang": "de",
     "site": "https://www.sueddeutsche.de",
     "src": [("kiosko", "de", "sueddeutsche_zeitung"), ("kiosko", "de", "sueddeutsche")]},
    {"id": "bild", "name": "Bild", "loc": "Germany", "lang": "de",
     "site": "https://www.bild.de", "src": [("kiosko", "de", "bild")]},
    {"id": "handelsblatt", "name": "Handelsblatt", "loc": "Germany", "lang": "de",
     "site": "https://www.handelsblatt.com", "src": [("kiosko", "de", "handelsblatt")]},
    {"id": "tagesspiegel", "name": "Der Tagesspiegel", "loc": "Germany", "lang": "de",
     "site": "https://www.tagesspiegel.de", "src": [("kiosko", "de", "der_tagesspiegel")]},
]


def candidate_url(src, d) -> str:
    if src[0] == "ff":
        return f"https://cdn.freedomforum.org/dfp/jpg{d.day}/lg/{src[1]}.jpg"
    if src[0] == "kiosko":
        return f"https://img.kiosko.net/{d:%Y/%m/%d}/{src[1]}/{src[2]}.750.jpg"
    raise ValueError(src)


def referer(url: str) -> str:
    if "kiosko" in url:
        return "https://en.kiosko.net/"
    return "https://www.freedomforum.org/todaysfrontpages/"


def get(session: requests.Session, url: str):
    try:
        r = session.get(url, timeout=TIMEOUT, headers={
            "User-Agent": UA, "Referer": referer(url),
            "Accept": "image/avif,image/webp,image/*,*/*"})
    except Exception as e:
        return False, f"ERR {e}"
    ct = r.headers.get("Content-Type", "")
    ok = r.status_code == 200 and ct.startswith("image/") and len(r.content) >= MIN_BYTES
    return ok, f"{r.status_code} {ct or '?'} {len(r.content)}B"


def main():
    session = requests.Session()
    results = []
    for c in CANDIDATES:
        winner = None
        last = ""
        for d in DATES:
            for src in c["src"]:
                url = candidate_url(src, d)
                ok, info = get(session, url)
                last = info
                if ok:
                    winner = (src, d, url)
                    break
            if winner:
                break

        row = {
            "id": c["id"], "name": c["name"], "loc": c["loc"], "lang": c["lang"],
            "site": c["site"], "ok": bool(winner),
        }
        if winner:
            src, d, url = winner
            row["src"] = list(src)
            row["date"] = d.isoformat()
            row["url"] = url
            print(f"OK  {c['name']:30s} -> {src}  ({d})")
        else:
            row["src"] = None
            row["last"] = last
            print(f"--  {c['name']:30s} (last: {last})")
        results.append(row)

    out = {
        "ran": datetime.now(timezone.utc).isoformat(),
        "dates_tried": [d.isoformat() for d in DATES],
        "ok_count": sum(1 for r in results if r["ok"]),
        "results": results,
    }
    out_path = Path(__file__).parent.parent / "state" / "probe_results.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {out_path} — {out['ok_count']}/{len(results)} reachable")


if __name__ == "__main__":
    main()
