#!/usr/bin/env python3
"""lan-identity-gui — curate, suggest, pool, rotate and apply LAN identities.

Entry point.  Runs entirely as the logged-in user: the only privileged step is
the ``pkexec`` helper, invoked on demand from the UI.

``--check`` prints an environment report without opening a window, which is the
quickest way to see whether a host can run the app at all.
"""

from __future__ import annotations

import signal
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import config  # noqa: E402  (path must be set first)
import listfiles  # noqa: E402
import macaddr  # noqa: E402
from catalog import Catalog  # noqa: E402
from cli import environment_report as _environment_report  # noqa: E402
from cli import run as run_headless, wants_headless  # noqa: E402


def bootstrap(catalog: Catalog, settings: config.Settings) -> list[str]:
    """Make the catalog match today's files on first run.

    Imports the collector's CSV when the catalog is empty, then seeds each pool
    from the existing list file so the very first regeneration is a no-op
    instead of a surprise diff.
    """
    notes: list[str] = []
    counts = catalog.counts()

    inventory = Path(settings.inventory_path)
    if counts["devices"] == 0 and inventory.exists():
        report = catalog.import_file(inventory, source="inventory")
        notes.append(f"imported {inventory.name}: {report.summary()}")

    if counts["pool_mac"] == 0:
        mac_path = Path(settings.mac_list_path)
        if mac_path.exists():
            entries = listfiles.parse_mac_list(
                mac_path.read_text("utf-8", errors="replace")
            )
            for entry in entries:
                check = macaddr.check(entry)
                if not check.usable:
                    notes.append(f"{mac_path.name}: skipped unusable entry {entry!r}")
                    continue
                device = catalog.device_by_mac(check.normalized)
                if device is None:
                    device_id, _ = catalog.add_device(
                        check.normalized, source=mac_path.name
                    )
                    if device_id is None:
                        continue
                    device = catalog.device(device_id)
                if device:
                    catalog.add_to_pool([device.id])
            if entries:
                notes.append(f"seeded the MAC pool from {mac_path.name}")

    if counts["pool_hostnames"] == 0:
        host_path = Path(settings.hostname_list_path)
        if host_path.exists():
            entries = listfiles.parse_hostname_list(
                host_path.read_text("utf-8", errors="replace")
            )
            added = 0
            for entry in entries:
                ok, _ = catalog.add_hostname(entry, source=host_path.name, pool=True)
                added += 1 if ok else 0
            if entries:
                notes.append(f"seeded {added} hostname(s) from {host_path.name}")

    # Which row is *this machine*?  Recorded every start, because a rotated MAC
    # changes the answer and the flag must follow the interface, not the history.
    _, own_notes = catalog.mark_live_identity(
        config.own_mac(settings.iface), config.current_hostname()
    )
    notes.extend(own_notes)

    return notes


def environment_report(settings: config.Settings) -> str:
    """Kept for callers that imported it from here; the report lives in cli."""
    return _environment_report(settings)


def main(argv: list[str]) -> int:
    config.ensure_dirs()
    settings = config.load_settings()

    # Headless verbs run before any Qt import, so they work with no display.
    if wants_headless(argv):
        return run_headless(argv, settings)

    catalog = Catalog(settings.catalog_path)
    notes = bootstrap(catalog, settings)

    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv[:1])
    app.setApplicationName(config.APP_NAME)
    app.setApplicationDisplayName(config.APP_TITLE)

    from ui.main_window import MainWindow

    window = MainWindow(catalog, settings)
    for note in notes:
        window.log(note)
    window.show()

    # Ctrl-C in the launching terminal should close the window, not be ignored.
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    from PySide6.QtCore import QTimer

    ticker = QTimer()
    ticker.start(400)
    ticker.timeout.connect(lambda: None)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
