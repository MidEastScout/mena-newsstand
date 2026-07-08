"""Regression tests for Top-5 title grounding (scripts/title_grounding.py).

Each test pins a REAL failure mode that shipped to the live site — most
prominently the NATO-summit case, where the strip's title claimed Turkey
"is selected to host" a summit that was already under way. verify_title is
the single acceptance check for every title path (Gemini, Claude, cache,
pre-publish gate), so behaviour pinned here holds for all of them.
"""
from title_grounding import extract_entities, verify_title


def _m(title, source="Reuters", published="2026-07-08T09:00:00+00:00", snippet=""):
    return {"title": title, "source": source, "published": published,
            "snippet": snippet}


# ---------------------------------------------------------------------------
# THE NATO CASE — the incident this architecture exists to prevent. An older
# member frames the summit as upcoming; newer members show it under way. Any
# title still using the anticipatory framing must be rejected.
# ---------------------------------------------------------------------------
NATO_MEMBERS = [
    _m("Turkish President Erdogan welcomes NATO leaders at summit in Ankara",
       "Anadolu Agency", "2026-07-08T09:16:00+00:00"),
    _m("NATO leaders open final day of Ankara summit with focus on Arctic, Iran, Ukraine",
       "TRT World", "2026-07-08T08:30:00+00:00"),
    _m("Turkey selected to host 2026 NATO summit",
       "Daily Sabah", "2026-07-07T18:00:00+00:00"),
]


class TestNatoRegression:
    def test_stale_selected_to_host_rejected(self):
        res = verify_title("Turkey is selected to host the NATO summit", NATO_MEMBERS)
        assert not res.ok
        assert any("anticipatory" in p for p in res.problems)

    def test_stale_to_host_rejected(self):
        assert not verify_title("Turkey to host NATO summit", NATO_MEMBERS).ok

    def test_current_status_accepted(self):
        assert verify_title(
            "NATO summit under way in Ankara as Erdogan welcomes leaders",
            NATO_MEMBERS).ok

    def test_anticipatory_fine_before_event_starts(self):
        # Before any source reports the event as current, anticipatory framing
        # is CORRECT and must pass — the check is recency-driven, not a ban.
        upcoming_only = [_m("Turkey selected to host 2026 NATO summit", "Daily Sabah")]
        assert verify_title("Turkey to host 2026 NATO summit", upcoming_only).ok

    def test_completion_not_invented(self):
        # No source says the summit finished — a title may not conclude it.
        res = verify_title("NATO summit concludes in Ankara with joint declaration",
                           NATO_MEMBERS)
        assert not res.ok
        assert any("concluded" in p for p in res.problems)


class TestEntityGrounding:
    def test_unsupported_actor_rejected(self):
        # 'Vienna' appears in no source → the title invented a location.
        res = verify_title("NATO leaders arrive in Vienna for summit", NATO_MEMBERS)
        assert not res.ok
        assert any("vienna" in p for p in res.problems)

    def test_unsupported_number_rejected(self):
        members = [_m("Strikes reported near Hormuz, casualties feared")]
        res = verify_title("12 killed in strikes near Hormuz", members)
        assert not res.ok
        assert any("'12'" in p for p in res.problems)

    def test_supported_number_accepted(self):
        members = [_m("Three ships struck in Hormuz flare-up, 12 crew hurt")]
        assert verify_title("12 crew hurt as ships struck near Hormuz", members).ok

    def test_diacritics_and_aliases(self):
        # Türkiye ↔ Turkey, Erdoğan ↔ Erdogan, Tehran ↔ Iran.
        members = [_m("Turkey's Erdogan speaks with Iran's president")]
        assert verify_title("Türkiye's Erdoğan speaks as Tehran responds", members).ok

    def test_hyphenated_arabic_names(self):
        members = [_m("Iran is not alone: Nouri al-Maliki says in message of solidarity")]
        assert verify_title("Maliki sends solidarity message to Iran", members).ok

    def test_snippet_grounds_arabic_titled_outlets(self):
        # Arabic-titled member: the grounding signal lives in the English snippet.
        members = [_m("عنوان بالعربية", source="Al Mayadeen",
                      snippet="Qatar accuses Iran over the Hormuz incident.")]
        assert verify_title("Qatar accuses Iran over Hormuz incident", members).ok

    def test_entity_extraction_skips_generic_role_words(self):
        ents = extract_entities("Czech President says Türkiye essential to NATO security")
        assert "president" not in ents
        assert "turkiye" in ents and "nato" in ents


class TestNeutrality:
    def test_loaded_terms_rejected(self):
        members = [_m("Funeral prayers held for Iran's late supreme leader in Iraq")]
        res = verify_title("Funeral held for Iran's martyred Leader in Iraq", members)
        assert not res.ok
        assert any("loaded" in p for p in res.problems)

    def test_colonists_rejected_even_if_sources_use_it(self):
        # One camp's vocabulary never becomes the strip's vocabulary, even when
        # it is technically "grounded" in a member headline.
        members = [_m("Colonists raid village in the West Bank", "Al Manar")]
        assert not verify_title("Colonists raid West Bank village", members).ok

    def test_outlet_as_subject_rejected(self):
        # Live regression, 2026-07-08: "The New Arab reports on Israel using
        # sexual violence as a tool of genocide." — one outlet's coverage
        # presented as the story itself.
        members = [
            _m("How Israel uses sexual violence as a tool of genocide", "The New Arab"),
            _m("New Israeli settlement in northeast Jerusalem displaces Bedouins",
               "The New Arab"),
        ]
        res = verify_title(
            "The New Arab reports on Israel using sexual violence", members)
        assert not res.ok
        assert any("outlet" in p for p in res.problems)


class TestGoodTitlesPass:
    def test_live_grounded_titles_pass(self):
        # Real generated lines from 2026-07-08 that WERE properly grounded.
        members = [
            _m("Türkiye ‘essential to NATO’s collective security,’ Czech President Pavel says",
               "Anadolu Agency"),
            _m("Turkish President Erdogan welcomes NATO leaders at summit in Ankara",
               "Anadolu Agency"),
        ]
        assert verify_title(
            "Czech President says Türkiye essential to NATO security as leaders "
            "meet in Ankara.", members).ok

    def test_empty_title_fails(self):
        assert not verify_title("", NATO_MEMBERS).ok
