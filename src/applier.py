"""Applying an identity: commands, revert script, pkexec helper, Konsole handoff.

Three routes, deliberately separate:

**Copy**
    Build the exact command sequence and put it on the clipboard.  Nothing is
    executed.  Zero privilege surface.

**pkexec**
    Run ``bin/apply-identity`` under ``pkexec``, which raises the desktop polkit
    dialog.  The helper validates the address, sets the MAC, sets the hostname
    while the link is down, brings the link up, waits for a lease, reads the MAC
    back and prints JSON.  The GUI shows the step results.

**Konsole**
    Hand the same settings to a terminal running ``mac-rotator.sh`` under sudo,
    for when the rotation must outlive the window.

In every route the revert script is written to disk **before** anything is
applied.  The CLI prints its revert line to a terminal that can be closed; a
file cannot be lost that way.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import macaddr
from config import REVERT_DIR, Settings, current_hostname, own_mac


@dataclass
class Step:
    name: str
    status: str  # ok | warn | error | skipped
    detail: str = ""

    @property
    def symbol(self) -> str:
        return {"ok": "\u2713", "warn": "!", "error": "\u2717", "skipped": "\u2013"}.get(
            self.status, "?"
        )


@dataclass
class ApplyResult:
    ok: bool
    mac: str
    hostname: str | None
    actual_mac: str | None = None
    steps: list[Step] = field(default_factory=list)
    error: str = ""
    revert_path: str | None = None
    dry_run: bool = False
    started_at: str = ""
    finished_at: str = ""

    def lines(self) -> list[str]:
        return [f"{step.symbol} {step.name}{': ' + step.detail if step.detail else ''}"
                for step in self.steps]


# --------------------------------------------------------------------------- #
# revert script
# --------------------------------------------------------------------------- #

def revert_script(iface: str, mac: str | None, hostname: str | None,
                  down_wait: int) -> str:
    """The script that puts the previous identity back.

    ``down_wait`` is required rather than defaulted: the revert has to wait what
    the apply waited, and a second default here would be free to drift from
    :class:`config.Settings`.

    Each leg records its own failure and the script carries on to the next one --
    a restore is worth attempting in full, and the exit status says whether all of
    it worked.  Before this, a failed leg printed its error and the script still
    reached ``revert complete`` with status 0.
    """
    body = [
        "#!/usr/bin/env bash",
        "# Revert written by lan-identity-gui before applying a new identity.",
        f"# Applied at: {time.strftime('%Y-%m-%dT%H:%M:%S%z')}",
        "# Run with sudo. Restores the identity that was in place beforehand.",
        "set -u",
        f"IFACE={shlex.quote(iface)}",
        "FAILED=0",
        'echo "bringing $IFACE down"',
        'ip link set "$IFACE" down || { echo "link down failed"; FAILED=1; }',
        f"sleep {int(down_wait)}",
    ]
    if mac:
        body.append(f'echo "restoring MAC {mac}"')
        body.append(
            f'ip link set "$IFACE" address {shlex.quote(mac)} '
            '|| { echo "MAC restore failed"; FAILED=1; }'
        )
    else:
        body.append('echo "no previous MAC recorded — leaving the current one"')
    if hostname:
        body.append(f'echo "restoring hostname {hostname}"')
        body.append(
            f"hostnamectl set-hostname {shlex.quote(hostname)} "
            "|| { echo 'hostname restore failed'; FAILED=1; }"
        )
    body += [
        'echo "bringing $IFACE up"',
        'ip link set "$IFACE" up || { echo "link up failed"; FAILED=1; }',
        'ip -brief addr show dev "$IFACE"',
        'if [ "$FAILED" -ne 0 ]; then',
        '    echo "revert incomplete: at least one step failed" >&2',
        "    exit 1",
        "fi",
        'echo "revert complete"',
        "",
    ]
    return "\n".join(body)


def write_revert(iface: str, mac: str | None, hostname: str | None,
                 down_wait: int, directory: Path = REVERT_DIR) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = directory / f"revert-{stamp}.sh"
    counter = 1
    while path.exists():
        path = directory / f"revert-{stamp}-{counter}.sh"
        counter += 1
    path.write_text(revert_script(iface, mac, hostname, down_wait), "utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


# --------------------------------------------------------------------------- #
# copy route
# --------------------------------------------------------------------------- #

def build_commands(settings: Settings, mac: str, hostname: str | None) -> list[str]:
    """The literal sequence mac-rotator.sh performs, in order."""
    iface = settings.iface
    commands = [
        f"ip link set {iface} down",
        f"sleep {settings.down_wait}",
        f"ip link set {iface} address {mac}",
    ]
    if hostname:
        commands.append(f"hostnamectl set-hostname {hostname}")
    commands.append(f"ip link set {iface} up")
    commands.append(f"ip -brief addr show dev {iface}")
    if settings.internet_check:
        commands.append("ping -c1 -W2 1.1.1.1")
        commands.append(
            "curl -sS -o /dev/null -w '%{http_code}\\n' --max-time 6 "
            "https://connectivitycheck.gstatic.com/generate_204"
        )
    return commands


def copy_text(settings: Settings, mac: str, hostname: str | None,
              prev_mac: str | None, prev_host: str | None) -> str:
    lines = [
        "# lan-identity-gui — apply this identity",
        f"# interface: {settings.iface}",
        f"# new MAC:   {mac}",
        f"# new name:  {hostname or '(unchanged)'}",
        f"# was MAC:   {prev_mac or '(unknown)'}",
        f"# was name:  {prev_host or '(unknown)'}",
        "",
        "sudo bash -c '",
    ]
    lines += [f"  {command}" for command in build_commands(settings, mac, hostname)]
    lines.append("'")
    lines += ["", "# revert:", f"#   sudo {shlex.quote(str(REVERT_DIR))}/<revert-file>.sh"]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# guards
# --------------------------------------------------------------------------- #

def rotator_running() -> list[str]:
    """Existing rotator processes.  Two engines must never fight for one NIC."""
    try:
        completed = subprocess.run(
            ["pgrep", "-af", "mac-rotator"], capture_output=True, text=True,
            timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in completed.stdout.splitlines():
        if "pgrep" in line:
            continue
        found.append(line.strip())
    return found


def ssh_on_interface(iface: str) -> str | None:
    """The SSH connection string when this session rides on ``iface``."""
    connection = os.environ.get("SSH_CONNECTION", "")
    if not connection:
        return None
    try:
        out = subprocess.run(
            ["ip", "-4", "-brief", "addr", "show", "dev", iface],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    local_ip = connection.split()[1] if len(connection.split()) > 1 else ""
    if local_ip and local_ip in out:
        return connection
    return None


# --------------------------------------------------------------------------- #
# pkexec route
# --------------------------------------------------------------------------- #

def helper_path() -> Path:
    return Path(__file__).resolve().parent.parent / "bin" / "apply-identity"


def apply_with_pkexec(settings: Settings, mac: str, hostname: str | None,
                      on_step=None) -> ApplyResult:
    """Run the privileged helper and translate its JSON into an ApplyResult."""
    result = ApplyResult(
        ok=False, mac=mac, hostname=hostname, dry_run=settings.dry_run,
        started_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    )

    check = macaddr.check(mac)
    if not check.usable:
        result.error = f"{mac} is not assignable: {check.reason or 'invalid'}"
        return result

    helper = helper_path()
    if not helper.exists():
        result.error = f"helper missing: {helper}"
        return result

    previous_mac = own_mac(settings.iface)
    previous_host = current_hostname()
    try:
        revert = write_revert(
            settings.iface, previous_mac, previous_host,
            down_wait=settings.down_wait,
        )
        result.revert_path = str(revert)
    except OSError as exc:
        result.error = f"could not write the revert script: {exc}"
        return result

    if settings.dry_run:
        result.ok = True
        result.steps = [Step("dry run", "skipped", "nothing was changed")]
        result.finished_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        return result

    args = [
        "pkexec", str(helper),
        "--interface", settings.iface,
        "--mac", mac,
        "--down-wait", str(settings.down_wait),
        "--dhcp-wait", str(settings.dhcp_wait),
        "--check-timeout", str(settings.check_timeout),
    ]
    if hostname:
        args += ["--hostname", hostname]
    if not settings.internet_check:
        args.append("--no-internet-check")

    if on_step:
        on_step("waiting for authorisation…")
    try:
        completed = subprocess.run(
            args, capture_output=True, text=True, timeout=300, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        result.error = f"pkexec failed: {exc}"
        result.finished_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        return result

    payload = None
    for line in completed.stdout.strip().splitlines()[::-1]:
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                payload = json.loads(line)
                break
            except ValueError:
                continue

    if payload is None:
        result.error = (completed.stderr or completed.stdout or "no output from helper").strip()
        result.finished_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        return result

    result.ok = bool(payload.get("ok"))
    result.error = payload.get("error") or ""
    result.actual_mac = payload.get("actual_mac")
    result.steps = [
        Step(item.get("name", "step"), item.get("status", "ok"), item.get("detail", ""))
        for item in payload.get("steps", [])
    ]
    result.finished_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    if on_step:
        on_step("done" if result.ok else "failed")
    return result


# --------------------------------------------------------------------------- #
# Konsole handoff
# --------------------------------------------------------------------------- #

def konsole_available() -> bool:
    return shutil.which("konsole") is not None


def handoff_argv(settings: Settings, once: bool = False) -> list[str]:
    """argv for a terminal running the real mac-rotator.sh under sudo.

    Built as an argument vector, never as a shell string: the hostname prefix is
    an empty argument, and an empty argument is exactly what shell quoting gets
    wrong.
    """
    script = Path.home() / "scripts" / "mac-rotator.sh"
    options = [
        "-i", settings.iface,
        "-f", settings.mac_list_path,
        "-H", settings.hostname_list_path,
        "-p", settings.hostname_prefix,
        "-t", str(settings.interval),
        "-d", str(settings.down_wait),
        "-W", str(settings.dhcp_wait),
        "-T", str(settings.check_timeout),
    ]
    if not settings.rotate_hostname:
        options.append("-N")
    if not settings.internet_check:
        options.append("-C")
    if settings.dry_run:
        options.append("-n")
    if once:
        options.append("-o")
    return ["konsole", "--hold", "-e", "sudo", str(script), *options]


def handoff_command(settings: Settings, once: bool = False) -> list[str]:
    """Compatibility alias for the argv builder."""
    return handoff_argv(settings, once)


def handoff_preview(settings: Settings, once: bool = False) -> str:
    """The same command as a readable, copy-pasteable line."""
    argv = handoff_argv(settings, once)
    inside = argv[argv.index("sudo"):]
    return " ".join(shlex.quote(part) for part in inside)


def launch_konsole(settings: Settings, once: bool = False) -> tuple[bool, str]:
    if not konsole_available():
        return False, "konsole is not installed"
    script = Path.home() / "scripts" / "mac-rotator.sh"
    if not script.exists():
        return False, f"{script} not found"
    try:
        subprocess.Popen(handoff_argv(settings, once), start_new_session=True)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    return True, "opened a terminal running mac-rotator.sh under sudo"
