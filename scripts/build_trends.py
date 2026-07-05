#!/usr/bin/env python3
"""Builds trends.json — the "Outlet & Topic Trends" intelligence view.

Runs every refresh (cheap, no API). Classifies each current headline into ONE
curated MENA topic bucket (by priority, so each outlet's mix is a clean 100%
split), then computes:

  • topic volume this period (+ rising/falling vs ~24h ago)
  • what each outlet is covering most (its topic mix)
  • where outlets diverge on the day's biggest topics (coverage share spread)

A small rolling history (state/trends_history.json) is kept so the
rising/falling deltas are real, not fabricated — they fill in once ~24h of
snapshots exist.

Output (trends.json):
  { "updated", "total_headlines", "topics":[…], "outlets":[…],
    "divergence":[…], "history_points":N }
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT       = Path(__file__).parent.parent
HL_PATH    = ROOT / "headlines.json"
COV_PATH   = ROOT / "coverage.json"          # broad per-outlet sample (preferred)
OUT_PATH   = ROOT / "trends.json"
HIST_PATH  = ROOT / "state" / "trends_history.json"

HIST_MAX        = 96          # ~48h of 30-min snapshots
DELTA_MIN_AGE_H = 12          # need a snapshot at least this old to show a trend
DELTA_TGT_AGE_H = 24         # compare against the snapshot nearest this age
DIVERGENCE_TOPICS = 5         # how many top topics to break down by outlet
MIN_OUTLET_HL   = 2           # ignore near-empty outlets in the mix view

# Curated topic buckets, in PRIORITY order: a headline is assigned to the FIRST
# bucket whose pattern it matches, so the more specific regional conflicts win
# over the broad "Israel–Palestine", "US & West" and "Economy" catch-alls.
# English keywords carry word boundaries; Arabic terms are matched as substrings
# (Arabic prefixes ال/و/ب/ل attach to words, so substrings catch inflections).
TOPICS = [
    # Tracked off-the-wall topic: World Cup coverage is kept off the Headlines
    # wall but counted here (and in Pulse). Listed FIRST so a "… World Cup" item
    # is bucketed as World Cup rather than absorbed by a country topic. Matches
    # TRACKED_OFFTOPIC_RE in fetch_headlines.py (English + Arabic).
    #
    # Each pattern carries English (word-boundaried) + Arabic + HEBREW keywords,
    # so the Israeli channels' Hebrew coverage classifies too. Arabic/Hebrew are
    # matched as substrings (prefixes attach to words in both scripts), so only
    # unambiguous multi-letter terms are used; acronyms allow any gershayim
    # variant ("/'/״).
    ("world_cup",  "World Cup",           "#3F8F4F",
     r"world\s*cup|كأس العالم|المونديال|מונדיאל|גביע העולם"),
    ("gaza",       "Gaza & Hamas",        "#B0413E",
     r"gaza|hamas|rafah|khan younis|deir al-?balah|al-?shifa|nuseirat|jabalia"
     r"|غزة|حماس|رفح|خان يونس|النصيرات|جباليا"
     r"|עזה|חמאס|רפיח|ח'אן יונס|ג'באליא|רצועת עזה|הרצועה"),
    ("lebanon",    "Lebanon & Hezbollah", "#9C6B2E",
     r"lebanon|lebanese|hezbollah|hizbollah|beirut|nasrallah|naim qassem|"
     r"nabatieh|litani|blue line"
     r"|لبنان|حزب الله|بيروت|نصرالله|النبطية|الليطاني|الضاحية"
     r"|לבנון|לבנוני|חיזבאללה|חזבאללה|ביירות|נסראללה|נעים קאסם|ליטני"),
    ("yemen",      "Yemen & Red Sea",     "#C77D34",
     r"yemen|yemeni|houthi|sana'?a|red sea|bab el-?mandeb|hodeida|aden"
     r"|اليمن|الحوثي|صنعاء|البحر الأحمر|الحديدة"
     r"|תימן|חות'י|חותי|צנעא|הים האדום|אל-חודיידה|באב אל-מנדב"),
    ("syria",      "Syria",               "#7D6BA6",
     r"syria|syrian|damascus|assad|idlib|aleppo|hayat tahrir|\bhts\b|\bsdf\b|"
     r"deir ez-?zor|سوريا|سورية|دمشق|الأسد|إدلب|حلب"
     r"|סוריה|סורי|דמשק|אסד|אידליב|חלב|א-שרע|אל-שרע"),
    ("iraq",       "Iraq",                "#5B6E8C",
     r"iraq|iraqi|baghdad|islamic state|\bisis\b|daesh|kurdistan|erbil|"
     r"popular mobiliz|العراق|بغداد|داعش|كردستان|أربيل"
     r"|עיראק|עיראקי|בגדאד|דאעש|כורדיסטן|אירביל"),
    ("turkey",     "Turkey",              "#A4434B",
     r"turkey|turkish|erdogan|ankara|istanbul|\bypg\b"
     r"|تركيا|أردوغان|أنقرة|إسطنبول"
     r"|טורקיה|טורקי|ארדואן|ארדואן|אנקרה|איסטנבול"),
    ("egypt_naf",  "Egypt & N. Africa",   "#B08A3E",
     r"egypt|egyptian|cairo|\bsisi\b|suez|sudan|sudanese|libya|libyan|tunisia|"
     r"algeria|algerian|morocco|moroccan|rapid support"
     r"|مصر|القاهرة|السيسي|السودان|ليبيا|تونس|الجزائر|المغرب"
     r"|מצרים|מצרי|קהיר|א-סיסי|אל-סיסי|סואץ|סודאן|לוב|תוניסיה|אלג'יריה|מרוקו"),
    ("iran",       "Iran & Nuclear",      "#1F7A6E",
     r"iran|iranian|tehran|\birgc\b|khamenei|pezeshkian|revolutionary guard|"
     r"nuclear|enrich|uranium|centrifuge|natanz|fordow"
     r"|إيران|طهران|نووي|الحرس الثوري|بزشكيان|خامنئي|اليورانيوم"
     r"|איראן|איראני|טהראן|גרעין|הגרעין|משמרות המהפכה|ח'אמנאי|חמינאי|פזשכיאן|"
     r"אורניום|נתנז|פורדו|הפורום"),
    ("gulf",       "Gulf & Normalization","#3E7C59",
     r"saudi|riyadh|\buae\b|emirates|abu dhabi|dubai|qatar|doha|bahrain|kuwait|"
     r"\boman\b|muscat|\bgcc\b|gulf cooperation|normaliz|abraham accords|"
     r"bin salman|\bmbs\b|aramco"
     r"|السعودية|الرياض|الإمارات|أبوظبي|دبي|قطر|الدوحة|البحرين|الكويت|"
     r"التطبيع|بن سلمان|أرامكو|الخليج"
     r"|סעודיה|סעודית|ריאד|איחוד האמירויות|אמירויות|אבו דאבי|דובאי|קטאר|דוחא|"
     r"בחריין|כווית|עומאן|נורמליזציה|הסכמי אברהם|בן סלמאן|איראמקו|המפרץ"),
    ("israel_pal", "Israel–Palestine",    "#2F4B7C",
     r"israel|israeli|palestin|west bank|jerusalem|settler|settlement|knesset|"
     r"netanyahu|\bidf\b|jenin|ramallah|al-?aqsa|zionist|ben gvir|smotrich|"
     r"tel aviv|hostage"
     r"|إسرائيل|الاحتلال|فلسطين|الضفة|القدس|نتنياهو|الأقصى|صهيوني|الأسرى|"
     r"المستوطن|جنين|رام الله|تل أبيب"
     r"|ישראל|ישראלי|פלסטין|פלסטיני|הגדה המערבית|ירושלים|מתנחל|התנחלות|הכנסת|"
     r"נתניהו|צה\"ל|צה'ל|צהל|ג'נין|רמאללה|אל-אקצא|בן גביר|סמוטריץ'|תל אביב|"
     r"חטופים|החטופים|בג\"ץ|בג'ץ|בגץ"),
    ("us_west",    "US & the West",       "#4A6FA5",
     r"united states|washington|white house|\bu\.?s\.?\b|biden|trump|american|"
     r"pentagon|state department|europe|european union|britain|british|\buk\b|"
     r"france|french|germany|german|\bnato\b|brussels"
     r"|أمريكا|أميركا|واشنطن|ترامب|أوروبا|فرنسا|بريطانيا|الناتو|البيت الأبيض"
     r"|ארצות הברית|ארה\"ב|ארה'ב|וושינגטון|הבית הלבן|טראמפ|ביידן|פנטגון|"
     r"אירופה|האיחוד האירופי|בריטניה|צרפת|גרמניה|נאט\"ו|נאטו"),
    ("econ",       "Economy & Energy",    "#6E7B3D",
     r"\boil\b|crude|\bopec\b|natural gas|energy|economy|economic|inflation|"
     r"interest rate|\btrade\b|tariff|investment|stock market|barrel|budget|"
     r"\bimf\b|world bank|currency|\bgdp\b"
     r"|النفط|الغاز|الاقتصاد|أوبك|التضخم|البورصة|صندوق النقد"
     r"|נפט|אופ\"ק|גז טבעי|אנרגיה|כלכלה|כלכלי|אינפלציה|ריבית|מכס|השקעה|"
     r"הבורסה|תקציב|קרן המטבע|הבנק העולמי"),
]
# Hebrew labels for the site's EN ↔ HE toggle. The topic list is curated and
# fixed, so these are translated once by hand — perfectly accurate, no API.
LABELS_HE = {
    "world_cup":  "המונדיאל",
    "gaza":       "עזה וחמאס",
    "lebanon":    "לבנון וחיזבאללה",
    "yemen":      "תימן והים האדום",
    "syria":      "סוריה",
    "iraq":       "עיראק",
    "turkey":     "טורקיה",
    "egypt_naf":  "מצרים וצפון אפריקה",
    "iran":       "איראן והגרעין",
    "gulf":       "המפרץ והנורמליזציה",
    "israel_pal": "ישראל–פלסטינים",
    "us_west":    "ארה\"ב והמערב",
    "econ":       "כלכלה ואנרגיה",
    "other":      "אחר",
}

TOPIC_RES = [(tid, label, color, re.compile(pat, re.I))
             for tid, label, color, pat in TOPICS]
TOPIC_META = {tid: {"label": label, "label_he": LABELS_HE.get(tid, label),
                    "color": color}
              for tid, label, color, _ in TOPICS}
OTHER = {"id": "other", "label": "Other", "label_he": LABELS_HE["other"],
         "color": "#9AA3B2"}


def classify(text: str) -> str:
    """Return the single primary topic id for a headline (first match by
    priority), or 'other' if nothing matches."""
    for tid, _label, _color, rx in TOPIC_RES:
        if rx.search(text):
            return tid
    return "other"


def load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def parse_ts(s):
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def load_regions():
    """Prefer coverage.json (the broad sample of what each outlet is actually
    covering — up to ~40 items/outlet). Fall back to headlines.json (the 5/outlet
    shown on the wall) if coverage isn't available, so trends never goes blank."""
    cov = load_json(COV_PATH) or {}
    regions = cov.get("regions", {})
    has_cov = any(o.get("coverage") for outs in regions.values() for o in outs)
    if has_cov:
        return regions, "coverage"
    data = load_json(HL_PATH) or {}
    return data.get("regions", {}), "headlines"


def main():
    regions, basis = load_regions()

    now = datetime.now(timezone.utc)
    total = 0
    topic_counts = {}                      # tid -> headlines
    outlet_rows = []                       # per-outlet mix

    for region, outlets in regions.items():
        for outlet in outlets:
            source = outlet.get("source", "")
            # coverage.json carries "coverage"; headlines.json carries "headlines".
            hls = outlet.get("coverage") or outlet.get("headlines") or []
            counts = {}
            n = 0
            for h in hls:
                title = (h.get("title") or "").strip()
                if not title:
                    continue
                blob = f"{title} {h.get('snippet', '')}"
                tid = classify(blob)
                counts[tid] = counts.get(tid, 0) + 1
                topic_counts[tid] = topic_counts.get(tid, 0) + 1
                n += 1
            total += n
            if n >= MIN_OUTLET_HL:
                mix = sorted(
                    ({"id": t, "label": TOPIC_META.get(t, OTHER)["label"],
                      "label_he": TOPIC_META.get(t, OTHER)["label_he"],
                      "color": TOPIC_META.get(t, OTHER)["color"],
                      "count": c, "share": round(100 * c / n)}
                     for t, c in counts.items()),
                    key=lambda x: -x["count"])
                outlet_rows.append({
                    "source": source, "country": outlet.get("country", ""),
                    "total": n, "mix": mix,
                })

    # ---- topic volume + rising/falling vs ~24h ago ----
    hist = load_json(HIST_PATH) or {"snapshots": []}
    snaps = hist.get("snapshots", [])

    cur_share = {t: (100 * c / total if total else 0)
                 for t, c in topic_counts.items()}

    # pick the snapshot closest to DELTA_TGT_AGE_H old, within the valid window
    ref = None
    best_gap = None
    for s in snaps:
        ts = parse_ts(s.get("ts", ""))
        if not ts:
            continue
        age_h = (now - ts).total_seconds() / 3600
        if age_h < DELTA_MIN_AGE_H:
            continue
        gap = abs(age_h - DELTA_TGT_AGE_H)
        if best_gap is None or gap < best_gap:
            best_gap, ref = gap, s

    topics_out = []
    ordered = sorted(topic_counts.items(), key=lambda x: -x[1])
    for tid, c in ordered:
        meta = TOPIC_META.get(tid, OTHER)
        row = {"id": tid, "label": meta["label"], "label_he": meta["label_he"],
               "color": meta["color"],
               "count": c, "share": round(cur_share.get(tid, 0), 1),
               "delta": None}
        if ref:
            prev = ref.get("shares", {}).get(tid, 0)
            row["delta"] = round(cur_share.get(tid, 0) - prev, 1)
        topics_out.append(row)

    # ---- divergence: for the biggest topics, each outlet's coverage share ----
    divergence = []
    top_ids = [t for t, _ in ordered if t != "other"][:DIVERGENCE_TOPICS]
    for tid in top_ids:
        meta = TOPIC_META.get(tid, OTHER)
        shares = []
        for o in outlet_rows:
            sh = next((m["share"] for m in o["mix"] if m["id"] == tid), 0)
            shares.append({"source": o["source"], "share": sh})
        shares.sort(key=lambda x: -x["share"])
        covering = [s for s in shares if s["share"] > 0]
        if len(covering) < 2:
            continue
        spread = covering[0]["share"] - covering[-1]["share"]
        divergence.append({
            "id": tid, "label": meta["label"], "label_he": meta["label_he"],
            "color": meta["color"],
            "spread": spread,
            "top": covering[:4],
            "bottom": [s for s in shares if s["share"] == 0][:4],  # ignoring it
        })
    divergence.sort(key=lambda x: -x["spread"])

    # ---- persist snapshot for future deltas ----
    snaps.append({"ts": now.isoformat(),
                  "shares": {t: round(v, 2) for t, v in cur_share.items()},
                  "total": total})
    snaps = snaps[-HIST_MAX:]
    HIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    HIST_PATH.write_text(json.dumps({"snapshots": snaps}, ensure_ascii=False),
                         encoding="utf-8")

    out = {
        "updated": now.isoformat(),
        "total_headlines": total,
        "has_trend": ref is not None,
        "topics": topics_out,
        "outlets": sorted(outlet_rows, key=lambda x: -x["total"]),
        "divergence": divergence,
        "history_points": len(snaps),
    }
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"Wrote trends.json — {total} items ({basis}), {len(topics_out)} topics, "
          f"{len(outlet_rows)} outlets, trend={'yes' if ref else 'building'}")


if __name__ == "__main__":
    main()
