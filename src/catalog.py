"""The catalog: one SQLite database that owns everything the app knows.

The two text files that ``mac-rotator.sh`` reads are **generated** from here, so
membership and order of both rotation pools are database state, not text
editing.  The collector's CSV is imported into this catalog but never written
back to — it stays the append-only evidence trail.

Re-importing is always safe: an existing row keeps its notes, protect flag,
repair decision and pool position, and only fills in fields that are empty.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import hostnames
import macaddr


def _lookup_vendor(mac: str | None) -> str | None:
    """Offline OUI name for a MAC, or ``None``.

    Resolved at insert time so the vendor column is populated the moment a list
    is imported, rather than only after a scan or after each row is opened.  The
    lookup reads a local file only - see vendor.py.
    """
    if not mac:
        return None
    try:
        from vendor import get_vendor_db

        return get_vendor_db().lookup(mac)
    except Exception:  # noqa: BLE001 - a missing OUI file must not block an import
        return None

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS devices (
    id                INTEGER PRIMARY KEY,
    mac_norm          TEXT,
    mac_raw           TEXT NOT NULL,
    status            TEXT NOT NULL,
    repair_to         TEXT,
    repair_confidence TEXT,
    vendor            TEXT,
    hostname          TEXT,
    hostname_source   TEXT,
    ip                TEXT,
    seen_at           TEXT,
    protected         INTEGER NOT NULL DEFAULT 0,
    is_own            INTEGER NOT NULL DEFAULT 0,
    notes             TEXT NOT NULL DEFAULT '',
    source            TEXT,
    pool_mac          INTEGER NOT NULL DEFAULT 0,
    pool_mac_pos      INTEGER,
    last_verified_at  TEXT,
    verify_state      TEXT,
    verify_note       TEXT,
    created_at        TEXT,
    updated_at        TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS devices_mac_norm
    ON devices(mac_norm) WHERE mac_norm IS NOT NULL;

CREATE TABLE IF NOT EXISTS hostnames (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    source     TEXT,
    pool       INTEGER NOT NULL DEFAULT 0,
    pool_pos   INTEGER,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS applies (
    id          INTEGER PRIMARY KEY,
    mac         TEXT,
    hostname    TEXT,
    iface       TEXT,
    applied_at  TEXT,
    prev_mac    TEXT,
    prev_host   TEXT,
    revert_path TEXT,
    result      TEXT,
    detail      TEXT
);

CREATE TABLE IF NOT EXISTS scans (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT,
    finished_at TEXT,
    iface       TEXT,
    in_use      INTEGER,
    seen_json   TEXT
);
"""

#: Fields a re-import is allowed to fill in when they are empty.
_FILLABLE = ("hostname", "ip", "seen_at", "vendor")

def _parse_instant(value: str | None) -> datetime | None:
    """The instant an ISO-8601 timestamp names, or ``None`` if it names none.

    Only an offset-carrying value is usable: without one the instant is
    ambiguous, and comparing it with an aware value raises.  Anything else is
    kept verbatim in the row and simply never decides an order.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class Device:
    id: int
    mac_norm: str | None
    mac_raw: str
    status: str
    repair_to: str | None
    repair_confidence: str | None
    vendor: str | None
    hostname: str | None
    hostname_source: str | None
    ip: str | None
    seen_at: str | None
    protected: bool
    is_own: bool
    notes: str
    source: str | None
    pool_mac: bool
    pool_mac_pos: int | None
    last_verified_at: str | None = None
    verify_state: str | None = None
    verify_note: str | None = None

    @property
    def effective_mac(self) -> str | None:
        """The address that would be used.  Only a confirmed row has one.

        A proposed repair is *not* an identity: it stays in ``repair_to`` until a
        human accepts it, which is what keeps the app from silently rotating to
        an address it guessed.
        """
        if self.status == macaddr.OK:
            return self.mac_norm
        return None

    @property
    def ready(self) -> bool:
        """Usable for rotating: confirmed, assignable, not protected."""
        return self.status == macaddr.OK and bool(self.mac_norm)

    @property
    def is_repair_status(self) -> bool:
        return self.status in macaddr.REPAIR_STATUSES


@dataclass
class ImportReport:
    added: int = 0
    updated: int = 0
    duplicates: int = 0
    broken: int = 0
    skipped_rows: int = 0
    hostnames_added: int = 0
    messages: list[str] = field(default_factory=list)

    def summary(self) -> str:
        bits = [f"{self.added} new", f"{self.updated} updated"]
        if self.duplicates:
            bits.append(f"{self.duplicates} duplicate")
        if self.broken:
            bits.append(f"{self.broken} broken")
        if self.hostnames_added:
            bits.append(f"{self.hostnames_added} hostname(s)")
        if self.skipped_rows:
            bits.append(f"{self.skipped_rows} row(s) skipped")
        return ", ".join(bits)


class Catalog:
    """Thin, explicit wrapper over the SQLite file. No ORM, no magic."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Bring an older database up to the current schema.

        ``CREATE TABLE IF NOT EXISTS`` leaves an existing file alone, so a column
        added to SCHEMA never appears in a catalog that predates it.  Each step is
        additive and idempotent: check for the column, add it if it is missing.
        """
        columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(devices)").fetchall()
        }
        if "is_own" not in columns:
            self.conn.execute(
                "ALTER TABLE devices ADD COLUMN is_own INTEGER NOT NULL DEFAULT 0"
            )
        # Phase D: verification evidence lives beside the device, so a verdict
        # survives a restart and a rescan does not overwrite it.
        for name, decl in (
            ("last_verified_at", "TEXT"),
            ("verify_state", "TEXT"),
            ("verify_note", "TEXT"),
        ):
            if name not in columns:
                self.conn.execute(
                    f"ALTER TABLE devices ADD COLUMN {name} {decl}"
                )

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    # -- meta ------------------------------------------------------------- #

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    # -- devices ---------------------------------------------------------- #

    def devices(self) -> list[Device]:
        rows = self.conn.execute(
            "SELECT * FROM devices ORDER BY "
            "CASE WHEN pool_mac = 1 THEN 0 ELSE 1 END, pool_mac_pos, id"
        ).fetchall()
        return [_device(row) for row in rows]

    def device(self, device_id: int) -> Device | None:
        row = self.conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
        return _device(row) if row else None

    def device_by_mac(self, mac: str) -> Device | None:
        # ``normalize()`` refuses unassignable addresses; the catalog still stores
        # them (a multicast row can exist), so look up the parsed form instead.
        canon = macaddr.check(mac).normalized
        if not canon:
            return None
        row = self.conn.execute(
            "SELECT * FROM devices WHERE mac_norm = ?", (canon,)
        ).fetchone()
        return _device(row) if row else None

    def device_by_raw(self, raw: str) -> Device | None:
        row = self.conn.execute(
            "SELECT * FROM devices WHERE mac_raw = ?", ((raw or "").strip(),)
        ).fetchone()
        return _device(row) if row else None

    def add_device(
        self,
        raw: str,
        hostname: str | None = None,
        ip: str | None = None,
        seen_at: str | None = None,
        source: str = "manual",
        vendor: str | None = None,
        notes: str = "",
    ) -> tuple[int | None, str]:
        """Insert one row. Returns ``(id, reason)``; id is None on rejection."""
        check = macaddr.check(raw)
        canon = check.normalized
        if canon:
            existing = self.device_by_mac(canon)
            if existing:
                return existing.id, "already in the catalog"
        else:
            # An unrecoverable row has no canonical form, so the raw text is the
            # only identity it has.  Without this, every re-import appends another
            # copy of the broken row.
            existing = self.device_by_raw(raw)
            if existing:
                return existing.id, "already in the catalog"
        stamp = now()
        if vendor is None:
            vendor = _lookup_vendor(canon)
        cur = self.conn.execute(
            "INSERT INTO devices(mac_norm, mac_raw, status, repair_to, repair_confidence,"
            " vendor, hostname, hostname_source, ip, seen_at, notes, source,"
            " created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                canon,
                raw.strip(),
                check.status,
                canon if check.needs_repair else None,
                check.confidence if check.needs_repair else None,
                vendor,
                hostname or None,
                "observed" if hostname else None,
                ip or None,
                seen_at or None,
                notes,
                source,
                stamp,
                stamp,
            ),
        )
        self.conn.commit()
        return cur.lastrowid, "added"

    def update_device(self, device_id: int, **fields) -> None:
        allowed = {
            "mac_norm", "mac_raw", "status", "repair_to", "repair_confidence",
            "vendor", "hostname", "hostname_source", "ip", "seen_at",
            "protected", "is_own", "notes", "source", "pool_mac", "pool_mac_pos",
            "last_verified_at", "verify_state", "verify_note",
        }
        payload = {k: v for k, v in fields.items() if k in allowed}
        if not payload:
            return
        payload["updated_at"] = now()
        assignments = ", ".join(f"{name} = ?" for name in payload)
        self.conn.execute(
            f"UPDATE devices SET {assignments} WHERE id = ?",
            (*payload.values(), device_id),
        )
        self.conn.commit()

    def delete_device(self, device_id: int) -> bool:
        row = self.conn.execute(
            "SELECT pool_mac, is_own FROM devices WHERE id = ?", (device_id,)
        ).fetchone()
        if row is None:
            return False
        if row["pool_mac"]:
            return False  # the caller must remove it from the pool first
        if row["is_own"]:
            return False  # this machine's own address is not a deletable row
        self.conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))
        self.conn.commit()
        return True

    def accept_repair(self, device_id: int) -> bool:
        """Promote a proposed repair into the row's canonical address."""
        device = self.device(device_id)
        if device is None or not device.repair_to:
            return False
        target = device.repair_to
        if self.device_by_mac(target) and self.device_by_mac(target).id != device_id:
            return False
        self.update_device(
            device_id,
            mac_norm=target,
            status=macaddr.OK,
            repair_to=None,
            repair_confidence=macaddr.HIGH,
        )
        return True

    def reject_repair(self, device_id: int) -> None:
        self.update_device(
            device_id, repair_to=None, repair_confidence=macaddr.NONE,
            status=macaddr.UNRECOVERABLE,
        )

    def set_manual_mac(self, device_id: int, raw: str) -> tuple[bool, str]:
        """Type in a replacement for an unrecoverable row."""
        check = macaddr.check(raw)
        if not check.usable:
            return False, f"{raw!r}: {check.reason or 'not a usable MAC'}"
        clash = self.device_by_mac(check.normalized)
        if clash and clash.id != device_id:
            return False, f"already catalogued as device #{clash.id}"
        self.update_device(
            device_id,
            mac_norm=check.normalized,
            mac_raw=raw.strip(),
            status=macaddr.OK,
            repair_to=None,
            repair_confidence=macaddr.HIGH,
        )
        return True, check.normalized

    def protect(self, device_id: int, protected: bool) -> None:
        self.update_device(device_id, protected=1 if protected else 0)

    # -- the machine's own identity --------------------------------------- #

    def live_identity(self) -> Device | None:
        row = self.conn.execute(
            "SELECT * FROM devices WHERE is_own = 1 ORDER BY id LIMIT 1"
        ).fetchone()
        return _device(row) if row else None

    def mark_live_identity(
        self, own_mac: str | None, own_hostname: str | None = None, *, pool: bool = True
    ) -> tuple[int | None, list[str]]:
        """Record which row is the address this machine carries right now.

        The flag is *not* protection: your own address is a legitimate rotation
        target ("be yourself again"), so it must stay usable.  What it buys is
        visibility - the pool and the table can say "this is you" instead of
        leaving the most important row in the catalog indistinguishable from a
        neighbour's.

        The row is created when the catalog has never seen it and (by default)
        put in the MAC pool, so returning to your own identity is always one
        entry away.  Re-running is idempotent; the flag moves to whichever row
        the interface now reports.
        """
        notes: list[str] = []
        check = macaddr.check(own_mac) if own_mac else None
        canon = check.normalized if check and check.usable else None
        if not canon:
            # Loopback reports 00:00:00:00:00:00, which normalizes to nothing, and
            # an interface without a carrier reports nothing at all.  Clearing the
            # flag here would throw away a known identity on a transient failure.
            return None, notes

        device = self.device_by_mac(canon)
        if device is None:
            device_id, reason = self.add_device(
                canon,
                hostname=own_hostname or None,
                source="live",
                notes="this machine's own address",
            )
            if device_id is None:
                notes.append(f"could not record {canon}: {reason}")
                return None, notes
            device = self.device(device_id)
            if device is not None:
                notes.append(f"recorded this machine's own address {canon}")
        if device is None:
            return None, notes

        if not device.is_own:
            # Exactly one row carries the flag: the interface has one address.
            self.conn.execute(
                "UPDATE devices SET is_own = 0 WHERE is_own = 1 AND id <> ?",
                (device.id,),
            )
            self.update_device(device.id, is_own=1)
            device = self.device(device.id) or device

        if own_hostname and device.hostname != own_hostname:
            self.update_device(device.id, hostname=own_hostname, hostname_source="live")

        if pool and not device.pool_mac:
            added, problems = self.add_to_pool([device.id])
            if added:
                notes.append(f"added your own address {canon} to the MAC pool")
            elif problems:
                notes.append(f"own address not pooled: {problems[0]}")
        return device.id, notes

    # -- rotation pool: MACs ---------------------------------------------- #

    def pool_devices(self) -> list[Device]:
        return [d for d in self.devices() if d.pool_mac]

    def pool_macs(self) -> list[str]:
        return [d.mac_norm for d in self.pool_devices() if d.mac_norm]

    def add_to_pool(self, device_ids: list[int]) -> tuple[int, list[str]]:
        position = self._next_mac_position()
        added = 0
        problems: list[str] = []
        for device_id in device_ids:
            device = self.device(device_id)
            if device is None:
                problems.append(f"#{device_id}: no longer exists")
                continue
            if device.pool_mac:
                problems.append(f"{device.mac_norm or device.mac_raw}: already in the pool")
                continue
            if not device.ready:
                problems.append(f"{device.mac_raw}: not a usable address")
                continue
            if device.protected:
                problems.append(f"{device.mac_norm}: protected")
                continue
            self.update_device(device_id, pool_mac=1, pool_mac_pos=position)
            position += 1
            added += 1
        return added, problems

    def remove_from_pool(self, device_ids: list[int]) -> int:
        removed = 0
        for device_id in device_ids:
            device = self.device(device_id)
            if device and device.pool_mac:
                self.update_device(device_id, pool_mac=0, pool_mac_pos=None)
                removed += 1
        self._resequence_macs()
        return removed

    def reorder_pool(self, device_ids: list[int]) -> None:
        for position, device_id in enumerate(device_ids):
            self.update_device(device_id, pool_mac=1, pool_mac_pos=position)
        self._resequence_macs()

    def _next_mac_position(self) -> int:
        row = self.conn.execute(
            "SELECT MAX(pool_mac_pos) AS top FROM devices WHERE pool_mac = 1"
        ).fetchone()
        return (row["top"] + 1) if row and row["top"] is not None else 0

    def _resequence_macs(self) -> None:
        rows = self.conn.execute(
            "SELECT id FROM devices WHERE pool_mac = 1 ORDER BY pool_mac_pos, id"
        ).fetchall()
        for position, row in enumerate(rows):
            self.conn.execute(
                "UPDATE devices SET pool_mac_pos = ? WHERE id = ?", (position, row["id"])
            )
        self.conn.commit()

    # -- rotation pool: hostnames ----------------------------------------- #

    def hostnames(self, pooled_only: bool = False) -> list[sqlite3.Row]:
        query = "SELECT * FROM hostnames"
        if pooled_only:
            query += " WHERE pool = 1"
        query += " ORDER BY CASE WHEN pool = 1 THEN 0 ELSE 1 END, pool_pos, name"
        return self.conn.execute(query).fetchall()

    def hostname_names(self) -> list[str]:
        return [row["name"] for row in self.hostnames()]

    def pool_hostnames(self) -> list[str]:
        return [row["name"] for row in self.hostnames(pooled_only=True)]

    def add_hostname(self, name: str, source: str = "manual", pool: bool = True) -> tuple[bool, str]:
        cleaned = name.strip().lower()
        if not cleaned:
            return False, "empty name"
        valid, reason = hostnames.validate(cleaned)
        if not valid:
            return False, reason
        existing = self.conn.execute(
            "SELECT * FROM hostnames WHERE name = ?", (cleaned,)
        ).fetchone()
        if existing:
            if pool and not existing["pool"]:
                self.conn.execute(
                    "UPDATE hostnames SET pool = 1, pool_pos = ? WHERE id = ?",
                    (self._next_hostname_position(), existing["id"]),
                )
                self.conn.commit()
                return True, "already known, added to the pool"
            return False, "already in the hostname list"
        self.conn.execute(
            "INSERT INTO hostnames(name, source, pool, pool_pos, created_at) VALUES(?,?,?,?,?)",
            (cleaned, source, 1 if pool else 0,
             self._next_hostname_position() if pool else None, now()),
        )
        self.conn.commit()
        return True, "added"

    def remove_hostname(self, name: str, from_pool_only: bool = True) -> bool:
        row = self.conn.execute(
            "SELECT * FROM hostnames WHERE name = ?", (name.lower(),)
        ).fetchone()
        if row is None:
            return False
        if from_pool_only:
            self.conn.execute(
                "UPDATE hostnames SET pool = 0, pool_pos = NULL WHERE id = ?", (row["id"],)
            )
        else:
            self.conn.execute("DELETE FROM hostnames WHERE id = ?", (row["id"],))
        self.conn.commit()
        self._resequence_hostnames()
        return True

    def reorder_hostnames(self, names: list[str]) -> None:
        for position, name in enumerate(names):
            self.conn.execute(
                "UPDATE hostnames SET pool = 1, pool_pos = ? WHERE name = ?",
                (position, name.lower()),
            )
        self.conn.commit()
        self._resequence_hostnames()

    def _next_hostname_position(self) -> int:
        row = self.conn.execute(
            "SELECT MAX(pool_pos) AS top FROM hostnames WHERE pool = 1"
        ).fetchone()
        return (row["top"] + 1) if row and row["top"] is not None else 0

    def _resequence_hostnames(self) -> None:
        rows = self.conn.execute(
            "SELECT name FROM hostnames WHERE pool = 1 ORDER BY pool_pos, name"
        ).fetchall()
        for position, row in enumerate(rows):
            self.conn.execute(
                "UPDATE hostnames SET pool_pos = ? WHERE name = ?", (position, row["name"])
            )
        self.conn.commit()

    # -- import ------------------------------------------------------------ #

    def import_file(self, path: str | Path, source: str | None = None) -> ImportReport:
        """Import the user's saved list: CSV rows plus bare MAC lines."""
        report = ImportReport()
        file_path = Path(path)
        label = source or file_path.name
        try:
            text = file_path.read_text("utf-8", errors="replace")
        except OSError as exc:
            report.messages.append(f"cannot read {file_path}: {exc}")
            return report

        seen: dict[str, int] = {}
        hostname_seen: set[str] = set()

        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                # A comment is not a device.  Saved lists carry free-form notes,
                # and importing one as an empty MAC row created a broken device.
                continue
            # A header row is recognised by its field names, not by position.
            lowered = [cell.strip().lower() for cell in stripped.split(",")]
            if "mac" in lowered and len(lowered) >= 2 and lowered[0] == "mac":
                continue
            cells = next(csv.reader([stripped]))
            raw_mac = (cells[0] if cells else "").strip()
            if not raw_mac:
                report.skipped_rows += 1
                continue
            hostname = (cells[1].strip() if len(cells) > 1 else "") or ""
            if hostname in ("-", "?", "n/a", "N/A"):
                hostname = ""
            ip = (cells[2].strip() if len(cells) > 2 else "") or ""
            seen_at = (cells[3].strip() if len(cells) > 3 else "") or ""

            check = macaddr.check(raw_mac)
            key = check.normalized or f"raw:{raw_mac.lower()}"
            if key in seen:
                report.duplicates += 1
                # A malformed variant of a row already imported is the same
                # device seen through a typo. Keep the good row and record the
                # variant, so the typo is not silently lost.
                if check.status != macaddr.OK:
                    self._note_variant(seen[key], raw_mac)
                    report.messages.append(
                        f"{raw_mac}: same device as {check.normalized}, recorded as a variant"
                    )
                continue

            device, reason = self.add_device(
                raw=raw_mac,
                hostname=hostname or None,
                ip=ip or None,
                seen_at=seen_at or None,
                source=label,
            )
            if device is None:
                report.skipped_rows += 1
                report.messages.append(f"{raw_mac}: {reason}")
                continue
            if reason == "added":
                report.added += 1
                if not check.usable:
                    report.broken += 1
            else:
                report.updated += 1
                self._merge_fields(device, hostname, ip, seen_at)
            seen[key] = device

            if hostname:
                normalised = hostname.lower()
                if not hostnames.is_valid(normalised):
                    report.messages.append(f"{hostname}: not a valid hostname label")
                elif normalised not in hostname_seen:
                    hostname_seen.add(normalised)
                    ok, _ = self.add_hostname(normalised, source="observed", pool=False)
                    if ok:
                        report.hostnames_added += 1

        return report

    def _note_variant(self, device_id: int, raw: str) -> None:
        """Record a malformed spelling of a device that is already catalogued."""
        device = self.device(device_id)
        if device is None:
            return
        note = f"also written as {raw}"
        if note in device.notes:
            return
        combined = (device.notes + "; " if device.notes else "") + note
        self.update_device(device_id, notes=combined[:500])

    def _merge_fields(self, device_id: int, hostname: str, ip: str, seen_at: str) -> None:
        """Fill empty fields; refresh one observation, ordered by its instant.

        ``ip`` and ``seen_at`` describe the same observation, so they move
        together and are ordered by the instant ``seen_at`` names.  Comparing
        them as text is what dropped a newer row whose address sorted lower, and
        what reversed the order across an offset change.

        An instant that cannot be parsed decides nothing: the stored value is
        kept verbatim and an incoming one is never treated as newer.
        """
        device = self.device(device_id)
        if device is None:
            return
        fields: dict[str, object] = {}
        if hostname and not device.hostname:
            fields["hostname"] = hostname
            fields["hostname_source"] = "observed"
        if not device.vendor:
            # Backfills rows imported before vendors were resolved at insert time.
            vendor = _lookup_vendor(device.mac_norm)
            if vendor:
                fields["vendor"] = vendor

        if not device.seen_at:
            # Nothing has been observed yet, so filling is not an ordering claim.
            for name, value in (("ip", ip), ("seen_at", seen_at)):
                if value:
                    fields[name] = value
        else:
            incoming = _parse_instant(seen_at)
            stored = _parse_instant(device.seen_at)
            if incoming is not None and stored is not None and incoming > stored:
                for name, value in (("ip", ip), ("seen_at", seen_at)):
                    if value:
                        fields[name] = value
        if fields:
            self.update_device(device_id, **fields)

    # -- applies and scans -------------------------------------------------- #

    def record_verification(self, device_id: int, state: str, note: str = "") -> None:
        """Store a verdict on the device, stamped with when it was read."""
        stamp = now()
        self.conn.execute(
            "UPDATE devices SET last_verified_at = ?, verify_state = ?, "
            "verify_note = ?, updated_at = ? WHERE id = ?",
            (stamp, state, note, stamp, device_id),
        )
        self.conn.commit()

    def record_apply(self, **fields) -> int:
        fields.setdefault("applied_at", now())
        columns = ", ".join(fields)
        placeholders = ", ".join("?" for _ in fields)
        cur = self.conn.execute(
            f"INSERT INTO applies({columns}) VALUES({placeholders})", tuple(fields.values())
        )
        self.conn.commit()
        return cur.lastrowid

    def recent_applies(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM applies ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    def record_scan(self, iface: str, started: str, finished: str,
                    seen: dict[str, dict]) -> None:
        self.conn.execute(
            "INSERT INTO scans(started_at, finished_at, iface, in_use, seen_json)"
            " VALUES(?,?,?,?,?)",
            (started, finished, iface, len(seen), json.dumps(seen, sort_keys=True)),
        )
        self.conn.commit()

    def last_scan(self) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM scans ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        try:
            seen = json.loads(row["seen_json"] or "{}")
        except ValueError:
            seen = {}
        return {
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "iface": row["iface"],
            "seen": seen,
        }

    # -- housekeeping ------------------------------------------------------- #

    def counts(self) -> dict[str, int]:
        def one(sql: str) -> int:
            row = self.conn.execute(sql).fetchone()
            return int(row[0]) if row else 0

        return {
            "devices": one("SELECT COUNT(*) FROM devices"),
            "usable": one(
                "SELECT COUNT(*) FROM devices WHERE mac_norm IS NOT NULL AND status IN "
                f"({','.join(repr(s) for s in macaddr.PARSED_STATUSES)})"
            ),
            "broken": one(
                "SELECT COUNT(*) FROM devices WHERE mac_norm IS NULL OR status IN "
                f"({macaddr.UNRECOVERABLE!r}, {macaddr.MULTICAST!r})"
            ),
            "repairs": one(
                f"SELECT COUNT(*) FROM devices WHERE status IN "
                f"({macaddr.NEEDS_SEPARATOR!r}, {macaddr.NEEDS_SUBSTITUTION!r})"
            ),
            "protected": one("SELECT COUNT(*) FROM devices WHERE protected = 1"),
            "own": one("SELECT COUNT(*) FROM devices WHERE is_own = 1"),
            "pool_mac": one("SELECT COUNT(*) FROM devices WHERE pool_mac = 1"),
            "hostnames": one("SELECT COUNT(*) FROM hostnames"),
            "pool_hostnames": one("SELECT COUNT(*) FROM hostnames WHERE pool = 1"),
            "applies": one("SELECT COUNT(*) FROM applies"),
        }


def _device(row: sqlite3.Row) -> Device:
    return Device(
        id=row["id"],
        mac_norm=row["mac_norm"],
        mac_raw=row["mac_raw"],
        status=row["status"],
        repair_to=row["repair_to"],
        repair_confidence=row["repair_confidence"],
        vendor=row["vendor"],
        hostname=row["hostname"],
        hostname_source=row["hostname_source"],
        ip=row["ip"],
        seen_at=row["seen_at"],
        protected=bool(row["protected"]),
        is_own=bool(row["is_own"]),
        notes=row["notes"] or "",
        source=row["source"],
        pool_mac=bool(row["pool_mac"]),
        pool_mac_pos=row["pool_mac_pos"],
        last_verified_at=row["last_verified_at"],
        verify_state=row["verify_state"],
        verify_note=row["verify_note"],
    )
