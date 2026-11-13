"""Tests for hostname validation and the offline suggestion engines."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import hostnames  # noqa: E402


class ValidationTests(unittest.TestCase):
    def test_plain_label(self):
        self.assertTrue(hostnames.is_valid("kontor1"))
        self.assertEqual(hostnames.normalize("Kontor1"), "kontor1")

    def test_hyphen_rules(self):
        self.assertTrue(hostnames.is_valid("lenovo-loq"))
        self.assertFalse(hostnames.is_valid("-leading"))
        self.assertFalse(hostnames.is_valid("trailing-"))
        self.assertFalse(hostnames.is_valid("--"))

    def test_invalid_characters(self):
        for name in ("under_score", "dot.name", "æble", "host!"):
            with self.subTest(name=name):
                self.assertFalse(hostnames.is_valid(name))

    def test_inner_whitespace_is_removed_like_the_script_does(self):
        # mac-rotator.sh strips [[:space:]] before validating, so this is valid.
        self.assertEqual(hostnames.normalize("spa ce"), "space")

    def test_too_long(self):
        self.assertFalse(hostnames.is_valid("a" * 64))
        self.assertTrue(hostnames.is_valid("a" * 63))

    def test_prefix_is_not_doubled(self):
        self.assertEqual(hostnames.normalize("pc-office", "pc-"), "pc-office")
        self.assertEqual(hostnames.normalize("office", "pc-"), "pc-office")

    def test_prefix_counts_against_the_limit(self):
        self.assertFalse(hostnames.is_valid("a" * 63, "pc-"))

    def test_reason_is_human_readable(self):
        valid, reason = hostnames.validate("bad_name")
        self.assertFalse(valid)
        self.assertIn("invalid characters", reason)

    def test_fleet_name_warning(self):
        notes = hostnames.warnings("tahoe", "somethingelse", set(), fleet=["tahoe"])
        self.assertTrue(any("network" in note for note in notes))

    def test_an_empty_fleet_list_warns_about_nothing(self):
        self.assertEqual(hostnames.warnings("tahoe", "somethingelse", set()), [])

    def test_current_hostname_warning(self):
        notes = hostnames.warnings("miranda", "miranda", set())
        self.assertTrue(any("current hostname" in note for note in notes))


class SuggestionTests(unittest.TestCase):
    def test_series_continuation(self):
        found = hostnames.suggest(
            vendor="HP", lan_names=["tahoe1", "tahoe2", "erie"]
        )
        names = [item.name for item in found]
        self.assertIn("tahoe3", names)

    def test_vendor_style_fallback(self):
        found = hostnames.suggest(vendor="Synology Incorporated")
        self.assertTrue(found)
        self.assertTrue(any("synology" in item.name.lower() for item in found))

    def test_vendor_default_pattern_for_intel(self):
        found = hostnames.suggest(vendor="Intel Corporate")
        names = [item.name for item in found]
        self.assertTrue(any(name.startswith("DESKTOP-") for name in names), names)

    def test_all_suggestions_are_valid(self):
        found = hostnames.suggest(
            vendor="Compal Information (Kunshan)",
            existing_names=["kontor1"],
            lan_names=["kontor1", "O-RUM", "erie"],
        )
        for item in found:
            with self.subTest(name=item.name):
                self.assertTrue(hostnames.is_valid(item.name), item)

    def test_generate_batch_is_distinct(self):
        produced = hostnames.generate_batch(5, existing_names=["kontor1"])
        self.assertEqual(len(produced), len(set(produced)))
        for name in produced:
            self.assertTrue(hostnames.is_valid(name), name)

    def test_unknown_vendor_still_yields_something(self):
        found = hostnames.suggest(vendor=None, lan_names=[])
        self.assertTrue(found)
        self.assertTrue(hostnames.is_valid(found[0].name))


if __name__ == "__main__":
    unittest.main(verbosity=2)
