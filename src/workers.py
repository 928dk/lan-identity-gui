"""QThread workers so the GUI never blocks on a network or a password dialog.

Two jobs only, both slow and both blocking by nature:

``ScanWorker``
    Runs the LAN sweep (``arp-scan`` + ``ip neigh``), 10-40 seconds.

``ApplyWorker``
    Runs the privileged helper via ``pkexec``, up to a few minutes when DHCP and
    the internet probe are involved.  Emits progress text so the panel can show
    where it is.

``VerifyWorker``
    Reads back what the apply produced: the MAC on the interface, the carrier,
    an IPv4 address and whether a sweep sees the address twice.  It reports a
    ``verify.Verdict`` and changes nothing.
"""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal

import applier
import scanner
import verify
from config import Settings


class ScanWorker(QThread):
    finished_ok = Signal(object)   # scanner.ScanResult
    failed = Signal(str)

    def __init__(self, iface: str, allow_arp_scan: bool = True, parent=None) -> None:
        super().__init__(parent)
        self.iface = iface
        self.allow_arp_scan = allow_arp_scan

    def run(self) -> None:  # noqa: D102 - Qt entry point
        try:
            result = scanner.scan(self.iface, self.allow_arp_scan)
        except Exception as exc:  # a worker must never kill the app
            self.failed.emit(f"scan failed: {exc}")
            return
        self.finished_ok.emit(result)


class ApplyWorker(QThread):
    progress = Signal(str)
    finished_ok = Signal(object)   # applier.ApplyResult
    failed = Signal(str)

    def __init__(self, settings: Settings, mac: str, hostname: str | None,
                 parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.mac = mac
        self.hostname = hostname

    def run(self) -> None:  # noqa: D102 - Qt entry point
        try:
            result = applier.apply_with_pkexec(
                self.settings, self.mac, self.hostname,
                on_step=lambda text: self.progress.emit(text),
            )
        except Exception as exc:
            self.failed.emit(f"apply failed: {exc}")
            return
        self.finished_ok.emit(result)


class VerifyWorker(QThread):
    """Read back an applied identity.  Read-only, and never reverts."""

    verdict = Signal(object)   # verify.Verdict

    def __init__(self, settings: Settings, mac: str, hostname: str | None,
                 parent=None, verify_fn=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.mac = mac
        self.hostname = hostname
        self._verify_fn = verify_fn or verify.verify_interface

    def run(self) -> None:  # noqa: D102 - Qt entry point
        try:
            result = self._verify_fn(
                self.mac,
                self.hostname,
                self.settings.iface,
                dhcp_wait=self.settings.dhcp_wait,
            )
        except Exception as exc:  # a worker must never kill the app
            result = verify.Verdict(
                verify.STATE_SKIPPED, self.mac or "", self.hostname,
                note=f"verification could not run: {exc}",
            )
        self.verdict.emit(result)
