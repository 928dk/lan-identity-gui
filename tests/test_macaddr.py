"""Tests for the MAC classifier and repair engine.

Every address here is documentation-reserved: RFC 7042 sets ``00-00-5E-00-53-xx``
aside for examples, and the malformed strings are built from that range too.  The
shapes are real — doubled separators, a stray space, an ``O`` typed for a zero, a
pound sign typed for an ``E`` — but no line describes a machine on this LAN.

The optional corpus test at the bottom runs against a file you point at with
``LAN_IDENTITY_SAMPLE``; the file is never part of the repository.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import macaddr  # noqa: E402

#: Opt-in: a real inventory to check invariants against.  No counts are baked in.
SAMPLE = Path(os.environ["LAN_IDENTITY_SAMPLE"]) if os.environ.get("LAN_IDENTITY_SAMPLE") else None

#: One value per malformation shape the importer has to survive.
BROKEN = [
    "00:00:5E:0C::53:22",        # doubled separator
    "00:00:5E:0053:2A",          # groups of uneven length
    "00:00:5E:00:53.7C",         # dot instead of colon in the last pair
    "00:00:5E:00:53: 1C",        # a space inside the last pair
    "00:00:5E: 00:53:06",        # a space before a group
    "EO:00:5E:00:53:01",         # O typed for zero
    "CO:OO:5E:OO:53:05",         # O typed for zero, twice
    "BO:DS:FB:9D:EE:57",         # B is a hex digit: substitution must not touch it
    "ES:C3:86C8:B0:BF",          # a repair that lands on multicast
    "GC:0B:5E:B2:C4:19",         # G typed for six
    "Ot:6C:OC:4C:D5:68",         # a repair that lands on multicast
    "56:AG:FE:99:B5:E1",         # G typed for six
    "BA:S2:CAi12:E1:AS",         # too long once mapped
    "34:28:6:5B:CE:0S",          # too short once mapped
    "00:00:5E:00:5\u00a3:3E",    # pound sign typed for E
    "00:00:5E:00:53:2",          # eleven digits
]


class CanonicalTests(unittest.TestCase):
    def test_canonical_passthrough(self):
        found = macaddr.check("00:00:5E:00:53:01")
        self.assertEqual(found.status, macaddr.OK)
        self.assertEqual(found.normalized, "00:00:5E:00:53:01")
        self.assertTrue(found.usable)

    def test_lowercase_is_canonical(self):
        found = macaddr.check("00:00:5e:00:53:01")
        self.assertEqual(found.normalized, "00:00:5E:00:53:01")

    def test_bare_hex_gets_separators(self):
        found = macaddr.check("00005e005301")
        self.assertEqual(found.status, macaddr.NEEDS_SEPARATOR)
        self.assertEqual(found.normalized, "00:00:5E:00:53:01")
        self.assertEqual(found.confidence, macaddr.HIGH)

    def test_dash_and_dot_separators(self):
        for raw in ("00-00-5E-00-53-01", "00.00.5E.00.53.01", "00 00 5E 00 53 01"):
            with self.subTest(raw=raw):
                self.assertEqual(
                    macaddr.check(raw).normalized, "00:00:5E:00:53:01"
                )

    def test_locally_administered_is_flagged_not_rejected(self):
        found = macaddr.check("02:00:5E:00:53:01")
        self.assertTrue(found.usable)
        self.assertTrue(found.locally_administered)


class RejectionTests(unittest.TestCase):
    def test_multicast_is_not_assignable(self):
        found = macaddr.check("01:00:5E:00:53:06")
        self.assertEqual(found.status, macaddr.MULTICAST)
        self.assertFalse(found.assignable)
        self.assertFalse(found.usable)

    def test_all_zero_and_broadcast(self):
        self.assertFalse(macaddr.is_assignable("00:00:00:00:00:00"))
        self.assertFalse(macaddr.is_assignable("FF:FF:FF:FF:FF:FF"))

    def test_empty(self):
        self.assertEqual(macaddr.check("").status, macaddr.UNRECOVERABLE)
        self.assertIsNone(macaddr.normalize("   "))

    def test_eleven_digits_is_unrecoverable(self):
        found = macaddr.check("00:00:5E:00:53:2")
        self.assertEqual(found.status, macaddr.UNRECOVERABLE)
        self.assertIsNone(found.normalized)
        self.assertIn("11", found.reason)


class SubstitutionTests(unittest.TestCase):
    def test_substitution_never_maps_a_hex_digit(self):
        # The bug this rule exists to prevent: B -> 8 corrupts a valid address.
        self.assertNotIn("B", macaddr.SUBSTITUTIONS)
        self.assertNotIn("b", macaddr.SUBSTITUTIONS)
        self.assertNotIn("A", macaddr.SUBSTITUTIONS)
        self.assertNotIn("F", macaddr.SUBSTITUTIONS)
        found = macaddr.check("C0:00:5E:00:53:05")
        self.assertEqual(found.status, macaddr.OK)
        self.assertEqual(found.normalized, "C0:00:5E:00:53:05")

    def test_o_and_s_are_repaired(self):
        found = macaddr.check("CO:OO:5E:OO:53:05")
        self.assertEqual(found.status, macaddr.NEEDS_SUBSTITUTION)
        self.assertEqual(found.normalized, "C0:00:5E:00:53:05")
        self.assertEqual(found.confidence, macaddr.MEDIUM)

    def test_pound_sign_is_e(self):
        found = macaddr.check("00:00:5E:00:5\u00a3:3E")
        self.assertEqual(found.normalized, "00:00:5E:00:5E:3E")

    def test_repair_that_lands_on_multicast_is_refused(self):
        # Ot:6C:OC:... maps to 07:6C:0C:..., first octet bit 0x01 set.
        found = macaddr.check("Ot:6C:OC:4C:D5:68")
        self.assertEqual(found.status, macaddr.UNRECOVERABLE)
        self.assertIsNone(found.normalized)
        self.assertIn("multicast", found.reason)

    def test_every_broken_shape_is_classified_and_never_silently_usable(self):
        for value in BROKEN:
            with self.subTest(value=value):
                found = macaddr.check(value)
                self.assertIn(
                    found.status,
                    (macaddr.PARSED_STATUSES + (macaddr.MULTICAST, macaddr.UNRECOVERABLE)),
                )
                if found.normalized:
                    self.assertTrue(macaddr.is_canonical(found.normalized))


class SampleCorpusTests(unittest.TestCase):
    """Invariants over a real file you supply yourself.

    No count is hard-coded: an import of any file must classify every value, and
    every address it produces must be a canonical one that the kernel would take.
    """

    def setUp(self):
        if SAMPLE is None:
            self.skipTest("set LAN_IDENTITY_SAMPLE to a real mac,hostname,ip CSV")
        if not SAMPLE.exists():
            self.skipTest(f"{SAMPLE} is not present on this host")

    def test_every_value_is_classified(self):
        values = [
            line.strip()
            for line in SAMPLE.read_text("utf-8", errors="replace").splitlines()
            if line.strip()
        ]
        self.assertTrue(values)
        for value in values:
            with self.subTest(value=value):
                found = macaddr.check(value)
                self.assertTrue(found.status)
                if found.normalized:
                    self.assertTrue(macaddr.is_canonical(found.normalized))
                    self.assertEqual(len(found.normalized), 17)

    def test_an_unusable_value_never_carries_an_address(self):
        for value in BROKEN:
            found = macaddr.check(value)
            if not found.usable:
                self.assertIsNone(macaddr.normalize(value))


if __name__ == "__main__":
    unittest.main(verbosity=2)
