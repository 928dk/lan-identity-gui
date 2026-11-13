"""Operations shared by the window and the headless CLI.

Everything here is Qt-free on purpose.  ``main.py --status`` has to run over ssh
with no display, and the window must not grow a second version of a write path
that already lives here — two implementations would eventually disagree about
backups and drift, and the disagreement would show up as lost data rather than
as a test failure.

The functions take an already-open :class:`~catalog.Catalog` so callers decide
the transaction boundary.  ``generate`` is the only one that writes to the
rotator lists, and it keeps the drift rule: refuse unless told otherwise.
"""

from __future__ import annotations

from pathlib import Path

import config
import listfiles
import macaddr
import scanner
from catalog import Catalog, ImportReport
from config import Settings

#: How ``generate`` treats a list file that changed on disk.
#: ``refuse``    - leave the edited file alone and report it (exit 2),
#: ``adopt``     - import the edit into the catalog, then rewrite from it,
#: ``report``    - write nothing, print what would change,
#: ``overwrite`` - write from the catalog and discard the edit (the window's
#:                 "Overwrite" button; never reachable from the CLI).
#:
#: ``adopt`` and ``overwrite`` differ in exactly one way: adoption imports the
#: edit first.  Conflating them would make the Overwrite button silently keep the
#: content the user just chose to discard.
DRIFT_POLICIES = ("refuse", "adopt", "report")
GENERATE_POLICIES = DRIFT_POLICIES + ("overwrite",)


# --------------------------------------------------------------------------- #
# read-only views
# --------------------------------------------------------------------------- #

def drift_state(settings: Settings, catalog: Catalog) -> dict[str, dict]:
    """Per-file drift, without writing anything."""
    state: dict[str, dict] = {}
    for label, raw in (
        ("mac_list", settings.mac_list_path),
        ("hostname_list", settings.hostname_list_path),
    ):
        path = Path(raw)
        state[label] = {
            "path": str(path),
            "exists": path.exists(),
            "drift": listfiles.has_drift(path, catalog),
        }
    return state


def status(settings: Settings, catalog: Catalog) -> dict:
    """One dictionary with everything ``--status`` prints."""
    counts = catalog.counts()
    live = catalog.live_identity()
    return {
        "interface": settings.iface,
        "own_mac": config.own_mac(settings.iface),
        "hostname": config.current_hostname(),
        "catalog": counts,
        "live_identity": None
        if live is None
        else {"id": live.id, "mac": live.effective_mac, "hostname": live.hostname},
        "drift": drift_state(settings, catalog),
        "inventory": {
            "path": settings.inventory_path,
            "exists": Path(settings.inventory_path).exists(),
            "last_import_epoch": catalog.get_meta("inventory_mtime"),
        },
        "last_scan_at": catalog.get_meta("last_scan_at"),
        "paths": {
            "catalog": settings.catalog_path,
            "config": str(config.CONFIG_PATH),
            "mac_list": settings.mac_list_path,
            "hostname_list": settings.hostname_list_path,
        },
    }


# --------------------------------------------------------------------------- #
# operations
# --------------------------------------------------------------------------- #

def scan(
    settings: Settings,
    *,
    timeout: int | None = None,
    allow_arp_scan: bool = True,
) -> scanner.ScanResult:
    """Sweep the LAN once.  Blocking by design; callers use a thread or accept it."""
    return scanner.scan(
        settings.iface,
        allow_arp_scan=allow_arp_scan,
        timeout=timeout or settings.check_timeout,
    )


def scan_to_dict(result: scanner.ScanResult) -> dict:
    """A JSON-safe view of a scan, ordered by MAC for stable output."""
    proxy = sorted(result.proxy_macs)
    return {
        "interface": result.iface,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "source": result.source,
        "answers": result.answers,
        "count": result.count,
        "proxy_macs": proxy,
        "errors": list(result.errors),
        "devices": [
            {
                "mac": mac,
                "ips": info.get("ips") or ([info.get("ip")] if info.get("ip") else []),
                # Which of those addresses the sweep confirmed with a live
                # answer; everything else is a cache record.
                "fresh": list(info.get("fresh") or []),
                "source": info.get("source", ""),
                "last_seen": info.get("last_seen", ""),
            }
            for mac, info in sorted(result.seen.items())
        ],
    }


def _mtime_token(path: Path) -> str:
    """A comparable token for a file's mtime, or ``""`` when it is absent."""
    try:
        return str(path.stat().st_mtime)
    except OSError:
        return ""


def inventory_is_newer(settings: Settings, catalog: Catalog) -> bool:
    """True when the collector CSV changed since the last import.

    ``--import`` always imports; the window uses this gate so a restart with an
    unchanged file costs nothing and a burst of appends costs one import.
    """
    path = Path(settings.inventory_path)
    if not path.exists():
        return False
    return _mtime_token(path) != (catalog.get_meta("inventory_mtime") or "")


def import_inventory(
    settings: Settings,
    catalog: Catalog,
    path: str | Path | None = None,
) -> ImportReport:
    """Import the collector's CSV (or ``path``) and remember when it happened."""
    target = Path(path or settings.inventory_path)
    if not target.exists():
        raise FileNotFoundError(f"{target} does not exist")
    report = catalog.import_file(target, source="inventory")
    token = _mtime_token(target)
    if token:
        catalog.set_meta("inventory_mtime", token)
    return report


def adopt_mac_entries(catalog: Catalog, entries: list[str]) -> int:
    """Replace the MAC pool with ``entries``, adding unknown addresses first."""
    wanted: list[int] = []
    for entry in entries:
        check = macaddr.check(entry)
        if not check.usable:
            continue
        device = catalog.device_by_mac(check.normalized)
        if device is None:
            device_id, _ = catalog.add_device(check.normalized, source="list-edit")
            if device_id is None:
                continue
            device = catalog.device(device_id)
        if device:
            wanted.append(device.id)
    current = {d.id for d in catalog.pool_devices()}
    catalog.remove_from_pool(list(current - set(wanted)))
    catalog.add_to_pool([i for i in wanted if i not in current])
    if wanted:
        catalog.reorder_pool(wanted)
    return len(wanted)


def adopt_hostname_entries(catalog: Catalog, entries: list[str]) -> int:
    """Replace the hostname pool with ``entries``, keeping their order."""
    wanted = [name.lower() for name in entries if name.strip()]
    current = {row["name"] for row in catalog.hostnames(pooled_only=True)}
    for name in current - set(wanted):
        catalog.remove_hostname(name, from_pool_only=True)
    for name in wanted:
        if name not in current:
            catalog.add_hostname(name, source="list-edit", pool=True)
    catalog.reorder_hostnames(wanted)
    return len(wanted)


def adopt_from_disk(settings: Settings, catalog: Catalog) -> list[Path]:
    """Take the on-disk list files as truth, importing them into the catalog."""
    adopted: list[Path] = []
    for raw, parser, apply_entries in (
        (settings.mac_list_path, listfiles.parse_mac_list, adopt_mac_entries),
        (settings.hostname_list_path, listfiles.parse_hostname_list, adopt_hostname_entries),
    ):
        path = Path(raw)
        try:
            text = path.read_text("utf-8", errors="replace")
        except OSError:
            continue
        apply_entries(catalog, parser(text))
        adopted.append(path)
    return adopted


def preview_generation(
    settings: Settings, catalog: Catalog
) -> list[listfiles.WriteResult]:
    """What a generate would do, without touching the files."""
    results: list[listfiles.WriteResult] = []
    for raw, rendered in (
        (
            settings.mac_list_path,
            listfiles.render_mac_list(catalog.pool_macs(), settings),
        ),
        (
            settings.hostname_list_path,
            listfiles.render_hostname_list(catalog.pool_hostnames(), settings),
        ),
    ):
        path = Path(raw)
        previous = ""
        if path.exists():
            try:
                previous = path.read_text("utf-8", errors="replace")
            except OSError:
                previous = ""
        results.append(
            listfiles.WriteResult(
                path=path,
                wrote=False,
                drift=listfiles.has_drift(path, catalog),
                backup=None,
                previous_text=previous,
                new_text=rendered,
                message="would change" if previous != rendered else "already up to date",
            )
        )
    return results


def generate(
    settings: Settings,
    catalog: Catalog,
    drift: str = "refuse",
    backup_dir: Path | None = None,
) -> list[listfiles.WriteResult]:
    """Regenerate both list files under the requested drift policy.

    ``refuse`` writes only the files that are not drifted, which is the same
    per-file rule the window uses.  ``adopt`` imports the edits first so the
    rewrite cannot lose them.  ``overwrite`` discards them on purpose.
    ``report`` never writes.
    """
    if drift not in GENERATE_POLICIES:
        raise ValueError(
            f"unknown drift policy {drift!r}; expected one of {GENERATE_POLICIES}"
        )
    if drift == "report":
        return preview_generation(settings, catalog)
    if drift == "adopt":
        adopt_from_disk(settings, catalog)
    results = listfiles.export_both(
        catalog,
        settings,
        backup_dir or config.EXPORT_DIR,
        force=drift in ("adopt", "overwrite"),
    )
    return list(results)
