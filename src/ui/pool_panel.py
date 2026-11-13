"""The rotation pool editor: ordered MAC list and hostname list.

This is the replacement for hand-editing ``mac-list.txt`` and
``hostnamelist.txt``.  Order is real data — ``mac-rotator.sh`` rotates in file
order — so both lists have move up/down and shuffle.

Membership is the catalog's, and the two text files are regenerated from it.
"""

from __future__ import annotations

import random

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton,
    QSpinBox, QVBoxLayout, QWidget,
)

import hostnames
import macaddr
from catalog import Catalog
from config import Settings, current_hostname
from vendor import get_vendor_db, short_vendor

WARN = "\u26a0"


class PoolPanel(QWidget):
    catalog_changed = Signal()
    regenerate_requested = Signal()
    status_message = Signal(str)

    def __init__(self, catalog: Catalog, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self.catalog = catalog
        self.settings = settings
        self._build()
        self.refresh()

    # ------------------------------------------------------------------ #

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)

        columns = QHBoxLayout()

        # -- MAC pool ------------------------------------------------------ #
        mac_box = QGroupBox("MAC rotation pool")
        mac_layout = QVBoxLayout(mac_box)
        self.mac_list = QListWidget()
        self.mac_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        mac_layout.addWidget(self.mac_list, 1)

        mac_buttons = QHBoxLayout()
        self.mac_add = QPushButton("New MAC…")
        self.mac_add.clicked.connect(self._add_new_mac)
        self.mac_remove = QPushButton("Remove")
        self.mac_remove.clicked.connect(self._remove_mac)
        self.mac_up = QPushButton("\u2191")
        self.mac_up.clicked.connect(lambda: self._move(self.mac_list, -1))
        self.mac_down = QPushButton("\u2193")
        self.mac_down.clicked.connect(lambda: self._move(self.mac_list, 1))
        self.mac_shuffle = QPushButton("Shuffle")
        self.mac_shuffle.clicked.connect(lambda: self._shuffle(self.mac_list))
        for widget in (self.mac_add, self.mac_remove):
            mac_buttons.addWidget(widget)
        mac_buttons.addStretch(1)
        for widget in (self.mac_up, self.mac_down, self.mac_shuffle):
            mac_buttons.addWidget(widget)
        mac_layout.addLayout(mac_buttons)
        self.mac_hint = QLabel("")
        self.mac_hint.setWordWrap(True)
        mac_layout.addWidget(self.mac_hint)
        columns.addWidget(mac_box, 1)

        # -- hostname pool -------------------------------------------------- #
        host_box = QGroupBox("Hostname rotation pool")
        host_layout = QVBoxLayout(host_box)
        self.hostname_list = QListWidget()
        self.hostname_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        host_layout.addWidget(self.hostname_list, 1)

        host_buttons = QHBoxLayout()
        self.host_add = QPushButton("Add…")
        self.host_add.clicked.connect(self._add_hostname)
        self.host_generate = QPushButton("Generate…")
        self.host_generate.clicked.connect(self._generate)
        self.host_remove = QPushButton("Remove")
        self.host_remove.clicked.connect(self._remove_hostname)
        host_buttons.addWidget(self.host_add)
        host_buttons.addWidget(self.host_generate)
        host_buttons.addWidget(self.host_remove)
        host_buttons.addStretch(1)
        self.host_up = QPushButton("\u2191")
        self.host_up.clicked.connect(lambda: self._move(self.hostname_list, -1))
        self.host_down = QPushButton("\u2193")
        self.host_down.clicked.connect(lambda: self._move(self.hostname_list, 1))
        self.host_shuffle = QPushButton("Shuffle")
        self.host_shuffle.clicked.connect(lambda: self._shuffle(self.hostname_list))
        for widget in (self.host_up, self.host_down, self.host_shuffle):
            host_buttons.addWidget(widget)
        host_layout.addLayout(host_buttons)
        self.host_hint = QLabel("")
        self.host_hint.setWordWrap(True)
        host_layout.addWidget(self.host_hint)
        columns.addWidget(host_box, 1)

        outer.addLayout(columns, 1)

        # -- settings ------------------------------------------------------- #
        settings_box = QGroupBox("Pairing and generated files")
        form = QFormLayout(settings_box)
        self.pairing_combo = QComboBox()
        self.pairing_combo.addItem(
            "Independent — each list wraps on its own length (mac-rotator behaviour)",
            "independent",
        )
        self.pairing_combo.addItem(
            "Paired — MAC[i] always with hostname[i], wrapping at the shorter list",
            "paired",
        )
        index = self.pairing_combo.findData(self.settings.pairing)
        self.pairing_combo.setCurrentIndex(max(0, index))
        self.pairing_combo.currentIndexChanged.connect(self._pairing_changed)
        form.addRow("Pairing:", self.pairing_combo)

        self.prefix_edit = QLineEdit(self.settings.hostname_prefix)
        self.prefix_edit.setPlaceholderText("usually empty")
        self.prefix_edit.editingFinished.connect(self._prefix_changed)
        form.addRow("Hostname prefix:", self.prefix_edit)

        self.files_label = QLabel("")
        self.files_label.setWordWrap(True)
        self.files_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow("Files:", self.files_label)

        buttons = QHBoxLayout()
        self.regenerate_button = QPushButton("Regenerate now")
        self.regenerate_button.clicked.connect(self.regenerate_requested.emit)
        buttons.addWidget(self.regenerate_button)
        buttons.addStretch(1)
        form.addRow(buttons)

        outer.addWidget(settings_box)

    # ------------------------------------------------------------------ #

    def refresh(self) -> None:
        self.mac_list.clear()
        for position, device in enumerate(self.catalog.pool_devices(), start=1):
            label = device.mac_norm or device.mac_raw
            vendor = short_vendor(device.vendor)
            text = f"{position}. {label}"
            if vendor:
                text += f"  ({vendor})"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, device.id)
            if device.is_own:
                text += f"   {WARN} this is you"
                item.setText(text)
                item.setToolTip(
                    "the address this machine carries right now — rotating to it "
                    "is the way back to your own identity"
                )
            if device.protected:
                item.setToolTip("protected — should not be in the rotation pool")
            self.mac_list.addItem(item)

        self.hostname_list.clear()
        live = current_hostname()
        fleet = {host.lower() for host in self.settings.fleet_hostnames}
        for position, row in enumerate(self.catalog.hostnames(pooled_only=True), start=1):
            name = row["name"]
            flags = []
            if name.lower() == live.lower():
                flags.append("in use now")
            if name.lower() in fleet:
                flags.append("used by another machine")
            text = f"{position}. {name}"
            if flags:
                text += f"   {WARN} " + ", ".join(flags)
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.hostname_list.addItem(item)

        self._update_hints()

    def _update_hints(self) -> None:
        macs = len(self.catalog.pool_macs())
        names = len(self.catalog.pool_hostnames())
        mode = self.settings.pairing
        if mode == "paired" and names:
            combinations = min(macs, names)
        elif names:
            combinations = macs * names
        else:
            combinations = macs
        self.mac_hint.setText(
            f"{macs} address(es). Only rows without a usable MAC or marked "
            f"protected are refused."
        )
        self.host_hint.setText(
            f"{names} name(s). Complete names are written out, so run "
            f"mac-rotator.sh with -p ''. Rotation: {combinations} combination(s)."
        )
        self.files_label.setText(
            f"{self.settings.mac_list_path}\n{self.settings.hostname_list_path}\n"
            f"rewritten on every change; last five versions kept as backups"
        )
        self.regenerate_button.setEnabled(True)

    # ------------------------------------------------------------------ #

    def _ask(self, title: str, label: str, text: str = "") -> str | None:
        value, ok = QInputDialog.getText(self, title, label, text=text)
        return value.strip() if ok and value.strip() else None

    def _add_new_mac(self) -> None:
        raw = self._ask("New MAC", "MAC address (colons, dashes or bare hex):")
        if not raw:
            return
        check = macaddr.check(raw)
        if not check.usable:
            QMessageBox.warning(
                self, "Not usable",
                f"{raw}\n\n{check.reason or 'not a usable MAC address'}",
            )
            return
        existing = self.catalog.device_by_mac(check.normalized)
        if existing:
            added, problems = self.catalog.add_to_pool([existing.id])
            if not added:
                QMessageBox.information(self, "Not added", "\n".join(problems))
        else:
            device_id, reason = self.catalog.add_device(
                check.normalized, source="pool", vendor=get_vendor_db().lookup(check.normalized)
            )
            if device_id:
                self.catalog.add_to_pool([device_id])
            else:
                QMessageBox.warning(self, "Not added", reason)
        self.refresh()
        self.catalog_changed.emit()

    def _remove_mac(self) -> None:
        ids = [item.data(Qt.ItemDataRole.UserRole) for item in self.mac_list.selectedItems()]
        if not ids:
            return
        removed = self.catalog.remove_from_pool(ids)
        self.status_message.emit(f"{removed} address(es) removed from the pool (still catalogued)")
        self.refresh()
        self.catalog_changed.emit()

    def _add_hostname(self) -> None:
        name = self._ask("Add hostname", "Hostname label:")
        if not name:
            return
        ok, message = self.catalog.add_hostname(name, source="manual", pool=True)
        if not ok:
            QMessageBox.warning(self, "Not added", message)
        else:
            self.status_message.emit(f"{name.lower()}: {message}")
        self.refresh()
        self.catalog_changed.emit()

    def _generate(self) -> None:
        count, ok = QInputDialog.getInt(self, "Generate hostnames", "How many?", 3, 1, 50)
        if not ok:
            return
        lan_names = [row["name"] for row in self.catalog.hostnames()]
        lan_names += [d.hostname for d in self.catalog.devices() if d.hostname]
        produced = hostnames.generate_batch(
            count, existing_names=self.catalog.hostname_names(),
            lan_names=list(dict.fromkeys(lan_names)),
        )
        added = 0
        for name in produced:
            success, _ = self.catalog.add_hostname(name, source="generated", pool=True)
            if success:
                added += 1
        self.status_message.emit(f"{added} generated hostname(s) added")
        self.refresh()
        self.catalog_changed.emit()

    def _remove_hostname(self) -> None:
        names = [item.data(Qt.ItemDataRole.UserRole) for item in self.hostname_list.selectedItems()]
        if not names:
            return
        answer = QMessageBox.question(
            self, "Remove from the pool?",
            "\n".join(names) + "\n\nThe name stays known to the catalog; only the "
            "rotation pool loses it.",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Ok:
            return
        for name in names:
            self.catalog.remove_hostname(name, from_pool_only=True)
        self.status_message.emit(f"{len(names)} hostname(s) removed from the pool")
        self.refresh()
        self.catalog_changed.emit()

    def _move(self, widget: QListWidget, delta: int) -> None:
        row = widget.currentRow()
        if row < 0:
            return
        target = row + delta
        if not 0 <= target < widget.count():
            return
        item = widget.takeItem(row)
        widget.insertItem(target, item)
        widget.setCurrentRow(target)
        self._commit_order(widget)

    def _shuffle(self, widget: QListWidget) -> None:
        if widget.count() < 2:
            return
        items = [widget.item(index) for index in range(widget.count())]
        random.shuffle(items)
        widget.clear()
        for item in items:
            widget.addItem(item)
        self._commit_order(widget)
        self.status_message.emit("order shuffled")

    def _commit_order(self, widget: QListWidget) -> None:
        payload = [widget.item(i).data(Qt.ItemDataRole.UserRole) for i in range(widget.count())]
        if widget is self.mac_list:
            self.catalog.reorder_pool(payload)
        else:
            self.catalog.reorder_hostnames(payload)
        self.refresh()
        self.catalog_changed.emit()

    def _pairing_changed(self, index: int) -> None:
        self.settings.pairing = self.pairing_combo.itemData(index) or "independent"
        self._update_hints()
        self.catalog_changed.emit()

    def _prefix_changed(self) -> None:
        self.settings.hostname_prefix = self.prefix_edit.text().strip().lower()
        self._update_hints()
        self.catalog_changed.emit()
