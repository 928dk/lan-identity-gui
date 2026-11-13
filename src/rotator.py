"""The rotation cursor — mac-rotator.sh's cycling behaviour, as an object.

Two modes, because the original script's behaviour is surprising:

``independent``
    The MAC list and the hostname list each advance on their own and wrap on
    their own length.  With 2 MACs and 13 names that is 26 combinations, and
    MAC #1 eventually wears every name.  This is what ``mac-rotator.sh`` does.

``paired``
    ``MAC[i]`` is always applied with ``hostname[i]`` and both wrap at the
    shorter list.  Stable identities, fewer combinations.

The cursor is positional and stateless beyond its two indices, so it can be
persisted, inspected and unit tested without touching a network interface.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

INDEPENDENT = "independent"
PAIRED = "paired"


@dataclass(frozen=True)
class Pair:
    """One rotation step: what to apply next."""

    mac: str
    hostname: str | None
    mac_index: int
    hostname_index: int | None

    @property
    def label(self) -> str:
        if self.hostname:
            return f"{self.mac} + {self.hostname}"
        return self.mac


class RotationError(Exception):
    pass


class RotationEngine:
    def __init__(
        self,
        macs: list[str],
        hostnames: list[str],
        pairing: str = INDEPENDENT,
        rotate_hostname: bool = True,
    ) -> None:
        self.macs = [m for m in macs if m]
        self.hostnames = [h for h in hostnames if h] if rotate_hostname else []
        self.pairing = PAIRED if pairing == PAIRED else INDEPENDENT
        self.mac_index = 0
        self.hostname_index = 0

    def validate(self) -> None:
        if not self.macs:
            raise RotationError("the MAC pool is empty — add at least one address")
        if self.pairing == PAIRED and self.hostnames and not self.macs:
            raise RotationError("nothing to pair")

    @property
    def combinations(self) -> int:
        if not self.macs:
            return 0
        if not self.hostnames:
            return len(self.macs)
        if self.pairing == PAIRED:
            return min(len(self.macs), len(self.hostnames))
        return len(self.macs) * len(self.hostnames)

    def current(self) -> Pair:
        if not self.macs:
            raise RotationError("the MAC pool is empty")
        mac = self.macs[self.mac_index % len(self.macs)]
        if not self.hostnames:
            return Pair(mac, None, self.mac_index % len(self.macs), None)
        index = self.hostname_index % len(self.hostnames)
        return Pair(mac, self.hostnames[index], self.mac_index % len(self.macs), index)

    def advance(self) -> Pair:
        """Return the current pair and step the cursor forward."""
        pair = self.current()
        self.mac_index = (self.mac_index + 1) % len(self.macs)
        if self.hostnames:
            if self.pairing == PAIRED:
                bound = min(len(self.macs), len(self.hostnames))
                self.hostname_index = (self.hostname_index + 1) % bound
                self.mac_index = self.hostname_index
            else:
                self.hostname_index = (self.hostname_index + 1) % len(self.hostnames)
        return pair

    def peek(self, steps: int = 1) -> list[Pair]:
        """Upcoming pairs without disturbing the cursor."""
        saved = (self.mac_index, self.hostname_index)
        preview = [self.advance() for _ in range(max(0, steps))]
        self.mac_index, self.hostname_index = saved
        return preview

    def reset(self) -> None:
        self.mac_index = 0
        self.hostname_index = 0

    def describe(self) -> str:
        if not self.macs:
            return "no MACs in the pool"
        mode = "paired" if self.pairing == PAIRED else "independent"
        names = f"{len(self.hostnames)} hostname(s)" if self.hostnames else "MAC only"
        return (
            f"{mode}: {len(self.macs)} MAC(s) \u00d7 {names} "
            f"= {self.combinations} combination(s)"
        )


class Countdown:
    """Monotonic countdown for the in-app timer, kept out of the UI layer."""

    def __init__(self, interval: int) -> None:
        self.interval = max(1, int(interval))
        self._armed_at: float | None = None
        self._deadline: float | None = None

    def arm(self, seconds: float | None = None) -> None:
        now = time.monotonic()
        span = self.interval if seconds is None else max(0.0, seconds)
        self._armed_at = now
        self._deadline = now + span

    def disarm(self) -> None:
        self._armed_at = None
        self._deadline = None

    @property
    def armed(self) -> bool:
        return self._deadline is not None

    def seconds_left(self) -> float:
        if self._deadline is None:
            return 0.0
        return max(0.0, self._deadline - time.monotonic())

    def due(self) -> bool:
        return self._deadline is not None and time.monotonic() >= self._deadline

    def rearm(self) -> None:
        self.arm()
