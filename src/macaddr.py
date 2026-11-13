"""MAC address parsing, normalisation, classification and repair.

The one rule that matters here: substitution may only touch characters that can
never be a hex digit.  ``B`` is a valid hex digit, so mapping it corrupts
addresses such as ``C0:0B:85:...``.  Every repair is then re-validated, because
a plausible-looking substitution can still land on a multicast address, which
the kernel refuses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

OK = "ok"
NEEDS_SEPARATOR = "needs_separator_fix"
NEEDS_SUBSTITUTION = "needs_substitution"
MULTICAST = "multicast"
UNRECOVERABLE = "unrecoverable"

HIGH = "high"
MEDIUM = "medium"
NONE = "none"

#: Statuses that carry a usable 12-digit candidate.
PARSED_STATUSES = (OK, NEEDS_SEPARATOR, NEEDS_SUBSTITUTION)
#: Statuses that need a human decision before the value can be trusted.
REPAIR_STATUSES = (NEEDS_SEPARATOR, NEEDS_SUBSTITUTION)

CANONICAL_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")
SEPARATORS_RE = re.compile(r"[:\-.\s]")
HEX_RE = re.compile(r"^[0-9A-Fa-f]{12}$")

#: Characters that can never be a hex digit.  ``B``/``b`` are deliberately
#: absent, as are A-F: mapping a valid digit is data corruption, not repair.
SUBSTITUTIONS = {
    "O": "0",
    "o": "0",
    "S": "5",
    "s": "5",
    "I": "1",
    "i": "1",
    "l": "1",
    "t": "7",
    "G": "6",
    "g": "6",
    "\u00a3": "E",  # pound sign typed instead of E
}

ALL_ZERO = "00:00:00:00:00:00"
BROADCAST = "FF:FF:FF:FF:FF:FF"


@dataclass(frozen=True)
class MacCheck:
    """The result of inspecting one raw MAC-ish string."""

    raw: str
    status: str
    normalized: str | None
    confidence: str
    assignable: bool
    locally_administered: bool
    oui: str | None
    reason: str

    @property
    def needs_repair(self) -> bool:
        return self.status in REPAIR_STATUSES

    @property
    def usable(self) -> bool:
        """Parses to a 12-digit address that a NIC can actually take."""
        return self.normalized is not None and self.assignable

    @property
    def needs_attention(self) -> bool:
        return self.status != OK or not self.assignable

    @property
    def label(self) -> str:
        return {
            OK: "ok",
            NEEDS_SEPARATOR: "fix separators",
            NEEDS_SUBSTITUTION: "fix characters",
            MULTICAST: "multicast",
            UNRECOVERABLE: "broken",
        }.get(self.status, self.status)


def format_canonical(hex12: str) -> str:
    """``00005e005301`` -> ``00:00:5E:00:53:01``."""
    upper = hex12.upper()
    return ":".join(upper[i : i + 2] for i in range(0, 12, 2))


def is_canonical(raw: str) -> bool:
    """Already ``00:00:5E:00:53:01`` (either case) — no repair needed."""
    return bool(CANONICAL_RE.match((raw or "").strip().upper()))


def check(raw: str) -> MacCheck:
    """Classify ``raw`` and, where possible, produce a canonical address."""
    text = (raw or "").strip()
    if not text:
        return MacCheck(raw, UNRECOVERABLE, None, NONE, False, False, None, "empty value")

    stripped = SEPARATORS_RE.sub("", text)
    hex12: str | None = None
    status = UNRECOVERABLE
    confidence = NONE
    reason = ""

    if HEX_RE.match(stripped):
        hex12 = stripped.upper()
        if is_canonical(text):
            status, confidence = OK, HIGH
        else:
            status, confidence = NEEDS_SEPARATOR, HIGH
            reason = "separators or whitespace only"
    else:
        mapped = "".join(SUBSTITUTIONS.get(ch, ch) for ch in stripped)
        if HEX_RE.match(mapped):
            hex12 = mapped.upper()
            status, confidence = NEEDS_SUBSTITUTION, MEDIUM
            changed = sorted({ch for ch in stripped if ch in SUBSTITUTIONS})
            reason = "characters substituted: " + ", ".join(changed)
        else:
            reason = f"{len(mapped)} hex digits after mapping (need 12)"

    if hex12 is None:
        return MacCheck(text, UNRECOVERABLE, None, NONE, False, False, None, reason)

    normalized = format_canonical(hex12)
    first_octet = int(normalized[:2], 16)
    locally_administered = bool(first_octet & 0x02)
    is_multicast = bool(first_octet & 0x01)
    oui = normalized.replace(":", "")[:6]

    if normalized in (ALL_ZERO, BROADCAST) or is_multicast:
        why = (
            "all-zero address"
            if normalized == ALL_ZERO
            else "broadcast address"
            if normalized == BROADCAST
            else "multicast address"
        )
        if status == NEEDS_SUBSTITUTION:
            # The guess is what produced an unassignable address: reject the
            # repair rather than offering something the kernel will refuse.
            return MacCheck(
                text,
                UNRECOVERABLE,
                None,
                NONE,
                False,
                locally_administered,
                None,
                f"repair would produce a {why}",
            )
        return MacCheck(text, MULTICAST, normalized, confidence, False,
                        locally_administered, oui, why)

    return MacCheck(text, status, normalized, confidence, True,
                    locally_administered, oui, reason)


def normalize(raw: str) -> str | None:
    """Canonical address for ``raw``, or ``None`` when it cannot be parsed."""
    found = check(raw)
    return found.normalized if found.usable else None


def is_assignable(raw: str) -> bool:
    return check(raw).usable
