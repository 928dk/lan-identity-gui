"""Verification has to be believable without a root prompt or a LAN.

So the reader, the scanner and the sleep are injected: each test describes one
kind of reading and asserts the verdict, not the machine.  The two things that
are easy to get wrong are covered explicitly — a reading that failed for an
unrelated reason (carrier, DHCP) is not a bad identity, and an address the sweep
did not see is "not seen", never "free".
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import verify  # noqa: E402
from catalog import Catalog  # noqa: E402

TARGET = "00:00:5E:00:53:41"
OTHER = "00:00:5E:00:53:42"


class FakeScan:
    def __init__(self, seen: dict[str, dict]) -> None:
        self.seen = seen


def reader_for(**overrides):
    """A reader whose every call returns the same reading unless overridden."""

    def read(iface: str) -> verify.InterfaceState:
        state = dict(
            iface=iface,
            mac=TARGET,
            carrier=True,
            ipv4="192.0.2.41",
            hostname="tahoe",
        )
        state.update(overrides)
        return verify.InterfaceState(**state)

    return read


class VerificationTests(unittest.TestCase):
    def _verify(self, **kwargs):
        kwargs.setdefault("reader", reader_for())
        kwargs.setdefault("scan", lambda iface: FakeScan({}))
        kwargs.setdefault("sleep", lambda seconds: None)
        kwargs.setdefault("dhcp_wait", 10)
        kwargs.setdefault("poll_interval", 5)
        return verify.verify_interface(TARGET, "tahoe", "eth0", **kwargs)

    # -- the happy path ------------------------------------------------- #

    def test_a_carried_address_with_a_lease_is_verified(self):
        verdict = self._verify(
            scan=lambda iface: FakeScan(
                {TARGET: {"ip": "192.0.2.41", "ips": ["192.0.2.41"]}}
            )
        )
        self.assertEqual(verdict.state, verify.STATE_VERIFIED)
        self.assertTrue(verdict.ok)
        self.assertFalse(verdict.is_failure)
        self.assertIn("192.0.2.41", verdict.note)

    def test_absence_from_the_sweep_is_not_called_free(self):
        verdict = self._verify(scan=lambda iface: FakeScan({}))
        self.assertEqual(verdict.state, verify.STATE_VERIFIED)
        self.assertIn("not seen", verdict.note)
        self.assertNotIn("free", verdict.note.lower())

    def test_a_failed_sweep_does_not_fail_the_identity(self):
        def boom(iface):
            raise RuntimeError("scan exploded")

        verdict = self._verify(scan=boom)
        self.assertEqual(verdict.state, verify.STATE_VERIFIED)
        self.assertIn("did not run", verdict.note)

    # -- the states ----------------------------------------------------- #

    def test_a_wrong_mac_on_the_interface_is_absent(self):
        verdict = self._verify(reader=reader_for(mac=OTHER))
        self.assertEqual(verdict.state, verify.STATE_ABSENT)
        self.assertTrue(verdict.is_failure)
        self.assertIn(OTHER, verdict.note)

    def test_no_mac_on_the_interface_is_absent(self):
        verdict = self._verify(reader=reader_for(mac=None))
        self.assertEqual(verdict.state, verify.STATE_ABSENT)
        self.assertIn("no MAC", verdict.note)

    def test_no_carrier_is_reported_as_itself(self):
        verdict = self._verify(reader=reader_for(carrier=False))
        self.assertEqual(verdict.state, verify.STATE_CARRIER_DOWN)
        self.assertTrue(verdict.is_failure)
        self.assertIn("carrier", verdict.note)

    def test_no_lease_within_the_wait_is_a_timeout(self):
        verdict = self._verify(reader=reader_for(ipv4=None), dhcp_wait=20)
        self.assertEqual(verdict.state, verify.STATE_TIMEOUT)
        self.assertTrue(verdict.is_failure)
        self.assertIn("20s", verdict.note)

    def test_a_late_lease_is_verified(self):
        """The bounded wait is a wait, not an instant check."""
        readings = [
            verify.InterfaceState("eth0", TARGET, True, None, "tahoe"),
            verify.InterfaceState("eth0", TARGET, True, None, "tahoe"),
            verify.InterfaceState("eth0", TARGET, True, "192.0.2.41", "tahoe"),
        ]
        calls = {"n": 0}

        def read(iface):
            index = min(calls["n"], len(readings) - 1)
            calls["n"] += 1
            return readings[index]

        verdict = self._verify(reader=read, dhcp_wait=30)
        self.assertEqual(verdict.state, verify.STATE_VERIFIED)
        self.assertGreaterEqual(calls["n"], 3)

    def test_the_same_mac_at_a_second_address_is_a_duplicate(self):
        verdict = self._verify(
            scan=lambda iface: FakeScan(
                {TARGET: {"ip": "192.0.2.99", "ips": ["192.0.2.99"]}}
            )
        )
        self.assertEqual(verdict.state, verify.STATE_DUPLICATE_MAC)
        self.assertTrue(verdict.is_failure)
        self.assertIn("192.0.2.99", verdict.note)

    def test_two_addresses_for_the_same_mac_are_a_duplicate(self):
        verdict = self._verify(
            scan=lambda iface: FakeScan(
                {TARGET: {"ips": ["192.0.2.41", "192.0.2.99"]}}
            )
        )
        self.assertEqual(verdict.state, verify.STATE_DUPLICATE_MAC)

    def test_no_address_skips_without_a_reading(self):
        calls = {"n": 0}

        def read(iface):
            calls["n"] += 1
            return verify.InterfaceState(iface, None, False, None)

        verdict = verify.verify_interface(
            None, None, "eth0", reader=read,
            scan=None, sleep=lambda seconds: None,
        )
        self.assertEqual(verdict.state, verify.STATE_SKIPPED)
        self.assertEqual(calls["n"], 0)

    def test_every_state_is_in_the_vocabulary(self):
        for state in verify.STATES:
            verdict = verify.Verdict(state, TARGET)
            self.assertIn(verdict.state, verify.STATES)
        self.assertEqual(len(set(verify.STATES)), len(verify.STATES))

    def test_describe_names_the_state(self):
        verdict = verify.Verdict(verify.STATE_TIMEOUT, TARGET, note="nothing arrived")
        self.assertIn("timeout", verify.describe(verdict))
        self.assertIn("nothing arrived", verify.describe(verdict))


class CatalogVerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_a_verdict_survives_a_reopen(self):
        path = self.root / "catalog.db"
        catalog = Catalog(path)
        device_id = catalog.add_device(TARGET)[0]
        catalog.record_verification(device_id, verify.STATE_VERIFIED, "read eth0")
        catalog.close()

        reopened = Catalog(path)
        self.addCleanup(reopened.close)
        device = reopened.device(device_id)
        self.assertEqual(device.verify_state, verify.STATE_VERIFIED)
        self.assertEqual(device.verify_note, "read eth0")
        self.assertTrue(device.last_verified_at)

    def test_an_old_catalog_gains_the_columns(self):
        """An older file has no verification columns; opening it must add them."""
        path = self.root / "old.db"
        conn = sqlite3.connect(str(path))
        conn.executescript(
            """
            CREATE TABLE devices (
                id INTEGER PRIMARY KEY,
                mac_norm TEXT,
                mac_raw TEXT NOT NULL,
                status TEXT NOT NULL,
                repair_to TEXT,
                repair_confidence TEXT,
                vendor TEXT,
                hostname TEXT,
                hostname_source TEXT,
                ip TEXT,
                seen_at TEXT,
                protected INTEGER NOT NULL DEFAULT 0,
                notes TEXT NOT NULL DEFAULT '',
                source TEXT,
                pool_mac INTEGER NOT NULL DEFAULT 0,
                pool_mac_pos INTEGER,
                created_at TEXT,
                updated_at TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO devices(mac_norm, mac_raw, status) VALUES(?,?,?)",
            ("00:00:5E:00:53:43", TARGET, "ok"),
        )
        conn.commit()
        conn.close()

        catalog = Catalog(path)
        self.addCleanup(catalog.close)
        rows = catalog.conn.execute("PRAGMA table_info(devices)").fetchall()
        names = {row["name"] for row in rows}
        for column in ("last_verified_at", "verify_state", "verify_note"):
            self.assertIn(column, names)
        device = catalog.devices()[0]
        self.assertIsNone(device.verify_state)
        catalog.record_verification(device.id, verify.STATE_ABSENT, "not on the interface")
        self.assertEqual(catalog.device(device.id).verify_state, verify.STATE_ABSENT)


def verify_seen(seen: dict[str, dict], **kwargs):
    """Verify a fixed reading against a fixed sweep, with nothing real reached."""
    kwargs.setdefault("reader", reader_for())
    kwargs.setdefault("scan", lambda iface: FakeScan(seen))
    kwargs.setdefault("sleep", lambda seconds: None)
    kwargs.setdefault("dhcp_wait", 10)
    kwargs.setdefault("poll_interval", 5)
    return verify.verify_interface(TARGET, "tahoe", "eth0", **kwargs)


class FreshnessTests(unittest.TestCase):
    """A neighbour cache entry is a record, not an answer.

    The defect this covers: a ``STALE`` entry for the MAC being verified, at an
    address other than the one the interface holds, was reported as
    ``duplicate_mac`` — the same verdict as a device that really did answer.  A
    leftover and a collision looked identical because the state word was parsed
    away before verification saw it.

    The rule after the fix is asymmetric on purpose.  A duplicate needs positive
    evidence: an address the sweep confirmed with a live answer.  An entry that
    carries no freshness information at all (an import, a hand-built map) proves
    neither, so it keeps the conservative verdict rather than silently stopping
    collision detection.
    """

    def test_a_stale_second_address_is_not_a_collision(self):
        seen = {
            TARGET: {"ip": "192.0.2.99", "ips": ["192.0.2.99"], "fresh": []},
        }
        verdict = verify_seen(seen)
        self.assertEqual(verdict.state, verify.STATE_VERIFIED)
        self.assertFalse(verdict.is_failure)
        self.assertIn("192.0.2.99", verdict.note)
        self.assertIn("not", verdict.note.lower())
        self.assertEqual(verdict.evidence["stale_at"], ["192.0.2.99"])

    def test_a_fresh_second_address_is_still_a_collision(self):
        seen = {
            TARGET: {"ip": "192.0.2.99", "ips": ["192.0.2.99"],
                     "fresh": ["192.0.2.99"]},
        }
        verdict = verify_seen(seen)
        self.assertEqual(verdict.state, verify.STATE_DUPLICATE_MAC)
        self.assertIn("192.0.2.99", verdict.note)

    def test_a_fresh_answer_beside_a_leftover_is_a_collision(self):
        seen = {
            TARGET: {"ips": ["192.0.2.98", "192.0.2.99"],
                     "fresh": ["192.0.2.99"]},
        }
        verdict = verify_seen(seen)
        self.assertEqual(verdict.state, verify.STATE_DUPLICATE_MAC)
        self.assertIn("192.0.2.99", verdict.note)
        self.assertNotIn("192.0.2.98", verdict.note)

    def test_an_entry_without_freshness_information_stays_a_collision(self):
        verdict = verify_seen({TARGET: {"ips": ["192.0.2.99"]}})
        self.assertEqual(verdict.state, verify.STATE_DUPLICATE_MAC)

    def test_the_interfaces_own_address_seen_stale_only_is_not_a_collision(self):
        verdict = verify_seen({TARGET: {"ips": ["192.0.2.41"], "fresh": []}})
        self.assertEqual(verdict.state, verify.STATE_VERIFIED)
        self.assertNotIn("stale_at", verdict.evidence)

    def test_a_scan_that_did_not_run_is_unchanged(self):
        def broken(iface):
            raise OSError("no sweep here")

        verdict = verify_seen({}, scan=broken)
        self.assertEqual(verdict.state, verify.STATE_VERIFIED)
        self.assertIn("the sweep did not run", verdict.note)


if __name__ == "__main__":
    unittest.main(verbosity=2)
