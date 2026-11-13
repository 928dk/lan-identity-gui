"""The headless verbs must work with no display, no session and no network.

Each test runs ``src/main.py`` as a subprocess with its own ``LAN_IDENTITY_HOME``
so the real config, catalog and rotator lists cannot decide the result.  That is
also the point of the exercise: the CLI's whole reason to exist is that a timer
or an ssh session can call it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MAIN = REPO / "src" / "main.py"

#: Documentation MACs only (RFC 7042's 00-00-5E-00-53-xx block).
MAC_A = "00:00:5E:00:53:01"
MAC_B = "00:00:5E:00:53:02"
MAC_C = "00:00:5E:00:53:03"


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.config_dir = self.home / ".config" / "lan-identity-gui"
        self.config_dir.mkdir(parents=True)
        self.mac_list = self.home / "mac-list.txt"
        self.host_list = self.home / "hostnamelist.txt"
        self.inventory = self.home / "hosts.csv"
        self.catalog_path = self.home / "state" / "catalog.db"
        self.catalog_path.parent.mkdir(parents=True)
        settings = {
            # A name that cannot exist: own_mac() then returns None on every host.
            "iface": "no-such-iface0",
            "mac_list_path": str(self.mac_list),
            "hostname_list_path": str(self.host_list),
            "inventory_path": str(self.inventory),
            "catalog_path": str(self.catalog_path),
            "dry_run": True,
        }
        (self.config_dir / "config.json").write_text(
            json.dumps(settings), "utf-8"
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- helpers ------------------------------------------------------- #

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["LAN_IDENTITY_HOME"] = str(self.home)
        env["PYTHONPATH"] = str(REPO / "src")
        env.pop("DISPLAY", None)
        env.pop("WAYLAND_DISPLAY", None)
        env["QT_QPA_PLATFORM"] = "offscreen"
        return subprocess.run(
            [sys.executable, str(MAIN), *args],
            capture_output=True, text=True, check=False, env=env, cwd=REPO,
        )

    def _seed_inventory(self) -> None:
        self.inventory.write_text(
            "# mac,hostname,ip,seen\n"
            f"{MAC_A},cobalt,192.0.2.11,2026-01-01T10:00:00\n"
            f"{MAC_B},tarn,192.0.2.12,2026-01-01T10:01:00\n",
            "utf-8",
        )

    def _seed_pool(self) -> None:
        """Put one imported device in the MAC pool, via the library."""
        sys.path.insert(0, str(REPO / "src"))
        import catalog as catalog_module  # noqa: PLC0415

        cat = catalog_module.Catalog(str(self.catalog_path))
        try:
            device = cat.device_by_mac(MAC_A)
            self.assertIsNotNone(device)
            cat.add_to_pool([device.id])
        finally:
            cat.close()

    # -- tests --------------------------------------------------------- #

    def test_check_runs_without_a_display(self):
        result = self._run("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("lan-identity-gui environment", result.stdout)
        self.assertIn("no-such-iface0", result.stdout)

    def test_status_prints_json(self):
        result = self._run("--status", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["interface"], "no-such-iface0")
        self.assertEqual(payload["catalog"]["devices"], 0)
        self.assertFalse(payload["drift"]["mac_list"]["exists"])

    def test_a_headless_run_imports_no_qt(self):
        code = (
            "import sys; sys.path.insert(0, 'src'); import main; "
            "rc = main.main(['--status']); "
            "print('QT' if 'PySide6' in sys.modules else 'NOQT', rc)"
        )
        env = dict(os.environ)
        env["LAN_IDENTITY_HOME"] = str(self.home)
        env.pop("DISPLAY", None)
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True,
            check=False, env=env, cwd=REPO,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.strip().endswith("NOQT 0"), result.stdout)

    def test_import_reads_the_inventory(self):
        self._seed_inventory()
        result = self._run("--import")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("2 new", result.stdout)
        again = self._run("--import")
        self.assertIn("0 new", again.stdout)
        self.assertIn("2 updated", again.stdout)

    def test_import_missing_file_exits_three(self):
        result = self._run("--import", str(self.home / "absent.csv"))
        self.assertEqual(result.returncode, 3)
        self.assertIn("does not exist", result.stderr)

    def test_generate_writes_both_files_and_is_deterministic(self):
        self._seed_inventory()
        self._run("--import")
        self._seed_pool()

        first = self._run("--generate")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("mac-list.txt: written", first.stdout)
        self.assertIn(MAC_A, self.mac_list.read_text("utf-8"))
        before = self.mac_list.read_text("utf-8")

        # Same catalog, same bytes: a timer must not churn the files.
        second = self._run("--generate")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("already up to date", second.stdout)
        self.assertEqual(before, self.mac_list.read_text("utf-8"))

    def test_generate_refuses_a_hand_edit(self):
        self._seed_inventory()
        self._run("--import")
        self._seed_pool()
        self._run("--generate")

        edited = self.mac_list.read_text("utf-8") + f"{MAC_C}\n"
        self.mac_list.write_text(edited, "utf-8")

        result = self._run("--generate")
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("DRIFT", result.stdout)
        self.assertIn(MAC_C, result.stderr, "the diff should name the edit")
        self.assertEqual(edited, self.mac_list.read_text("utf-8"))

    def test_generate_adopts_a_hand_edit(self):
        self._seed_inventory()
        self._run("--import")
        self._seed_pool()
        self._run("--generate")

        edited = self.mac_list.read_text("utf-8") + f"{MAC_C}\n"
        self.mac_list.write_text(edited, "utf-8")

        result = self._run("--generate", "--drift", "adopt")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(MAC_C, self.mac_list.read_text("utf-8"))
        status = json.loads(self._run("--status", "--json").stdout)
        self.assertEqual(status["catalog"]["pool_mac"], 2)

    def test_generate_report_writes_nothing(self):
        self._seed_inventory()
        self._run("--import")
        self._seed_pool()
        self._run("--generate")

        edited = self.mac_list.read_text("utf-8") + f"{MAC_C}\n"
        self.mac_list.write_text(edited, "utf-8")

        result = self._run("--generate", "--drift", "report")
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertEqual(edited, self.mac_list.read_text("utf-8"))

    def test_a_usage_error_exits_three_not_two(self):
        # 2 means "drift"; argparse's default would make the two indistinguishable.
        result = self._run("--generate", "--drift", "nonsense")
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)

    def test_scan_uses_only_the_neighbour_table(self):
        result = self._run("--scan", "--no-arp-scan", "--json", "--timeout", "2")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["interface"], "no-such-iface0")
        self.assertIsInstance(payload["devices"], list)


if __name__ == "__main__":
    unittest.main(verbosity=2)
