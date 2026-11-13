"""Tests for the apply layer that do not need root: commands, revert, guards.

The pkexec path itself is not exercised here — it needs an authenticated desktop
session.  What is tested is everything the privileged path depends on, so a
failure in the helper invocation is not a failure in the command construction.
"""

from __future__ import annotations

import ast
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import applier  # noqa: E402
from config import Settings  # noqa: E402


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(
            iface="eth0", down_wait=4, dhcp_wait=30, check_timeout=10,
            internet_check=False,
        )

    def test_order_matches_the_script(self):
        commands = applier.build_commands(self.settings, "00:00:5E:00:53:01", "kontor1")
        self.assertEqual(commands[0], "ip link set eth0 down")
        self.assertEqual(commands[1], "sleep 4")
        self.assertEqual(commands[2], "ip link set eth0 address 00:00:5E:00:53:01")
        self.assertEqual(commands[3], "hostnamectl set-hostname kontor1")
        self.assertEqual(commands[4], "ip link set eth0 up")
        self.assertNotIn("ping", " ".join(commands))

    def test_hostname_optional(self):
        commands = applier.build_commands(self.settings, "00:00:5E:00:53:01", None)
        self.assertFalse(any(command.startswith("hostnamectl") for command in commands))

    def test_internet_check_adds_probes(self):
        self.settings.internet_check = True
        commands = applier.build_commands(self.settings, "00:00:5E:00:53:01", None)
        joined = " ".join(commands)
        self.assertIn("ping -c1 -W2 1.1.1.1", joined)
        self.assertIn("generate_204", joined)

    def test_copy_text_reports_before_and_after(self):
        text = applier.copy_text(
            self.settings, "00:00:5E:00:53:01", "kontor1",
            "00:00:5E:00:53:30", "miranda",
        )
        self.assertIn("00:00:5E:00:53:01", text)
        self.assertIn("00:00:5E:00:53:30", text)
        self.assertIn("miranda", text)
        self.assertIn("sudo bash -c", text)


class RevertTests(unittest.TestCase):
    def test_script_restores_mac_and_hostname(self):
        text = applier.revert_script("eth0", "00:00:5E:00:53:30", "miranda", down_wait=4)
        self.assertIn("IFACE=eth0", text)
        self.assertIn("ip link set \"$IFACE\" address 00:00:5E:00:53:30", text)
        self.assertIn("hostnamectl set-hostname miranda", text)
        self.assertIn("ip link set \"$IFACE\" up", text)

    def test_script_handles_unknown_previous_mac(self):
        text = applier.revert_script("eth0", None, None, down_wait=4)
        self.assertIn("no previous MAC recorded", text)
        self.assertNotIn("hostnamectl set-hostname", text)

    def test_write_is_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = applier.write_revert(
                "eth0", "00:00:5E:00:53:30", "miranda", 4, directory=Path(tmp)
            )
            self.assertTrue(path.exists())
            mode = path.stat().st_mode
            self.assertTrue(mode & stat.S_IXUSR)
            self.assertIn("revert", path.read_text("utf-8"))


class GuardTests(unittest.TestCase):
    def test_ssh_guard_is_silent_without_an_ssh_session(self):
        saved = os.environ.pop("SSH_CONNECTION", None)
        try:
            self.assertIsNone(applier.ssh_on_interface("lo"))
        finally:
            if saved is not None:
                os.environ["SSH_CONNECTION"] = saved

    def test_rotator_running_returns_a_list(self):
        self.assertIsInstance(applier.rotator_running(), list)

    def test_handoff_command_quotes_and_sets_prefix(self):
        settings = Settings(iface="eth0", hostname_prefix="", interval=120)
        argv = applier.handoff_argv(settings)
        self.assertEqual(argv[:4], ["konsole", "--hold", "-e", "sudo"])
        self.assertIn("mac-rotator.sh", argv[4])
        # The empty prefix must survive as its own argument, not as text.
        index = argv.index("-p")
        self.assertEqual(argv[index + 1], "")
        self.assertIn("120", argv[argv.index("-t") + 1])

    def test_preview_is_a_readable_command_line(self):
        settings = Settings(iface="eth0", hostname_prefix="")
        preview = applier.handoff_preview(settings)
        self.assertTrue(preview.startswith("sudo "))
        self.assertIn("-p ''", preview)

    def test_dry_run_is_forwarded(self):
        settings = Settings(iface="eth0", dry_run=True)
        self.assertIn("-n", applier.handoff_argv(settings))


class HelperScriptTests(unittest.TestCase):
    def setUp(self):
        self.helper = Path(__file__).resolve().parent.parent / "bin" / "apply-identity"

    def test_helper_exists_and_is_executable(self):
        self.assertTrue(self.helper.exists(), self.helper)
        self.assertTrue(self.helper.stat().st_mode & stat.S_IXUSR)

    def test_helper_rejects_a_multicast_mac_without_touching_the_network(self):
        import subprocess

        completed = subprocess.run(
            [sys.executable, str(self.helper), "--interface", "eth0",
             "--mac", "01:00:5E:00:53:06"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        self.assertEqual(completed.returncode, 1)
        self.assertIn("multicast", completed.stdout)
        self.assertIn('"ok": false', completed.stdout.replace('"ok":false', '"ok": false'))

    def test_helper_rejects_an_invalid_interface_name(self):
        import subprocess

        completed = subprocess.run(
            [sys.executable, str(self.helper), "--interface", "../etc",
             "--mac", "00:00:5E:00:53:01"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        self.assertEqual(completed.returncode, 1)
        self.assertIn("invalid interface", completed.stdout)


def _namespace_reason() -> str | None:
    """Why the revert cannot be executed on this host, or ``None`` if it can.

    A user namespace with its own network namespace is enough to give the script
    a real link to act on without touching the machine's own NIC.
    """
    if shutil.which("unshare") is None:
        return "unshare is not installed, so the revert script cannot be executed here"
    try:
        probe = subprocess.run(
            ["unshare", "-rn", "true"], capture_output=True, timeout=15, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"unshare -rn could not be run: {exc}"
    if probe.returncode != 0:
        detail = probe.stderr.decode("utf-8", "replace").strip().splitlines()
        return "unshare -rn is not permitted here" + (f": {detail[-1]}" if detail else "")
    return None


MAC_BEFORE = "02:00:5E:10:00:99"
MAC_REVERT = "02:00:5E:10:00:01"


class RevertHonestyTests(unittest.TestCase):
    """The revert must wait what the apply waited, and must not call a failure success.

    Two defects, both measured in this packet's F4 rows: the script slept a
    hardcoded three seconds while the apply waited ``settings.down_wait``, and a
    failed hostname leg printed its error and still reached "revert complete"
    with exit 0 — so the log of a failed restore looked exactly like a good one.
    """

    def test_the_revert_waits_the_configured_down_wait(self):
        text = applier.revert_script("eth0", MAC_REVERT, None, down_wait=7)
        self.assertIn("sleep 7", text)
        self.assertNotIn("sleep 3", text)

    def test_the_revert_and_the_apply_wait_the_same_number(self):
        settings = Settings(iface="eth0", down_wait=4, internet_check=False)
        applied = applier.build_commands(settings, MAC_REVERT, None)
        reverted = applier.revert_script(
            settings.iface, MAC_REVERT, None, down_wait=settings.down_wait
        )
        self.assertIn("sleep 4", applied)
        self.assertIn("sleep 4", reverted)

    def test_a_failed_leg_is_not_echoed_and_forgotten(self):
        text = applier.revert_script("eth0", MAC_REVERT, "miranda", down_wait=0)
        self.assertNotIn('|| echo "MAC restore failed"', text)
        self.assertNotIn("|| echo 'hostname restore failed'", text)
        self.assertIn("FAILED=1", text)
        self.assertLess(
            text.index("FAILED=1"),
            text.index("revert complete"),
            "the success line must come after the failure check",
        )

    def _run(self, script: Path, path_prefix: Path | None = None):
        """Run the script against a real link inside a throwaway namespace."""
        inner = "\n".join(
            [
                "set -e",
                "ip link set lo down",
                f"ip link set lo address {MAC_BEFORE}",
                "ip link set lo up",
                "set +e",
                f"bash {shlex.quote(str(script))}",
                "rc=$?",
                'echo "SCRIPT_EXIT=$rc"',
                "ip -brief link show lo",
                "exit $rc",
            ]
        )
        env = dict(os.environ)
        if path_prefix is not None:
            env["PATH"] = f"{path_prefix}:{env.get('PATH', '')}"
        return subprocess.run(
            ["unshare", "-rn", "bash", "-c", inner],
            capture_output=True, text=True, timeout=60, env=env, check=False,
        )

    def test_a_restore_that_works_exits_zero_and_restores_the_mac(self):
        reason = _namespace_reason()
        if reason:
            self.skipTest(reason)
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "revert.sh"
            script.write_text(
                applier.revert_script("lo", MAC_REVERT, None, down_wait=0), "utf-8"
            )
            done = self._run(script)
        self.assertIn("SCRIPT_EXIT=0", done.stdout, done.stderr)
        self.assertIn(MAC_REVERT.lower(), done.stdout.lower())

    def test_a_failed_hostname_leg_exits_non_zero(self):
        reason = _namespace_reason()
        if reason:
            self.skipTest(reason)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = root / "bin"
            fake.mkdir()
            # A fake name service: it must not be possible for a test to rename
            # the host, and the script only has to see a failing leg.
            hostnamectl = fake / "hostnamectl"
            hostnamectl.write_text(
                "#!/usr/bin/env bash\necho 'fake: no name service here' >&2\nexit 1\n",
                "utf-8",
            )
            hostnamectl.chmod(hostnamectl.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            script = root / "revert.sh"
            script.write_text(
                applier.revert_script("lo", MAC_REVERT, "miranda", down_wait=0), "utf-8"
            )
            done = self._run(script, path_prefix=fake)
        combined = done.stdout + done.stderr
        self.assertIn("SCRIPT_EXIT=1", done.stdout, done.stderr)
        self.assertIn("hostname restore failed", combined)
        self.assertIn("revert incomplete", combined)
        self.assertNotIn("revert complete", done.stdout)


class NameServiceTests(unittest.TestCase):
    """A changed hostname has to reach the name service, and must not be assumed to.

    ``avahi-daemon`` reads the system hostname when it starts.  ``--reload``
    re-reads ``/etc/resolv.conf`` and the files in ``/etc/avahi/services/`` but
    explicitly not ``avahi-daemon.conf``, so it cannot republish a new name.  A
    step that called it and reported ``ok`` would be the same quiet lie as a
    revert that exits 0 after a failed leg.

    Measured on the deployment host before this phase: ``host-name`` is commented
    out in the shipped config, so the published name does come from the system
    hostname at startup -- and only a restart changes it.
    """

    def setUp(self):
        self.module = load_helper()
        self.module.STEPS.clear()
        self.calls = []
        self.state = "active"
        self.restart_code = 0

        def fake_run(argv, timeout=20):
            argv = list(argv)
            self.calls.append(argv)
            if argv[:2] == ["systemctl", "is-active"]:
                return (0 if self.state == "active" else 3), f"{self.state}\n", ""
            if argv[:2] == ["systemctl", "restart"]:
                error = "" if self.restart_code == 0 else "stub restart failed\n"
                return self.restart_code, "", error
            return 0, "", ""

        self.module.has_carrier = lambda iface: True
        self.module.interface_mac = lambda iface: "00:00:5E:00:53:01"
        self.module.run = fake_run
        self.module.probe = lambda budget: (True, "stub probe")
        self.module.wait_for_link = lambda iface, budget: (True, "192.0.2.11", 1)

    def apply(self, *extra):
        argv = ["--interface", "eth0", "--mac", "00:00:5E:00:53:01",
                "--down-wait", "0"] + list(extra)
        with contextlib.redirect_stdout(io.StringIO()):
            return self.module.main(argv)

    def names(self):
        return [step["name"] for step in self.module.STEPS]

    def status_of(self, name):
        for step in self.module.STEPS:
            if step["name"] == name:
                return step["status"]
        return None

    def restarts(self):
        return [call for call in self.calls if call[:2] == ["systemctl", "restart"]]

    def test_no_hostname_means_no_name_service_step(self):
        self.assertEqual(self.apply("--no-internet-check"), 0)
        self.assertNotIn("name service", self.names())
        self.assertEqual(self.restarts(), [])

    def test_an_active_name_service_is_restarted_once(self):
        self.assertEqual(self.apply("--hostname", "miranda", "--no-internet-check"), 0)
        self.assertEqual(self.status_of("set hostname"), "ok")
        self.assertEqual(self.status_of("name service"), "ok")
        self.assertEqual(self.restarts(), [["systemctl", "restart", "avahi-daemon"]])

    def test_a_failed_restart_warns_and_is_not_fatal(self):
        self.restart_code = 1
        self.assertEqual(self.apply("--hostname", "miranda", "--no-internet-check"), 0)
        self.assertEqual(self.status_of("name service"), "warn")
        self.assertEqual(self.status_of("lease"), "ok")

    def test_an_inactive_name_service_is_skipped_not_started(self):
        self.state = "inactive"
        self.assertEqual(self.apply("--hostname", "miranda", "--no-internet-check"), 0)
        self.assertEqual(self.status_of("name service"), "skipped")
        self.assertEqual(self.restarts(), [], "an inactive service must not be started")

    def test_a_failed_hostname_change_does_not_restart_the_name_service(self):
        def failing_hostnamectl(argv, timeout=20):
            argv = list(argv)
            self.calls.append(argv)
            if argv[0] == "hostnamectl":
                return 1, "", "stub hostnamectl failed\n"
            if argv[:2] == ["systemctl", "is-active"]:
                return 0, "active\n", ""
            return 0, "", ""

        self.module.run = failing_hostnamectl
        self.assertEqual(self.apply("--hostname", "miranda", "--no-internet-check"), 0)
        self.assertEqual(self.status_of("set hostname"), "warn")
        self.assertEqual(self.status_of("name service"), "skipped")
        self.assertEqual(self.restarts(), [])

    def test_the_refresh_runs_after_the_link_is_up(self):
        self.assertEqual(self.apply("--hostname", "miranda", "--no-internet-check"), 0)
        names = self.names()
        self.assertLess(names.index("link up"), names.index("name service"))


class NameServiceSubprocessTests(unittest.TestCase):
    """The same step, through a real helper process and stubs on ``PATH``.

    A stub ``systemctl`` is used rather than a stub ``avahi-daemon`` because the
    decision this phase made is to restart the unit: a reload cannot republish
    the name.  ``hostnamectl`` is stubbed too, so the test cannot rename the
    machine it runs on.
    """

    SYSTEMCTL = """#!/usr/bin/env bash
printf 'systemctl %s\\n' "$*" >> "$STUB_LOG"
if [ "$1" = "is-active" ]; then
    echo "active"
    exit 0
fi
if [ "$1" = "restart" ]; then
    exit "${STUB_RESTART_CODE:-0}"
fi
exit 0
"""

    HOSTNAMECTL = """#!/usr/bin/env bash
printf 'hostnamectl %s\\n' "$*" >> "$STUB_LOG"
exit 0
"""

    def _run(self, restart_code: str):
        reason = _namespace_reason()
        if reason:
            self.skipTest(reason)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            for name, text in (
                ("systemctl", self.SYSTEMCTL),
                ("hostnamectl", self.HOSTNAMECTL),
            ):
                path = bin_dir / name
                path.write_text(text, "utf-8")
                path.chmod(0o755)
            log = root / "calls.log"
            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            env["STUB_LOG"] = str(log)
            env["STUB_RESTART_CODE"] = restart_code
            done = subprocess.run(
                [
                    "unshare", "-rn", sys.executable, str(HELPER_PATH),
                    "--interface", "lo", "--mac", "02:00:5E:10:00:01",
                    "--hostname", "miranda", "--down-wait", "0",
                    "--dhcp-wait", "1", "--no-internet-check",
                ],
                capture_output=True, text=True, timeout=90, env=env, check=False,
            )
            calls = log.read_text("utf-8") if log.exists() else ""
        payload = json.loads(done.stdout.strip().splitlines()[-1])
        return done, payload, calls

    def test_the_step_appears_in_the_helper_report(self):
        done, payload, calls = self._run("0")
        self.assertEqual(done.returncode, 0, done.stderr)
        steps = {step["name"]: step["status"] for step in payload["steps"]}
        self.assertEqual(steps.get("name service"), "ok")
        self.assertEqual(calls.count("systemctl restart avahi-daemon"), 1)

    def test_a_failing_restart_is_reported_and_the_exit_stays_zero(self):
        done, payload, calls = self._run("1")
        self.assertEqual(done.returncode, 0, done.stderr)
        steps = {step["name"]: step["status"] for step in payload["steps"]}
        self.assertEqual(steps.get("name service"), "warn")
        self.assertEqual(calls.count("systemctl restart avahi-daemon"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)


HELPER_PATH = Path(__file__).resolve().parent.parent / "bin" / "apply-identity"


def load_helper():
    """Import bin/apply-identity so its step logic can be tested without root.

    The helper carries no .py extension because pkexec executes it directly, so a
    plain import cannot find it.  Every system call it makes is replaced inside
    the tests below: nothing here touches a real interface, a real lease or the
    network.
    """
    loader = importlib.machinery.SourceFileLoader("apply_identity_helper", str(HELPER_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class HelperStepTests(unittest.TestCase):
    """The lease wait must not be switched off by the internet probe's flag.

    `--no-internet-check` exists to keep an apply off the network.  It also used
    to cancel the lease wait, so the privacy-preserving configuration lost the
    only evidence that produces a timeout verdict: the helper reported steps for
    validate, link down, set MAC, link up and read back, and nothing about
    whether the interface ever got a usable address.
    """

    def setUp(self):
        self.module = load_helper()
        self.module.STEPS.clear()
        self.probed = []

        def fake_probe(budget):
            self.probed.append(budget)
            return True, "stub probe"

        self.module.has_carrier = lambda iface: True
        self.module.interface_mac = lambda iface: "00:00:5E:00:53:01"
        self.module.run = lambda argv, timeout=20: (0, "", "")
        self.module.probe = fake_probe
        self.module.wait_for_link = lambda iface, budget: (True, "192.0.2.11", 1)

    def apply(self, *extra):
        argv = ["--interface", "eth0", "--mac", "00:00:5E:00:53:01",
                "--down-wait", "0"] + list(extra)
        with contextlib.redirect_stdout(io.StringIO()):
            return self.module.main(argv)

    def names(self):
        return [step["name"] for step in self.module.STEPS]

    def status_of(self, name):
        for step in self.module.STEPS:
            if step["name"] == name:
                return step["status"]
        return None

    def test_lease_is_reported_when_the_internet_check_is_off(self):
        code = self.apply("--no-internet-check")
        self.assertEqual(code, 0)
        self.assertIn("lease", self.names())
        self.assertEqual(self.status_of("lease"), "ok")
        self.assertEqual(self.probed, [], "the probe must stay behind its own flag")

    def test_probe_still_runs_when_the_check_is_on(self):
        code = self.apply()
        self.assertEqual(code, 0)
        self.assertEqual(self.status_of("lease"), "ok")
        self.assertEqual(self.status_of("internet"), "ok")
        self.assertEqual(len(self.probed), 1)

    def test_failed_lease_reports_and_does_not_probe(self):
        self.module.wait_for_link = lambda iface, budget: (False, "no carrier on eth0 after 1s", 1)
        self.assertEqual(self.apply(), 0)
        self.assertEqual(self.status_of("lease"), "warn")
        self.assertEqual(self.probed, [])
        self.assertNotIn("internet", self.names())

    def test_lease_wait_is_not_inside_the_probe_flag(self):
        tree = ast.parse(HELPER_PATH.read_text("utf-8"))
        gated = [
            {
                call.func.id
                for call in ast.walk(node)
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
            }
            for node in ast.walk(tree)
            if isinstance(node, ast.If) and "no_internet_check" in ast.unparse(node.test)
        ]
        self.assertTrue(gated, "expected a --no-internet-check block to inspect")
        for calls in gated:
            self.assertNotIn("wait_for_link", calls)
            self.assertIn("probe", calls)
