"""The watcher has one hard case and one soft one.

The hard case is atomic replacement: every write this app makes is a ``.tmp``
file plus ``os.replace``, and ``QFileSystemWatcher`` drops the path when that
happens.  A watcher that is not re-armed after the first replacement goes silent,
and silence looks exactly like "nothing changed" — so the second replacement is
the assertion that matters.

The soft case is the burst: a save can arrive as several events and must become
one import, not one per event.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

try:
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except Exception:  # noqa: BLE001 - report as a skip, never a hard failure
    HAVE_QT = False

import config  # noqa: E402
import ops  # noqa: E402
from catalog import Catalog  # noqa: E402
from watcher import PathWatcher  # noqa: E402


def wait_until(predicate, timeout_ms: int = 3000) -> bool:
    """Pump the Qt event loop until ``predicate`` is true or time runs out."""
    app = QApplication.instance()
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if app is not None:
            app.processEvents()
        if predicate():
            return True
        QTest.qWait(10)
    return predicate()


@unittest.skipUnless(HAVE_QT, "PySide6 is not installed")
class PathWatcherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "hosts.csv"
        self.events: list[list[str]] = []

    def _watcher(self, debounce_ms: int = 30) -> PathWatcher:
        watcher = PathWatcher([self.path], debounce_ms=debounce_ms)
        self.addCleanup(watcher.stop)
        watcher.changed.connect(self.events.append)
        return watcher

    def _replace(self, content: str) -> None:
        tmp = self.root / "hosts.csv.tmp"
        tmp.write_text(content, "utf-8")
        os.replace(tmp, self.path)

    # -- tests --------------------------------------------------------- #

    def test_a_new_file_is_reported(self):
        self._watcher()
        self.path.write_text("mac,hostname,ip,seen_at\n", "utf-8")
        self.assertTrue(wait_until(lambda: len(self.events) >= 1))
        self.assertEqual(self.events[0], [str(self.path)])

    def test_an_in_place_write_is_reported(self):
        self.path.write_text("one", "utf-8")
        self._watcher()
        self.path.write_text("two", "utf-8")
        self.assertTrue(wait_until(lambda: len(self.events) >= 1))

    def test_an_atomic_replace_is_reported(self):
        self.path.write_text("prime", "utf-8")
        self._watcher()
        self._replace("replaced")
        self.assertTrue(wait_until(lambda: len(self.events) >= 1))

    def test_the_watcher_re_arms_after_a_replace(self):
        """The failure mode this class exists for: silence after the first write."""
        self.path.write_text("prime", "utf-8")
        self._watcher()
        self._replace("second")
        self.assertTrue(wait_until(lambda: len(self.events) >= 1))
        self._replace("third-longer")
        self.assertTrue(
            wait_until(lambda: len(self.events) >= 2),
            "the watcher went silent after an atomic replacement",
        )

    def test_a_burst_of_writes_is_one_event(self):
        self.path.write_text("prime", "utf-8")
        self._watcher(debounce_ms=40)
        for index in range(4):
            self.path.write_text(f"burst-{index}", "utf-8")
        self.assertTrue(wait_until(lambda: len(self.events) >= 1))
        QTest.qWait(250)
        self.app.processEvents()
        self.assertEqual(len(self.events), 1, self.events)

    def test_a_deletion_is_reported(self):
        self.path.write_text("prime", "utf-8")
        self._watcher()
        self.path.unlink()
        self.assertTrue(wait_until(lambda: len(self.events) >= 1))

    def test_stop_ends_the_reports(self):
        self.path.write_text("prime", "utf-8")
        watcher = self._watcher()
        watcher.stop()
        self.path.write_text("after stop", "utf-8")
        QTest.qWait(200)
        self.app.processEvents()
        self.assertEqual(self.events, [])

    def test_an_untracked_neighbour_is_ignored(self):
        self.path.write_text("prime", "utf-8")
        self._watcher()
        (self.root / "other.csv").write_text("noise", "utf-8")
        QTest.qWait(200)
        self.app.processEvents()
        self.assertEqual(self.events, [])


class InventoryGateTests(unittest.TestCase):
    """The window imports only when the CSV changed since the last import."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.catalog = Catalog(self.root / "catalog.db")
        self.addCleanup(self.catalog.close)
        self.inventory = self.root / "hosts.csv"
        self.settings = config.Settings(
            iface="lo",
            mac_list_path=str(self.root / "mac-list.txt"),
            hostname_list_path=str(self.root / "hostnamelist.txt"),
            catalog_path=str(self.catalog.path),
            inventory_path=str(self.inventory),
            dry_run=True,
        )

    def _write(self, content: str) -> None:
        self.inventory.write_text(content, "utf-8")

    def test_no_inventory_is_not_an_error(self):
        self.assertFalse(ops.inventory_is_newer(self.settings, self.catalog))

    def test_a_first_appearance_is_newer(self):
        self._write("mac,hostname,ip,seen_at\n")
        self.assertTrue(ops.inventory_is_newer(self.settings, self.catalog))

    def test_import_clears_the_gate(self):
        self._write(
            "mac,hostname,ip,seen_at\n"
            "00:00:5E:00:53:71,kepler,192.0.2.71,\n"
        )
        report = ops.import_inventory(self.settings, self.catalog)
        self.assertEqual(report.added, 1)
        self.assertFalse(ops.inventory_is_newer(self.settings, self.catalog))

    def test_a_new_mtime_reopens_the_gate(self):
        self._write("mac,hostname,ip,seen_at\n")
        ops.import_inventory(self.settings, self.catalog)
        stat = self.inventory.stat()
        os.utime(self.inventory, (stat.st_atime + 5, stat.st_mtime + 5))
        self.assertTrue(ops.inventory_is_newer(self.settings, self.catalog))

    def test_a_second_import_adds_nothing(self):
        self._write(
            "mac,hostname,ip,seen_at\n"
            "00:00:5E:00:53:72,tarn,192.0.2.72,\n"
        )
        first = ops.import_inventory(self.settings, self.catalog)
        second = ops.import_inventory(self.settings, self.catalog)
        self.assertEqual(first.added, 1)
        self.assertEqual(second.added, 0)
        self.assertEqual(second.updated, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
