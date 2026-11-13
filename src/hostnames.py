"""Hostname validation and offline suggestion engines.

There is no such thing as a useful "most common hostname" list on the internet:
hostnames are site-local.  What actually produces plausible candidates is

1. the vendor, from the local OUI table          -> ``hp-01``, ``synology-03``
2. names already on this LAN                     -> ``tahoe3`` after 1 and 2
3. vendor default patterns bundled with the app  -> ``DESKTOP-4F2K9QZ``, ``iPhone``
4. word styles observed on the LAN plus a small word list

All four run offline.  Every suggestion is validated with the same rules
``mac-rotator.sh`` applies, so a name offered here can never be one the rotator
would refuse.
"""

from __future__ import annotations

import json
import random
import re
import string
from dataclasses import dataclass
from pathlib import Path

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
VENDOR_DEFAULTS_PATH = ASSETS_DIR / "vendor-defaults.json"

#: Mirrors ``normalize_hostname()`` in mac-rotator.sh.
LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
MAX_HOSTNAME_LEN = 63
NON_LABEL_RE = re.compile(r"[^a-z0-9-]+")

#: Words that make a name look like a device on this particular LAN.  Kept short
#: on purpose: a long list produces noise, not suggestions.
WORD_STEMS = (
    "kontor", "stue", "koekken", "vaerelse", "arbejdsvaerelse", "kælder",
    "sal", "rum", "gang", "bord", "printer", "scanner", "nas", "router",
    "switch", "tv", "højttaler", "telefon", "tablet", "laptop", "station",
)

_SEQUENCE_RE = re.compile(r"^(?P<stem>.*?)(?P<num>\d{1,3})$")


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #

def normalize(name: str, prefix: str = "") -> str | None:
    """Return the name the rotator would actually apply, or ``None`` if invalid.

    Identical in effect to ``normalize_hostname()`` in ``mac-rotator.sh``:
    lowercase, drop whitespace, strip a repeated prefix, enforce the label
    grammar and the 63-character ceiling including the prefix.
    """
    prefix = (prefix or "").lower()
    text = "".join((name or "").split()).lower()
    if text.startswith(prefix) and prefix:
        text = text[len(prefix):]
    if not text.replace("-", ""):
        return None
    if not LABEL_RE.match(text):
        return None
    result = f"{prefix}{text}"
    if len(result) > MAX_HOSTNAME_LEN:
        return None
    return result


def is_valid(name: str, prefix: str = "") -> bool:
    return normalize(name, prefix) is not None


def validate(name: str, prefix: str = "") -> tuple[bool, str]:
    """Validation plus a human-readable reason, for inline error text."""
    text = "".join((name or "").split())
    if not text:
        return False, "empty"
    if len(f"{prefix}{text.lower()}") > MAX_HOSTNAME_LEN:
        return False, f"longer than {MAX_HOSTNAME_LEN} characters with the prefix"
    if text.startswith("-") or text.endswith("-"):
        return False, "must not start or end with a hyphen"
    if not LABEL_RE.match(text.lower()):
        bad = sorted(set(re.findall(r"[^a-z0-9-]", text.lower())))
        if bad:
            return False, "invalid characters: " + " ".join(repr(c) for c in bad)
        return False, "not a valid hostname label"
    return True, ""


def slugify(text: str, fallback: str = "device") -> str:
    text = text.lower().replace("&", " and ")
    text = NON_LABEL_RE.sub("-", text).strip("-")
    text = re.sub(r"-{2,}", "-", text)
    return text or fallback


def warnings(
    name: str, in_use: str, existing: set[str], fleet: list[str] | tuple[str, ...] = ()
) -> list[str]:
    """Non-blocking notes shown next to a candidate name."""
    lowered = (name or "").lower()
    notes: list[str] = []
    if lowered and lowered == (in_use or "").lower():
        notes.append("that is this machine's current hostname")
    elif lowered and lowered in {host.lower() for host in fleet}:
        notes.append("shadows another machine on your network")
    if lowered in existing:
        notes.append("already in the hostname pool")
    return notes


# --------------------------------------------------------------------------- #
# suggestions
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Suggestion:
    name: str
    source: str
    rationale: str

    @property
    def display(self) -> str:
        return f"{self.name}  \u2014 {self.rationale}"


def _load_vendor_defaults() -> list[dict]:
    try:
        return json.loads(VENDOR_DEFAULTS_PATH.read_text("utf-8"))
    except (OSError, ValueError):
        return []


WORD_POOL_PATH = ASSETS_DIR / "hostname-pool.json"
_WORD_POOL: dict[str, list[str]] | None = None


def word_pool() -> dict[str, list[str]]:
    """The bundled name bank, category -> valid names.

    A curated subset of Greg Ercolano's hostname gist, kept on disk so suggesting
    a name never touches the network.  Missing or broken file means "no bank",
    never a crash: suggestions fall back to the vendor and LAN-style engines.
    """
    global _WORD_POOL
    if _WORD_POOL is not None:
        return _WORD_POOL
    try:
        data = json.loads(WORD_POOL_PATH.read_text("utf-8"))
    except (OSError, ValueError):
        _WORD_POOL = {}
        return _WORD_POOL
    pool: dict[str, list[str]] = {}
    for category, names in (data.get("categories") or {}).items():
        cleaned = [str(name).strip().lower() for name in names if str(name).strip()]
        pool[category] = [name for name in cleaned if is_valid(name)]
    _WORD_POOL = pool
    return pool


def word_pool_categories() -> list[str]:
    return sorted(name for name, names in word_pool().items() if names)


def _word_bank(taken: set[str], limit: int = 2) -> list[tuple[str, str]]:
    """One name from a couple of the bundled categories, skipping taken ones."""
    pool = word_pool()
    categories = [name for name in pool if pool[name]]
    if not categories:
        return []
    random.shuffle(categories)
    found: list[tuple[str, str]] = []
    for category in categories:
        available = [name for name in pool[category] if name not in taken]
        if not available:
            continue
        name = random.choice(available)
        taken.add(name)
        found.append((name, f"one of the {category} names bundled offline"))
        if len(found) >= limit:
            break
    return found


def _next_in_series(names: list[str]) -> list[tuple[str, str]]:
    """``['tahoe1', 'tahoe2']`` -> ``[('tahoe3', ...)]``."""
    groups: dict[str, list[tuple[str, int]]] = {}
    for name in names:
        match = _SEQUENCE_RE.match(name.strip())
        if not match:
            continue
        stem = match.group("stem")
        if not stem:
            continue
        groups.setdefault(stem.lower(), []).append((stem, int(match.group("num"))))
    found: list[tuple[str, str]] = []
    for entries in groups.values():
        stem = entries[0][0]
        highest = max(number for _, number in entries)
        candidate = f"{stem}{highest + 1}"
        if is_valid(candidate):
            found.append((candidate, f"next free number after {stem}{highest}"))
    return found


def _vendor_defaults(vendor: str | None, taken: set[str]) -> list[tuple[str, str]]:
    if not vendor:
        return []
    lowered = vendor.lower()
    found: list[tuple[str, str]] = []
    for entry in _load_vendor_defaults():
        for keyword in entry.get("keywords", []):
            if keyword.lower() in lowered:
                pattern = entry.get("pattern", "")
                if not pattern:
                    continue
                if "{rand7}" in pattern:
                    name = pattern.replace("{rand7}", "".join(
                        random.choices(string.ascii_uppercase + string.digits, k=7)))
                elif "{rand8}" in pattern:
                    name = pattern.replace("{rand8}", "".join(
                        random.choices("0123456789abcdef", k=8)))
                else:
                    name = pattern
                if name.lower() not in taken and is_valid(name):
                    found.append((name, f"{entry.get('label', vendor)} default style"))
                break
    return found


def _vendor_slugs(vendor: str | None, taken: set[str]) -> list[tuple[str, str]]:
    if not vendor:
        return []
    first_word = vendor.split()[0] if vendor.split() else ""
    stem = slugify(first_word, "device")
    found: list[tuple[str, str]] = []
    for number in range(1, 6):
        candidate = f"{stem}-{number:02d}"
        if candidate.lower() not in taken:
            found.append((candidate, f"vendor style from {first_word}"))
            break
    return found


def _lan_word_styles(lan_names: list[str], taken: set[str]) -> list[tuple[str, str]]:
    """Mimic the naming style already present, then fall back to word stems."""
    found: list[tuple[str, str]] = []
    if lan_names:
        style = "upper" if any(n.isupper() for n in lan_names if n) else "lower"
        for stem in WORD_STEMS:
            for number in range(1, 4):
                candidate = f"{stem}{number}" if style == "lower" else f"{stem.upper()}{number}"
                if candidate.lower() in taken:
                    continue
                if is_valid(candidate):
                    found.append((candidate, f"style already used on this LAN ({style})"))
                    break
            if len(found) >= 3:
                break
    return found


def suggest(
    mac: str | None = None,
    vendor: str | None = None,
    existing_names: list[str] | None = None,
    lan_names: list[str] | None = None,
    limit: int = 8,
) -> list[Suggestion]:
    """Candidate hostnames for one device, best first, all offline."""
    existing_names = existing_names or []
    lan_names = lan_names or []
    taken = {n.lower() for n in existing_names}

    collected: list[tuple[str, str, str]] = []

    for name, why in _next_in_series(lan_names or existing_names):
        collected.append((name, "series", why))
    for name, why in _vendor_slugs(vendor, taken | {c[0].lower() for c in collected}):
        collected.append((name, "vendor", why))
    for name, why in _vendor_defaults(vendor, taken | {c[0].lower() for c in collected}):
        collected.append((name, "vendor-default", why))
    for name, why in _word_bank(taken | {c[0].lower() for c in collected}):
        collected.append((name, "word-bank", why))
    for name, why in _lan_word_styles(lan_names, taken | {c[0].lower() for c in collected}):
        collected.append((name, "lan-word", why))

    if not collected:
        base = slugify((vendor or "device").split()[0] if vendor else "device")
        for number in range(1, 4):
            candidate = f"{base}-{number:02d}"
            if candidate.lower() not in taken:
                collected.append((candidate, "fallback", "generic vendor-style name"))
                break

    seen: set[str] = set()
    result: list[Suggestion] = []
    for name, source, why in collected:
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(Suggestion(name=name, source=source, rationale=why))
        if len(result) >= limit:
            break
    return result


def generate_batch(
    count: int,
    existing_names: list[str] | None = None,
    lan_names: list[str] | None = None,
) -> list[str]:
    """``count`` distinct new names, for the pool's Generate button."""
    existing = list(existing_names or [])
    taken = {n.lower() for n in existing}
    produced: list[str] = []
    guard = 0
    while len(produced) < count and guard < count * 20:
        guard += 1
        pool = suggest(existing_names=existing, lan_names=lan_names or existing, limit=4)
        added = False
        for item in pool:
            if item.name.lower() in taken:
                continue
            taken.add(item.name.lower())
            existing.append(item.name)
            produced.append(item.name)
            added = True
            if len(produced) >= count:
                break
        if not added:
            # Exhausted the structured engines: extend the last series manually.
            base = produced[-1] if produced else "device"
            match = _SEQUENCE_RE.match(base)
            stem = match.group("stem") if match else base
            number = int(match.group("num")) + 1 if match else 1
            candidate = f"{stem}{number}"
            if is_valid(candidate) and candidate.lower() not in taken:
                taken.add(candidate.lower())
                existing.append(candidate)
                produced.append(candidate)
            else:
                break
    return produced[:count]
