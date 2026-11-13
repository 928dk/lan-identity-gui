"""Offline OUI (vendor) lookup.

Reads the OUI tables that are already on disk — ships with ``nmap`` and
``arp-scan``, both installed — and caches the parsed result as JSON so repeat
startups do not re-parse 1.4 MB of text.

No network call is ever made.  Sending MAC addresses to a vendor API would hand
a third party a map of the LAN, which is exactly what this app exists to keep
local.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from config import OUI_SOURCES, STATE_DIR

CACHE_PATH = STATE_DIR / "oui-cache.json"

#: nmap format: "24 6A 0E HP" is not used; the file is "246A0E HP".
_NMAP_LINE = re.compile(r"^([0-9A-Fa-f]{6})\s+(.+)$")


class VendorDB:
    """Prefix -> vendor name, loaded once per process."""

    def __init__(self) -> None:
        self._map: dict[str, str] = {}

    def load(self) -> "VendorDB":
        if self._map:
            return self
        if CACHE_PATH.exists():
            try:
                data = json.loads(CACHE_PATH.read_text("utf-8"))
                self._map = {k.upper(): v for k, v in data.items()}
                if self._map:
                    return self
            except (OSError, ValueError):
                pass
        self._map = self._parse_sources()
        self._write_cache()
        return self

    def _parse_sources(self) -> dict[str, str]:
        for path in OUI_SOURCES:
            if not path.exists():
                continue
            found = self._parse_nmap(path) if path.name.startswith("nmap") else self._parse_oui(path)
            if found:
                return found
        return {}

    @staticmethod
    def _parse_nmap(path: Path) -> dict[str, str]:
        result: dict[str, str] = {}
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if not line or line[0] in "#\n\r":
                        continue
                    match = _NMAP_LINE.match(line.strip())
                    if match:
                        result[match.group(1).upper()] = match.group(2).strip()
        except OSError:
            return {}
        return result

    @staticmethod
    def _parse_oui(path: Path) -> dict[str, str]:
        """arp-scan's ieee-oui.txt: TAB-separated ``PREFIX\\tVENDOR``."""
        result: dict[str, str] = {}
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line or line.startswith("#") or "\t" not in line:
                        continue
                    prefix, vendor = line.split("\t", 1)
                    prefix = prefix.strip().upper()
                    if len(prefix) >= 6:
                        result[prefix[:6]] = vendor.strip()
        except OSError:
            return {}
        return result

    def _write_cache(self) -> None:
        if not self._map:
            return
        try:
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            CACHE_PATH.write_text(json.dumps(self._map, separators=(",", ":")), "utf-8")
        except OSError:
            pass

    def lookup(self, mac: str | None) -> str | None:
        """Vendor for a canonical or raw MAC, or ``None`` when unknown."""
        if not mac:
            return None
        digits = re.sub(r"[^0-9A-Fa-f]", "", mac).upper()
        if len(digits) < 6:
            return None
        return self._map.get(digits[:6])

    def __len__(self) -> int:
        return len(self._map)


_singleton: VendorDB | None = None


def get_vendor_db() -> VendorDB:
    global _singleton
    if _singleton is None:
        _singleton = VendorDB().load()
    return _singleton


def short_vendor(name: str | None, limit: int = 28) -> str:
    """Trim the long legal names to something a table column can hold."""
    if not name:
        return ""
    cleaned = re.sub(r"\s+", " ", name).strip()
    for suffix in (" Incorporated", " Inc.", " Inc", " Corporation", " Corp.",
                   " Company", " Co., Ltd", " Co.,Ltd", " Ltd.", " Ltd",
                   " GmbH", " B.V.", " S.A.", " Technologies", " Technology"):
        cleaned = cleaned.replace(suffix, "")
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1].rstrip() + "\u2026"
    return cleaned
