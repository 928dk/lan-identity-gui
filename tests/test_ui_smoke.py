"""Offscreen smoke test: build the real window and drive its main paths.

This does not prove the app looks right — only a human can say that — but it does
prove that every panel constructs, the model renders, selection works, and a
catalog change regenerates the list files.  Run it with:

    QT_QPA_PLATFORM=offscreen python3 -m unittest tests.test_ui_smoke -v
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

import macaddr  # noqa: E402
from catalog import Catalog  # noqa: E402
from config import Settings  # noqa: E402


@unittest.skipUnless(HAVE_QT, "PySide6 is not installed")
class WindowSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.catalog = Catalog(root / "catalog.db")
        self.settings = Settings(
            iface="lo",
            mac_list_path=str(root / "mac-list.txt"),
            hostname_list_path=str(root / "hostnamelist.txt"),
            catalog_path=str(root / "catalog.db"),
            inventory_path=str(root / "hosts.csv"),
            dry_run=True,
        )
        self.settings.first_export_confirmed = True

        # A mixed fixture: one good, one repairable, one broken, one multicast.
        for raw in ("00:00:5E:00:53:01", "CO:OO:5E:OO:53:05",
                    "00:00:5E:00:53:2", "01:00:5E:00:53:06"):
            self.catalog.add_device(raw)
        self.catalog.add_hostname("tahoe")
        self.catalog.add_hostname("erie")

    def _window(self):
        from ui.main_window import MainWindow

        window = MainWindow(self.catalog, self.settings)
        self.addCleanup(window.close)
        self.addCleanup(self.catalog.close)
        return window

    def test_window_constructs_with_all_panels(self):
        window = self._window()
        self.assertEqual(window.tabs.count(), 4)
        self.assertEqual(window.model.rowCount(), 4)
        self.assertEqual(window.proxy.rowCount(), 4)
        text = window.counts_label.text()
        self.assertIn("4 devices", text)
        self.assertIn("2 names", text)

    def test_selection_populates_the_detail_panel(self):
        window = self._window()
        window.table.selectRow(0)
        self.app.processEvents()
        self.assertNotEqual(window.detail_panel.title.text(), "No device selected")

    def test_scope_filter_hides_broken_rows(self):
        window = self._window()
        window.scope_combo.setCurrentIndex(1)  # assignable + absent
        self.app.processEvents()
        self.assertEqual(window.proxy.rowCount(), 1)

        window.scope_combo.setCurrentIndex(2)  # needs attention
        self.app.processEvents()
        self.assertEqual(window.proxy.rowCount(), 3)

    def test_text_filter(self):
        window = self._window()
        window.filter_edit.setText("tahoe")
        self.app.processEvents()
        self.assertEqual(window.proxy.rowCount(), 0)
        window.filter_edit.setText("00:00:5E:00:53:01")
        self.app.processEvents()
        self.assertEqual(window.proxy.rowCount(), 1)

    def test_repairable_row_shows_a_repair_and_accepting_it_works(self):
        window = self._window()
        device = self.catalog.devices()
        target = next(d for d in device if d.status == macaddr.NEEDS_SUBSTITUTION)
        window.detail_panel.show_device(target)
        self.assertTrue(window.detail_panel.repair_box.isVisible()
                        or not window.detail_panel.isVisible())
        self.assertIn("C0:00:5E:00:53:05", window.detail_panel.repair_text.text())
        window.detail_panel._accept_repair()
        self.assertEqual(self.catalog.device(target.id).mac_norm, "C0:00:5E:00:53:05")

    def test_pool_panel_reflects_membership(self):
        window = self._window()
        device = next(d for d in self.catalog.devices() if d.ready)
        self.catalog.add_to_pool([device.id])
        window.pool_panel.refresh()
        self.assertEqual(window.pool_panel.mac_list.count(), 1)
        self.assertEqual(window.pool_panel.hostname_list.count(), 2)
        self.assertIn("mac-rotator.sh with -p ''", window.pool_panel.host_hint.text())

    def test_rotation_panel_engine_matches_the_pool(self):
        window = self._window()
        device = next(d for d in self.catalog.devices() if d.ready)
        self.catalog.add_to_pool([device.id])
        window.rotation_panel.reload_engine()
        self.assertEqual(len(window.rotation_panel.engine.macs), 1)
        self.assertEqual(len(window.rotation_panel.engine.hostnames), 2)
        self.assertTrue(window.rotation_panel.start_button.isEnabled())
        self.assertIn("Next:", window.rotation_panel.next_label.text())

    def test_regenerate_writes_both_files(self):
        window = self._window()
        device = next(d for d in self.catalog.devices() if d.ready)
        self.catalog.add_to_pool([device.id])
        window.regenerate(interactive=False, force=True)
        mac_path = Path(self.settings.mac_list_path)
        host_path = Path(self.settings.hostname_list_path)
        self.assertTrue(mac_path.exists())
        self.assertTrue(host_path.exists())
        self.assertIn("GENERATED", mac_path.read_text("utf-8"))
        self.assertIn("tahoe", host_path.read_text("utf-8"))

    def test_new_inventory_rows_arrive_without_a_button(self):
        """The collector appends; the window follows, with no button and no restart."""
        import ui.main_window as main_window

        original = main_window.INVENTORY_DEBOUNCE_MS
        main_window.INVENTORY_DEBOUNCE_MS = 30
        self.addCleanup(setattr, main_window, "INVENTORY_DEBOUNCE_MS", original)

        window = self._window()
        before = window.model.rowCount()
        inventory = Path(self.settings.inventory_path)
        inventory.write_text(
            "mac,hostname,ip,seen_at\n"
            "00:00:5E:00:53:99,newcomer,192.0.2.99,\n",
            "utf-8",
        )

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and window.model.rowCount() == before:
            self.app.processEvents()
            QTest.qWait(10)
        self.assertEqual(window.model.rowCount(), before + 1)
        self.assertIsNotNone(self.catalog.device_by_mac("00:00:5E:00:53:99"))

    def test_a_verdict_is_recorded_after_an_apply(self):
        """The window records what verification read and offers nothing else."""
        import verify

        window = self._window()
        device = next(d for d in self.catalog.devices() if d.ready)
        window.verify_fn = lambda mac, host, iface, dhcp_wait=60: verify.Verdict(
            verify.STATE_DUPLICATE_MAC, mac, host,
            note=f"{mac} answered for a second address",
        )
        window._start_verification(
            type("R", (), {"mac": device.mac_norm, "hostname": "tahoe"})()
        )

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self.app.processEvents()
            if self.catalog.device(device.id).verify_state:
                break
            QTest.qWait(10)

        stored = self.catalog.device(device.id)
        self.assertEqual(stored.verify_state, verify.STATE_DUPLICATE_MAC)
        self.assertIn("second address", stored.verify_note)
        self.assertIn("second address", window.rotation_panel.verdict_label.text())

    def test_a_dry_run_is_not_verified(self):
        import verify

        window = self._window()
        calls = []
        window.verify_fn = lambda *a, **k: calls.append(a)
        window._start_verification(
            type("R", (), {"mac": "00:00:5E:00:53:01", "hostname": "", "ok": True,
                           "dry_run": True})()
        )
        self.app.processEvents()
        self.assertEqual(calls, [])

    def test_drift_is_detected_after_a_hand_edit(self):
        window = self._window()
        device = next(d for d in self.catalog.devices() if d.ready)
        self.catalog.add_to_pool([device.id])
        window.regenerate(interactive=False, force=True)
        mac_path = Path(self.settings.mac_list_path)
        mac_path.write_text(mac_path.read_text("utf-8") + "02:00:5E:00:53:40\n", "utf-8")
        import listfiles

        self.assertTrue(listfiles.has_drift(mac_path, self.catalog))


if __name__ == "__main__":
    unittest.main(verbosity=2)
