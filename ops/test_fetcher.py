#!/usr/bin/env python3
"""Unit tests for fetcher.py. Run: python3 -m unittest discover -s ops -v
(or python3 ops/test_fetcher.py). Stdlib only, no network."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetcher  # noqa: E402


def rec(**kw):
    base = {
        "group_name": "TestCrew",
        "post_title": "Example Corp",
        "activity": "Retail",
        "country": "SE",
        "description": "",
        "discovered": "2026-10-20T07:00:00+00:00",
        "published": "",
    }
    base.update(kw)
    return base


class TestSanitize(unittest.TestCase):
    def test_invisible_format_chars_stripped(self):
        for ch in ("\u202e", "\u200b", "\u00ad", "\u061c", "\u180e", "￻"):
            self.assertNotIn(ch, fetcher.sanitize_str("ab" + ch + "cd", 100))

    def test_tags_block_stripped(self):
        self.assertNotIn("\U000E0065", fetcher.sanitize_str("ab\U000E0065cd", 100))

    def test_cap(self):
        self.assertEqual(len(fetcher.sanitize_str("x" * 5000, 60)), 60)


class TestTransform(unittest.TestCase):
    def test_wrong_typed_field_drops_element_not_batch(self):
        # regression: one malformed record must not abort the whole source
        evs = fetcher.transform([rec(), rec(post_title=5), rec(post_title="Other Corp")])
        self.assertEqual(len(evs), 2)

    def test_missing_required_fields_fail_closed(self):
        # F2: all-invalid non-empty responses refuse rather than wipe
        with self.assertRaises(ValueError):
            fetcher.transform([rec(group_name="")])

    def test_iso3_country_dropped_not_truncated(self):
        evs = fetcher.transform([rec(country="CHN")])
        self.assertEqual(evs[0]["c"], "")

    def test_nonempty_raw_all_invalid_raises(self):
        with self.assertRaises(ValueError):
            fetcher.transform([rec(discovered="not-a-date", published="")])

    def test_fractional_seconds_any_width_accepted(self):
        evs = fetcher.transform([rec(discovered="2026-10-20T07:00:00.1234567+00:00")])
        self.assertEqual(len(evs), 1)

    def test_output_shape(self):
        ev = fetcher.transform([rec()])[0]
        self.assertEqual(
            sorted(ev.keys()),
            ["a", "c", "d", "di", "g", "k", "pu", "t"],
        )


class TestCanonAndMerge(unittest.TestCase):
    def test_canon_merges_suffixes_and_underscores(self):
        self.assertEqual(
            fetcher.canon_text("Acme_Corp Inc."), fetcher.canon_text("acme corp")
        )

    def test_merge_corroborates_and_fills(self):
        a = {"k": "x", "g": "A", "t": "T", "a": "", "c": "", "d": "",
             "di": "2026-10-22T00:00:00+00:00", "pu": ""}
        b = {"k": "x", "g": "A2", "t": "T2", "a": "Legal", "c": "US", "d": "text",
             "di": "2026-10-21T00:00:00+00:00", "pu": ""}
        m = fetcher.merge([("primary", 1, [a]), ("acme-ti", 2, [b])])[0]
        self.assertEqual(m["s"], ["primary", "acme-ti"])
        self.assertEqual(m["c"], "US")       # fill-down from secondary
        self.assertEqual(m["di"], "2026-10-21T00:00:00+00:00")  # earliest wins

    def test_rival_claims_stay_separate(self):
        a = rec(post_title="Victim").copy()
        a = fetcher.transform([rec(post_title="Victim")])[0]
        b = fetcher.transform([rec(group_name="OtherCrew", post_title="Victim")])[0]
        self.assertNotEqual(a["k"], b["k"])

    def test_earliest_compares_instants_across_offsets(self):
        a = {"k": "x", "g": "A", "t": "T", "a": "", "c": "", "d": "",
             "di": "2026-10-29T01:00:00+05:00", "pu": ""}  # = 28th 20:00Z
        b = {"k": "x", "g": "A", "t": "T", "a": "", "c": "", "d": "",
             "di": "2026-10-29T00:00:00+00:00", "pu": ""}
        m = fetcher.merge([("primary", 1, [a]), ("acme-ti", 2, [b])])[0]
        self.assertEqual(m["di"], "2026-10-29T01:00:00+05:00")


class TestProMapping(unittest.TestCase):
    def test_pro_fields_project(self):
        pro = [{"group": "Q", "victim": "V", "activity": "Retail", "country": "SE",
                "description": "d", "discovered": "2026-10-20T07:00:00+00:00",
                "attackdate": "2026-10-19T22:00:00+00:00", "permalink": "x"}]
        ev = fetcher.transform(fetcher.map_raw(pro, {"inv": fetcher.PRO_FIELD_MAP}))[0]
        self.assertEqual((ev["g"], ev["t"], ev["c"]), ("Q", "V", "SE"))
        self.assertEqual(ev["pu"], "2026-10-19T22:00:00+00:00")
        self.assertNotIn("permalink", json.dumps(ev))


class TestSourcesConfig(unittest.TestCase):
    def cfg(self, obj):
        p = tempfile.mktemp(suffix=".json")
        with open(p, "w") as fh:
            fh.write(json.dumps(obj))
        self.addCleanup(os.unlink, p)
        return p

    def test_example_file_is_valid_json(self):
        here = os.path.dirname(os.path.abspath(__file__))
        srcs = fetcher.load_sources(os.path.join(here, "sources.json.example"))
        self.assertEqual(srcs[0]["name"], "acme-ti")
        self.assertEqual(srcs[0]["inv"]["actor_name"], "group_name")

    def test_duplicate_map_field_rejected(self):
        with self.assertRaises(ValueError):
            fetcher.load_sources(
                self.cfg([{"name": "x", "url": "https://x.example/v",
                           "map": {"group": "same", "title": "same"}}])
            )

    def test_bad_name_rejected(self):
        with self.assertRaises(ValueError):
            fetcher.load_sources(self.cfg([{"name": "Bad Name",
                                            "url": "https://x.example/v",
                                            "map": {"group": "g", "title": "t"}}]))

    def test_http_url_rejected(self):
        with self.assertRaises(ValueError):
            fetcher.load_sources(self.cfg([{"name": "x", "url": "http://x.example/v",
                                            "map": {"group": "g", "title": "t"}}]))


class TestAuthKey(unittest.TestCase):
    def test_illegal_key_rejected_without_echo(self):
        os.environ["GJ_TEST_KEY"] = "sec\r\nret"
        try:
            with self.assertRaises(ValueError) as cm:
                fetcher.fetch_http("https://example.invalid/x",
                                   "Authorization", "GJ_TEST_KEY")
            self.assertNotIn("sec", str(cm.exception))
        finally:
            del os.environ["GJ_TEST_KEY"]


class TestDemo(unittest.TestCase):
    def test_demo_shape(self):
        evs = fetcher.demo_events()
        self.assertEqual(len(evs), 40)
        self.assertTrue(all(e["g"].startswith("DEMO") for e in evs))
        self.assertTrue(any(e["a"] == "Government & Defense" for e in evs))
        self.assertTrue(all("s" not in e for e in evs))  # s is added by merge()


if __name__ == "__main__":
    unittest.main()
