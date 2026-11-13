"""Drift has to be noticed, and the app's own write must stay quiet.

The distinction is not "did the file change" — every generate changes it.  It is
"is the file still what this app last wrote", which the stored digest answers.
So the tests below drive the real window: one writes through the app and expects
silence, the others edit the file behind its back and expect the banner.
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

from catalog import Catalog  # noqa: E402
from config import Settings  # noqa: E402

MAC_A = "00:00:5E:00:53:11"
MAC_B = "00:00:5E:00:53:12"
MAC_C = "00:00:5E:00:53:13"
MAC_D = "00:00:5E:00:53:14"


def pump(predicate, timeout_ms: int = 5000) -> bool:
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
class DriftWatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

        import ui.main_window as main_window

        original = main_window.LIST_DEBOUNCE_MS
        main_window.LIST_DEBOUNCE_MS = 30
        self.addCleanup(setattr, main_window, "LIST_DEBOUNCE_MS", original)

        self.catalog = Catalog(self.root / "catalog.db")
        self.addCleanup(self.catalog.close)
        self.settings = Settings(
            iface="lo",
            mac_list_path=str(self.root / "mac-list.txt"),
            hostname_list_path=str(self.root / "hostnamelist.txt"),
            catalog_path=str(self.root / "catalog.db"),
            inventory_path=str(self.root / "hosts.csv"),
            dry_run=True,
        )
        self.settings.first_export_confirmed = True

        self.catalog.add_device(MAC_A)
        self.catalog.add_device(MAC_B)
        self.catalog.add_hostname("tahoe")
        device = self.catalog.device_by_mac(MAC_A)
        self.catalog.add_to_pool([device.id])

        self.window = main_window.MainWindow(self.catalog, self.settings)
        self.addCleanup(self.window.close)
        self.window.regenerate(interactive=False, force=True)
        self.mac_path = Path(self.settings.mac_list_path)

    # -- helpers -------------------------------------------------------- #

    def _edit(self, extra: str) -> None:
        self.mac_path.write_text(
            self.mac_path.read_text("utf-8") + extra + "\n", "utf-8"
        )

    def _wait_for_banner(self) -> bool:
        return pump(lambda: self.window.drift_banner.is_reporting())

    # -- tests ---------------------------------------------------------- #

    def test_the_apps_own_write_is_not_drift(self):
        # setUp already regenerated; give the watcher time to notice and settle.
        QTest.qWait(300)
        self.app.processEvents()
        self.assertFalse(self.window.drift_banner.is_reporting())

    def test_an_external_edit_raises_the_banner_with_a_diff(self):
        self._edit(MAC_C)
        self.assertTrue(self._wait_for_banner())
        self.assertIn("changed on disk", self.window.drift_banner.message.text())
        self.assertIn(MAC_C, self.window.drift_banner.detail.toPlainText())

    def test_adopting_keeps_the_edit(self):
        self._edit(MAC_C)
        self.assertTrue(self._wait_for_banner())
        self.window.drift_banner.adopt_button.click()
        self.assertTrue(pump(lambda: not self.window.drift_banner.is_reporting()))
        self.assertIsNotNone(self.catalog.device_by_mac(MAC_C))
        self.assertIn(MAC_C, self.mac_path.read_text("utf-8"))

    def test_overwriting_discards_the_edit(self):
        self._edit(MAC_C)
        self.assertTrue(self._wait_for_banner())
        self.window.drift_banner.overwrite_button.click()
        self.assertTrue(pump(lambda: not self.window.drift_banner.is_reporting()))
        self.assertNotIn(MAC_C, self.mac_path.read_text("utf-8"))

    def test_ignoring_silences_the_banner_until_the_next_edit(self):
        self._edit(MAC_C)
        self.assertTrue(self._wait_for_banner())
        self.window.drift_banner.ignore_button.click()
        self.assertFalse(self.window.drift_banner.is_reporting())

        # It stays quiet while nothing else happens...
        QTest.qWait(300)
        self.app.processEvents()
        self.assertFalse(self.window.drift_banner.is_reporting())

        # ...and a new edit re-arms it.
        self._edit(MAC_D)
        self.assertTrue(self._wait_for_banner())
        self.assertIn(MAC_D, self.window.drift_banner.detail.toPlainText())

    def test_a_deleted_generated_file_is_not_a_drift_warning(self):
        self.mac_path.unlink()
        QTest.qWait(300)
        self.app.processEvents()
        self.assertFalse(self.window.drift_banner.is_reporting())


if __name__ == "__main__":
    unittest.main(verbosity=2)
