"""Post-apply verification: record what came up, never revert on its own.

An apply reports that its steps ran.  Whether the interface actually carries the
new address, has a link, obtained an IPv4 address and is not answering for a
second address is a separate question — and it is the one a person asks next.

Two rules shape this module, both learned from packet 206:

* **Presence is observable; absence is not provable.**  This LAN uses proxy ARP,
  so a MAC that does not answer may simply be switched off on another segment.
  Nothing here ever calls an address "free"; the strongest negative is
  "not seen".
* **A reading can fail for reasons that are not the identity.**  A missing
  carrier or an absent DHCP lease is reported as itself, not as a bad address —
  an automatic revert on either would undo an identity that may be fine.
* **A cache entry is not an answer.**  The neighbour table keeps addresses for
  devices that stopped replying.  A duplicate needs an address the sweep
  confirmed with a live answer; a ``STALE`` leftover is reported, not counted as
  a collision.

The reader, the scanner and the sleep are injected so the tests need no root, no
LAN and no waiting.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

import macaddr

#: Everything a verdict can say.
STATE_VERIFIED = "verified"
STATE_ABSENT = "absent"
STATE_CARRIER_DOWN = "carrier_down"
STATE_DUPLICATE_MAC = "duplicate_mac"
STATE_TIMEOUT = "timeout"
STATE_SKIPPED = "skipped"

STATES = (
    STATE_VERIFIED,
    STATE_ABSENT,
    STATE_CARRIER_DOWN,
    STATE_DUPLICATE_MAC,
    STATE_TIMEOUT,
    STATE_SKIPPED,
)

#: States that mean "read the evidence", not "undo it".
FAILURE_STATES = (
    STATE_ABSENT,
    STATE_CARRIER_DOWN,
    STATE_DUPLICATE_MAC,
    STATE_TIMEOUT,
)


@dataclass(frozen=True)
class InterfaceState:
    """One reading of the interface, with no interpretation."""

    iface: str
    mac: str | None
    carrier: bool
    ipv4: str | None
    hostname: str | None = None


@dataclass(frozen=True)
class Verdict:
    """What verification concluded, and the reading it concluded it from."""

    state: str
    mac: str
    hostname: str | None = None
    note: str = ""
    evidence: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.state == STATE_VERIFIED

    @property
    def is_failure(self) -> bool:
        return self.state in FAILURE_STATES


# --------------------------------------------------------------------------- #
# the real reader and scanner
# --------------------------------------------------------------------------- #

def read_interface(iface: str) -> InterfaceState:
    """Read the interface through the same helpers the rest of the app uses."""
    import config

    return InterfaceState(
        iface=iface,
        mac=config.own_mac(iface),
        carrier=config.have_carrier(iface),
        ipv4=config.own_ip(iface),
        hostname=config.current_hostname(),
    )


def scan_interface(iface: str):
    """Sweep the LAN; imported lazily so this module stays importable headless."""
    import scanner

    return scanner.scan(iface)


# --------------------------------------------------------------------------- #
# verification
# --------------------------------------------------------------------------- #

def _canonical(mac: str | None) -> str | None:
    if not mac:
        return None
    return macaddr.normalize(mac) or mac.strip().upper()


def _addresses_for(seen: dict, mac: str) -> list[str]:
    """Every IP the sweep attributes to ``mac``, compared case-insensitively."""
    found: list[str] = []
    for key, info in (seen or {}).items():
        if _canonical(key) != mac:
            continue
        addresses = info.get("ips") or ([info["ip"]] if info.get("ip") else [])
        for address in addresses:
            if address and address not in found:
                found.append(address)
    return found


def _fresh_addresses(seen: dict, mac: str) -> list[str] | None:
    """Addresses the sweep confirmed with a live answer for ``mac``.

    ``None`` means the entry carries no freshness information at all, which is
    not the same as "nothing was fresh": a map built by an import or by hand
    proves neither, so the caller keeps its conservative verdict for it.
    """
    confirmed: list[str] | None = []
    for key, info in (seen or {}).items():
        if _canonical(key) != mac:
            continue
        fresh = info.get("fresh")
        if fresh is None:
            return None
        for address in fresh:
            if address not in confirmed:
                confirmed.append(address)
    return confirmed


def verify_interface(
    mac: str | None,
    hostname: str | None = None,
    iface: str | None = None,
    *,
    reader: Callable[[str], InterfaceState] = read_interface,
    scan: Callable[[str], object] | None = scan_interface,
    sleep: Callable[[float], None] = time.sleep,
    dhcp_wait: int = 60,
    poll_interval: int = 5,
) -> Verdict:
    """Check an applied identity and return the evidence, not a decision.

    Order matters: carrier first, because nothing else can be read without it;
    then the MAC, because a wrong address is not a DHCP problem; then a bounded
    wait for an address; then the sweep, which can only add a duplicate finding.
    """
    expected = _canonical(mac)
    if not expected:
        return Verdict(
            STATE_SKIPPED, "", hostname,
            note="no assignable address to verify",
        )

    if iface is None:
        import config

        iface = config.default_interface()

    state = reader(iface)
    evidence = {
        "iface": iface,
        "expected_mac": expected,
        "reported_mac": state.mac,
        "carrier": state.carrier,
        "ipv4": state.ipv4,
    }

    if not state.carrier:
        return Verdict(
            STATE_CARRIER_DOWN, expected, hostname,
            note=f"{iface} reports no carrier; the network was not read",
            evidence=evidence,
        )

    if _canonical(state.mac) != expected:
        return Verdict(
            STATE_ABSENT, expected, hostname,
            note=(
                f"{iface} reports {state.mac or 'no MAC'}, "
                f"not the requested {expected}"
            ),
            evidence=evidence,
        )

    # Bounded wait for a lease.  ``poll_interval`` is injected so a test can
    # drive this to completion without sleeping.
    attempts = max(1, int(round(max(1, dhcp_wait) / max(1, poll_interval))))
    for _ in range(attempts):
        if state.ipv4:
            break
        sleep(poll_interval)
        state = reader(iface)
        evidence["ipv4"] = state.ipv4

    if not state.ipv4:
        return Verdict(
            STATE_TIMEOUT, expected, hostname,
            note=(
                f"{iface} carries {expected} but no IPv4 address arrived "
                f"within {dhcp_wait}s"
            ),
            evidence=evidence,
        )

    if scan is not None:
        try:
            result = scan(iface)
        except Exception as exc:  # noqa: BLE001 - a failed sweep is not a bad MAC
            evidence["scan"] = f"failed: {exc}"
            return Verdict(
                STATE_VERIFIED, expected, hostname,
                note=(
                    f"{iface} carries {expected} with {state.ipv4}; "
                    f"the sweep did not run ({exc})"
                ),
                evidence=evidence,
            )

        seen = getattr(result, "seen", None)
        if seen is None:
            seen = result if isinstance(result, dict) else {}
        addresses = _addresses_for(seen, expected)
        evidence["seen_at"] = addresses
        extra = [address for address in addresses if address != state.ipv4]
        if extra:
            # A duplicate needs a positive answer.  When the sweep recorded no
            # freshness at all, the conservative verdict is kept rather than
            # silently dropping collision detection.
            confirmed = _fresh_addresses(seen, expected)
            live = [a for a in extra if confirmed is None or a in confirmed]
            if live:
                return Verdict(
                    STATE_DUPLICATE_MAC, expected, hostname,
                    note=(
                        f"the sweep saw {expected} answering for {live[0]} as well as "
                        f"{state.ipv4}"
                    ),
                    evidence=evidence,
                )
            evidence["stale_at"] = extra

    note = f"{iface} carries {expected} with {state.ipv4}"
    if evidence.get("stale_at"):
        note += (
            f"; a cached neighbour entry also lists {evidence['stale_at'][0]}, "
            "which did not answer, so it is not counted as a collision"
        )
    elif not evidence.get("seen_at"):
        # Deliberately "not seen", never "free": proxy ARP hides absence.
        note += "; not seen by the sweep (absence is not provable here)"

    return Verdict(STATE_VERIFIED, expected, hostname, note=note, evidence=evidence)


def describe(verdict: Verdict) -> str:
    """One line for a log or a label."""
    return f"{verdict.state}: {verdict.note}" if verdict.note else verdict.state
