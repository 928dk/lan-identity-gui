"""Headless entry points.

These exist because the window is the wrong front end for a timer or an ssh
session, and because every automation this app grows (inventory import, drift
watch, post-apply verification) needs a way to run without a display.

No module here imports PySide6.  That is a property worth keeping: the CLI must
work on a host with no session, and a stray top-level Qt import would break that
without breaking any test that runs under ``QT_QPA_PLATFORM=offscreen``.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import config
import listfiles
import ops
from catalog import Catalog

EXIT_OK = 0
#: A generated list file changed on disk and was not overwritten.
EXIT_DRIFT = 2
EXIT_ERROR = 3
#: Reserved for ``--verify`` (packet 209 phase D).
EXIT_VERIFY = 4

#: Flags that mean "do not open a window".  Keeping this list here means
#: ``main.py`` does not have to grow a second dispatch table.
HEADLESS_FLAGS = ("--check", "--status", "--scan", "--import", "--generate",
                  "-h", "--help")


def wants_headless(argv: list[str]) -> bool:
    return any(arg in HEADLESS_FLAGS or arg.split("=", 1)[0] in HEADLESS_FLAGS
               for arg in argv)


class _Parser(argparse.ArgumentParser):
    """argparse exits 2 on a usage error; 2 means drift here."""

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        raise SystemExit(EXIT_ERROR)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="lan-identity-gui",
        description="Inspect and drive the LAN identity catalog without a window.",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true",
                        help="print an environment report and exit")
    action.add_argument("--status", action="store_true",
                        help="catalog counts, live identity, drift state")
    action.add_argument("--scan", action="store_true",
                        help="sweep the LAN once and list what answered")
    action.add_argument("--import", dest="import_path", nargs="?", const="",
                        metavar="PATH",
                        help="import the collector CSV (or PATH) into the catalog")
    action.add_argument("--generate", action="store_true",
                        help="write mac-list.txt and hostnamelist.txt from the catalog")

    parser.add_argument("--drift", choices=ops.DRIFT_POLICIES, default="refuse",
                        help="generate: refuse on a hand-edited file (default), "
                             "adopt it into the catalog, or only report")
    parser.add_argument("--json", action="store_true",
                        help="emit JSON instead of text")
    parser.add_argument("--timeout", type=int, default=None,
                        help="scan timeout in seconds (default: settings value)")
    parser.add_argument("--no-arp-scan", action="store_true",
                        help="use ip neigh only; no ARP sweep")
    return parser


# --------------------------------------------------------------------------- #
# renderers
# --------------------------------------------------------------------------- #

def environment_report(settings: config.Settings) -> str:
    lines = ["lan-identity-gui environment", ""]

    def probe(label: str, value: str) -> None:
        lines.append(f"  {label:<24} {value}")

    probe("settings", config.CONFIG_PATH)
    probe("catalog", settings.catalog_path)
    probe("interface", settings.iface)
    probe("own MAC", config.own_mac(settings.iface) or "(unknown)")
    probe("hostname", config.current_hostname())
    probe("MAC list", settings.mac_list_path)
    probe("hostname list", settings.hostname_list_path)

    try:
        import PySide6
        from PySide6 import QtCore

        probe("PySide6", f"{PySide6.__version__} (Qt {QtCore.__version__})")
    except Exception as exc:  # noqa: BLE001 - report, never crash
        probe("PySide6", f"MISSING ({exc})")

    for tool in ("arp-scan", "pkexec", "konsole", "hostnamectl", "ip"):
        path = shutil.which(tool)
        probe(tool, path or "not installed")

    from vendor import get_vendor_db

    db = get_vendor_db()
    probe("OUI prefixes", f"{len(db):,}")

    catalog = Catalog(settings.catalog_path)
    try:
        counts = catalog.counts()
        probe("catalog devices", str(counts["devices"]))
        probe("pool", f"{counts['pool_mac']} MAC(s), {counts['pool_hostnames']} name(s)")
    finally:
        catalog.close()

    helper = Path(__file__).resolve().parent.parent / "bin" / "apply-identity"
    probe("pkexec helper", f"{helper} ({'ok' if helper.exists() else 'MISSING'})")
    return "\n".join(lines)


def _text_status(payload: dict) -> str:
    counts = payload["catalog"]
    lines = ["lan-identity-gui status", ""]

    def row(label: str, value: object) -> None:
        lines.append(f"  {label:<18} {value}")

    row("interface", payload["interface"])
    row("own MAC", payload["own_mac"] or "(unknown)")
    row("hostname", payload["hostname"] or "(unknown)")
    row("catalog", f"{counts['devices']} device(s), {counts['usable']} usable, "
                    f"{counts['repairs']} repair(s), {counts['broken']} broken")
    row("pool", f"{counts['pool_mac']} MAC(s), {counts['pool_hostnames']} name(s)")
    live = payload["live_identity"]
    row("live identity",
        f"{live['mac']} (id {live['id']})" if live and live.get("mac") else "(not in catalog)")

    for label, key in (("mac-list.txt", "mac_list"), ("hostnamelist.txt", "hostname_list")):
        entry = payload["drift"][key]
        if not entry["exists"]:
            state = "missing"
        elif entry["drift"]:
            state = "DRIFT (edited on disk)"
        else:
            state = "ok"
        row(label, f"{entry['path']}  [{state}]")

    inventory = payload["inventory"]
    row("inventory", f"{inventory['path']}  "
                     f"[{'ok' if inventory['exists'] else 'missing'}]")
    row("last scan", payload["last_scan_at"] or "never")
    return "\n".join(lines)


def _text_scan(payload: dict) -> str:
    lines = [
        f"scan on {payload['interface']} via {payload['source'] or 'no source'}",
        f"  {payload['count']} MAC(s), {payload['answers']} address(es)",
    ]
    if payload["proxy_macs"]:
        lines.append(f"  proxy ARP: {len(payload['proxy_macs'])} MAC(s) answer for many addresses")
    for error in payload["errors"]:
        lines.append(f"  error: {error}")
    for device in payload["devices"]:
        lines.append(f"  {device['mac']}  {', '.join(device['ips']) or '(no address)'}")
    return "\n".join(lines)


def _text_generate(results: list[listfiles.WriteResult]) -> str:
    lines: list[str] = []
    for result in results:
        name = result.path.name
        if result.drift:
            lines.append(f"{name}: DRIFT - {result.message}")
        elif result.wrote:
            backup = result.backup.name if result.backup else "no backup"
            lines.append(f"{name}: written ({backup})")
        else:
            lines.append(f"{name}: {result.message}")
    return "\n".join(lines)


def _emit(payload: dict, as_json: bool, render) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False))
    else:
        print(render(payload))


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #

def run(argv: list[str], settings: config.Settings) -> int:
    args = build_parser().parse_args(argv)

    if args.check:
        print(environment_report(settings))
        return EXIT_OK

    catalog = Catalog(settings.catalog_path)
    try:
        if args.status:
            _emit(ops.status(settings, catalog), args.json, _text_status)
            return EXIT_OK

        if args.scan:
            result = ops.scan(
                settings,
                timeout=args.timeout,
                allow_arp_scan=not args.no_arp_scan,
            )
            catalog.set_meta("last_scan_at", result.finished_at)
            _emit(ops.scan_to_dict(result), args.json, _text_scan)
            return EXIT_OK

        if args.import_path is not None:
            target = args.import_path or None
            report = ops.import_inventory(settings, catalog, target)
            payload = {
                "path": str(target or settings.inventory_path),
                "added": report.added,
                "updated": report.updated,
                "duplicates": report.duplicates,
                "broken": report.broken,
                "hostnames_added": report.hostnames_added,
                "skipped_rows": report.skipped_rows,
                "messages": list(report.messages),
                "summary": report.summary(),
            }
            if args.json:
                print(json.dumps(payload, indent=2, ensure_ascii=False))
            else:
                print(f"imported {Path(payload['path']).name}: {report.summary()}")
                for message in report.messages:
                    print(f"  {message}")
            return EXIT_OK

        if args.generate:
            results = ops.generate(settings, catalog, drift=args.drift)
            if args.json:
                payload = {
                    "drift_policy": args.drift,
                    "files": [
                        {
                            "path": str(r.path),
                            "wrote": r.wrote,
                            "drift": r.drift,
                            "message": r.message,
                            "backup": str(r.backup) if r.backup else None,
                            "diff": r.diff,
                        }
                        for r in results
                    ],
                    "drifted": any(r.drift for r in results),
                }
                print(json.dumps(payload, indent=2, ensure_ascii=False))
            else:
                print(_text_generate(results))
                for result in results:
                    if result.drift and result.diff:
                        print(result.diff, file=sys.stderr)
            return EXIT_DRIFT if any(result.drift for result in results) else EXIT_OK

        build_parser().print_help()
        return EXIT_ERROR
    except (OSError, ValueError) as exc:
        print(f"lan-identity-gui: {exc}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        catalog.close()
