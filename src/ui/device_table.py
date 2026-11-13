"""The device table: model, filter proxy and a small colour delegate.

Qt's ``QTableView`` plus a proxy model gives sorting, filtering and per-row
colouring cheaply — which is the whole reason this app is PySide6 rather than
customtkinter.

The model owns no data of its own: it reads the catalog's ``Device`` list, so a
refresh is "rebuild the list and reset the model".
"""

from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import QStyledItemDelegate

import macaddr
from catalog import Device
from scanner import proxy_macs
from vendor import short_vendor

COLUMNS = ("MAC", "Vendor", "Hostname", "IP", "Seen", "Status", "Pool")
COL_MAC, COL_VENDOR, COL_HOST, COL_IP, COL_SEEN, COL_STATUS, COL_POOL = range(7)

SCOPE_ASSIGNABLE = "assignable"
SCOPE_ALL = "all"
SCOPE_PROBLEM = "problems"
SCOPE_POOL = "pool"
SCOPE_INUSE = "in-use"

STATUS_TEXT = {
    macaddr.OK: "ok",
    macaddr.NEEDS_SEPARATOR: "fix separators",
    macaddr.NEEDS_SUBSTITUTION: "fix characters",
    macaddr.MULTICAST: "multicast",
    macaddr.UNRECOVERABLE: "broken",
}

COLOR_BROKEN = QColor("#c0392b")
COLOR_REPAIR = QColor("#b9770e")
COLOR_IN_USE = QColor("#1e8449")
COLOR_PROTECTED = QColor("#7f8c8d")
COLOR_POOL = QColor("#1f4e79")
COLOR_OWN = QColor("#6c3483")


def _short_time(value: str | None) -> str:
    if not value:
        return ""
    text = value.replace("T", " ")
    return text[:16] if len(text) > 16 else text


class DeviceTableModel(QAbstractTableModel):
    def __init__(self, devices: list[Device] | None = None, parent=None) -> None:
        super().__init__(parent)
        self._devices: list[Device] = devices or []
        self._seen: dict[str, dict] = {}
        self._proxies: set[str] = set()

    # -- Qt plumbing ------------------------------------------------------- #

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802 - Qt API
        return 0 if parent.isValid() else len(self._devices)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802 - Qt API
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        device = self._devices[index.row()]
        column = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            return self._display(device, column)
        if role == Qt.ItemDataRole.ToolTipRole:
            return self._tooltip(device)
        if role == Qt.ItemDataRole.ForegroundRole:
            return self._foreground(device)
        if role == Qt.ItemDataRole.FontRole:
            if device.pool_mac or device.protected or device.is_own:
                font = QFont()
                font.setBold(True)
                return font
            return None
        if role == Qt.ItemDataRole.UserRole:
            return device.id
        return None

    def _display(self, device: Device, column: int) -> str:
        if column == COL_MAC:
            return device.mac_norm or device.mac_raw
        if column == COL_VENDOR:
            return short_vendor(device.vendor)
        if column == COL_HOST:
            return device.hostname or ""
        if column == COL_IP:
            live = self._seen.get(device.mac_norm or "")
            return (live or {}).get("ip") or device.ip or ""
        if column == COL_SEEN:
            live = self._seen.get(device.mac_norm or "")
            if live:
                return "now"
            return _short_time(device.seen_at)
        if column == COL_STATUS:
            if device.is_own:
                return "YOU"
            if device.mac_norm in self._seen:
                return "IN USE"
            if device.mac_norm in self._proxies:
                return "proxy ARP"
            if device.status in (macaddr.UNRECOVERABLE, macaddr.MULTICAST):
                return STATUS_TEXT[device.status]
            if device.is_repair_status:
                return STATUS_TEXT[device.status]
            if device.protected:
                return "protected"
            return "absent"
        if column == COL_POOL:
            if device.pool_mac:
                return f"#{device.pool_mac_pos + 1}" if device.pool_mac_pos is not None else "yes"
            return ""
        return ""

    def _tooltip(self, device: Device) -> str:
        lines = [f"raw: {device.mac_raw}", f"status: {device.status}"]
        if device.repair_to:
            lines.append(
                f"repair -> {device.repair_to} ({device.repair_confidence or 'unknown'})"
            )
        if device.vendor:
            lines.append(f"vendor: {device.vendor}")
        if self._seen.get(device.mac_norm or ""):
            live = self._seen[device.mac_norm]
            lines.append(f"live: {live.get('ip', '?')} via {live.get('source', '?')}")
        if device.mac_norm in self._proxies:
            addresses = self._seen[device.mac_norm].get("ips") or []
            lines.append(
                f"proxy ARP: this MAC answered for {len(addresses)} addresses, so "
                "several devices hide behind it and a silent address is not proof "
                "of a free one"
            )
        if device.is_own:
            lines.append(
                "this machine's own address — the identity it carries right now"
            )
        if device.protected:
            lines.append("protected: never offered for rotation")
        if device.notes:
            lines.append(f"notes: {device.notes}")
        if device.source:
            lines.append(f"source: {device.source}")
        return "\n".join(lines)

    def _foreground(self, device: Device) -> QBrush | None:
        if device.is_own:
            return QBrush(COLOR_OWN)
        if device.mac_norm and device.mac_norm in self._seen:
            return QBrush(COLOR_IN_USE)
        if device.status in (macaddr.UNRECOVERABLE, macaddr.MULTICAST):
            return QBrush(COLOR_BROKEN)
        if device.is_repair_status:
            return QBrush(COLOR_REPAIR)
        if device.protected:
            return QBrush(COLOR_PROTECTED)
        if device.pool_mac:
            return QBrush(COLOR_POOL)
        return None

    # -- public API -------------------------------------------------------- #

    def set_devices(self, devices: list[Device]) -> None:
        self.beginResetModel()
        self._devices = list(devices)
        self.endResetModel()

    def set_seen(self, seen: dict[str, dict]) -> None:
        self._seen = seen or {}
        self._proxies = proxy_macs(self._seen)
        if self._devices:
            top = self.index(0, 0)
            bottom = self.index(len(self._devices) - 1, len(COLUMNS) - 1)
            self.dataChanged.emit(top, bottom)

    def devices(self) -> list[Device]:
        return list(self._devices)

    def device_at(self, row: int) -> Device | None:
        if 0 <= row < len(self._devices):
            return self._devices[row]
        return None


class DeviceFilterProxy(QSortFilterProxyModel):
    """Scope + free-text filtering, on top of the model."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setSortRole(Qt.ItemDataRole.DisplayRole)
        self._scope = SCOPE_ALL
        self._text = ""
        self.setDynamicSortFilter(True)

    def _refilter(self) -> None:
        """Re-run the filter.

        PySide6 6.12 marks invalidateFilter(), invalidateRowsFilter() and
        invalidateColumnsFilter() all as deprecated; plain invalidate() is not,
        and it re-runs both row and column filtering, which is a superset of what
        this proxy needs.
        """
        self.invalidate()

    def set_scope(self, scope: str) -> None:
        self._scope = scope
        self._refilter()

    def set_text(self, text: str) -> None:
        self._text = (text or "").strip().lower()
        self._refilter()

    def filterAcceptsRow(self, source_row: int, source_parent) -> bool:  # noqa: N802
        model = self.sourceModel()
        if model is None:
            return True
        device = model.device_at(source_row)
        if device is None:
            return False

        in_use = device.mac_norm in getattr(model, "_seen", {})
        if self._scope == SCOPE_ASSIGNABLE:
            if not device.ready or device.protected or in_use:
                return False
        elif self._scope == SCOPE_PROBLEM:
            if device.status == macaddr.OK and not device.repair_to:
                return False
        elif self._scope == SCOPE_POOL:
            if not device.pool_mac:
                return False
        elif self._scope == SCOPE_INUSE:
            if not in_use:
                return False

        if not self._text:
            return True
        haystack = " ".join(
            filter(None, [
                device.mac_norm or "",
                device.mac_raw,
                device.vendor or "",
                device.hostname or "",
                device.ip or "",
                device.notes or "",
            ])
        ).lower()
        return self._text in haystack


class StatusDelegate(QStyledItemDelegate):
    """Colour the status cell's background so the table scans at a glance."""

    STATUS_COLORS = {
        "YOU": QColor("#e8daef"),
        "IN USE": QColor("#d5f5e3"),
        "proxy ARP": QColor("#d4e6f1"),
        "absent": QColor("#f4f6f7"),
        "multicast": QColor("#fadbd8"),
        "broken": QColor("#fadbd8"),
        "fix separators": QColor("#fdebd0"),
        "fix characters": QColor("#fdebd0"),
        "protected": QColor("#eaecee"),
    }

    def paint(self, painter, option, index):  # noqa: D102 - Qt API
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        colour = self.STATUS_COLORS.get(str(text))
        if colour is not None:
            painter.save()
            painter.fillRect(option.rect, colour)
            painter.restore()
        super().paint(painter, option, index)
