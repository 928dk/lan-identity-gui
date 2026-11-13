"""Would this identity collide with something we can prove is in use?

The honest answer on this LAN is asymmetric: a *positive* answer proves a clash,
a negative one proves almost nothing.  The gateway answers ARP for hosts that are
switched off or on another segment (proxy ARP), so sixty addresses reply with one
MAC and an absent address may still be somebody's laptop in a bag.

So the rules here are:

* **block** what a scan proves — the address answered, or it is the gateway that
  answers for everything;
* **warn** about what cannot be proved — no answer at all;
* **allow** your own address, because rotating back to it is a legitimate move.

Nothing in this module touches the network or the GUI: it takes a ``seen`` map
from :mod:`scanner` and returns a :class:`Verdict`.  That keeps the judgement
testable without an interface.
"""

from __future__ import annotations

from dataclasses import dataclass

import macaddr
from scanner import proxy_macs

CLEAR = "clear"
OWN = "own"
IN_USE = "in_use"
PROXY = "proxy"
UNKNOWN = "unknown"

#: A verdict that stops a rotation before it writes anything.
BLOCKING = frozenset({IN_USE, PROXY})


@dataclass(frozen=True)
class Verdict:
    code: str
    message: str
    evidence: str = ""

    @property
    def blocking(self) -> bool:
        return self.code in BLOCKING

    @property
    def advisory(self) -> bool:
        """Worth saying out loud, but not a reason to refuse."""
        return self.code in (UNKNOWN, OWN)

    def __bool__(self) -> bool:
        return not self.blocking


def _addresses(entry: dict) -> list[str]:
    return list(entry.get("ips") or ([entry["ip"]] if entry.get("ip") else []))


def check(
    seen: dict[str, dict],
    mac: str,
    own_mac: str | None = None,
    own_ip: str | None = None,
    proxies: set[str] | None = None,
) -> Verdict:
    """Judge one candidate MAC against a scan of the LAN."""
    parsed = macaddr.check(mac)
    if not parsed.usable:
        return Verdict(UNKNOWN, f"{mac or '(nothing)'} is not an assignable address")

    canon = parsed.normalized
    assert canon is not None  # ``usable`` guarantees a canonical form

    if own_mac:
        own = macaddr.check(own_mac)
        if own.usable and own.normalized == canon:
            return Verdict(
                OWN,
                f"{canon} is the address this interface already carries — "
                "rotating to it would change nothing",
            )

    entry = seen.get(canon)
    if entry is None:
        return Verdict(
            UNKNOWN,
            f"{canon} did not answer. That is not proof it is free: this LAN's "
            "gateway answers for absent hosts, so a silent address may belong to "
            "a device that is switched off or on another segment.",
        )

    addresses = _addresses(entry)
    if own_ip and own_ip in addresses:
        return Verdict(
            OWN,
            f"{canon} is still mapped to your own address {own_ip} — that is "
            "left over from the identity you are leaving, not a second device",
        )

    proxies = proxy_macs(seen) if proxies is None else proxies
    if canon in proxies:
        return Verdict(
            PROXY,
            f"{canon} answered for {len(addresses)} addresses, which is the "
            "gateway doing proxy ARP. Taking its address would cut the LAN off "
            "from its router.",
            evidence=", ".join(addresses[:4])
            + (" …" if len(addresses) > 4 else ""),
        )

    where = addresses[0] if addresses else "an address"
    return Verdict(
        IN_USE,
        f"{canon} answered as {where}"
        + (f" (and {len(addresses) - 1} more)" if len(addresses) > 1 else "")
        + " — that address is in use right now.",
        evidence=where,
    )


def check_pool(
    seen: dict[str, dict],
    macs: list[str],
    own_mac: str | None = None,
    own_ip: str | None = None,
    proxies: set[str] | None = None,
) -> list[tuple[str, Verdict]]:
    """Judge a whole rotation pool, keeping the order the file will use."""
    proxies = proxy_macs(seen) if proxies is None else proxies
    return [
        (mac, check(seen, mac, own_mac=own_mac, own_ip=own_ip, proxies=proxies))
        for mac in macs
    ]


def check_name(
    name: str,
    live_hostname: str | None = None,
    resolved: str | None = None,
    own_ips: set[str] | frozenset[str] = frozenset(),
) -> Verdict:
    """Judge a hostname, using only what a lookup actually returned.

    ``resolved`` is the address a fresh lookup gave for the name (``None`` when
    nothing answered).  A name is only *provably* taken while its owner is up and
    answering, so this blocks on a foreign answer and stays quiet otherwise.
    """
    cleaned = (name or "").strip().lower()
    if not cleaned:
        return Verdict(CLEAR, "no hostname is being changed")
    if live_hostname and cleaned == live_hostname.strip().lower():
        return Verdict(
            OWN, f"{cleaned} is the hostname this machine already has", evidence=""
        )
    if resolved and resolved not in own_ips:
        return Verdict(
            IN_USE,
            f"{cleaned} already resolves to {resolved} on this network — "
            "two machines with one name is exactly the clash to avoid.",
            evidence=resolved,
        )
    return Verdict(
        CLEAR,
        f"{cleaned} did not resolve to anything else. Names are only taken while "
        "their owner answers, so this is a weak answer rather than a promise.",
    )


def summarize(verdicts: list[tuple[str, Verdict]]) -> tuple[list[str], list[str]]:
    """Split a pool judgement into ``(blocking, advisory)`` human lines."""
    blocking: list[str] = []
    advisory: list[str] = []
    for label, verdict in verdicts:
        line = f"{label}: {verdict.message}"
        if verdict.blocking:
            blocking.append(line)
        elif verdict.advisory:
            advisory.append(line)
    return blocking, advisory
