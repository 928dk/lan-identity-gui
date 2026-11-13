"""Tests for generated list files: rendering, parsing, drift and backups."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import listfiles  # noqa: E402
from catalog import Catalog  # noqa: E402
from config import Settings  # noqa: E402


class ListFileTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.catalog = Catalog(root / "catalog.db")
        self.addCleanup(self.catalog.close)
        self.settings = Settings(
            mac_list_path=str(root / "mac-list.txt"),
            hostname_list_path=str(root / "hostnamelist.txt"),
        )
        self.backups = root / "exports"

    def _seed(self):
        for index in range(2):
            self.catalog.add_device(f"00:00:5E:00:53:{0x01 + index:02X}")
        for device in self.catalog.devices():
            self.catalog.add_to_pool([device.id])
        for name in ("tahoe", "erie"):
            self.catalog.add_hostname(name)


class RenderTests(ListFileTestCase):
    def test_generated_header_states_the_prefix_requirement(self):
        self._seed()
        text = listfiles.render_hostname_list(self.catalog.pool_hostnames(), self.settings)
        self.assertIn("-p ''", text)
        self.assertIn("GENERATED", text)
        self.assertIn("tahoe", text)

    def test_round_trip(self):
        self._seed()
        text = listfiles.render_mac_list(self.catalog.pool_macs(), self.settings)
        parsed = listfiles.parse_mac_list(text)
        self.assertEqual(parsed, self.catalog.pool_macs())

    def test_comments_and_blanks_are_ignored(self):
        text = "# comment\n\n00:00:5E:00:53:01   # trailing\n"
        self.assertEqual(listfiles.parse_mac_list(text), ["00:00:5E:00:53:01"])


class WriteTests(ListFileTestCase):
    def test_write_then_no_drift(self):
        self._seed()
        mac_result, host_result = listfiles.export_both(
            self.catalog, self.settings, self.backups
        )
        self.assertTrue(mac_result.wrote)
        self.assertTrue(host_result.wrote)
        self.assertFalse(listfiles.has_drift(Path(self.settings.mac_list_path), self.catalog))

    def test_hand_edit_is_detected_and_not_overwritten(self):
        self._seed()
        listfiles.export_both(self.catalog, self.settings, self.backups)
        path = Path(self.settings.mac_list_path)
        path.write_text(path.read_text("utf-8") + "02:00:5E:00:53:40\n", "utf-8")
        self.assertTrue(listfiles.has_drift(path, self.catalog))

        result = listfiles.write_file(
            path, listfiles.render_mac_list(self.catalog.pool_macs(), self.settings),
            self.catalog, self.backups,
        )
        self.assertFalse(result.wrote)
        self.assertTrue(result.drift)
        self.assertIn("02:00:5E:00:53:40", path.read_text("utf-8"))

    def test_forced_overwrite_keeps_a_backup(self):
        self._seed()
        listfiles.export_both(self.catalog, self.settings, self.backups)
        path = Path(self.settings.mac_list_path)
        path.write_text("02:00:5E:00:53:40\n", "utf-8")
        result = listfiles.write_file(
            path, listfiles.render_mac_list(self.catalog.pool_macs(), self.settings),
            self.catalog, self.backups, force=True,
        )
        self.assertTrue(result.wrote)
        self.assertIsNotNone(result.backup)
        self.assertIn("02:00:5E:00:53:40", result.backup.read_text("utf-8"))

    def test_backup_rotation_keeps_five(self):
        self._seed()
        path = Path(self.settings.mac_list_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        for index in range(8):
            path.write_text(f"# version {index}\n", "utf-8")
            listfiles.write_file(
                path, f"# generated {index}\n", self.catalog, self.backups, force=True
            )
        kept = listfiles.backups(path, self.backups)
        self.assertEqual(len(kept), listfiles.BACKUP_KEEP, [p.name for p in kept])

    def test_unchanged_content_is_not_rewritten(self):
        self._seed()
        listfiles.export_both(self.catalog, self.settings, self.backups)
        path = Path(self.settings.mac_list_path)
        rendered = listfiles.render_mac_list(self.catalog.pool_macs(), self.settings)
        result = listfiles.write_file(path, rendered, self.catalog, self.backups)
        self.assertFalse(result.wrote)
        self.assertEqual(result.message, "already up to date")

    def test_two_renders_are_byte_identical(self):
        """A generation timestamp made every render look like a change."""
        self._seed()
        macs = self.catalog.pool_macs()
        names = self.catalog.pool_hostnames()
        self.assertEqual(
            listfiles.render_mac_list(macs, self.settings),
            listfiles.render_mac_list(macs, self.settings),
        )
        self.assertEqual(
            listfiles.render_hostname_list(names, self.settings),
            listfiles.render_hostname_list(names, self.settings),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
