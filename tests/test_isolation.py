"""The test run must never write into the real user config.

``WindowSmokeTests`` drives code paths that call ``save_settings()``.  If
``run-tests.sh`` ever stops setting ``LAN_IDENTITY_HOME``, those writes go to
``~/.config/lan-identity-gui/config.json`` and quietly replace the user's real
settings with a temp directory - which is exactly what happened once.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import config  # noqa: E402


class IsolationTests(unittest.TestCase):
    def test_the_suite_runs_under_a_redirected_home(self):
        override = os.environ.get("LAN_IDENTITY_HOME", "").strip()
        self.assertTrue(
            override,
            "LAN_IDENTITY_HOME is not set - run the suite through ./run-tests.sh",
        )
        self.assertEqual(config.APP_HOME, Path(override).expanduser())

    def test_config_and_state_live_under_that_home(self):
        self.assertTrue(str(config.CONFIG_PATH).startswith(str(config.APP_HOME)))
        self.assertTrue(str(config.CATALOG_PATH).startswith(str(config.APP_HOME)))
        self.assertNotEqual(config.CONFIG_PATH.parent, Path.home() / ".config" / config.APP_NAME)

    def test_real_scripts_are_still_addressed_through_the_real_home(self):
        # Only the app's own state is redirected; mac-rotator.sh is not ours.
        self.assertEqual(
            config.DEFAULT_MAC_LIST, Path.home() / "scripts" / "mac-list.txt"
        )

    def test_the_collectors_inventory_is_not_redirected(self):
        # packet 204's collector owns that file; a redirected app home must not
        # hide it, or a first run silently finds no inventory.
        self.assertEqual(
            config.INVENTORY_CSV,
            Path.home() / ".local" / "state" / "lan-mac-inventory" / "hosts.csv",
        )


if __name__ == "__main__":
    unittest.main()
