#!/usr/bin/env python3
"""Regression tests for the Top-5 stories pipeline.

Run any time you touch scripts/topstories_pipeline.py or
scripts/build_topstories.py:

    python3 tests/test_topstories.py           # all tests
    python3 tests/test_topstories.py -v        # with test names
    python3 -m unittest discover tests         # equivalent

Stdlib unittest only — no pytest, no install, so it runs in CI (which
installs just requirements.txt) and on a bare checkout alike.

WHAT THESE LOCK DOWN. Each test class maps to a failure this feature
actually shipped at some point, so a future change that reintroduces one
fails here instead of on the live site:

  ScriptDetection / Normalize — Hebrew leaked into the English strip for
      weeks because the "is this English?" guard screened Arabic and
      Persian but deliberately skipped Hebrew, and because the English
      snippet used as a fallback is not always English.
  Relevance — exam results, human-interest and listicle items reached the
      Top 5 by riding inside a cluster whose other members were security
      stories.
  Cluster — two unrelated stories merged because they shared only
      geography ("…in Gaza and the West Bank").
  Rank — the displayed outlet badge disagreed with the sort order in 67%
      of cycles, because ranking used a freshness-decayed weight while the
      badge showed the raw count.
  ValidationGate / Fallback — nothing enforced the display contract before
      publishing, and there was no last-known-good to fall back to.
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import topstories_pipeline as tp  # noqa: E402

# A fixed "now" so freshness/staleness behaviour is deterministic.
NOW = datetime(2026, 7, 24, 20, 0, tzinfo=timezone.utc)
NOW_TS = NOW.timestamp()


def ago(hours: float) -> str:
    return (NOW - timedelta(hours=hours)).isoformat()


def item(source: str, title: str, snippet: str = "", hours_ago: float = 1.0,
         title_he: str = "") -> dict:
    """One raw pipeline item, shaped exactly as fetch_stage produces them."""
    published = ago(hours_ago)
    return {
        "source": source,
        "category": tp.SRC_CATS.get(source),
        "region": "test",
        "title": title,
        "title_he": title_he,
        "snippet": snippet,
        "url": f"https://example.test/{source}/{abs(hash(title)) % 10**8}",
        "published": published,
        "_t": tp.parse_ts(published),
    }


def normalized(items, translate=None):
    return tp.normalize_stage(items, translate=translate, now=NOW_TS)["items"]


def fake_translator(mapping: dict):
    """Stands in for the Azure Translator; returns None for anything it
    doesn't know, which is how a real outage/quota failure behaves."""
    return lambda texts: [mapping.get(t) for t in texts]


# Realistic headlines reused across tests.
HE_STRIKE = "דיווח: ישראל תקפה יעדים של חיזבאללה בדרום לבנון"
HE_STRIKE_EN = "Report: Israel struck Hezbollah targets in southern Lebanon"
AR_GAZA = "حماس: الاحتلال يسعى إلى تصعيد وتيرة عدوانه على غزة"
AR_GAZA_EN = "Hamas says the occupation seeks to escalate its assault on Gaza"


# ===========================================================================
class ScriptDetection(unittest.TestCase):
    """The display contract: English means Latin script, nothing else."""

    def test_hebrew_is_not_english(self):
        # The original bug: the guard screened Arabic but let Hebrew pass.
        self.assertFalse(tp.is_english_display(HE_STRIKE))
        self.assertFalse(tp.is_english_display("טראמפ: הגיע הזמן שהסעודים יצטרפו"))
        self.assertEqual(tp.detect_script(HE_STRIKE), "hebrew")

    def test_arabic_and_persian_are_not_english(self):
        self.assertFalse(tp.is_english_display(AR_GAZA))
        self.assertFalse(tp.is_english_display("ایران اینترنشنال"))
        self.assertEqual(tp.detect_script(AR_GAZA), "arabic")

    def test_other_scripts_are_not_english(self):
        for text in ("Россия нанесла удар", "Τουρκία", "中国宣布", "이란"):
            self.assertFalse(tp.is_english_display(text), text)

    def test_plain_english_passes(self):
        self.assertTrue(tp.is_english_display("Israel strikes Hezbollah in south Lebanon"))
        self.assertEqual(tp.detect_script("Israel strikes Hezbollah"), "latin")

    def test_english_with_accents_and_punctuation_passes(self):
        # Real outlet names/words must not be mistaken for a foreign script.
        self.assertTrue(tp.is_english_display("Türkiye's Erdoğan meets Iran's Pezeshkian"))
        self.assertTrue(tp.is_english_display("L'Orient Today: 'ceasefire' holds — for now"))

    def test_a_single_hebrew_word_disqualifies_the_whole_string(self):
        # Partial contamination is still contamination.
        self.assertFalse(tp.is_english_display("Israel strikes חיזבאללה positions"))

    def test_empty_and_symbol_only_are_not_english(self):
        for text in ("", "   ", "—", "123 456"):
            self.assertFalse(tp.is_english_display(text), repr(text))


class Thresholds(unittest.TestCase):
    """The published thresholds, locked.

    These four numbers are the feature's contract and are documented in
    scripts/build_topstories.py and README-topstories.md. Changing one is a
    legitimate product decision — but it must be deliberate, so it has to be
    changed here too, which forces the documentation to be updated with it.
    """

    def test_documented_thresholds(self):
        self.assertEqual(tp.TOP_N, 5, "the site shows five stories")
        self.assertEqual(tp.MIN_STORY_OUTLETS, 2,
                         "a 'top' story needs at least two distinct outlets")
        self.assertEqual(tp.FRESHNESS_WINDOW_H, 48,
                         "dated items older than 48h never enter the pipeline")
        self.assertEqual(tp.STALE_GAP_SEC, 6 * 3600,
                         "the headline must come from the newest 6h of coverage")


class TitleCleaning(unittest.TestCase):
    def test_trailing_outlet_attribution_stripped(self):
        self.assertEqual(
            tp.clean_title("Presidency condemns settler attacks - WAFA Agency", "WAFA News"),
            "Presidency condemns settler attacks")

    def test_trailing_foreign_attribution_stripped(self):
        self.assertEqual(
            tp.clean_title("Jordan intercepts Iranian drones - سانا", "SANA"),
            "Jordan intercepts Iranian drones")

    def test_most_read_rank_prefix_stripped(self):
        # Most-read scrapes leak their list position: "1 ANALYSIS …".
        self.assertEqual(tp.clean_title("1 ANALYSIS Iran bears the cost", "Iran International"),
                         "ANALYSIS Iran bears the cost")

    def test_leading_number_kept_when_it_is_news(self):
        # "5 killed…" is a fact, not a list position.
        self.assertEqual(tp.clean_title("5 killed in strike on Gaza", "Al Jazeera"),
                         "5 killed in strike on Gaza")

    def test_bidi_control_marks_removed(self):
        cleaned = tp.clean_title("‏Israel strikes south Lebanon‎", "Kan 11")
        self.assertEqual(cleaned, "Israel strikes south Lebanon")
        self.assertTrue(tp.is_english_display(cleaned))


# ===========================================================================
class Normalize(unittest.TestCase):
    """Stage 2 — nothing non-English may leave here, and it runs BEFORE
    clustering so a foreign title can never reach the display layer."""

    def test_hebrew_title_is_translated_to_english(self):
        items = normalized([item("Kan 11", HE_STRIKE, snippet="")],
                           translate=fake_translator({HE_STRIKE: HE_STRIKE_EN}))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["display_title"], HE_STRIKE_EN)
        self.assertEqual(items[0]["title_source"], "translated")
        self.assertTrue(tp.is_english_display(items[0]["display_title"]))

    def test_arabic_title_is_translated_to_english(self):
        items = normalized([item("Al Mayadeen", AR_GAZA)],
                           translate=fake_translator({AR_GAZA: AR_GAZA_EN}))
        self.assertEqual(items[0]["display_title"], AR_GAZA_EN)
        self.assertTrue(tp.is_english_display(items[0]["display_title"]))

    def test_english_snippet_used_when_translation_unavailable(self):
        items = normalized([item("N12", HE_STRIKE,
                                 snippet="Israeli jets struck Hezbollah sites overnight. "
                                         "The military confirmed the operation.")])
        self.assertEqual(items[0]["title_source"], "snippet")
        self.assertEqual(items[0]["display_title"],
                         "Israeli jets struck Hezbollah sites overnight.")

    def test_item_dropped_when_no_english_is_obtainable(self):
        # The snippet is NOT reliably English: when Gemini's quota is spent,
        # fetch_headlines.py copies the feed's own Hebrew description into it.
        # Displaying that was the original leak, so the item must be dropped.
        out = tp.normalize_stage(
            [item("Kan 11", HE_STRIKE, snippet="לאחר כיממה של חיפושים, יובל קוגן נמצא")],
            translate=None, now=NOW_TS)
        self.assertEqual(out["items"], [])
        self.assertEqual(len(out["dropped"]), 1)
        self.assertIn("no English", out["dropped"][0]["reason"])

    def test_native_english_title_is_kept_verbatim(self):
        items = normalized([item("Reuters", "Iran strikes US base in Jordan")])
        self.assertEqual(items[0]["display_title"], "Iran strikes US base in Jordan")
        self.assertEqual(items[0]["title_source"], "native")

    def test_hebrew_title_he_is_preserved_for_the_hebrew_view(self):
        # HE mode still needs the Hebrew; only the EN field is forced.
        items = normalized([item("Kan 11", HE_STRIKE, title_he=HE_STRIKE)],
                           translate=fake_translator({HE_STRIKE: HE_STRIKE_EN}))
        self.assertEqual(items[0]["title_he"], HE_STRIKE)
        self.assertEqual(items[0]["display_title"], HE_STRIKE_EN)

    def test_stale_items_never_enter_the_pipeline(self):
        # 96h is hardcoded on purpose: deriving it from FRESHNESS_WINDOW_H
        # would make this test move with the constant and quietly stop
        # testing anything if the window were ever widened or disabled.
        out = tp.normalize_stage(
            [item("Reuters", "Old strike report", hours_ago=96),
             item("Reuters", "Fresh strike report", hours_ago=2)],
            now=NOW_TS)
        titles = [i["display_title"] for i in out["items"]]
        self.assertEqual(titles, ["Fresh strike report"])
        self.assertIn("stale", out["dropped"][0]["reason"])

    def test_undated_items_are_kept(self):
        # Many outlets publish without a timestamp; they must not be culled.
        undated = item("Al Jazeera", "Iran launches new Gulf attacks")
        undated["published"], undated["_t"] = "", 0.0
        self.assertEqual(len(normalized([undated])), 1)

    def test_translation_is_requested_only_for_non_english_titles(self):
        asked = []

        def spy(texts):
            asked.extend(texts)
            return [None] * len(texts)

        normalized([item("Reuters", "Iran strikes US base in Jordan"),
                    item("Kan 11", HE_STRIKE, snippet="Israeli jets struck sites.")],
                   translate=spy)
        self.assertEqual(asked, [HE_STRIKE])


# ===========================================================================
class Relevance(unittest.TestCase):
    """Stage 3 — the stricter Top-5 second pass, applied per item so an
    off-topic story cannot ride into a cluster on its neighbours' vocabulary."""

    def _keeps(self, title, snippet=""):
        kept = tp.relevance_stage(normalized([item("Al Jazeera", title, snippet)]))["items"]
        return bool(kept)

    def test_sports_is_filtered(self):
        self.assertFalse(self._keeps(
            "Barcelona beat Real Madrid 3-1 in the Champions League final"))
        self.assertFalse(self._keeps("Egypt's Salah scores hat-trick in AFCON opener"))

    def test_lifestyle_and_listicles_are_filtered(self):
        self.assertFalse(self._keeps("PHOTO Best restaurants in Istanbul, ranked"))
        self.assertFalse(self._keeps("Top 20 museums in Türkiye"))

    def test_exam_results_are_filtered(self):
        # Shipped inside Top-5 clusters for a dozen cycles.
        self.assertFalse(self._keeps(
            "The Minister of Education announced that 1103 high school students from Gaza "
            "and the West Bank passed the general secondary exams"))

    def test_human_interest_is_filtered(self):
        self.assertFalse(self._keeps(
            "A 4-year-old named Yuval was found safe and sound in Ashkelon after being missing"))

    def test_natural_disaster_without_conflict_is_filtered(self):
        self.assertFalse(self._keeps(
            "Landslide kills 30 and displaces hundreds after heavy flooding"))

    def test_disaster_with_a_conflict_dimension_is_kept(self):
        self.assertTrue(self._keeps(
            "Earthquake hits region as Israeli air strikes continue, rescue teams blocked"))

    def test_real_security_stories_are_kept(self):
        for title in (
            "Iran strikes US bases in Jordan and Bahrain with drones",
            "Israeli forces kill four Palestinians in the occupied West Bank",
            "Air defences intercept seven Iranian missiles, six drones",
            "Gulf states and Jordan reaffirm right to self-defence against Iranian attacks",
        ):
            self.assertTrue(self._keeps(title), title)

    def test_diplomacy_is_kept(self):
        self.assertTrue(self._keeps("US and Saudi Arabia sign nuclear agreement"))
        self.assertTrue(self._keeps("Qatar and Pakistan propose 10-day Iran-US ceasefire"))

    def test_drop_reasons_are_recorded_for_debugging(self):
        out = tp.relevance_stage(normalized([
            item("Hürriyet Daily News", "PHOTO Best restaurants in Istanbul, ranked")]))
        self.assertEqual(len(out["dropped"]), 1)
        d = out["dropped"][0]
        self.assertIn("reason", d)
        self.assertIn("sec", d)
        self.assertIn("off", d)


# ===========================================================================
class Clustering(unittest.TestCase):
    """Stage 4 — same story together, different stories apart."""

    def _clusters(self, items):
        norm = normalized(items)
        groups = tp.cluster_heuristic(norm)
        return norm, [sorted(g) for g in groups]

    def test_paraphrases_of_one_story_cluster_together(self):
        items = [
            item("Reuters", "Israel strikes Hezbollah positions in south Lebanon"),
            item("Al Jazeera", "IDF targets Hezbollah sites in southern Lebanon"),
            item("Anadolu Agency", "Israeli jets hit Hezbollah posts in southern Lebanon"),
        ]
        norm, groups = self._clusters(items)
        self.assertEqual(len(groups), 1, "differently-worded reports of one event must merge")
        self.assertEqual(len(groups[0]), 3)

    def test_unrelated_stories_do_not_merge(self):
        items = [
            item("Reuters", "Israel strikes Hezbollah positions in south Lebanon"),
            item("Al Jazeera", "IDF targets Hezbollah sites in southern Lebanon"),
            item("Arab News", "US and Saudi Arabia sign nuclear agreement"),
            item("Gulf News", "Riyadh and Washington conclude nuclear deal talks"),
        ]
        norm, groups = self._clusters(items)
        for g in groups:
            titles = " ".join(norm[i]["display_title"] for i in g)
            self.assertFalse("Hezbollah" in titles and "nuclear" in titles,
                             "a Lebanon strike and a nuclear deal must not share a cluster")

    def test_known_limit_lexically_disjoint_paraphrases_do_not_merge(self):
        """DOCUMENTED LIMITATION, not an aspiration.

        "US and Saudi Arabia sign nuclear agreement" and "Riyadh and
        Washington conclude nuclear deal talks" are the same event but share
        exactly one content word once stopwords and geography are removed.
        No word-overlap heuristic can join them without also joining
        unrelated stories — which is the blob-merging failure this clusterer
        was built to avoid — so the heuristic path leaves them apart.

        In practice this is mild: a real story carries more outlets and more
        shared vocabulary (the live US-Saudi story clustered across five
        outlets), and the semantic Claude path in build_topstories.py merges
        cases like this when ANTHROPIC_API_KEY is set. If this test ever
        starts failing because the two DID merge, that is an improvement —
        verify no unrelated stories merged too, then update it."""
        _, groups = self._clusters([
            item("Arab News", "US and Saudi Arabia sign nuclear agreement"),
            item("Gulf News", "Riyadh and Washington conclude nuclear deal talks"),
        ])
        self.assertEqual(len(groups), 2)

    def test_shared_geography_alone_does_not_merge(self):
        """The production bug, reproduced.

        An education item was folded into the West Bank violence story
        because both named "Gaza"/"the West Bank" — inflating that story's
        outlet count and putting exam results on the site's most visible
        surface. Geography is not an event.

        The fixture needs the items that name BOTH territories: they make
        `gaza` and `westbank` co-occur often enough to merge into a single
        topic anchor, which is what put the education item in the same
        anchor as the violence items in production. Without them the
        education item anchors elsewhere and never exercises this rule.
        Revert the GEO_TOKENS subtraction in cluster_heuristic and all six
        items collapse into one cluster.
        """
        items = [
            item("TRT World", "Israeli forces kill four Palestinians in occupied West Bank"),
            item("WAFA News", "Four Palestinians killed by Israeli forces in the West Bank"),
            item("Al Arabiya", "Four Palestinians and one Israeli dead after West Bank shooting"),
            item("Al Jazeera",
                 "Israeli raids continue across Gaza and the West Bank, Palestinians say"),
            item("Middle East Eye",
                 "Israeli military operations expand in Gaza and the West Bank"),
            item("Falastin al-Youm",
                 "Education ministry says 1103 students from Gaza and the West Bank "
                 "sat their secondary certificate exams"),
        ]
        norm, groups = self._clusters(items)
        exam = next(i for i, n in enumerate(norm) if "exams" in n["display_title"])
        exam_group = next(g for g in groups if exam in g)
        self.assertEqual(exam_group, [exam],
                         "the education item must not be folded into the violence story")

    def test_cluster_stage_accepts_llm_clusters_unchanged(self):
        norm = normalized([item("Reuters", "Iran strikes US base in Jordan"),
                           item("IRNA", "Iranian army hits US positions in Jordan")])
        given = [{"member_indices": [0, 1], "representative_index": 0,
                  "summary": "Iran strikes a US base in Jordan.", "summary_he": "..."}]
        out = tp.cluster_stage(norm, llm_clusters=given)
        self.assertEqual(out["method"], "llm")
        self.assertEqual(out["clusters"], given)

    def test_heuristic_is_used_without_llm_clusters(self):
        norm = normalized([item("Reuters", "Iran strikes US base in Jordan")])
        self.assertEqual(tp.cluster_stage(norm)["method"], "heuristic")


class RepresentativeChoice(unittest.TestCase):
    """Which member's wording heads the story."""

    def test_neutral_wording_beats_loaded_wording(self):
        norm = normalized([
            item("Al Manar", "Zionist entity's criminal aggression against martyred heroes"),
            item("Reuters", "Israel strikes Hezbollah positions in south Lebanon"),
        ])
        chosen = tp.pick_representative(norm, [0, 1])
        self.assertEqual(norm[chosen]["source"], "Reuters")

    def test_bare_stub_headline_is_avoided(self):
        norm = normalized([
            item("L'Orient Today", "LIVE"),
            item("Reuters", "Israel strikes Hezbollah positions in south Lebanon"),
        ])
        self.assertEqual(norm[tp.pick_representative(norm, [0, 1])]["source"], "Reuters")

    def test_explanation_names_the_reason_a_member_was_passed_over(self):
        norm = normalized([
            item("Al Manar", "Zionist entity's criminal aggression against martyred heroes"),
            item("Reuters", "Israel strikes Hezbollah positions in south Lebanon"),
        ])
        chosen = tp.pick_representative(norm, [0, 1])
        why = tp.describe_representative(norm, chosen, [0, 1])
        self.assertEqual(why["source"], "Reuters")
        self.assertTrue(why["why"])
        self.assertEqual(why["passed_over"][0]["rejected_by"], "loaded/partisan wording")


# ===========================================================================
class Ranking(unittest.TestCase):
    """Stage 5 — distinct outlets, then camp spread, then recency. The
    displayed badge and the sort key must be the same number."""

    def _rank(self, spec):
        """spec: [[(source, hours_ago), ...], ...] — one list per cluster."""
        items, clusters = [], []
        for group in spec:
            idxs = []
            for source, hrs in group:
                idxs.append(len(items))
                items.append(item(source, f"Missile strike reported near {source} bureau",
                                  hours_ago=hrs))
            clusters.append({"member_indices": idxs, "representative_index": idxs[0]})
        norm = normalized(items)
        return tp.rank_stage(norm, clusters)["stories"]

    def test_more_outlets_ranks_first(self):
        stories = self._rank([
            [("Reuters", 3), ("BBC", 3)],                              # 2 outlets
            [("IRNA", 1), ("Mehr News", 1), ("Al Jazeera", 1)],        # 3 outlets, newer
        ])
        self.assertEqual([s["outlets"] for s in stories], [3, 2],
                         "outlet count outranks recency")

    def test_recency_never_outranks_outlet_count(self):
        # The regression: a freshness-decayed weight let a fresh 2-outlet
        # story beat an older 5-outlet story, so the badge disagreed with
        # the order. Ranking is now the plain count.
        stories = self._rank([
            [("Reuters", 40), ("BBC", 40), ("CNN", 40), ("AFP", 40), ("Al Jazeera", 40)],
            [("IRNA", 0.2), ("Mehr News", 0.2)],
        ])
        self.assertEqual([s["outlets"] for s in stories], [5, 2])

    def test_camp_spread_breaks_an_outlet_tie(self):
        stories = self._rank([
            [("IRNA", 2), ("Mehr News", 2)],          # both iranian → 1 camp
            [("Reuters", 3), ("Al Jazeera", 3)],      # intl + panarab → 2 camps
        ])
        self.assertEqual(len(stories[0]["categories"]), 2)

    def test_recency_breaks_a_full_tie(self):
        stories = self._rank([
            [("Reuters", 10), ("Al Jazeera", 10)],
            [("BBC", 1), ("Middle East Eye", 1)],
        ])
        self.assertEqual(stories[0]["members"][0]["source"], "BBC")

    def test_single_outlet_stories_never_qualify(self):
        stories = self._rank([
            [("Reuters", 1), ("BBC", 1)],
            [("CNN", 1)],
        ])
        self.assertEqual(len(stories), 1)
        self.assertTrue(all(s["outlets"] >= tp.MIN_STORY_OUTLETS for s in stories))

    def test_same_outlet_twice_counts_once(self):
        stories = self._rank([[("Reuters", 1), ("Reuters", 2), ("Reuters", 3)]])
        self.assertEqual(stories, [], "three items from one outlet is not a cross-outlet story")

    def test_fewer_than_five_rather_than_padding(self):
        stories = self._rank([
            [("Reuters", 1), ("BBC", 1)],
            [("CNN", 1)],
            [("AFP", 1)],
        ])
        self.assertEqual(len(stories), 1)

    def test_at_most_five_stories(self):
        stories = self._rank([[(f"Reuters", 1), ("BBC", 1)]] * 8)
        self.assertLessEqual(len(stories), tp.TOP_N)

    def test_ranks_are_contiguous_and_members_newest_first(self):
        stories = self._rank([
            [("Reuters", 5), ("BBC", 1), ("CNN", 3)],
            [("IRNA", 2), ("Mehr News", 4)],
        ])
        self.assertEqual([s["rank"] for s in stories], list(range(1, len(stories) + 1)))
        for s in stories:
            ts = [tp.parse_ts(m["published"]) for m in s["members"]]
            self.assertEqual(ts, sorted(ts, reverse=True))


# ===========================================================================
class ValidationGate(unittest.TestCase):
    """Stage 6 — the contract enforced before anything is displayed."""

    def _story(self, **over):
        base = {
            "rank": 1, "outlets": 2, "categories": ["intl"],
            "rep": {"source": "Reuters", "title": "Israel strikes Hezbollah in south Lebanon",
                    "title_he": "", "url": "u1", "published": ago(1)},
            "members": [
                {"source": "Reuters", "title": "Israel strikes Hezbollah in south Lebanon",
                 "title_he": "", "url": "u1", "published": ago(1)},
                {"source": "BBC", "title": "Israeli jets hit Hezbollah sites in Lebanon",
                 "title_he": "", "url": "u2", "published": ago(2)},
            ],
        }
        base.update(over)
        return base

    def test_clean_story_passes(self):
        self.assertTrue(tp.validate_stage([self._story()])["ok"])

    def test_hebrew_representative_title_fails_the_cycle(self):
        s = self._story()
        s["rep"]["title"] = HE_STRIKE
        v = tp.validate_stage([s])
        self.assertFalse(v["ok"])
        self.assertIn("english", [f["check"] for f in v["failures"]])

    def test_hebrew_member_title_fails_the_cycle(self):
        s = self._story()
        s["members"][1]["title"] = HE_STRIKE
        self.assertFalse(tp.validate_stage([s])["ok"])

    def test_hebrew_summary_is_repaired_not_failed(self):
        # The summary is the one displayed string that can be safely dropped:
        # the representative headline behind it is already English.
        s = self._story(summary="כותרת בעברית")
        v = tp.validate_stage([s])
        self.assertTrue(v["ok"])
        self.assertNotIn("summary", v["stories"][0])
        self.assertTrue(v["repairs"])

    def test_english_summary_is_kept(self):
        v = tp.validate_stage([self._story(summary="Israel strikes Hezbollah sites.")])
        self.assertTrue(v["ok"])
        self.assertEqual(v["stories"][0]["summary"], "Israel strikes Hezbollah sites.")

    def test_single_outlet_story_fails(self):
        s = self._story(outlets=1)
        s["members"] = s["members"][:1]
        v = tp.validate_stage([s])
        self.assertFalse(v["ok"])
        self.assertIn("outlets", [f["check"] for f in v["failures"]])

    def test_outlet_count_must_match_the_members(self):
        # The badge must never lie about the story behind it.
        v = tp.validate_stage([self._story(outlets=9)])
        self.assertFalse(v["ok"])
        self.assertIn("outlets", [f["check"] for f in v["failures"]])

    def test_out_of_order_stories_fail(self):
        big = self._story(rank=2, outlets=3)
        big["members"] = big["members"] + [
            {"source": "CNN", "title": "Third outlet reports the Lebanon strike",
             "title_he": "", "url": "u3", "published": ago(3)}]
        v = tp.validate_stage([self._story(rank=1), big])
        self.assertFalse(v["ok"])
        self.assertIn("ordering", [f["check"] for f in v["failures"]])

    def test_irrelevant_story_fails(self):
        s = self._story()
        for m in s["members"]:
            m["title"] = "Barcelona beat Real Madrid 3-1 in the Champions League final"
        s["rep"]["title"] = s["members"][0]["title"]
        v = tp.validate_stage([s])
        self.assertFalse(v["ok"])
        self.assertIn("relevance", [f["check"] for f in v["failures"]])

    def test_empty_result_fails(self):
        self.assertFalse(tp.validate_stage([])["ok"])

    def test_non_contiguous_ranks_fail(self):
        self.assertFalse(tp.validate_stage([self._story(rank=2)])["ok"])

    def test_relevance_can_be_skipped_for_republished_payloads(self):
        # A published file has titles but no snippets; it already passed the
        # full check when it was published.
        s = self._story()
        self.assertTrue(tp.validate_stage([s], check_relevance=False)["ok"])

    def test_internal_keys_are_stripped_before_publishing(self):
        s = self._story()
        s["_members_full"] = s["members"]
        out = tp.strip_internal([s])
        self.assertNotIn("_members_full", out[0])
        self.assertIn("members", out[0])


# ===========================================================================
class EndToEnd(unittest.TestCase):
    """A full cycle over a realistic mixed feed: Hebrew and Arabic outlets,
    sports, lifestyle, exam results, and two genuine cross-outlet stories."""

    def setUp(self):
        raw = [
            # Story A — 4 outlets, differently worded, two of them non-English.
            item("Reuters", "Iran strikes US bases in Jordan and Bahrain with drones", hours_ago=1),
            item("Al Jazeera", "Iranian drones hit US military positions in Jordan", hours_ago=2),
            item("IRNA", "Iran hits US military locations in Jordan, Bahrain", hours_ago=3),
            item("Al Mayadeen", AR_GAZA, snippet="", hours_ago=2),   # translated below
            # Story B — 2 outlets.
            item("TRT World", "Israeli forces kill four Palestinians in occupied West Bank",
                 hours_ago=4),
            item("WAFA News", "Four Palestinians killed by Israeli forces in the West Bank",
                 hours_ago=5),
            # Noise that must never reach the Top 5.
            item("Hürriyet Daily News", "PHOTO Best restaurants in Istanbul, ranked"),
            item("Egypt Independent", "Barcelona beat Real Madrid 3-1 in the cup final"),
            item("Falastin al-Youm",
                 "Education ministry says 1103 students sat their secondary exams"),
            item("Channel 13", "A 4-year-old was found safe and sound in Ashkelon"),
            # Hebrew with no English anywhere — must be dropped, never shown.
            item("Kan 11", HE_STRIKE, snippet="לאחר כיממה של חיפושים"),
        ]
        translate = fake_translator({
            AR_GAZA: "Iranian drones struck US positions in Jordan and Bahrain, Hamas says",
        })
        norm = tp.normalize_stage(raw, translate=translate, now=NOW_TS)
        rel = tp.relevance_stage(norm["items"])
        clus = tp.cluster_stage(rel["items"])
        self.ranked = tp.rank_stage(rel["items"], clus["clusters"])
        self.validated = tp.validate_stage(self.ranked["stories"])
        self.norm, self.rel = norm, rel

    def test_cycle_passes_validation(self):
        self.assertTrue(self.validated["ok"], self.validated["failures"])

    def test_every_displayed_string_is_english(self):
        for s in self.validated["stories"]:
            self.assertTrue(tp.is_english_display(s["rep"]["title"]), s["rep"]["title"])
            for m in s["members"]:
                self.assertTrue(tp.is_english_display(m["title"]), m["title"])

    def test_no_irrelevant_content_survives(self):
        blob = " ".join(m["title"].lower()
                        for s in self.validated["stories"] for m in s["members"])
        for banned in ("restaurant", "barcelona", "exams", "found safe"):
            self.assertNotIn(banned, blob)

    def test_stories_are_ordered_by_outlet_count(self):
        counts = [s["outlets"] for s in self.validated["stories"]]
        self.assertEqual(counts, sorted(counts, reverse=True))

    def test_the_bigger_story_leads(self):
        self.assertEqual(self.validated["stories"][0]["outlets"], 4)

    def test_untranslatable_hebrew_item_was_dropped_not_displayed(self):
        dropped = [d["title"] for d in self.norm["dropped"]]
        self.assertIn(HE_STRIKE, dropped)

    def test_every_story_meets_the_minimum_outlet_count(self):
        for s in self.validated["stories"]:
            self.assertGreaterEqual(s["outlets"], tp.MIN_STORY_OUTLETS)

    def test_audit_table_explains_every_candidate(self):
        rows = [r for r in self.ranked["table"] if "rank_key" in r]
        self.assertTrue(rows)
        for r in rows:
            self.assertEqual(r["rank_key"]["outlets"], len(r["outlet_names"]))
            if not r["qualified"]:
                self.assertTrue(r["why_not"])


class Documentation(unittest.TestCase):
    """README-topstories.md must keep describing the code that exists.

    Documentation rots silently: the thresholds, function names and knobs it
    quotes drift as the code changes, and nothing notices until someone
    trusts a stale number. These checks fail instead.
    """

    @classmethod
    def setUpClass(cls):
        cls.doc_path = Path(__file__).parent.parent / "README-topstories.md"
        cls.doc = cls.doc_path.read_text(encoding="utf-8")

    def test_documented_thresholds_match_the_code(self):
        for name, value in (("TOP_N", tp.TOP_N),
                            ("MIN_STORY_OUTLETS", tp.MIN_STORY_OUTLETS),
                            ("FRESHNESS_WINDOW_H", tp.FRESHNESS_WINDOW_H),
                            ("STALE_GAP_SEC", tp.STALE_GAP_SEC)):
            self.assertRegex(self.doc, rf"\|\s*`{name}`\s*\|\s*{value}\b",
                             f"README-topstories.md must list {name} = {value}")

    def test_every_stage_function_named_in_the_doc_exists(self):
        for fn in ("normalize_stage", "relevance_stage", "cluster_stage",
                   "rank_stage", "validate_stage", "pick_representative",
                   "describe_representative", "is_english_display"):
            self.assertIn(fn, self.doc, f"{fn} should be documented")
            self.assertTrue(hasattr(tp, fn), f"{fn} is documented but does not exist")

    def test_every_tunable_named_in_the_doc_exists(self):
        for name in ("SEC_WORDS", "SEC_PHRASES", "OFF_WORDS", "OFF_PHRASES",
                     "DISASTER_WORDS", "CONFLICT_WORDS", "ENTITIES_STRONG",
                     "ENTITIES_BROAD", "GEO_TOKENS", "SRC_CATS", "CAT_ORDER",
                     "_REP_SOURCE_RANK"):
            self.assertIn(name, self.doc, f"{name} should be documented")
            self.assertTrue(hasattr(tp, name), f"{name} is documented but does not exist")

    def test_every_camp_is_documented(self):
        for camp in tp.CAT_ORDER:
            self.assertIn(f"`{camp}`", self.doc, f"camp {camp} missing from the doc")

    def test_ranking_rule_is_stated_in_priority_order(self):
        outlets = self.doc.index("number of distinct outlets")
        camps = self.doc.index("spread across outlet camps")
        recency = self.doc.index("**recency**")
        self.assertLess(outlets, camps)
        self.assertLess(camps, recency)


if __name__ == "__main__":
    unittest.main(verbosity=2)
