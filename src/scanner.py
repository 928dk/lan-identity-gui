"""Live presence detection using tools already installed.

``arp-scan`` carries ``cap_net_raw=eip`` on this host, so it runs unprivileged;
``ip neigh`` supplies whatever the kernel already knows without touching the
wire.  Both are merged into one ``{MAC: {ip, source, last_seen}}`` map.

The scanner is deliberately serial and blocking.  Callers run it in a worker
thread; a GUI that blocks on a 15-second ARP sweep looks hung.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime

import macaddr

ARP_SCAN_LINE = ("\t", " ")  # accepted field separators

#: One MAC answering for this many addresses or more is proxy ARP, not a client.
PROXY_MIN_IPS = 5


#: Every neighbour state the kernel can print.
NUD_STATES = frozenset(
    {
        "INCOMPLETE",
        "REACHABLE",
        "STALE",
        "DELAY",
        "PROBE",
        "FAILED",
        "NOARP",
        "PERMANENT",
        "UNKNOWN",
    }
)

#: States in which the kernel confirmed an answer recently.  Everything else --
#: ``STALE`` above all -- is a cache record, not evidence that the MAC replied.
LIVE_STATES = frozenset({"REACHABLE", "DELAY", "PROBE"})


@dataclass
class ScanResult:
    iface: str
    started_at: str
    finished_at: str
    seen: dict[str, dict] = field(default_factory=dict)
    source: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.seen)

    @property
    def answers(self) -> int:
        """IP addresses that replied, which is not the same as distinct MACs."""
        return sum(len(info.get("ips") or [info.get("ip")]) for info in self.seen.values())

    @property
    def proxy_macs(self) -> set[str]:
        return proxy_macs(self.seen)


def proxy_macs(seen: dict[str, dict], threshold: int = PROXY_MIN_IPS) -> set[str]:
    """MACs that answer for many addresses — a router doing proxy ARP.

    On a LAN like this one the gateway answers for hosts that are switched off or
    on another segment, so *its* MAC turns up against dozens of IPs.  Two
    consequences matter to the app: the number of distinct MACs is not the number
    of devices, and "this MAC did not answer" is not evidence that it is unused.
    """
    found = set()
    for mac, info in seen.items():
        addresses = info.get("ips") or ([info["ip"]] if info.get("ip") else [])
        if len(addresses) >= threshold:
            found.add(mac)
    return found



def _run(args: list[str], timeout: int) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False
        )
        return completed.returncode, completed.stdout, completed.stderr
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)


def parse_arp_scan(output: str) -> dict[str, dict]:
    """Parse ``arp-scan`` tabular output into the seen map.

    A MAC that answers for several addresses keeps them all.  Collapsing to one
    IP is what hid this LAN's proxy ARP: sixty addresses answered with the
    gateway's MAC and were counted as one device.
    """
    seen: dict[str, dict] = {}
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            parts = line.split()
        if len(parts) < 2:
            continue
        ip, mac = parts[0].strip(), parts[1].strip()
        canonical = macaddr.normalize(mac)
        if not canonical:
            continue
        entry = seen.setdefault(
            canonical,
            {
                "ip": ip,
                "ips": [],
                # An arp-scan line is a reply: this MAC answered for this
                # address just now, so the address counts as fresh evidence.
                "fresh": [],
                "source": "arp-scan",
                "vendor": (parts[2].strip() if len(parts) > 2 else ""),
            },
        )
        if ip not in entry["ips"]:
            entry["ips"].append(ip)
        if ip not in entry["fresh"]:
            entry["fresh"].append(ip)
    return seen


def _nud_state(tokens: list[str]) -> str:
    """The first neighbour-state word in ``tokens``, or ``UNKNOWN``.

    IPv6 entries append flags such as ``router`` after the MAC, so the state is
    not reliably the last field.
    """
    for token in tokens:
        if token in NUD_STATES:
            return token
    return "UNKNOWN"


def parse_ip_neigh(output: str) -> dict[str, dict]:
    """Parse ``ip neigh`` output, keeping each entry's state and freshness.

    Every line carries a neighbour-unreachability-detection state.  ``STALE`` is
    a cached address whose owner did not answer; ``REACHABLE``, ``DELAY`` and
    ``PROBE`` are the states in which the kernel confirmed an answer recently.

    The word used to be dropped, which made a leftover indistinguishable from a
    live answer once :mod:`verify` asked whether the MAC answered elsewhere.
    ``state`` describes the entry's first address; ``fresh`` is the per-address
    truth, and only addresses confirmed by a live state appear in it.
    """
    seen: dict[str, dict] = {}
    for line in output.splitlines():
        parts = line.split()
        if "lladdr" not in parts:
            continue
        index = parts.index("lladdr")
        ip = parts[0]
        canonical = macaddr.normalize(parts[index + 1]) if index + 1 < len(parts) else None
        if not canonical:
            continue
        state = _nud_state(parts[index + 2:])
        entry = seen.setdefault(
            canonical,
            {
                "ip": ip,
                "ips": [],
                "fresh": [],
                "state": state,
                "source": "ip-neigh",
                "vendor": "",
            },
        )
        if ip not in entry["ips"]:
            entry["ips"].append(ip)
        if state in LIVE_STATES and ip not in entry["fresh"]:
            entry["fresh"].append(ip)
    return seen


def scan(iface: str, allow_arp_scan: bool = True, timeout: int = 40) -> ScanResult:
    """Sweep the LAN and merge both sources."""
    started = datetime.now().astimezone().isoformat(timespec="seconds")
    result = ScanResult(iface=iface, started_at=started, finished_at=started)

    neigh_code, neigh_out, neigh_err = _run(["ip", "neigh", "show", "dev", iface], 8)
    if neigh_code == 0:
        result.seen.update(parse_ip_neigh(neigh_out))
        result.source = "ip neigh"
    elif neigh_err:
        result.errors.append(f"ip neigh: {neigh_err.strip()}")

    if allow_arp_scan and shutil.which("arp-scan"):
        code, out, err = _run(
            ["arp-scan", f"--interface={iface}", "--localnet", "--quiet"], timeout
        )
        combined = out + err
        if out:
            found = parse_arp_scan(out)
            for mac, info in found.items():
                # arp-scan output wins over a stale neighbour entry, but the
                # neighbour may know an address the sweep did not ask about.
                existing = result.seen.get(mac)
                if existing:
                    addresses = list(info["ips"])
                    for ip in existing.get("ips") or [existing.get("ip")]:
                        if ip and ip not in addresses:
                            addresses.append(ip)
                    fresh = list(info["fresh"])
                    for ip in existing.get("fresh") or []:
                        if ip not in fresh:
                            fresh.append(ip)
                    info = {**info, "ips": addresses, "fresh": fresh}
                result.seen[mac] = info
            result.source = ("arp-scan + ip neigh" if result.seen else "arp-scan")
        elif code != 0 and err.strip():
            result.errors.append(f"arp-scan: {err.strip().splitlines()[-1]}")
    elif allow_arp_scan:
        result.errors.append("arp-scan is not installed; used ip neigh only")

    result.finished_at = datetime.now().astimezone().isoformat(timespec="seconds")
    return result


def reachable_macs(result: ScanResult) -> set[str]:
    return set(result.seen)


def resolve_hostname(name: str, timeout: int = 2) -> str | None:
    """Best-effort lookup of a hostname on this LAN.

    DHCP-assigned names resolve through the router; mDNS names need avahi.  Both
    are tried, and ``None`` means "nothing answered" — never "the name is free".
    """
    cleaned = (name or "").strip()
    if not cleaned:
        return None
    attempts = (
        ["getent", "hosts", cleaned],
        ["avahi-resolve", "-n", f"{cleaned}.local"],
    )
    for argv in attempts:
        if not shutil.which(argv[0]):
            continue
        code, out, _err = _run(argv, timeout)
        if code != 0 or not out.strip():
            continue
        for token in out.split():
            candidate = token.split("/", 1)[0]
            parts = candidate.split(".")
            if len(parts) == 4 and all(
                part.isdigit() and 0 <= int(part) <= 255 for part in parts
            ):
                return candidate
    return None
