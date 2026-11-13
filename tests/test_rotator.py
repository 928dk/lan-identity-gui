"""Tests for the rotation cursor and the countdown."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rotator import (  # noqa: E402
    INDEPENDENT, PAIRED, Countdown, RotationEngine, RotationError,
)

MACS = ["00:00:5E:00:53:01", "00:00:5E:00:53:02"]
NAMES = ["tahoe", "erie", "ontario"]


class IndependentTests(unittest.TestCase):
    def test_each_list_wraps_on_its_own_length(self):
        engine = RotationEngine(MACS, NAMES, INDEPENDENT)
        seen = [engine.advance() for _ in range(6)]
        self.assertEqual([p.mac for p in seen], [MACS[i % 2] for i in range(6)])
        self.assertEqual([p.hostname for p in seen], [NAMES[i % 3] for i in range(6)])

    def test_combination_count_matches_mac_rotator(self):
        engine = RotationEngine(MACS, NAMES, INDEPENDENT)
        self.assertEqual(engine.combinations, 6)

    def test_two_macs_thirteen_names(self):
        engine = RotationEngine(MACS, [f"name{i}" for i in range(13)], INDEPENDENT)
        self.assertEqual(engine.combinations, 26)

    def test_current_does_not_advance(self):
        engine = RotationEngine(MACS, NAMES, INDEPENDENT)
        self.assertEqual(engine.current(), engine.current())


class PairedTests(unittest.TestCase):
    def test_pairs_advance_together_and_wrap_at_the_shorter_list(self):
        engine = RotationEngine(MACS, NAMES, PAIRED)
        pairs = [(p.mac, p.hostname) for p in (engine.advance() for _ in range(4))]
        self.assertEqual(pairs, [
            (MACS[0], NAMES[0]),
            (MACS[1], NAMES[1]),
            (MACS[0], NAMES[0]),
            (MACS[1], NAMES[1]),
        ])

    def test_combination_count_is_the_shorter_list(self):
        engine = RotationEngine(MACS, NAMES, PAIRED)
        self.assertEqual(engine.combinations, 2)


class EdgeCaseTests(unittest.TestCase):
    def test_empty_mac_pool_raises(self):
        engine = RotationEngine([], NAMES)
        with self.assertRaises(RotationError):
            engine.validate()
        with self.assertRaises(RotationError):
            engine.current()

    def test_mac_only_mode(self):
        engine = RotationEngine(MACS, NAMES, INDEPENDENT, rotate_hostname=False)
        self.assertIsNone(engine.advance().hostname)
        self.assertEqual(engine.combinations, 2)

    def test_no_hostnames_configured(self):
        engine = RotationEngine(MACS, [], INDEPENDENT)
        pair = engine.advance()
        self.assertIsNone(pair.hostname)
        self.assertEqual(pair.mac, MACS[0])

    def test_peek_does_not_move_the_cursor(self):
        engine = RotationEngine(MACS, NAMES, INDEPENDENT)
        before = engine.current()
        upcoming = engine.peek(3)
        self.assertEqual(len(upcoming), 3)
        self.assertEqual(engine.current(), before)
        self.assertEqual(upcoming[0].mac, MACS[0])

    def test_reset(self):
        engine = RotationEngine(MACS, NAMES, INDEPENDENT)
        engine.advance()
        engine.advance()
        engine.reset()
        self.assertEqual(engine.current().mac, MACS[0])

    def test_describe_mentions_both_lengths(self):
        engine = RotationEngine(MACS, NAMES, INDEPENDENT)
        text = engine.describe()
        self.assertIn("2 MAC", text)
        self.assertIn("3 hostname", text)


class CountdownTests(unittest.TestCase):
    def test_disarmed_is_not_due(self):
        countdown = Countdown(60)
        self.assertFalse(countdown.armed)
        self.assertFalse(countdown.due())

    def test_arm_and_expire_immediately(self):
        countdown = Countdown(60)
        countdown.arm(0)
        self.assertTrue(countdown.armed)
        self.assertTrue(countdown.due())
        self.assertEqual(countdown.seconds_left(), 0.0)

    def test_rearm_resets_the_span(self):
        countdown = Countdown(60)
        countdown.arm(0)
        self.assertTrue(countdown.due())
        countdown.rearm()
        self.assertFalse(countdown.due())


if __name__ == "__main__":
    unittest.main(verbosity=2)
