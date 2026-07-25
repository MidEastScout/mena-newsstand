#!/usr/bin/env python3
"""Regression tests for the web-push payload encoder (scripts/send_push.py).

A web-push record is capped at 4096 bytes on the wire. Hebrew notifications
silently blew past that for every Hebrew subscriber — json.dumps escapes each
Hebrew character to a 6-byte \\uXXXX sequence, so a digest that costs 1768
bytes in English came to 4219 in Hebrew. The push service answered

    400 binary data passed in the request must be less than 4096 bytes

and because 400 is not 404/410 the subscription was never pruned either: it
just failed again on the next cycle, forever, while the run stayed green.

These tests pin the two properties that keep that from recurring:
  * Hebrew is encoded as UTF-8, not as \\u escapes.
  * The encoder ALWAYS returns something inside the budget, whatever the
    headlines look like, and always terminates while doing so.
"""

import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE = "https://mideastscout.github.io/mena-newsstand/"


def _load():
    """Import send_push.py by path — scripts/ is not a package."""
    spec = importlib.util.spec_from_file_location("send_push", ROOT / "scripts" / "send_push.py")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:          # the script guards on missing config at import
        pass
    return mod


sp = _load()


def story(n_chars=60, url_chars=40, he_text="כ", en_text="x"):
    return {
        "summary": en_text * n_chars,
        "summary_he": he_text * n_chars,
        "outlets": 9,
        "rep": {"title": en_text * n_chars,
                "title_he": he_text * n_chars,
                "url": "https://example.com/" + "u" * url_chars},
    }


class PayloadEncoding(unittest.TestCase):
    def test_hebrew_is_utf8_not_escaped(self):
        """The bug itself: \\u escaping tripled the cost of every Hebrew payload."""
        raw = sp._encode_payload(sp.build_payload([story()], SITE, he=True))
        self.assertNotIn("\\u05", raw, "Hebrew is being \\u-escaped again")
        self.assertIn("כ", raw)

    def test_hebrew_payload_is_far_smaller_than_the_escaped_form(self):
        payload = sp.build_payload([story() for _ in range(5)], SITE, he=True)
        utf8 = len(sp._encode_payload(payload).encode("utf-8"))
        escaped = len(json.dumps(payload).encode("utf-8"))
        self.assertLess(utf8, escaped)

    def test_round_trips_to_the_same_object(self):
        payload = sp.build_payload([story() for _ in range(5)], SITE, he=True)
        self.assertEqual(json.loads(sp._encode_payload(payload)), payload)


class PayloadBudget(unittest.TestCase):
    """Whatever the input, the encoder must produce a record that fits."""

    CASES = {
        "typical":       (60, 40),
        "long":          (400, 300),
        "absurd":        (5_000, 2_000),
        "pathological":  (50_000, 9_000),
    }

    def test_every_shape_fits_the_budget(self):
        for name, (n, u) in self.CASES.items():
            for he in (False, True):
                with self.subTest(case=name, lang="he" if he else "en"):
                    stories = [story(n, u) for _ in range(5)]
                    out = sp._encode_payload(sp.build_payload(stories, SITE, he=he))
                    self.assertLessEqual(
                        len(out.encode("utf-8")), sp.PAYLOAD_BUDGET,
                        f"{name} payload exceeds the web-push budget")

    def test_budget_leaves_room_for_encryption_overhead(self):
        """aes128gcm adds an 86-byte header, a 16-byte tag and padding on top."""
        self.assertLess(sp.PAYLOAD_BUDGET, 4096)

    def test_output_is_always_valid_json_with_a_title(self):
        """A notification with no title is useless — the title must survive trimming."""
        for name, (n, u) in self.CASES.items():
            for he in (False, True):
                with self.subTest(case=name, lang="he" if he else "en"):
                    stories = [story(n, u) for _ in range(5)]
                    d = json.loads(sp._encode_payload(sp.build_payload(stories, SITE, he=he)))
                    self.assertTrue(d["title"], "title was trimmed away entirely")
                    self.assertTrue(d["url"], "tap-through url was trimmed away")

    def test_trimming_sheds_story_links_before_touching_the_text(self):
        stories = [story(400, 300) for _ in range(5)]
        d = json.loads(sp._encode_payload(sp.build_payload(stories, SITE, he=True)))
        self.assertLess(len(d["stories"]), 5, "expected the tap-through list to be trimmed")
        self.assertTrue(d["title"])

    def test_small_payloads_are_untouched(self):
        stories = [story(20, 10)]
        payload = sp.build_payload(stories, SITE, he=True)
        self.assertEqual(json.loads(sp._encode_payload(payload)), payload)
        self.assertEqual(len(json.loads(sp._encode_payload(payload))["stories"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
