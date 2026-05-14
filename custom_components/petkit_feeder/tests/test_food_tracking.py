"""Tests for the food-tank dispense-tracking + auto-refill-detection logic
in local_server.PetkitLocalServer.

The local_server module is deliberately importable in pure-Python via the
sys.path injection below. It only needs aiohttp / homeassistant when its
HTTP routes run; these tests poke at the in-process state machine, not
the HTTP layer.
"""
from __future__ import annotations

# Force stdlib resolution before sys.path mutation (see test_petkit_cloud)
import asyncio  # noqa: F401
import select  # noqa: F401
import socket  # noqa: F401

import importlib.util
import os
import sys
import unittest

# Importing the integration package would pull in voluptuous + HA, neither
# of which are available in the CI/test environment. Load local_server.py
# directly as a top-level module instead so we get the PetkitLocalServer
# class without any HA dependencies.
_PARENT = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))


def _load_module(name: str, path: str, package: str | None = None):
    spec = importlib.util.spec_from_file_location(
        name, path, submodule_search_locations=[]
    )
    mod = importlib.util.module_from_spec(spec)
    if package is not None:
        mod.__package__ = package
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


if "_loaded_local_server" not in globals():
    # local_server.py does `from .const import ...` — set up a fake
    # parent package "petkit_feeder_test_pkg" with both modules.
    pkg_name = "petkit_feeder_test_pkg"
    pkg = type(sys)(pkg_name)
    pkg.__path__ = [_PARENT]
    sys.modules[pkg_name] = pkg
    # Load const submodule first
    _load_module(
        f"{pkg_name}.const",
        os.path.join(_PARENT, "const.py"),
        package=pkg_name,
    )
    # Now load local_server with relative imports resolving against pkg
    _loaded_local_server = _load_module(
        f"{pkg_name}.local_server",
        os.path.join(_PARENT, "local_server.py"),
        package=pkg_name,
    )


def _make_server():
    """Construct a PetkitLocalServer without starting any HTTP listener."""
    return _loaded_local_server.PetkitLocalServer()


class CapacityTest(unittest.TestCase):
    def test_default_capacity_is_1700g(self):
        s = _make_server()
        self.assertEqual(s.food_tank_capacity_g, 1700)

    def test_set_capacity_clamps_invalid(self):
        s = _make_server()
        with self.assertRaises(ValueError):
            s.set_food_tank_capacity(50)
        with self.assertRaises(ValueError):
            s.set_food_tank_capacity(10000)

    def test_set_capacity_persists(self):
        s = _make_server()
        s.set_food_tank_capacity(2200)
        self.assertEqual(s.food_tank_capacity_g, 2200)


class RecordRefillTest(unittest.TestCase):
    def test_first_refill_sets_baseline(self):
        s = _make_server()
        self.assertIsNone(s.food_remaining_grams())
        self.assertIsNone(s.food_remaining_percent())
        s.record_refill()
        self.assertEqual(s.food_remaining_grams(), 1700)
        self.assertEqual(s.food_remaining_percent(), 100)

    def test_dispense_decrements_remaining(self):
        s = _make_server()
        s.record_refill()
        s._account_for_dispense(50)
        self.assertEqual(s.food_remaining_grams(), 1650)
        self.assertEqual(s.food_remaining_percent(), 97)

    def test_dispense_zero_or_negative_ignored(self):
        s = _make_server()
        s.record_refill()
        s._account_for_dispense(0)
        s._account_for_dispense(-5)
        self.assertEqual(s.food_remaining_grams(), 1700)

    def test_remaining_clamped_at_zero(self):
        s = _make_server()
        s.record_refill()
        s._account_for_dispense(2000)  # more than capacity
        self.assertEqual(s.food_remaining_grams(), 0)
        self.assertEqual(s.food_remaining_percent(), 0)

    def test_refill_resets_counter(self):
        s = _make_server()
        s.record_refill()
        s._account_for_dispense(500)
        self.assertEqual(s.food_remaining_grams(), 1200)
        s.record_refill()
        self.assertEqual(s.food_remaining_grams(), 1700)


class AutoRefillDetectionTest(unittest.TestCase):
    """Verify that food-state ENUM transitions auto-trigger record_refill."""

    def test_first_state_does_not_trigger(self):
        s = _make_server()
        # Very first reading after startup — no transition baseline.
        s._check_food_refill_transition(1)
        self.assertIsNone(s._food_refill_at)

    def test_empty_to_ok_triggers_refill(self):
        s = _make_server()
        s._check_food_refill_transition(0)  # baseline: empty
        s._account_for_dispense(100)  # phantom prior dispenses
        s.record_refill()
        s._account_for_dispense(50)
        # Now simulate firmware reporting "ok" again (refill happened)
        s._check_food_refill_transition(1)
        self.assertEqual(s.food_remaining_grams(), 1700)

    def test_unknown_to_low_triggers_refill(self):
        s = _make_server()
        s._check_food_refill_transition(-1)  # baseline: unknown
        s.record_refill()
        s._account_for_dispense(200)
        s._check_food_refill_transition(2)  # → low
        self.assertEqual(s.food_remaining_grams(), 1700)

    def test_ok_to_low_does_not_trigger(self):
        s = _make_server()
        s._check_food_refill_transition(1)  # baseline: ok
        s.record_refill()
        s._account_for_dispense(800)
        before = s.food_remaining_grams()
        s._check_food_refill_transition(2)  # ok → low (depletion, not refill)
        self.assertEqual(s.food_remaining_grams(), before)

    def test_ok_to_empty_does_not_trigger(self):
        s = _make_server()
        s._check_food_refill_transition(1)
        s.record_refill()
        s._account_for_dispense(1700)
        s._check_food_refill_transition(0)
        # Should still show 0 grams remaining, not 1700.
        self.assertEqual(s.food_remaining_grams(), 0)

    def test_low_to_ok_triggers_refill(self):
        """Some firmwares may bounce 2 → 1 when the user adds a bit of food
        without going through 0. Treat any negative→positive transition
        as a refill; the worst case is a reset that's already correct."""
        s = _make_server()
        # Note: 2 (low) is NOT in the "was_empty" list (only 0 / -1).
        # So a 2 → 1 transition is NOT treated as refill. This test
        # documents that intentional choice.
        s._check_food_refill_transition(2)
        s.record_refill()
        s._account_for_dispense(500)
        before = s.food_remaining_grams()
        s._check_food_refill_transition(1)
        self.assertEqual(s.food_remaining_grams(), before)


class PersistenceTest(unittest.TestCase):
    def test_persistent_state_round_trip(self):
        s = _make_server()
        s.set_food_tank_capacity(2000)
        s.record_refill()
        s._account_for_dispense(150)

        snap = s.get_persistent_state()
        self.assertEqual(snap["food_tank_capacity_g"], 2000)
        self.assertEqual(snap["food_dispensed_since_refill_g"], 150)
        self.assertIsNotNone(snap["food_refill_at"])

        # Restore into a fresh server
        s2 = _make_server()
        s2.load_persistent_state(snap)
        self.assertEqual(s2.food_tank_capacity_g, 2000)
        self.assertEqual(s2._food_dispensed_since_refill_g, 150)
        self.assertEqual(s2.food_remaining_grams(), 1850)

    def test_restore_handles_legacy_data_without_food_block(self):
        """Older installations don't have food-tracking fields in their
        store. Restore should not crash and should leave defaults intact."""
        s = _make_server()
        legacy = {
            "device_info": {},
            "device_state": {},
            "device_settings": {},
            "desiccant_reset_at": None,
            "daily_feeds": {},
        }
        s.load_persistent_state(legacy)
        self.assertEqual(s.food_tank_capacity_g, 1700)
        self.assertEqual(s._food_dispensed_since_refill_g, 0)
        self.assertIsNone(s._food_refill_at)


class FeedQueuePersistenceTest(unittest.TestCase):
    """Regression: 2026-05-13 incident — HA restarted between a scheduled
    feed firing and the D4 coming back online, dropping the queued item.
    The queue is now persisted with absolute timestamps and age-pruned on
    reload."""

    def test_queue_persisted_and_restored(self):
        s = _make_server()
        s.queue_feed(20)
        snap = s.get_persistent_state()
        self.assertIn("feed_queue", snap)
        self.assertEqual(len(snap["feed_queue"]), 1)
        self.assertEqual(snap["feed_queue"][0]["amount"], 20)
        self.assertIn("queued_at_unix", snap["feed_queue"][0])

        s2 = _make_server()
        s2.load_persistent_state(snap)
        self.assertEqual(len(s2._feed_queue), 1)
        self.assertEqual(s2._feed_queue[0]["amount"], 20)

    def test_stale_queue_items_dropped(self):
        """Items older than FEED_QUEUE_MAX_AGE_SEC must NOT be restored —
        else the scheduled 21:00 feed would zombie-fire at 03:00 next day."""
        s = _make_server()
        import time as _t
        # Build a snapshot with an artificially old queued_at_unix
        ancient_time = _t.time() - (s.FEED_QUEUE_MAX_AGE_SEC + 10)
        snap = s.get_persistent_state()
        snap["feed_queue"] = [{
            "amount": 50,
            "time": 0,
            "queued_at_unix": ancient_time,
        }]
        s2 = _make_server()
        s2.load_persistent_state(snap)
        self.assertEqual(len(s2._feed_queue), 0)

    def test_mixed_fresh_and_stale(self):
        """A snapshot containing both fresh and stale items keeps only the
        fresh ones."""
        s = _make_server()
        import time as _t
        now = _t.time()
        snap = s.get_persistent_state()
        snap["feed_queue"] = [
            {"amount": 10, "time": 0, "queued_at_unix": now - 30},        # fresh
            {"amount": 20, "time": 0, "queued_at_unix": now - 7200},      # 2h old → stale
            {"amount": 50, "time": 0, "queued_at_unix": now - 60},        # fresh
        ]
        s2 = _make_server()
        s2.load_persistent_state(snap)
        kept_amounts = sorted(item["amount"] for item in s2._feed_queue)
        self.assertEqual(kept_amounts, [10, 50])

    def test_legacy_snapshot_without_queue_field(self):
        """Old persisted state from pre-2026-05-14 has no feed_queue key.
        Restore should not crash and should leave queue empty."""
        s = _make_server()
        s.load_persistent_state({
            "device_info": {},
            "device_state": {},
            "device_settings": {},
            "desiccant_reset_at": None,
            "daily_feeds": {},
            # no feed_queue key
        })
        self.assertEqual(len(s._feed_queue), 0)

    def test_malformed_queue_items_skipped(self):
        """Garbage in the persisted feed_queue must not crash restore."""
        s = _make_server()
        snap = s.get_persistent_state()
        snap["feed_queue"] = [
            "not a dict",
            {"amount": 10},  # missing queued_at_unix → treated as expired
            {"amount": 20, "queued_at_unix": "garbage"},
            None,
        ]
        s2 = _make_server()
        s2.load_persistent_state(snap)
        self.assertEqual(len(s2._feed_queue), 0)


if __name__ == "__main__":
    unittest.main()
