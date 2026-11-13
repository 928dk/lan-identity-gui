"""Main window: the table, the side panels, and all the wiring.

Deliberately one table plus four tabs.  The catalog is the only state; panels
emit signals, the window saves settings and regenerates the two text files.

Regeneration happens on **every** catalog change, so what the GUI shows and what
``mac-rotator.sh`` would read never diverge.  A hand edit on disk stops that
from being silent: the drift dialog asks before anything is overwritten.
"""

from __future__ import annotations

import csv
from pathlib import Path

from PySide6.QtCore import QEventLoop, QTimer, Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QSplitter,
    QTableView, QTabWidget, QVBoxLayout, QWidget,
)

import collision
import listfiles
import macaddr
import ops
import verify
from catalog import Catalog
from config import EXPORT_DIR, Settings, save_settings
from ui.detail_panel import DetailPanel
from ui.drift_banner import DriftBanner
from ui.device_table import (
    COL_HOST, COL_MAC, SCOPE_ALL, SCOPE_ASSIGNABLE, SCOPE_INUSE, SCOPE_POOL,
    SCOPE_PROBLEM, DeviceFilterProxy, DeviceTableModel, StatusDelegate,
)
from ui.pool_panel import PoolPanel
from ui.rotation_panel import RotationPanel
from watcher import PathWatcher
from workers import ApplyWorker, ScanWorker, VerifyWorker

#: How old a recorded scan may be before the pre-flight insists on a new one.
COLLISION_SCAN_MAX_AGE = 600
#: How long the pre-flight waits for a fresh sweep before falling back.
COLLISION_SCAN_WAIT = 8
#: How long the inventory watcher waits for a burst of collector writes.
INVENTORY_DEBOUNCE_MS = 2000
#: The same, for the two generated list files.
LIST_DEBOUNCE_MS = 2000


class MainWindow(QMainWindow):
    def __init__(self, catalog: Catalog, settings: Settings) -> None:
        super().__init__()
        self.catalog = catalog
        self.settings = settings
        self.model = DeviceTableModel(catalog.devices(), self)
        self.proxy = DeviceFilterProxy(self)
        self.proxy.setSourceModel(self.model)
        self._scan_worker: ScanWorker | None = None
        self._apply_worker: ApplyWorker | None = None
        self._verify_worker: VerifyWorker | None = None
        self._pending_pair: tuple[str, str] | None = None
        #: Drifted list files the user asked to stop hearing about.  Cleared per
        #: file as soon as that file changes again, so a new edit re-arms it.
        self._ignored_drift: set[str] = set()

        self.setWindowTitle("LAN Identity")
        self.resize(1360, 860)
        self._build()

        # The collector writes its CSV on its own schedule.  Watch it so new rows
        # appear while the window is open, and gate the startup import on the
        # mtime so a restart with an unchanged file does nothing.
        self._inventory_watcher = PathWatcher(
            [settings.inventory_path],
            debounce_ms=INVENTORY_DEBOUNCE_MS,
            parent=self,
        )
        self._inventory_watcher.changed.connect(self._inventory_changed)

        self._list_watcher = PathWatcher(
            [settings.mac_list_path, settings.hostname_list_path],
            debounce_ms=LIST_DEBOUNCE_MS,
            parent=self,
        )
        self._list_watcher.changed.connect(self._list_files_changed)

        self.refresh_all()
        # The collector may have appended while the app was closed; the mtime
        # gate makes this a no-op when it did not.
        self.import_new_inventory()
        QTimer.singleShot(200, self._first_run_check)

    # ------------------------------------------------------------------ #
    # layout
    # ------------------------------------------------------------------ #

    def _build(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(6, 6, 6, 6)

        toolbar = QHBoxLayout()
        self.rescan_button = QPushButton("Rescan LAN")
        self.rescan_button.clicked.connect(self.start_scan)
        self.import_button = QPushButton("Import…")
        self.import_button.clicked.connect(self.import_file)
        self.regenerate_button = QPushButton("Regenerate list files")
        self.regenerate_button.clicked.connect(lambda: self.regenerate(interactive=True))
        self.export_button = QPushButton("Export CSV…")
        self.export_button.clicked.connect(self.export_csv)
        self.add_pool_button = QPushButton("Add selected to pool")
        self.add_pool_button.clicked.connect(self.add_selected_to_pool)
        self.protect_button = QPushButton("Protect selected")
        self.protect_button.setCheckable(True)
        self.protect_button.clicked.connect(self.toggle_protect_selected)

        for widget in (self.rescan_button, self.import_button, self.regenerate_button,
                       self.export_button):
            toolbar.addWidget(widget)
        toolbar.addSpacing(16)
        toolbar.addWidget(self.add_pool_button)
        toolbar.addWidget(self.protect_button)
        toolbar.addStretch(1)

        toolbar.addWidget(QLabel("Show:"))
        self.scope_combo = QComboBox()
        for label, value in (
            ("everything", SCOPE_ALL),
            ("assignable + absent", SCOPE_ASSIGNABLE),
            ("rows needing attention", SCOPE_PROBLEM),
            ("rotation pool", SCOPE_POOL),
            ("seen on the LAN", SCOPE_INUSE),
        ):
            self.scope_combo.addItem(label, value)
        self.scope_combo.setCurrentIndex(0)
        self.scope_combo.currentIndexChanged.connect(self._scope_changed)
        toolbar.addWidget(self.scope_combo)

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("filter mac, vendor, hostname, ip, notes…")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._text_changed)
        self.filter_edit.setMaximumWidth(320)
        toolbar.addWidget(self.filter_edit)
        layout.addLayout(toolbar)

        self.drift_banner = DriftBanner(self)
        self.drift_banner.adopt_requested.connect(self._adopt_drift)
        self.drift_banner.overwrite_requested.connect(self._overwrite_drift)
        self.drift_banner.ignore_requested.connect(self._ignore_drift)
        layout.addWidget(self.drift_banner)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(COL_MAC, Qt.SortOrder.AscendingOrder)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableView.SelectionMode.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setItemDelegateForColumn(5, StatusDelegate(self.table))
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.selectionModel().selectionChanged.connect(self._selection_changed)
        splitter.addWidget(self.table)

        self.tabs = QTabWidget()
        self.detail_panel = DetailPanel(self.catalog, self.settings)
        self.pool_panel = PoolPanel(self.catalog, self.settings)
        self.rotation_panel = RotationPanel(self.catalog, self.settings)

        self.tabs.addTab(self.detail_panel, "Device")
        self.tabs.addTab(self.pool_panel, "Pool")
        self.tabs.addTab(self.rotation_panel, "Rotate")

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.tabs.addTab(self.log_view, "Log")
        splitter.addWidget(self.tabs)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([820, 540])
        layout.addWidget(splitter, 1)

        self.status = self.statusBar()
        self.counts_label = QLabel("")
        self.status.addPermanentWidget(self.counts_label)

        # -- wiring -------------------------------------------------------- #
        self.detail_panel.catalog_changed.connect(self.on_catalog_changed)
        self.detail_panel.status_message.connect(self.log)
        self.detail_panel.copy_requested.connect(self.copy_commands)
        self.detail_panel.apply_requested.connect(self.apply_identity)

        self.pool_panel.catalog_changed.connect(self.on_catalog_changed)
        self.pool_panel.status_message.connect(self.log)
        self.pool_panel.regenerate_requested.connect(lambda: self.regenerate(interactive=True))

        self.rotation_panel.apply_requested.connect(self.apply_identity)
        self.rotation_panel.status_message.connect(self.log)
        self.rotation_panel.settings_changed.connect(self.settings_changed)
        # The Konsole handoff rotates the whole pool, so it is judged as a pool.
        self.rotation_panel.pool_guard = self.collision_guard

        quit_action = QAction("Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        self.addAction(quit_action)

        rescan_action = QAction("Rescan LAN", self)
        rescan_action.setShortcut(QKeySequence.StandardKey.Refresh)
        rescan_action.triggered.connect(self.start_scan)
        self.addAction(rescan_action)

    # ------------------------------------------------------------------ #
    # refresh
    # ------------------------------------------------------------------ #

    def refresh_all(self) -> None:
        devices = self.catalog.devices()
        selected = self._selected_ids()
        self.model.set_devices(devices)
        last = self.catalog.last_scan()
        if last:
            self.model.set_seen(last["seen"])
        self.pool_panel.refresh()
        self.rotation_panel.reload_engine()
        self._restore_selection(selected)
        counts = self.catalog.counts()
        self.counts_label.setText(
            f"{counts['devices']} devices · {counts['pool_mac']} MACs · "
            f"{counts['pool_hostnames']} names · {counts['repairs']} repairable · "
            f"{counts['broken']} broken"
        )

    def refresh_live_identity(self) -> None:
        """Re-read the interface and move the "this is you" flag where it belongs.

        Called after anything that can change the address the machine carries —
        an apply, a scan — so the flag describes the machine as it is now rather
        than as it was when the catalog was first built.
        """
        from config import current_hostname, own_mac

        _, notes = self.catalog.mark_live_identity(
            own_mac(self.settings.iface), current_hostname()
        )
        for note in notes:
            self.log(note)

    def _selected_ids(self) -> list[int]:
        selection = self.table.selectionModel()
        ids: list[int] = []
        if selection is None:
            return ids
        for index in selection.selectedRows():
            source = self.proxy.mapToSource(index)
            device = self.model.device_at(source.row())
            if device:
                ids.append(device.id)
        return ids

    def _restore_selection(self, ids: list[int]) -> None:
        if not ids:
            if self.model.rowCount():
                self.table.selectRow(0)
            return
        wanted = set(ids)
        for row in range(self.proxy.rowCount()):
            source = self.proxy.mapToSource(self.proxy.index(row, 0))
            device = self.model.device_at(source.row())
            if device and device.id in wanted:
                self.table.selectRow(row)
                return
        if self.model.rowCount():
            self.table.selectRow(0)

    def _selection_changed(self) -> None:
        ids = self._selected_ids()
        if not ids:
            self.detail_panel.show_device(None)
            return
        device = self.catalog.device(ids[0])
        self.detail_panel.show_device(device)

    def _scope_changed(self, index: int) -> None:
        self.proxy.set_scope(self.scope_combo.itemData(index) or SCOPE_ALL)

    def _text_changed(self, text: str) -> None:
        self.proxy.set_text(text)

    def log(self, message: str) -> None:
        from datetime import datetime

        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"[{stamp}] {message}")
        self.status.showMessage(message, 8000)

    # ------------------------------------------------------------------ #
    # catalog changes + file generation
    # ------------------------------------------------------------------ #

    def on_catalog_changed(self) -> None:
        self.refresh_all()
        self.regenerate(interactive=False)

    def settings_changed(self) -> None:
        save_settings(self.settings)
        self.rotation_panel.reload_engine()

    def regenerate(self, interactive: bool = True, force: bool = False) -> None:
        """Write both list files, asking about drift when there is any."""
        results = ops.generate(
            self.settings,
            self.catalog,
            drift="overwrite" if force else "refuse",
            backup_dir=EXPORT_DIR,
        )
        drifted = [r for r in results if r.drift]
        if not drifted:
            for result in results:
                if result.wrote:
                    self.log(f"{result.path.name}: written ({result.backup.name if result.backup else 'no backup'})")
            self._refresh_drift_banner()
            return

        if not interactive:
            self.log(
                "list file changed on disk — not overwritten. "
                "Use 'Regenerate list files' to review."
            )
            self.tabs.setTabText(2, "Rotate \u26a0")
            return

        decision = self._ask_drift(drifted)
        if decision == "overwrite":
            self.regenerate(interactive=False, force=True)
            self.tabs.setTabText(2, "Rotate")
        elif decision == "import":
            self._adopt_from_disk(drifted)
            self.regenerate(interactive=False, force=True)
            self.tabs.setTabText(2, "Rotate")

    def _ask_drift(self, drifted: list[listfiles.WriteResult]) -> str:
        detail = "\n\n".join(
            f"{result.path}\n" + (result.diff or "(no textual difference)")
            for result in drifted
        )
        box = QMessageBox(self)
        box.setWindowTitle("A generated list file was edited outside the app")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(
            "These files are generated from the catalog, but the copy on disk has "
            "changed since the app last wrote it.\n\n"
            "Import the edit into the catalog, overwrite it with the catalog, or cancel.\n"
        )
        box.setDetailedText(detail[:12000])
        import_button = box.addButton("Import the edit", QMessageBox.ButtonRole.AcceptRole)
        overwrite_button = box.addButton("Overwrite", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is import_button:
            return "import"
        if box.clickedButton() is overwrite_button:
            return "overwrite"
        return "cancel"

    def _adopt_from_disk(self, drifted: list[listfiles.WriteResult]) -> None:
        # One implementation, shared with `--generate --drift adopt`.
        ops.adopt_from_disk(self.settings, self.catalog)
        self.log("imported the hand edits into the catalog")
        self.refresh_all()

    def _first_run_check(self) -> None:
        """Show what the first write would change, before anything is written."""
        if self.settings.first_export_confirmed:
            return
        mac_path = Path(self.settings.mac_list_path)
        host_path = Path(self.settings.hostname_list_path)
        existing = [path for path in (mac_path, host_path) if path.exists()]
        if not existing:
            self.settings.first_export_confirmed = True
            save_settings(self.settings)
            return

        diffs = []
        for path, rendered in (
            (mac_path, listfiles.render_mac_list(self.catalog.pool_macs(), self.settings)),
            (host_path, listfiles.render_hostname_list(
                self.catalog.pool_hostnames(), self.settings)),
        ):
            if path.exists():
                before = path.read_text("utf-8", errors="replace")
                diffs.append(listfiles.unified_diff(before, rendered, str(path)))

        if not any(d.strip() for d in diffs):
            self.settings.first_export_confirmed = True
            save_settings(self.settings)
            return

        box = QMessageBox(self)
        box.setWindowTitle("First write of the generated list files")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(
            "The catalog will now own mac-list.txt and hostnamelist.txt.\n\n"
            "The changes below are entry-level. After this, the files are rewritten "
            "whenever the pool changes, and a hand edit raises a prompt instead of "
            "being lost."
        )
        box.setDetailedText("\n".join(diffs)[:12000])
        ok = box.addButton("Write the files", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Not now", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is ok:
            self.settings.first_export_confirmed = True
            save_settings(self.settings)
            self.regenerate(interactive=False, force=True)
            self.log("first write done; the list files are now generated")

    # ------------------------------------------------------------------ #
    # actions
    # ------------------------------------------------------------------ #

    def start_scan(self) -> None:
        if self._scan_worker is not None and self._scan_worker.isRunning():
            self.log("a scan is already running")
            return
        self.rescan_button.setEnabled(False)
        self.log(f"scanning {self.settings.iface}…")
        self._scan_worker = ScanWorker(self.settings.iface, True, self)
        self._scan_worker.finished_ok.connect(self._scan_done)
        self._scan_worker.failed.connect(self._scan_failed)
        self._scan_worker.start()

    def _scan_done(self, result) -> None:
        self.rescan_button.setEnabled(True)
        self.catalog.record_scan(
            result.iface, result.started_at, result.finished_at, result.seen
        )
        self.model.set_seen(result.seen)
        for device in self.catalog.devices():
            live = result.seen.get(device.mac_norm or "")
            if live:
                fields = {"ip": live.get("ip") or device.ip,
                          "seen_at": result.finished_at}
                if live.get("vendor"):
                    fields["vendor"] = live["vendor"]
                self.catalog.update_device(device.id, **fields)
        self.refresh_live_identity()
        self.refresh_all()
        problems = f" ({'; '.join(result.errors)})" if result.errors else ""
        proxies = result.proxy_macs
        detail = f"{result.answers} answer(s) from {result.count} MAC(s)"
        if proxies:
            detail += (
                f"; proxy ARP from {', '.join(sorted(proxies))} — many devices hide "
                "behind that MAC, so a silent address is not a free one"
            )
        self.log(f"scan done via {result.source or 'nothing'}: {detail}{problems}")
        self.pool_panel.refresh()

    def _scan_failed(self, message: str) -> None:
        self.rescan_button.setEnabled(True)
        self.log(message)

    def import_file(self) -> None:
        start = str(Path(self.settings.inventory_path).parent)
        path, _ = QFileDialog.getOpenFileName(
            self, "Import a saved list or the collector's CSV",
            start, "Text and CSV (*.txt *.csv);;All files (*)",
        )
        if not path:
            return
        report = self.catalog.import_file(path)
        self.settings.last_import_path = path
        save_settings(self.settings)
        self.refresh_all()
        self.regenerate(interactive=False)
        box = QMessageBox(self)
        box.setWindowTitle("Import finished")
        box.setText(f"{Path(path).name}: {report.summary()}")
        if report.messages:
            box.setDetailedText("\n".join(report.messages[:400]))
        box.exec()
        self.log(f"imported {Path(path).name}: {report.summary()}")

    # -- list-file drift ------------------------------------------------ #

    def _list_files_changed(self, paths: list[str]) -> None:
        for path in paths:
            # A new edit re-arms a file the user had told the banner to ignore.
            self._ignored_drift.discard(Path(path).name)
        self._refresh_drift_banner()

    def _refresh_drift_banner(self) -> None:
        """Show the banner for the first list file that drifted, if any.

        ``has_drift`` compares the file with the digest this app recorded when it
        last wrote it, so the app's own write is already accounted for by the
        time the watcher event arrives.  No separate suppression flag: the
        recorded digest *is* the suppression.
        """
        drifted = [
            result
            for result in ops.preview_generation(self.settings, self.catalog)
            if result.drift and result.path.name not in self._ignored_drift
        ]
        if not drifted:
            self.drift_banner.clear()
            return
        first = drifted[0]
        self.drift_banner.show_drift(first.path.name, first.diff)

    def _adopt_drift(self) -> None:
        self._ignored_drift.clear()
        ops.adopt_from_disk(self.settings, self.catalog)
        self.log("imported the hand edits into the catalog")
        self.refresh_all()
        self.regenerate(interactive=False, force=True)
        self._refresh_drift_banner()

    def _overwrite_drift(self) -> None:
        self._ignored_drift.clear()
        self.regenerate(interactive=False, force=True)
        self.log("overwrote the edited list files from the catalog")
        self._refresh_drift_banner()

    def _ignore_drift(self) -> None:
        for result in ops.preview_generation(self.settings, self.catalog):
            if result.drift:
                self._ignored_drift.add(result.path.name)
        self.drift_banner.clear()
        self.log("drift ignored until the file changes again")

    def import_new_inventory(self) -> bool:
        """Follow the collector CSV when its mtime says it changed.

        Runs on the GUI thread, like every other catalog write, so an import and
        a pool edit cannot interleave.  The pool is deliberately untouched: a new
        device is not a decision to rotate through it.
        """
        if not ops.inventory_is_newer(self.settings, self.catalog):
            return False
        try:
            report = ops.import_inventory(self.settings, self.catalog)
        except (OSError, ValueError) as exc:
            self.log(f"inventory import failed: {exc}")
            return False
        self.log(f"inventory: {report.summary()}")
        if report.added or report.updated or report.hostnames_added or report.broken:
            self.refresh_all()
        return True

    def _inventory_changed(self, paths: list[str]) -> None:
        for path in paths:
            self.log(f"{Path(path).name} changed on disk")
        self.import_new_inventory()

    def export_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export the catalog as CSV",
            str(Path.home() / "lan-identity-export.csv"), "CSV (*.csv)",
        )
        if not path:
            return
        rows = self.catalog.devices()
        try:
            with open(path, "w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow([
                    "mac", "raw", "status", "vendor", "hostname", "ip", "seen_at",
                    "protected", "pool_pos", "notes", "source",
                ])
                for device in rows:
                    writer.writerow([
                        device.mac_norm or "",
                        device.mac_raw,
                        device.status,
                        device.vendor or "",
                        device.hostname or "",
                        device.ip or "",
                        device.seen_at or "",
                        "yes" if device.protected else "",
                        device.pool_mac_pos + 1 if device.pool_mac_pos is not None else "",
                        device.notes,
                        device.source or "",
                    ])
        except OSError as exc:
            QMessageBox.warning(self, "Export failed", str(exc))
            return
        self.log(f"exported {len(rows)} row(s) to {path}")

    def add_selected_to_pool(self) -> None:
        ids = self._selected_ids()
        if not ids:
            return
        added, problems = self.catalog.add_to_pool(ids)
        self.log(f"{added} added to the MAC pool")
        if problems:
            self.log("refused: " + "; ".join(problems[:5]))
        self.on_catalog_changed()
        self._selection_changed()

    def toggle_protect_selected(self) -> None:
        ids = self._selected_ids()
        if not ids:
            return
        value = self.protect_button.isChecked()
        for device_id in ids:
            self.catalog.protect(device_id, value)
        self.log(("protected " if value else "unprotected ") + f"{len(ids)} row(s)")
        self.on_catalog_changed()

    def copy_commands(self, mac: str, hostname: str) -> None:
        from config import current_hostname, own_mac

        import applier

        text = applier.copy_text(
            self.settings, mac, hostname or None,
            own_mac(self.settings.iface), current_hostname(),
        )
        QApplication.clipboard().setText(text)
        self.log("commands copied to the clipboard")

    def collision_guard(
        self,
        pairs: list[tuple[str, str]],
        names: list[str] | tuple[str, ...] = (),
        resolve_names: bool = False,
    ) -> bool:
        """Refuse a rotation that would take an address we can prove is in use.

        Only what a scan proves is blocking.  Silence is reported as a warning and
        allowed through, because this LAN's gateway answers ARP for absent hosts:
        absence here is not evidence of freedom, and a guard that treated it as
        freedom would be lying with more confidence than the scan deserves.
        """
        from config import current_hostname, own_ip, own_mac

        seen, note = self._collision_evidence()
        iface = self.settings.iface
        own = own_mac(iface)
        residency = own_ip(iface)
        proxies = collision.proxy_macs(seen)

        verdicts: list[tuple[str, collision.Verdict]] = [
            (mac, collision.check(seen, mac, own_mac=own, own_ip=residency, proxies=proxies))
            for mac, _ in pairs
        ]
        live = current_hostname()
        if names:
            from scanner import resolve_hostname

            for hostname in names:
                resolved = resolve_hostname(hostname) if resolve_names else None
                verdicts.append(
                    (hostname, collision.check_name(hostname, live, resolved=resolved))
                )

        blocking, advisory = collision.summarize(verdicts)
        checked = f"{len(pairs)} address(es)" + (f", {len(names)} name(s)" if names else "")
        self.log(f"collision check ({note}): {checked}")
        for line in advisory:
            self.log("  ! " + line)
        if not blocking:
            return True

        for line in blocking:
            self.log("  ✗ " + line)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Address in use")
        box.setText(
            "This rotation was stopped: the target is provably in use.\n\n"
            + blocking[0]
        )
        if len(blocking) > 1:
            box.setDetailedText("\n".join(blocking))
        box.exec()
        return False

    def _collision_evidence(self) -> tuple[dict, str]:
        """The view of the LAN the pre-flight judges against, and where it came from.

        A scan younger than ten minutes is good enough.  Anything older and the
        wire is asked again, waiting at most :data:`COLLISION_SCAN_WAIT` seconds;
        a sweep that does not finish in time is not fatal, it just makes the
        evidence older than we would like — and the verdict says so.
        """
        from datetime import datetime

        last = self.catalog.last_scan()
        if last:
            try:
                finished = datetime.fromisoformat(last["finished_at"])
            except (KeyError, TypeError, ValueError):
                finished = None
            if finished is not None:
                age = (datetime.now().astimezone() - finished).total_seconds()
                if 0 <= age <= COLLISION_SCAN_MAX_AGE:
                    return last["seen"], f"recorded {int(age)} s ago"

        self.log("pre-flight: asking the LAN before touching anything…")
        result = self._scan_and_wait()
        if result is not None:
            self.catalog.record_scan(
                result.iface, result.started_at, result.finished_at, result.seen
            )
            self.model.set_seen(result.seen)
            return result.seen, (
                f"fresh sweep, {result.answers} answer(s) from {result.count} MAC(s)"
            )
        if last:
            return last["seen"], "the fresh sweep did not finish; using the older scan"
        return {}, "no scan available — nothing could be checked"

    def _scan_and_wait(self):
        """Start a scan and stay responsive while waiting for its result."""
        if self._scan_worker is not None and self._scan_worker.isRunning():
            worker = self._scan_worker
        else:
            self.start_scan()
            worker = self._scan_worker
        if worker is None:
            return None

        loop = QEventLoop()
        box: dict = {}
        worker.finished_ok.connect(lambda result: (box.setdefault("result", result), loop.quit()))
        worker.failed.connect(lambda message: (box.setdefault("error", message), loop.quit()))
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QTimer.singleShot(COLLISION_SCAN_WAIT * 1000, loop.quit)
        try:
            if not worker.isFinished():
                loop.exec()
        finally:
            QApplication.restoreOverrideCursor()
        if "result" in box:
            return box["result"]
        if "error" in box:
            self.log(f"pre-flight scan failed: {box['error']}")
        else:
            self.log(
                f"pre-flight scan did not finish within {COLLISION_SCAN_WAIT} s — "
                "judging on the evidence already in hand"
            )
        return None

    def apply_identity(self, mac: str, hostname: str) -> None:
        if self._apply_worker is not None and self._apply_worker.isRunning():
            self.log("an apply is already running")
            return
        if not macaddr.is_assignable(mac):
            self.log(f"{mac} is not assignable — refusing to apply it")
            self.rotation_panel.on_apply_failed(f"{mac} is not an assignable address")
            return
        if not self.collision_guard(
            [(mac, "")],
            names=[hostname] if hostname else [],
            resolve_names=True,
        ):
            self.rotation_panel.on_apply_failed("stopped: the target address is in use")
            if self.rotation_panel.running:
                self.rotation_panel.stop()
            return
        self._pending_pair = (mac, hostname or "")
        self._apply_worker = ApplyWorker(self.settings, mac, hostname or None, self)
        self._apply_worker.progress.connect(self.log)
        self._apply_worker.finished_ok.connect(self._apply_done)
        self._apply_worker.failed.connect(self._apply_failed)
        self.log(f"applying {mac}" + (f" + {hostname}" if hostname else ""))

        from config import current_hostname, own_mac

        self.catalog.record_apply(
            mac=mac, hostname=hostname or None, iface=self.settings.iface,
            prev_mac=own_mac(self.settings.iface), prev_host=current_hostname(),
            result="started", detail="dry run" if self.settings.dry_run else "",
        )
        self.tabs.setCurrentWidget(self.rotation_panel)
        self._apply_worker.start()

    def _apply_done(self, result) -> None:
        self.rotation_panel.on_apply_finished(result)
        if result.revert_path:
            self.log(f"revert script: {result.revert_path}")
        for line in result.lines():
            self.log("  " + line)
        if result.ok:
            self.log("identity applied" if not result.dry_run else "dry run complete")
        else:
            self.log(f"apply failed: {result.error}")
        if self._pending_pair:
            mac, hostname = self._pending_pair
            self.catalog.record_apply(
                mac=mac, hostname=hostname or None, iface=self.settings.iface,
                revert_path=result.revert_path,
                result="ok" if result.ok else "failed",
                detail=result.error or "",
            )
            self._pending_pair = None
        self.refresh_live_identity()
        self.refresh_all()
        if result.ok:
            self._start_verification(result)

    def _start_verification(self, result) -> None:
        """Read back what the apply produced, in a worker, and record it.

        A dry run is not verified: nothing changed, so there is nothing to read.
        The guard is here rather than at the call site so a second caller cannot
        forget it.
        """
        if getattr(result, "dry_run", False):
            self.log("dry run — nothing to verify")
            return
        if self._verify_worker is not None and self._verify_worker.isRunning():
            self.log("a verification is already running")
            return
        self.log("verifying the applied identity…")
        # ``verify_fn`` is a seam for tests: the real worker reads the machine.
        self._verify_worker = VerifyWorker(
            self.settings, result.mac, result.hostname, self,
            verify_fn=getattr(self, "verify_fn", None),
        )
        self._verify_worker.verdict.connect(self._verification_done)
        self._verify_worker.start()

    def _verification_done(self, verdict) -> None:
        self.log("verification: " + verify.describe(verdict))
        device = self.catalog.device_by_mac(verdict.mac)
        if device is not None:
            self.catalog.record_verification(device.id, verdict.state, verdict.note)
            self.refresh_all()
        self.rotation_panel.show_verdict(verdict)
        if verdict.is_failure:
            self.log(
                "the identity was not confirmed — Revert is available; "
                "nothing was undone automatically"
            )

    def _apply_failed(self, message: str) -> None:
        self.rotation_panel.on_apply_failed(message)
        self.log(message)

    # ------------------------------------------------------------------ #

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        if self.rotation_panel.running:
            answer = QMessageBox.question(
                self, "Rotation is running",
                "Stop the rotation and close the window?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.rotation_panel.stop()
        save_settings(self.settings)
        self._inventory_watcher.stop()
        self._list_watcher.stop()
        if self._scan_worker is not None and self._scan_worker.isRunning():
            self._scan_worker.wait(2000)
        if self._verify_worker is not None and self._verify_worker.isRunning():
            self._verify_worker.wait(2000)
        self.catalog.close()
        super().closeEvent(event)
