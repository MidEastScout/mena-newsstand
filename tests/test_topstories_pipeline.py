"""Tests for scripts/build_topstories.py ranking, clustering and summary
attachment, plus the scripts/check_topstories.py pre-publish gate."""
import json
import random

import build_topstories as bt
import check_topstories as ct


NOW = 1780000000.0          # fixed 'now' so ranking tests are reproducible
H = 3600.0


def _item(source, title, age_h=1.0, url=None, snippet=""):
    from datetime import datetime, timezone
    t = NOW - age_h * H
    return {
        "source": source,
        "category": bt.SRC_CATS.get(source),
        "title": title,
        "title_he": "",
        "snippet": snippet,
        "url": url or f"https://example.com/{abs(hash((source, title)))}",
        "published": datetime.fromtimestamp(t, timezone.utc).isoformat(),
        "_t": t,
    }


def _clusters_for(items, groups):
    return [{"member_indices": g,
             "representative_index": bt.pick_representative_heuristic(items, g)}
            for g in groups]


class TestRanking:
    def test_camp_diversity_lifts_equal_coverage(self):
        # Two stories, same outlet count and identical freshness; A spans one
        # camp, B spans four. B must outrank A — cross-camp coverage is a real
        # ranking signal, not a dead tie-break.
        a_srcs = ["Kan 11", "N12", "Channel 13", "Haaretz", "Ynet News"]
        b_srcs = ["Al Jazeera", "IRNA", "Anadolu Agency", "Gulf News", "WAFA News"]
        items, groups = [], []
        for srcs, evt in ((a_srcs, "Israeli military exercise drills continue near border"),
                          (b_srcs, "US strikes near Hormuz after tanker attacks disrupt strait")):
            idxs = []
            for s in srcs:
                idxs.append(len(items))
                items.append(_item(s, f"{evt} says {s}", age_h=2.0))
            groups.append(idxs)
        stories = bt.build_stories(items, _clusters_for(items, groups), now=NOW)
        assert "Hormuz" in stories[0]["rep"]["title"]
        assert stories[0]["outlets"] == stories[1]["outlets"] == 5

    def test_ranking_independent_of_input_order(self):
        # Shuffling item AND cluster order never changes the published ranking.
        rng = random.Random(42)
        base_items, base_groups = [], []
        for n, (evt, srcs) in enumerate([
            ("Gaza strikes continue overnight amid heavy raids",
             ["Al Jazeera", "WAFA News", "Middle East Eye"]),
            ("US strikes near Hormuz after tanker attacks disrupt strait",
             ["Reuters", "IRNA", "Gulf News", "Anadolu Agency"]),
            ("Khamenei funeral processions held across holy cities",
             ["IRNA", "Mehr News"]),
        ]):
            idxs = []
            for s in srcs:
                idxs.append(len(base_items))
                base_items.append(_item(s, f"{evt} report {s}", age_h=1.0 + n))
            base_groups.append(idxs)

        def run(seed):
            order = list(range(len(base_items)))
            rng2 = random.Random(seed)
            rng2.shuffle(order)
            items = [base_items[i] for i in order]
            remap = {old: new for new, old in enumerate(order)}
            groups = [[remap[i] for i in g] for g in base_groups]
            rng2.shuffle(groups)
            stories = bt.build_stories(items, _clusters_for(items, groups), now=NOW)
            return [tuple(sorted(m["url"] for m in s["members"])) for s in stories]

        baseline = run(0)
        for seed in range(1, 6):
            assert run(seed) == baseline

    def test_freshness_decay_outranks_stale_breadth(self):
        # 4 outlets fresh beats 5 outlets from yesterday: salience is 'talked
        # about NOW', not accumulated coverage hours.
        fresh = [_item(s, "US strikes near Hormuz after tanker attacks disrupt strait", age_h=1.0)
                 for s in ["Reuters", "IRNA", "Gulf News", "Anadolu Agency"]]
        stale = [_item(s, "Gaza strikes continue overnight amid heavy raids", age_h=30.0)
                 for s in ["Al Jazeera", "WAFA News", "Middle East Eye", "Al Manar", "SANA"]]
        items = fresh + stale
        groups = [list(range(4)), list(range(4, 9))]
        stories = bt.build_stories(items, _clusters_for(items, groups), now=NOW)
        assert "Hormuz" in stories[0]["rep"]["title"]


class TestStragglerGuard:
    def test_unrelated_straggler_stays_separate(self):
        # 'funeral' and 'khamenei' co-occur (>=2 headlines), so they merge into
        # one anchor; a village-funeral item shares ONLY that single topic
        # entity with the Khamenei story and must NOT be folded into it.
        items = [
            _item("IRNA", "Khamenei funeral draws crowds in Tehran"),
            _item("Mehr News", "Funeral of Khamenei begins in holy city"),
            _item("Al Jazeera", "Khamenei succession debate intensifies across Iran"),
            _item("Reuters", "Iran's Khamenei leaves complex legacy after decades"),
            _item("Jordan Times", "Village holds funeral for local elder"),
        ]
        clusters = bt.cluster_heuristic(items)
        by_member = {i: frozenset(g) for g in clusters for i in g}
        assert by_member[4] == frozenset({4}), \
            "unrelated funeral item must stay its own story"
        assert by_member[0] == frozenset({0, 1, 2, 3})

    def test_cross_language_member_still_folds(self):
        # An Arabic-titled item whose English snippet shares the story's
        # entities (khamenei + funeral + iran) must still join the cluster —
        # the guard may not strand cross-language coverage.
        items = [
            _item("IRNA", "Khamenei funeral draws crowds in Tehran"),
            _item("Mehr News", "Funeral of Khamenei begins in holy city"),
            _item("Al Jazeera", "Khamenei succession debate intensifies across Iran"),
            _item("Al Mayadeen", "تشييع القائد في طهران",
                  snippet="Funeral processions for Khamenei under way in Tehran, Iran."),
        ]
        clusters = bt.cluster_heuristic(items)
        by_member = {i: frozenset(g) for g in clusters for i in g}
        assert 3 in by_member[0], "cross-language coverage must stay counted"


class TestSummaryAttachment:
    def test_unverified_cluster_time_summary_stripped(self, tmp_path, monkeypatch):
        # A Claude-path summary asserting a stale status must be stripped even
        # with no Gemini key available (story falls back to its rep headline).
        monkeypatch.setattr(bt, "SUMM_CACHE", tmp_path / "cache.json")
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        items = [
            _item("Anadolu Agency",
                  "Turkish President Erdogan welcomes NATO leaders at summit in Ankara",
                  age_h=1.0),
            _item("Daily Sabah", "Turkey selected to host 2026 NATO summit", age_h=20.0),
        ]
        clusters = [{"member_indices": [0, 1], "representative_index": 0,
                     "summary": "Turkey is selected to host the NATO summit",
                     "summary_he": "טורקיה נבחרה לארח"}]
        stories = bt.build_stories(items, clusters, now=NOW)
        bt.attach_neutral_summaries(stories)
        assert "summary" not in stories[0]
        assert "summary_he" not in stories[0]
        assert stories[0]["rep"]["title"].startswith("Turkish President Erdogan")

    def test_verified_cluster_time_summary_kept(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bt, "SUMM_CACHE", tmp_path / "cache.json")
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        items = [
            _item("Anadolu Agency",
                  "Turkish President Erdogan welcomes NATO leaders at summit in Ankara"),
        ]
        clusters = [{"member_indices": [0], "representative_index": 0,
                     "summary": "NATO summit under way in Ankara as Erdogan welcomes leaders",
                     "summary_he": "ועידת נאט\"ו נפתחה באנקרה"}]
        stories = bt.build_stories(items, clusters, now=NOW)
        bt.attach_neutral_summaries(stories)
        assert stories[0]["summary"].startswith("NATO summit under way")

    def test_stale_cached_line_dropped(self, tmp_path, monkeypatch):
        # A previously cached line whose framing the sources have overtaken is
        # re-verified against CURRENT members every cycle and dropped.
        cache_path = tmp_path / "cache.json"
        monkeypatch.setattr(bt, "SUMM_CACHE", cache_path)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        items = [
            _item("Anadolu Agency",
                  "Turkish President Erdogan welcomes NATO leaders at summit in Ankara"),
            _item("Daily Sabah", "Turkey selected to host 2026 NATO summit", age_h=20.0),
        ]
        clusters = [{"member_indices": [0, 1], "representative_index": 0}]
        stories = bt.build_stories(items, clusters, now=NOW)
        key = bt._story_cache_key(stories[0])
        cache_path.write_text(json.dumps({
            "version": bt.SUMM_PROMPT_VERSION,
            "entries": {key: {"en": "Turkey to host NATO summit", "he": "..."}},
        }), encoding="utf-8")
        bt.attach_neutral_summaries(stories)
        assert "summary" not in stories[0]


class _StubGemini:
    """Stands in for the google-genai client: returns queued JSON responses."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.prompts = []
        self.models = self

    def generate_content(self, model, contents):
        self.prompts.append(contents)
        class R:
            pass
        r = R()
        r.text = self._responses.pop(0)
        return r


class TestGenerateVerified:
    def test_reject_then_retry_with_feedback_then_fallback(self):
        members = [
            {"source": "Anadolu Agency", "category": "turkish",
             "title": "Turkish President Erdogan welcomes NATO leaders at summit in Ankara",
             "url": "https://example.com/a", "published": "2026-07-08T09:00:00+00:00"},
            {"source": "Daily Sabah", "category": "turkish",
             "title": "Turkey selected to host 2026 NATO summit",
             "url": "https://example.com/b", "published": "2026-07-07T18:00:00+00:00"},
        ]
        stories = [
            {"rank": 1, "outlets": 2, "categories": ["turkish"],
             "rep": members[0], "members": members},
            {"rank": 2, "outlets": 2, "categories": ["turkish"],
             "rep": members[0], "members": members},
        ]
        # Call 1: story 1 grounded, story 2 stale. Call 2 (retry, story 2 only):
        # still ungrounded (invented city) → falls back to rep headline.
        stub = _StubGemini([
            json.dumps([{"en": "NATO summit under way in Ankara", "he": "א"},
                        {"en": "Turkey to host NATO summit", "he": "ב"}]),
            json.dumps([{"en": "NATO leaders gather in Vienna", "he": "ג"}]),
        ])
        out = bt._generate_verified(stub, stories, NOW)
        assert out[0]["en"] == "NATO summit under way in Ankara"
        assert out[1] == {"en": "", "he": ""}
        # The retry prompt must carry the verifier's complaint about the reject.
        assert "REJECTED" in stub.prompts[1]
        assert "anticipatory" in stub.prompts[1]

    def test_retry_success_is_used(self):
        members = [{"source": "Reuters", "category": "intl",
                    "title": "US strikes Iran after Hormuz attacks, Tehran threatens response",
                    "url": "https://example.com/c", "published": "2026-07-08T02:00:00+00:00"}]
        stories = [{"rank": 1, "outlets": 1, "categories": ["intl"],
                    "rep": members[0], "members": members}]
        stub = _StubGemini([
            json.dumps([{"en": "US strikes Qatar after Hormuz attacks", "he": "א"}]),
            json.dumps([{"en": "US strikes Iran after Hormuz attacks; Tehran threatens response",
                         "he": "ב"}]),
        ])
        out = bt._generate_verified(stub, stories, NOW)
        assert out[0]["en"].startswith("US strikes Iran")


class TestPublishGate:
    def _top(self):
        member = {"source": "Anadolu Agency", "category": "turkish",
                  "title": "Turkish President Erdogan welcomes NATO leaders at summit in Ankara",
                  "title_he": "", "url": "https://example.com/1",
                  "published": "2026-07-08T09:00:00+00:00"}
        return {"stories": [{
            "rank": 1, "outlets": 1, "categories": ["turkish"],
            "summary": "Turkey is selected to host the NATO summit",
            "summary_he": "טורקיה נבחרה לארח",
            "rep": dict(member), "members": [dict(member)],
        }]}

    def test_gate_strips_failing_summary(self):
        top = self._top()
        findings = ct.check_stories(top, {})
        assert [f["kind"] for f in findings] == ["summary"]
        # The workflow (non --check-only) path strips summary + summary_he.
        f = findings[0]
        f["story"].pop("summary"), f["story"].pop("summary_he")
        assert ct.check_stories(top, {}) == []

    def test_gate_uses_headline_snippets_for_grounding(self):
        top = self._top()
        top["stories"][0]["summary"] = \
            "NATO summit under way in Ankara; Zelensky attends, Erdogan welcomes leaders"
        # 'Zelensky' is only grounded via the snippet joined from headlines.json.
        assert len(ct.check_stories(top, {})) == 1
        snips = {"https://example.com/1":
                 "Erdogan welcomed leaders including Ukraine's Zelensky to the NATO summit."}
        assert ct.check_stories(top, snips) == []
