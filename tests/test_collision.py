"""Collision rules: block what a scan proves, warn about everything else.

Every MAC here is locally administered (``02:``/``0A:``) and every address comes
from the reservation range ``192.0.2.0/24``: these tests describe the rules, not
this household's hardware.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import collision  # noqa: E402

GATEWAY = "0A:00:5E:00:53:FE"
PEER = "02:00:5E:10:00:01"
THEIRS = "02:00:5E:10:00:02"
OWN = "00:00:5E:00:53:01"


def seen_map() -> dict[str, dict]:
    """One neighbour, one dead address, and a gateway answering for five."""
    return {
        PEER: {"ip": "192.0.2.11", "ips": ["192.0.2.11"], "source": "arp-scan", "vendor": "Example"},
        GATEWAY: {
            "ip": "192.0.2.1",
            "ips": [f"192.0.2.{n}" for n in (1, 21, 22, 23, 24)],
            "source": "arp-scan",
            "vendor": "Gateway",
        },
    }


class MacVerdictTests(unittest.TestCase):
    def test_an_answer_blocks_the_rotation(self):
        verdict = collision.check(seen_map(), PEER)
        self.assertTrue(verdict.blocking)
        self.assertEqual(verdict.code, collision.IN_USE)
        self.assertIn("192.0.2.11", verdict.message)
        self.assertFalse(verdict)  # falsy means "do not rotate"

    def test_the_proxy_arp_router_is_called_out_and_blocked(self):
        verdict = collision.check(seen_map(), GATEWAY)
        self.assertTrue(verdict.blocking)
        self.assertEqual(verdict.code, collision.PROXY)
        self.assertIn("gateway", verdict.message.lower())
        self.assertIn("5 addresses", verdict.message)

    def test_silence_is_a_warning_not_a_verdict(self):
        verdict = collision.check(seen_map(), THEIRS)
        self.assertFalse(verdict.blocking)
        self.assertTrue(verdict.advisory)
        self.assertEqual(verdict.code, collision.UNKNOWN)
        self.assertTrue(verdict)
        self.assertIn("not proof", verdict.message)

    def test_your_own_address_is_always_allowed(self):
        seen = seen_map()
        seen[OWN] = {"ip": "192.0.2.50", "ips": ["192.0.2.50"], "source": "arp-scan"}
        verdict = collision.check(seen, OWN, own_mac=OWN)
        self.assertFalse(verdict.blocking)
        self.assertEqual(verdict.code, collision.OWN)
        self.assertIn("already carries", verdict.message)

    def test_a_stale_neighbour_entry_for_your_own_ip_is_not_a_collision(self):
        seen = {OWN: {"ip": "192.0.2.50", "ips": ["192.0.2.50"], "source": "ip-neigh"}}
        verdict = collision.check(seen, OWN, own_mac=None, own_ip="192.0.2.50")
        self.assertFalse(verdict.blocking)
        self.assertEqual(verdict.code, collision.OWN)

    def test_an_unassignable_address_is_refused_but_not_as_in_use(self):
        for raw in ("", "nonsense", "01:00:5E:00:00:01", "00:00:00:00:00:00"):
            verdict = collision.check(seen_map(), raw)
            self.assertFalse(verdict.blocking, raw)
            self.assertEqual(verdict.code, collision.UNKNOWN, raw)

    def test_the_pool_keeps_its_order(self):
        verdicts = collision.check_pool(seen_map(), [PEER, THEIRS, GATEWAY])
        self.assertEqual([label for label, _ in verdicts], [PEER, THEIRS, GATEWAY])
        self.assertEqual(
            [verdict.code for _, verdict in verdicts],
            [collision.IN_USE, collision.UNKNOWN, collision.PROXY],
        )

    def test_a_verdict_explains_itself(self):
        verdict = collision.check(seen_map(), PEER)
        blocking, advisory = collision.summarize([(PEER, verdict)])
        self.assertEqual(advisory, [])
        self.assertEqual(len(blocking), 1)
        self.assertTrue(blocking[0].startswith(PEER))


class HostnameVerdictTests(unittest.TestCase):
    def test_the_current_name_is_not_a_collision(self):
        verdict = collision.check_name("tahoe", live_hostname="Tahoe")
        self.assertEqual(verdict.code, collision.OWN)
        self.assertFalse(verdict.blocking)

    def test_a_name_that_resolves_elsewhere_blocks(self):
        verdict = collision.check_name("baikal", resolved="192.0.2.31")
        self.assertTrue(verdict.blocking)
        self.assertIn("192.0.2.31", verdict.message)

    def test_our_own_answer_is_not_a_collision(self):
        verdict = collision.check_name(
            "baikal", resolved="192.0.2.31", own_ips={"192.0.2.31"}
        )
        self.assertFalse(verdict.blocking)

    def test_an_unresolved_name_is_weak_evidence_and_still_allowed(self):
        verdict = collision.check_name("victoria", live_hostname="tahoe")
        self.assertFalse(verdict.blocking)
        self.assertEqual(verdict.code, collision.CLEAR)


if __name__ == "__main__":
    unittest.main(verbosity=2)
