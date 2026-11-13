"""The privacy check must pass on this tree — and must catch a planted leak.

Every value that should trip the check is assembled at runtime.  A literal here
would be the very thing the check reports, so the test would fail for the wrong
reason.

Local hostnames come from `config.json`, not from the script, so the hostname
test writes its own config and points the check at it.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "privacy-check.sh"

#: Built by concatenation so this file contains no address of its own.
PLANTED_MAC = ":".join(["A0", "56", "2C", "00", "00", "01"])
PLANTED_IP = ".".join(["10", "1", "2", "3"])
PLANTED_HOST = "lag" + "kage"
PLANTED_HOME = "/" + "home" + "/" + "someoneelse" + "/project"


class PrivacyCheckTests(unittest.TestCase):
    def setUp(self):
        if not SCRIPT.exists():
            self.skipTest(f"{SCRIPT} is missing")

    def _run(self, root: Path | None = None, config: Path | None = None) -> subprocess.CompletedProcess:
        argv = ["bash", str(SCRIPT)]
        if root is not None:
            argv.append(str(root))
        env = dict(os.environ)
        # Point at a config that does not exist unless the test made one, so a
        # developer's real deny list cannot decide the result.
        env["LAN_IDENTITY_CONFIG"] = str(config or Path(os.devnull))
        return subprocess.run(argv, capture_output=True, text=True, check=False, env=env)

    def test_the_committed_tree_is_clean(self):
        result = self._run()
        self.assertEqual(
            result.returncode,
            0,
            "privacy-check found real network data:\n"
            + result.stdout
            + result.stderr,
        )
        self.assertIn("clean", result.stdout)

    def test_a_planted_mac_fails_the_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "leak.py").write_text(f"MAC = {PLANTED_MAC!r}\n", "utf-8")
            result = self._run(Path(tmp))
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("real-looking MAC", result.stdout)

    def test_a_private_address_fails_the_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "leak.md").write_text(f"host at {PLANTED_IP}\n", "utf-8")
            result = self._run(Path(tmp))
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("private IPv4", result.stdout)

    def test_a_local_hostname_fails_the_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "leak.txt").write_text(f"{PLANTED_HOST}\n", "utf-8")
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"fleet_hostnames": [PLANTED_HOST]}), "utf-8")
            result = self._run(tmp_path, config)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("local hostname", result.stdout)

    def test_no_config_means_no_hostname_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "leak.txt").write_text(f"{PLANTED_HOST}\n", "utf-8")
            result = self._run(Path(tmp))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("skipping the hostname rule", result.stdout)

    def test_an_absolute_home_path_fails_the_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "entry.desktop").write_text(
                f"[Desktop Entry]\nExec={PLANTED_HOME}/run.sh\n", "utf-8"
            )
            result = self._run(Path(tmp))
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("absolute home path", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
