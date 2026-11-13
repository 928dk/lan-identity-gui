"""Tests for the catalog: import, merge, repair decisions, pool ordering.

The fixture addresses are documentation-reserved: MACs from the range RFC 7042
sets aside for examples (``00-00-5E-00-53-xx``, plus locally administered
``02:`` variants) and addresses from ``192.0.2.0/24``.  Hostnames come from the
bundled name bank.  None of this describes a real network.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import macaddr  # noqa: E402
from catalog import Catalog  # noqa: E402

#: A real inventory is never committed.  Point this at your own file to run the
#: corpus test:  LAN_IDENTITY_SAMPLE=/path/to/hosts.csv ./run-tests.sh
REAL_SAMPLE = (
    Path(os.environ["LAN_IDENTITY_SAMPLE"])
    if os.environ.get("LAN_IDENTITY_SAMPLE")
    else None
)

SAMPLE = """\
mac,hostname,ip,seen_at
00:00:5e:00:53:20,tahoe,192.0.2.21,2026-11-05T20:06:30+01:00
0a:00:5e:00:53:fe,-,192.0.2.1,2026-11-05T20:06:30+01:00
02:00:5E:00:53:21
02:00:5E:00:53:21
00:00:5E:00:53:2
01:00:5E:00:53:06
"""


def vendor_mac() -> tuple[str, str] | None:
    """A MAC built from a prefix the local OUI table actually knows.

    Chosen at runtime so the test never hard-codes a vendor: a committed MAC
    would be a fact about somebody's hardware rather than about the lookup.
    """
    from vendor import get_vendor_db

    prefixes = getattr(get_vendor_db(), "_map", {})
    for prefix, name in sorted(prefixes.items()):
        if len(prefix) == 6 and name:
            octets = ":".join(prefix[i : i + 2] for i in range(0, 6, 2))
            return f"{octets}:00:53:99", name
    return None


class CatalogTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "catalog.db"
        self.catalog = Catalog(self.path)
        self.addCleanup(self.catalog.close)


class ImportTests(CatalogTestCase):
    def _write(self, text: str) -> Path:
        path = Path(self.tmp.name) / "sample.txt"
        path.write_text(text, "utf-8")
        return path

    def test_import_counts_and_statuses(self):
        report = self.catalog.import_file(self._write(SAMPLE))
        # 2 CSV rows + 02:00:5E (its second copy is a duplicate) + the short
        # value + the multicast value.
        self.assertEqual(report.added, 5)
        self.assertEqual(report.duplicates, 1)     # the repeated 02:00:5E row
        counts = self.catalog.counts()
        self.assertEqual(counts["devices"], 5)
        self.assertEqual(counts["broken"], 2)      # 11-digit + multicast

    def test_comment_lines_are_not_devices(self):
        path = self._write(
            "# a note from the person who keeps this list\n"
            "#mac,hostname,ip,seen\n"
            "00:00:5E:00:53:31,kepler,192.0.2.31,\n"
        )
        report = self.catalog.import_file(path)
        self.assertEqual(report.added, 1)
        self.assertEqual(report.broken, 0)
        self.assertEqual(self.catalog.counts()["devices"], 1)

    def test_hostname_observed_and_dash_ignored(self):
        self.catalog.import_file(self._write(SAMPLE))
        device = self.catalog.device_by_mac("00:00:5E:00:53:20")
        self.assertIsNotNone(device)
        self.assertEqual(device.hostname, "tahoe")
        other = self.catalog.device_by_mac("0A:00:5E:00:53:FE")
        self.assertIsNone(other.hostname)

    def test_vendor_is_resolved_on_insert(self):
        sample = vendor_mac()
        if sample is None:
            self.skipTest("no local OUI table to look a vendor up in")
        mac, expected = sample
        self.catalog.add_device(mac)
        device = self.catalog.device_by_mac(mac)
        self.assertEqual(device.vendor, expected)

    def test_vendor_is_backfilled_on_reimport(self):
        sample = vendor_mac()
        if sample is None:
            self.skipTest("no local OUI table to look a vendor up in")
        mac, _expected = sample
        path = self._write(f"mac\n{mac}\n")
        self.catalog.import_file(path)
        device = self.catalog.device_by_mac(mac)
        self.catalog.update_device(device.id, vendor=None)   # simulate an old row
        self.catalog.import_file(path)
        again = self.catalog.device_by_mac(mac)
        self.assertTrue(again.vendor)

    def test_reimport_is_idempotent_and_keeps_notes(self):
        path = self._write(SAMPLE)
        self.catalog.import_file(path)
        device = self.catalog.device_by_mac("00:00:5E:00:53:20")
        self.catalog.update_device(device.id, notes="workshop printer", protected=1)
        report = self.catalog.import_file(path)
        self.assertEqual(report.added, 0)
        again = self.catalog.device(device.id)
        self.assertEqual(again.notes, "workshop printer")
        self.assertTrue(again.protected)

    def test_repair_accept_moves_the_row_forward(self):
        self.catalog.add_device("CO:OO:5E:OO:53:05")
        device = self.catalog.devices()[0]
        self.assertEqual(device.status, macaddr.NEEDS_SUBSTITUTION)
        self.assertEqual(device.repair_to, "C0:00:5E:00:53:05")
        self.assertTrue(self.catalog.accept_repair(device.id))
        after = self.catalog.device(device.id)
        self.assertEqual(after.mac_norm, "C0:00:5E:00:53:05")
        self.assertEqual(after.status, macaddr.OK)

    def test_reject_repair_requires_manual_entry(self):
        self.catalog.add_device("CO:OO:5E:OO:53:05")
        device = self.catalog.devices()[0]
        self.catalog.reject_repair(device.id)
        after = self.catalog.device(device.id)
        self.assertEqual(after.status, macaddr.UNRECOVERABLE)
        self.assertIsNone(after.repair_to)
        ok, detail = self.catalog.set_manual_mac(device.id, "C0:00:5E:00:53:05")
        self.assertTrue(ok, detail)
        self.assertEqual(self.catalog.device(device.id).mac_norm, "C0:00:5E:00:53:05")

    def test_manual_mac_refuses_a_duplicate(self):
        first, _ = self.catalog.add_device("00:00:5E:00:53:01")
        second, _ = self.catalog.add_device("00:00:5E:00:53:2")
        ok, detail = self.catalog.set_manual_mac(second, "00:00:5E:00:53:01")
        self.assertFalse(ok)
        self.assertIn("device #", detail)

    def test_multicast_row_cannot_join_the_pool(self):
        device_id, _ = self.catalog.add_device("01:00:5E:00:53:06")
        added, problems = self.catalog.add_to_pool([device_id])
        self.assertEqual(added, 0)
        self.assertTrue(problems)


class PoolTests(CatalogTestCase):
    def _seed(self, count: int) -> list[int]:
        ids = []
        for index in range(count):
            mac = f"00:00:5E:00:53:{0x10 + index:02X}"
            device_id, _ = self.catalog.add_device(mac)
            ids.append(device_id)
        return ids

    def test_order_is_positional_and_resequenced(self):
        ids = self._seed(3)
        self.catalog.add_to_pool(ids)
        self.assertEqual(self.catalog.pool_macs(), [
            "00:00:5E:00:53:10", "00:00:5E:00:53:11", "00:00:5E:00:53:12",
        ])
        self.catalog.remove_from_pool([ids[0]])
        self.assertEqual(self.catalog.pool_macs(), [
            "00:00:5E:00:53:11", "00:00:5E:00:53:12",
        ])
        positions = [d.pool_mac_pos for d in self.catalog.pool_devices()]
        self.assertEqual(positions, [0, 1])

    def test_reorder(self):
        ids = self._seed(3)
        self.catalog.add_to_pool(ids)
        self.catalog.reorder_pool([ids[2], ids[0], ids[1]])
        self.assertEqual(self.catalog.pool_macs(), [
            "00:00:5E:00:53:12", "00:00:5E:00:53:10", "00:00:5E:00:53:11",
        ])

    def test_protected_rows_are_refused(self):
        ids = self._seed(1)
        self.catalog.protect(ids[0], True)
        added, problems = self.catalog.add_to_pool(ids)
        self.assertEqual(added, 0)
        self.assertIn("protected", problems[0])

    def test_delete_is_refused_while_pooled(self):
        ids = self._seed(1)
        self.catalog.add_to_pool(ids)
        self.assertFalse(self.catalog.delete_device(ids[0]))
        self.catalog.remove_from_pool(ids)
        self.assertTrue(self.catalog.delete_device(ids[0]))

    def test_hostname_pool_add_remove_order(self):
        for name in ("tahoe", "erie", "ontario"):
            ok, _ = self.catalog.add_hostname(name)
            self.assertTrue(ok)
        self.assertEqual(self.catalog.pool_hostnames(), ["tahoe", "erie", "ontario"])
        self.catalog.reorder_hostnames(["ontario", "tahoe", "erie"])
        self.assertEqual(self.catalog.pool_hostnames(), ["ontario", "tahoe", "erie"])
        self.catalog.remove_hostname("tahoe")
        self.assertEqual(self.catalog.pool_hostnames(), ["ontario", "erie"])
        # Removing from the pool keeps the name known.
        self.assertIn("tahoe", self.catalog.hostname_names())

    def test_duplicate_hostname_is_reported(self):
        self.catalog.add_hostname("tahoe")
        ok, message = self.catalog.add_hostname("tahoe")
        self.assertFalse(ok)
        self.assertIn("already", message)

    def test_invalid_hostname_is_refused(self):
        ok, message = self.catalog.add_hostname("bad_name")
        self.assertFalse(ok)
        self.assertIn("invalid", message)


class LiveIdentityTests(CatalogTestCase):
    """The row that is *this machine* — flagged, kept usable, never deletable."""

    def test_marks_the_address_and_puts_it_in_the_pool(self):
        device_id, notes = self.catalog.mark_live_identity(
            "02:00:5E:10:00:01", "tahoe"
        )
        device = self.catalog.device(device_id)
        self.assertTrue(device.is_own)
        self.assertTrue(device.pool_mac)
        self.assertFalse(device.protected, "own address must stay a rotation target")
        self.assertEqual(device.source, "live")
        self.assertEqual(device.hostname, "tahoe")
        self.assertEqual(self.catalog.counts()["own"], 1)
        self.assertTrue(any("own address" in note for note in notes))

    def test_the_flag_moves_and_only_one_row_carries_it(self):
        first, _ = self.catalog.mark_live_identity("02:00:5E:10:00:01")
        second, _ = self.catalog.mark_live_identity("02:00:5E:10:00:02")
        self.assertEqual(self.catalog.counts()["own"], 1)
        self.assertFalse(self.catalog.device(first).is_own)
        self.assertTrue(self.catalog.device(second).is_own)

    def test_repeat_marking_is_a_no_op(self):
        first, _ = self.catalog.mark_live_identity("02:00:5E:10:00:01")
        again, notes = self.catalog.mark_live_identity("02:00:5E:10:00:01")
        self.assertEqual(first, again)
        self.assertEqual(notes, [])
        self.assertEqual(self.catalog.counts()["pool_mac"], 1)

    def test_own_row_cannot_be_deleted(self):
        device_id, _ = self.catalog.mark_live_identity("02:00:5E:10:00:01")
        self.catalog.remove_from_pool([device_id])
        self.assertFalse(self.catalog.delete_device(device_id))
        self.assertIsNotNone(self.catalog.device(device_id))

    def test_no_address_marks_nothing(self):
        self.assertEqual(self.catalog.mark_live_identity(None), (None, []))
        self.assertEqual(
            self.catalog.mark_live_identity("00:00:00:00:00:00"), (None, [])
        )
        self.assertEqual(self.catalog.counts()["own"], 0)

    def test_a_catalog_written_before_the_column_gains_it(self):
        path = Path(self.tmp.name) / "old.db"
        legacy = sqlite3.connect(path)
        legacy.execute(
            "CREATE TABLE devices ("
            "id INTEGER PRIMARY KEY, mac_norm TEXT, mac_raw TEXT, status TEXT,"
            " repair_to TEXT, repair_confidence TEXT, vendor TEXT, hostname TEXT,"
            " hostname_source TEXT, ip TEXT, seen_at TEXT,"
            " protected INTEGER NOT NULL DEFAULT 0,"
            " notes TEXT NOT NULL DEFAULT '', source TEXT,"
            " pool_mac INTEGER NOT NULL DEFAULT 0, pool_mac_pos INTEGER,"
            " created_at TEXT, updated_at TEXT)"
        )
        legacy.commit()
        legacy.close()

        upgraded = Catalog(path)
        self.addCleanup(upgraded.close)
        columns = {
            row["name"]
            for row in upgraded.conn.execute("PRAGMA table_info(devices)")
        }
        self.assertIn("is_own", columns)
        device_id, _ = upgraded.mark_live_identity("02:00:5E:10:00:01")
        self.assertTrue(upgraded.device(device_id).is_own)


class RealInventoryTests(CatalogTestCase):
    """Invariants of the importer against a real file you supply yourself.

    The corpus is deliberately not in the repository, and no expected count is
    hard-coded here: an import must collapse duplicates, never invent or lose a
    device, and be idempotent.  Those hold whatever your LAN happens to contain.
    """

    def setUp(self):
        super().setUp()
        if REAL_SAMPLE is None:
            self.skipTest("set LAN_IDENTITY_SAMPLE to a real mac,hostname,ip CSV")
        if not REAL_SAMPLE.exists():
            self.skipTest(f"{REAL_SAMPLE} is not present on this host")

    def test_import_collapses_duplicates_and_is_idempotent(self):
        report = self.catalog.import_file(REAL_SAMPLE)
        rows = sum(
            1
            for line in REAL_SAMPLE.read_text("utf-8", errors="replace").splitlines()
            if line.strip() and not line.lower().startswith("mac")
        )
        self.assertGreater(report.added, 0)
        self.assertLessEqual(report.added, rows, "cannot add more rows than it read")
        first = self.catalog.counts()
        self.assertEqual(first["devices"], report.added)
        again = self.catalog.import_file(REAL_SAMPLE)
        self.assertEqual(again.added, 0)
        self.assertEqual(self.catalog.counts(), first)

    def test_every_stored_mac_is_either_canonical_or_explained(self):
        self.catalog.import_file(REAL_SAMPLE)
        for device in self.catalog.devices():
            with self.subTest(raw=device.mac_raw):
                if device.mac_norm:
                    self.assertEqual(device.mac_norm, device.mac_norm.upper())
                else:
                    self.assertIn(
                        device.status, (macaddr.UNRECOVERABLE, macaddr.MULTICAST)
                    )


class MergeOrderTests(CatalogTestCase):
    """A re-import follows the instant, not the spelling of either field.

    ``_merge_fields`` compared ``ip`` and ``seen_at`` as strings with one ``>``.
    Two consequences, both measured before this packet existed: a newer
    observation whose address sorts lower was dropped, and an offset change
    reversed the order because the string compares wall-clock digits rather than
    instants.

    The rule after the fix is per field, which is what "per field" means here:
    ``ip`` and ``seen_at`` describe one observation and move together, ordered by
    the instant ``seen_at`` names; ``hostname`` is filled when empty and never
    replaced.  An instant that cannot be parsed decides no order at all — it is
    kept verbatim and is never treated as newer.
    """

    MAC = "00:00:5E:00:53:30"

    def _import(self, rows: str):
        path = Path(self.tmp.name) / "merge.txt"
        path.write_text("mac,hostname,ip,seen_at\n" + rows, "utf-8")
        return self.catalog.import_file(path)

    def _row(self):
        row = self.catalog.device_by_mac(self.MAC)
        self.assertIsNotNone(row, "the fixture row did not survive the import")
        return row

    def test_a_newer_row_at_a_lower_address_wins(self):
        self._import(f"{self.MAC},-,192.0.2.90,2026-11-05T20:00:00+01:00\n")
        self._import(f"{self.MAC},-,192.0.2.20,2026-11-05T21:00:00+01:00\n")
        row = self._row()
        self.assertEqual(row.ip, "192.0.2.20")
        self.assertEqual(row.seen_at, "2026-11-05T21:00:00+01:00")

    def test_an_older_row_changes_nothing(self):
        self._import(f"{self.MAC},-,192.0.2.90,2026-11-05T21:00:00+01:00\n")
        self._import(f"{self.MAC},-,192.0.2.99,2026-11-05T20:00:00+01:00\n")
        row = self._row()
        self.assertEqual(row.ip, "192.0.2.90")
        self.assertEqual(row.seen_at, "2026-11-05T21:00:00+01:00")

    def test_an_offset_change_is_ordered_by_the_instant(self):
        # 03:15+02:00 is 01:15Z; 02:45+01:00 is 01:45Z.  The string says the second
        # is older, the instant says it is newer.
        self._import(f"{self.MAC},-,192.0.2.90,2026-03-29T03:15:00+02:00\n")
        self._import(f"{self.MAC},-,192.0.2.20,2026-03-29T02:45:00+01:00\n")
        row = self._row()
        self.assertEqual(row.seen_at, "2026-03-29T02:45:00+01:00")
        self.assertEqual(row.ip, "192.0.2.20")

    def test_an_equal_instant_changes_nothing(self):
        self._import(f"{self.MAC},-,192.0.2.90,2026-11-05T21:00:00+01:00\n")
        self._import(f"{self.MAC},-,192.0.2.20,2026-11-05T20:00:00Z\n")
        row = self._row()
        self.assertEqual(row.ip, "192.0.2.90")
        self.assertEqual(row.seen_at, "2026-11-05T21:00:00+01:00")

    def test_an_unparseable_incoming_value_is_not_newer(self):
        self._import(f"{self.MAC},-,192.0.2.90,2026-11-05T21:00:00+01:00\n")
        self._import(f"{self.MAC},-,192.0.2.20,yesterday\n")
        row = self._row()
        self.assertEqual(row.ip, "192.0.2.90")
        self.assertEqual(row.seen_at, "2026-11-05T21:00:00+01:00")

    def test_a_timestamp_without_an_offset_does_not_order(self):
        # A naive value is well formed but names no instant: 21:00 where?  It is
        # kept verbatim and never used to decide which observation is newer.
        self._import(f"{self.MAC},-,192.0.2.90,2026-11-05T21:00:00\n")
        self._import(f"{self.MAC},-,192.0.2.20,2026-11-05T22:00:00\n")
        row = self._row()
        self.assertEqual(row.ip, "192.0.2.90")
        self.assertEqual(row.seen_at, "2026-11-05T21:00:00")

    def test_a_stored_value_that_cannot_be_ordered_is_kept_verbatim(self):
        self._import(f"{self.MAC},-,192.0.2.90,not-a-time\n")
        self._import(f"{self.MAC},-,192.0.2.20,2026-11-05T21:00:00+01:00\n")
        row = self._row()
        self.assertEqual(row.seen_at, "not-a-time")
        self.assertEqual(row.ip, "192.0.2.90")

    def test_a_row_without_an_observation_takes_one(self):
        self._import(f"{self.MAC},-,, \n")
        self._import(f"{self.MAC},-,192.0.2.20,2026-11-05T21:00:00+01:00\n")
        row = self._row()
        self.assertEqual(row.ip, "192.0.2.20")
        self.assertEqual(row.seen_at, "2026-11-05T21:00:00+01:00")

    def test_a_hostname_is_filled_but_never_replaced(self):
        self._import(f"{self.MAC},tahoe,192.0.2.90,2026-11-05T20:00:00+01:00\n")
        self._import(f"{self.MAC},donner,192.0.2.20,2026-11-05T21:00:00+01:00\n")
        row = self._row()
        self.assertEqual(row.hostname, "tahoe")
        self.assertEqual(row.ip, "192.0.2.20")


if __name__ == "__main__":
    unittest.main(verbosity=2)
