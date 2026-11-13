"""Per-device detail editor: repair, hostname suggestion, protect, pool, apply.

Everything here is a catalog edit or an unprivileged preview until the user
presses Apply.  The panel never guesses silently: a repair is shown as
before -> after with a confidence label, and a repair that would produce a
multicast address is refused outright by ``macaddr``.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QTextBrowser,
    QVBoxLayout, QWidget,
)

import applier
import hostnames
import macaddr
from catalog import Catalog, Device
from config import Settings, current_hostname, own_mac
from vendor import get_vendor_db, short_vendor


class DetailPanel(QWidget):
    catalog_changed = Signal()
    copy_requested = Signal(str, str)          # mac, hostname
    apply_requested = Signal(str, str)         # mac, hostname (hostname may be "")
    status_message = Signal(str)

    def __init__(self, catalog: Catalog, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self.catalog = catalog
        self.settings = settings
        self._device: Device | None = None
        self._build()

    # ------------------------------------------------------------------ #

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)

        self.title = QLabel("No device selected")
        title_font = QFont()
        title_font.setPointSize(title_font.pointSize() + 2)
        title_font.setBold(True)
        self.title.setFont(title_font)
        self.title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        outer.addWidget(self.title)

        self.subtitle = QLabel("")
        self.subtitle.setWordWrap(True)
        outer.addWidget(self.subtitle)

        # -- repair -------------------------------------------------------- #
        self.repair_box = QGroupBox("Repair")
        repair_layout = QVBoxLayout(self.repair_box)
        self.repair_text = QLabel("")
        self.repair_text.setWordWrap(True)
        self.repair_text.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        repair_layout.addWidget(self.repair_text)

        repair_buttons = QHBoxLayout()
        self.accept_button = QPushButton("Use this address")
        self.accept_button.clicked.connect(self._accept_repair)
        self.reject_button = QPushButton("Reject")
        self.reject_button.clicked.connect(self._reject_repair)
        self.manual_edit = QLineEdit()
        self.manual_edit.setPlaceholderText("type the correct MAC…")
        self.manual_button = QPushButton("Set")
        self.manual_button.clicked.connect(self._set_manual)
        repair_buttons.addWidget(self.accept_button)
        repair_buttons.addWidget(self.reject_button)
        repair_buttons.addWidget(self.manual_edit, 1)
        repair_buttons.addWidget(self.manual_button)
        repair_layout.addLayout(repair_buttons)
        outer.addWidget(self.repair_box)

        # -- hostname ------------------------------------------------------ #
        name_box = QGroupBox("Hostname")
        name_layout = QVBoxLayout(name_box)
        row = QHBoxLayout()
        self.hostname_edit = QLineEdit()
        self.hostname_edit.setPlaceholderText("hostname for this device")
        self.hostname_edit.editingFinished.connect(self._save_hostname)
        self.suggest_button = QPushButton("Suggest…")
        self.suggest_button.clicked.connect(self._show_suggestions)
        row.addWidget(self.hostname_edit, 1)
        row.addWidget(self.suggest_button)
        name_layout.addLayout(row)

        self.hostname_note = QLabel("")
        self.hostname_note.setWordWrap(True)
        name_layout.addWidget(self.hostname_note)
        outer.addWidget(name_box)

        # -- flags --------------------------------------------------------- #
        flags = QGroupBox("Marking")
        flags_layout = QFormLayout(flags)
        self.protect_check = QCheckBox("Protected — never offer for rotation")
        self.protect_check.toggled.connect(self._toggle_protect)
        flags_layout.addRow(self.protect_check)
        self.notes_edit = QLineEdit()
        self.notes_edit.setPlaceholderText("free-form note, kept across imports")
        self.notes_edit.editingFinished.connect(self._save_notes)
        flags_layout.addRow("Notes:", self.notes_edit)
        self.vendor_label = QLabel("")
        flags_layout.addRow("Vendor:", self.vendor_label)
        self.source_label = QLabel("")
        flags_layout.addRow("Source:", self.source_label)
        outer.addWidget(flags)

        # -- actions ------------------------------------------------------- #
        actions = QHBoxLayout()
        self.pool_button = QPushButton("Add to MAC pool")
        self.pool_button.clicked.connect(self._toggle_pool)
        self.copy_button = QPushButton("Copy commands")
        self.copy_button.clicked.connect(self._copy)
        self.apply_button = QPushButton("Apply…")
        self.apply_button.clicked.connect(self._apply)
        self.delete_button = QPushButton("Delete from catalog")
        self.delete_button.clicked.connect(self._delete)
        actions.addWidget(self.pool_button)
        actions.addStretch(1)
        actions.addWidget(self.delete_button)
        actions.addWidget(self.copy_button)
        actions.addWidget(self.apply_button)
        outer.addLayout(actions)

        # -- preview ------------------------------------------------------- #
        preview_box = QGroupBox("Preview")
        preview_layout = QVBoxLayout(preview_box)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFont(QFont("monospace", 9))
        self.preview.setMinimumHeight(120)
        preview_layout.addWidget(self.preview)
        outer.addWidget(preview_box, 1)

        self.identity_note = QTextBrowser()
        self.identity_note.setMaximumHeight(70)
        outer.addWidget(self.identity_note)

        self._set_enabled(False)

    def _set_enabled(self, enabled: bool) -> None:
        for widget in (
            self.hostname_edit, self.suggest_button, self.protect_check,
            self.notes_edit, self.pool_button, self.copy_button,
            self.apply_button, self.delete_button, self.manual_edit,
            self.manual_button,
        ):
            widget.setEnabled(enabled)

    # ------------------------------------------------------------------ #

    def show_device(self, device: Device | None) -> None:
        self._device = device
        if device is None:
            self.title.setText("No device selected")
            self.subtitle.setText("")
            self.repair_box.setVisible(False)
            self.preview.clear()
            self.identity_note.clear()
            self._set_enabled(False)
            return

        self._set_enabled(True)
        self.title.setText(device.mac_norm or device.mac_raw)
        details = [f"status: {device.status}"]
        if device.ip:
            details.append(f"last IP {device.ip}")
        if device.seen_at:
            details.append(f"seen {device.seen_at}")
        if device.source:
            details.append(f"from {device.source}")
        self.subtitle.setText("  ·  ".join(details))

        self.hostname_edit.setText(device.hostname or "")
        self.protect_check.blockSignals(True)
        self.protect_check.setChecked(device.protected)
        self.protect_check.blockSignals(False)
        self.notes_edit.setText(device.notes)
        self.vendor_label.setText(short_vendor(device.vendor) or "(unknown prefix)")
        self.source_label.setText(device.source or "")

        self._update_repair_box(device)
        self._update_pool_button(device)
        self._update_hostname_note()
        self._update_preview()
        self._update_identity_note(device)

    def _update_repair_box(self, device: Device) -> None:
        if device.status == macaddr.OK:
            self.repair_box.setVisible(False)
            return
        self.repair_box.setVisible(True)
        self.accept_button.setVisible(bool(device.repair_to))
        if device.repair_to:
            confidence = (device.repair_confidence or "unknown").upper()
            self.repair_text.setText(
                f"{device.mac_raw}\n"
                f"  \u2192  {device.repair_to}   [{confidence} confidence]\n\n"
                + (
                    "Only separators or whitespace were wrong."
                    if device.status == macaddr.NEEDS_SEPARATOR
                    else "Characters that cannot be hex digits were substituted "
                         "(O\u21920, S\u21925, I\u21921, t\u21927, G\u21926, \u00a3\u2192E). "
                         "Verify against the device before using it."
                )
            )
        elif device.status == macaddr.MULTICAST:
            self.repair_text.setText(
                f"{device.mac_raw}\n\nThis is a multicast or broadcast address. "
                "A network card cannot take it — it is not a device identity."
            )
        else:
            self.repair_text.setText(
                f"{device.mac_raw}\n\nNot recoverable automatically: "
                + (device.notes or "")
                + "\nType the correct address if you have it."
            )

    def _update_pool_button(self, device: Device) -> None:
        if device.pool_mac:
            self.pool_button.setText("Remove from MAC pool")
        else:
            self.pool_button.setText("Add to MAC pool")
        self.pool_button.setEnabled(device.ready and (device.pool_mac or not device.protected))
        self.delete_button.setEnabled(not device.pool_mac and not device.is_own)
        self.apply_button.setEnabled(bool(device.effective_mac))

    def _update_hostname_note(self, text: str = "") -> None:
        device = self._device
        if device is None:
            self.hostname_note.setText("")
            return
        name = self.hostname_edit.text().strip().lower()
        if not name:
            self.hostname_note.setText("No hostname set — use Suggest… for offline candidates.")
            return
        valid, reason = hostnames.validate(name, self.settings.hostname_prefix)
        notes = hostnames.warnings(
            name, current_hostname(), set(self.catalog.hostname_names()),
            fleet=self.settings.fleet_hostnames,
        )
        parts = []
        if not valid:
            parts.append(f"⚠ invalid: {reason}")
        elif notes:
            parts.append("⚠ " + "; ".join(notes))
        else:
            applied = hostnames.normalize(name, self.settings.hostname_prefix)
            if applied and applied != name:
                parts.append(f"will be applied as {applied}")
            else:
                parts.append("valid")
        if text:
            parts.append(text)
        self.hostname_note.setText("  ·  ".join(parts))

    def _update_preview(self) -> None:
        device = self._device
        if device is None or not device.effective_mac:
            self.preview.setPlainText("No usable address to preview.")
            return
        name = self.hostname_edit.text().strip() or None
        self.preview.setPlainText(
            applier.copy_text(
                self.settings, device.effective_mac, name,
                own_mac(self.settings.iface), current_hostname(),
            )
        )

    def _update_identity_note(self, device: Device) -> None:
        lines = []
        live_mac = own_mac(self.settings.iface)
        live_host = current_hostname()
        if device.is_own or (device.mac_norm and live_mac == device.mac_norm):
            lines.append(
                f"<b>This is the address {self.settings.iface} is using right now.</b> "
                "Rotating to it is the way back to your own identity, so it stays "
                "usable — it is only flagged, never withheld."
            )
        if device.hostname and device.hostname.lower() == live_host.lower():
            lines.append(f"<b>{device.hostname}</b> is this machine's current hostname.")
        bundle = get_vendor_db().lookup(device.mac_norm or device.mac_raw)
        if bundle and not device.vendor:
            lines.append(f"Vendor from the local OUI table: {bundle}")
        self.identity_note.setHtml("<br>".join(lines))

    # ------------------------------------------------------------------ #
    # actions
    # ------------------------------------------------------------------ #

    def _refresh(self) -> None:
        self.catalog_changed.emit()
        if self._device:
            self.show_device(self.catalog.device(self._device.id))

    def _accept_repair(self) -> None:
        if not self._device:
            return
        if self.catalog.accept_repair(self._device.id):
            self.status_message.emit("repair accepted")
        else:
            self.status_message.emit("could not accept: another row already uses that address")
        self._refresh()

    def _reject_repair(self) -> None:
        if not self._device:
            return
        self.catalog.reject_repair(self._device.id)
        self.status_message.emit("repair rejected — the row now needs a manual address")
        self._refresh()

    def _set_manual(self) -> None:
        if not self._device:
            return
        raw = self.manual_edit.text().strip()
        if not raw:
            return
        ok, detail = self.catalog.set_manual_mac(self._device.id, raw)
        if ok:
            self.manual_edit.clear()
            self.status_message.emit(f"set to {detail}")
        else:
            QMessageBox.warning(self, "Not a usable MAC", detail)
        self._refresh()

    def _save_hostname(self) -> None:
        if not self._device:
            return
        name = self.hostname_edit.text().strip()
        if name:
            valid, reason = hostnames.validate(name, self.settings.hostname_prefix)
            if not valid:
                self._update_hostname_note()
                return
            name = hostnames.normalize(name, self.settings.hostname_prefix) or name
        self.catalog.update_device(
            self._device.id,
            hostname=name or None,
            hostname_source="manual" if name else None,
        )
        self._device = self.catalog.device(self._device.id)
        self._update_hostname_note("saved")
        self._update_preview()
        self.catalog_changed.emit()

    def _show_suggestions(self) -> None:
        device = self._device
        if device is None:
            return
        lan_names = [d.hostname for d in self.catalog.devices() if d.hostname]
        lan_names += self.catalog.hostname_names()
        candidates = hostnames.suggest(
            mac=device.mac_norm,
            vendor=device.vendor or get_vendor_db().lookup(device.mac_norm),
            existing_names=self.catalog.hostname_names(),
            lan_names=list(dict.fromkeys(lan_names)),
        )
        if not candidates:
            QMessageBox.information(
                self, "No suggestions",
                "Nothing plausible could be derived. Type a name instead.",
            )
            return

        menu = QMenu(self)
        for item in candidates:
            action = menu.addAction(item.display)
            action.setData(item.name)
        chosen = menu.exec(self.suggest_button.mapToGlobal(
            self.suggest_button.rect().bottomLeft()
        ))
        if chosen is None:
            return
        name = chosen.data()
        self.hostname_edit.setText(name)
        self._save_hostname()
        self.status_message.emit(f"hostname suggested: {name}")

    def _toggle_protect(self, checked: bool) -> None:
        if not self._device:
            return
        self.catalog.protect(self._device.id, checked)
        self._refresh()

    def _save_notes(self) -> None:
        if not self._device:
            return
        self.catalog.update_device(self._device.id, notes=self.notes_edit.text().strip())
        self.catalog_changed.emit()

    def _toggle_pool(self) -> None:
        if not self._device:
            return
        if self._device.pool_mac:
            self.catalog.remove_from_pool([self._device.id])
            self.status_message.emit("removed from the MAC pool")
        else:
            added, problems = self.catalog.add_to_pool([self._device.id])
            if added:
                self.status_message.emit("added to the MAC pool")
            else:
                QMessageBox.warning(self, "Not added", "\n".join(problems) or "already pooled")
        self._refresh()

    def _copy(self) -> None:
        if not self._device or not self._device.effective_mac:
            return
        name = self.hostname_edit.text().strip()
        self.copy_requested.emit(self._device.effective_mac, name)
        self.status_message.emit("commands copied to the clipboard")

    def _apply(self) -> None:
        if not self._device or not self._device.effective_mac:
            return
        name = self.hostname_edit.text().strip()
        answer = QMessageBox.question(
            self, "Apply this identity?",
            f"Interface: {self.settings.iface}\n"
            f"MAC:       {self._device.effective_mac}\n"
            f"Hostname:  {name or '(unchanged)'}\n\n"
            f"A revert script will be written first, then pkexec will ask for "
            f"authorisation.\n\n"
            + ("This is a DRY RUN: nothing will be changed."
               if self.settings.dry_run else "The link will drop for about "
               f"{self.settings.down_wait}s."),
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Ok:
            return
        self.apply_requested.emit(self._device.effective_mac, name)

    def _delete(self) -> None:
        device = self._device
        if device is None:
            return
        if device.pool_mac:
            QMessageBox.information(
                self, "Still in the pool",
                "Remove it from the MAC pool first, then delete it.",
            )
            return
        if device.is_own:
            QMessageBox.information(
                self, "That is this machine",
                f"{device.mac_norm} is the address this interface carries right "
                "now, so it is never deleted. Rotate to another address first and "
                "it becomes an ordinary row again.",
            )
            return
        answer = QMessageBox.warning(
            self, "Delete from the catalog?",
            f"{device.mac_norm or device.mac_raw}\n\n"
            "Its notes, repair decision and history go with it. "
            "This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if self.catalog.delete_device(device.id):
            self.status_message.emit("device deleted")
            self._device = None
            self.show_device(None)
        else:
            QMessageBox.warning(self, "Not deleted", "It is still in the rotation pool.")
        self.catalog_changed.emit()
