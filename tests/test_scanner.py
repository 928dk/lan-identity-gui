"""Scanner parsing: one MAC can answer for many addresses, and that matters."""

from __future__ import annotations

import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import scanner  # noqa: E402

GATEWAY = "0A:00:5E:00:53:FE"
PEER = "02:00:5E:10:00:01"

ARP_OUTPUT = f"""\
192.0.2.1\t{GATEWAY}\tExample Gateway
192.0.2.11\t{PEER}\tExample Peer
192.0.2.21\t{GATEWAY}\tExample Gateway
192.0.2.22\t{GATEWAY}\tExample Gateway
192.0.2.23\t{GATEWAY}\tExample Gateway
192.0.2.24\t{GATEWAY}\tExample Gateway
"""

NEIGH_OUTPUT = """\
192.0.2.11 dev eth0 lladdr 02:00:5e:10:00:01 REACHABLE
192.0.2.41 dev eth0 lladdr 02:00:5e:10:00:03 STALE
"""


class ArpScanParsingTests(unittest.TestCase):
    def test_every_address_a_mac_answers_for_is_kept(self):
        seen = scanner.parse_arp_scan(ARP_OUTPUT)
        self.assertEqual(set(seen), {GATEWAY, PEER})
        self.assertEqual(len(seen[GATEWAY]["ips"]), 5)
        self.assertEqual(seen[PEER]["ips"], ["192.0.2.11"])

    def test_the_first_address_is_the_primary_one(self):
        seen = scanner.parse_arp_scan(ARP_OUTPUT)
        self.assertEqual(seen[GATEWAY]["ip"], "192.0.2.1")

    def test_repeated_addresses_are_not_counted_twice(self):
        doubled = ARP_OUTPUT + f"192.0.2.11\t{PEER}\tExample Peer\n"
        self.assertEqual(len(scanner.parse_arp_scan(doubled)[PEER]["ips"]), 1)

    def test_garbage_lines_are_ignored(self):
        self.assertEqual(scanner.parse_arp_scan("nonsense\n\n"), {})


class ProxyArpTests(unittest.TestCase):
    def test_a_mac_answering_for_many_addresses_is_flagged(self):
        seen = scanner.parse_arp_scan(ARP_OUTPUT)
        self.assertEqual(scanner.proxy_macs(seen), {GATEWAY})

    def test_a_normal_neighbour_is_not_flagged(self):
        seen = scanner.parse_arp_scan(f"192.0.2.11\t{PEER}\tExample Peer\n")
        self.assertEqual(scanner.proxy_macs(seen), set())

    def test_the_threshold_is_what_decides(self):
        seen = scanner.parse_arp_scan(ARP_OUTPUT)
        self.assertEqual(scanner.proxy_macs(seen, threshold=2), {GATEWAY})
        self.assertEqual(scanner.proxy_macs(seen, threshold=6), set())

    def test_the_answer_count_differs_from_the_mac_count(self):
        result = scanner.ScanResult(iface="eth0", started_at="now", finished_at="now")
        result.seen = scanner.parse_arp_scan(ARP_OUTPUT)
        self.assertEqual(result.count, 2)
        self.assertEqual(result.answers, 6)
        self.assertEqual(result.proxy_macs, {GATEWAY})


class NeighbourParsingTests(unittest.TestCase):
    def test_only_entries_with_a_lladdr_are_used(self):
        seen = scanner.parse_ip_neigh(NEIGH_OUTPUT)
        self.assertEqual(set(seen), {PEER, "02:00:5E:10:00:03"})
        self.assertEqual(seen[PEER]["ips"], ["192.0.2.11"])

    def test_a_neighbour_entry_also_lists_one_address(self):
        seen = scanner.parse_ip_neigh(NEIGH_OUTPUT)
        self.assertEqual(seen[PEER]["ip"], "192.0.2.11")


class NeighbourFreshnessTests(unittest.TestCase):
    """The NUD state word is the only difference between an answer and a leftover.

    ``ip neigh`` prints a state per entry.  Dropping it made a cached ``STALE``
    address look exactly like a ``REACHABLE`` one, so :mod:`verify` counted a
    neighbour record as a live collision.
    """

    def test_the_state_word_is_kept(self):
        seen = scanner.parse_ip_neigh(NEIGH_OUTPUT)
        self.assertEqual(seen[PEER]["state"], "REACHABLE")
        self.assertEqual(seen["02:00:5E:10:00:03"]["state"], "STALE")

    def test_a_stale_address_is_not_fresh(self):
        seen = scanner.parse_ip_neigh(NEIGH_OUTPUT)
        self.assertEqual(seen[PEER]["fresh"], ["192.0.2.11"])
        self.assertEqual(seen["02:00:5E:10:00:03"]["fresh"], [])

    def test_every_address_of_one_mac_keeps_its_own_state(self):
        output = (
            "192.0.2.51 dev eth0 lladdr 02:00:5e:10:00:04 STALE\n"
            "192.0.2.52 dev eth0 lladdr 02:00:5e:10:00:04 REACHABLE\n"
        )
        entry = scanner.parse_ip_neigh(output)["02:00:5E:10:00:04"]
        self.assertEqual(entry["ips"], ["192.0.2.51", "192.0.2.52"])
        self.assertEqual(entry["fresh"], ["192.0.2.52"])

    def test_an_ipv6_router_flag_does_not_hide_the_state(self):
        output = "fe80::1 dev eth0 lladdr 02:00:5e:10:00:05 router REACHABLE\n"
        seen = scanner.parse_ip_neigh(output)
        self.assertEqual(seen["02:00:5E:10:00:05"]["state"], "REACHABLE")
        self.assertEqual(seen["02:00:5E:10:00:05"]["fresh"], ["fe80::1"])

    def test_a_state_that_is_not_an_answer_is_not_fresh(self):
        for state in ("PERMANENT", "NOARP", "FAILED", "INCOMPLETE"):
            output = f"192.0.2.61 dev eth0 lladdr 02:00:5e:10:00:06 {state}\n"
            entry = scanner.parse_ip_neigh(output)["02:00:5E:10:00:06"]
            self.assertEqual(entry["state"], state)
            self.assertEqual(entry["fresh"], [], state)


class MergeFreshnessTests(unittest.TestCase):
    """A sweep reply is an answer; a neighbour address it did not confirm is not."""

    def test_an_arp_scan_reply_stays_fresh_after_the_merge(self):
        def fake_run(argv, timeout):
            if argv[0] == "ip":
                return 0, (
                    "192.0.2.71 dev eth0 lladdr 02:00:5e:10:00:07 STALE\n"
                    "192.0.2.72 dev eth0 lladdr 02:00:5e:10:00:07 STALE\n"
                ), ""
            return 0, "192.0.2.71\t02:00:5e:10:00:07\tExample Peer\n", ""

        with unittest.mock.patch.object(scanner, "_run", side_effect=fake_run), \
                unittest.mock.patch.object(
                    scanner.shutil, "which", return_value="/usr/bin/arp-scan"
                ):
            result = scanner.scan("eth0")

        entry = result.seen["02:00:5E:10:00:07"]
        self.assertEqual(entry["ips"], ["192.0.2.71", "192.0.2.72"])
        self.assertEqual(entry["fresh"], ["192.0.2.71"])
        self.assertEqual(result.source, "arp-scan + ip neigh")


if __name__ == "__main__":
    unittest.main(verbosity=2)
