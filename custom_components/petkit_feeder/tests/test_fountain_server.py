"""Tests for FountainServer — BLE-relay state machine + CTW3 cloud emulation.

Lifecycle (observed live 2026-04-21):
  IDLE --send_*--> push connect(1) --> CONNECTING
  CONNECTING --event52(action=1,result=0)--> drain pending,
              push ble+..+connect(0) --> DISCONNECTING
  DISCONNECTING --event52(action=0,*)--> IDLE
"""
from __future__ import annotations

import base64
import json
import os
import sys
import unittest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ble_frame import (
    CMD_RESET_FILTER,
    CMD_SET_MODE,
    DEVICE_TYPE_CTW3,
    parse_frame,
)
from ctw3_state import Ctw3State
from fountain_server import (
    FountainServer,
    LINK_CONNECTED,
    LINK_CONNECTING,
    LINK_DISCONNECTING,
    LINK_IDLE,
)

MAC = "aabbcc112233"
DEVICE_ID = 400000001


def _sample_fountain() -> Ctw3State:
    return Ctw3State(
        id=DEVICE_ID,
        mac=MAC,
        secret="cafebabe1234",
        sn="EXAMPLEFAKESN001",
        firmware=111,
        type_code=2,
    )


def _register_and_mark_synced(server: FountainServer, fountain: Ctw3State) -> None:
    """Register a fountain AND skip the auto-sync flow.

    Most lifecycle tests predate the auto-sync-on-register feature — they
    assume the server starts in IDLE with an empty push queue. This helper
    reproduces that "pre-auto-sync" world by:
      1. Registering the fountain normally (which flags sync_requested,
         pushes a connect, and goes CONNECTING).
      2. Marking the fountain as already synced, clearing the auto-queued
         connect from the push queue, and resetting link state to IDLE.
    """
    server.register_fountain(fountain)
    server._settings_synced.add(fountain.mac)
    server._sync_requested.discard(fountain.mac)
    server._push_queue.clear()
    server._link_state[fountain.mac] = LINK_IDLE


def _connect_ok_event(mac: str = MAC) -> dict:
    """Shape mirrors real D4 /d4/dev_event_report body for event 52."""
    return {
        "event_type": "52",
        "content": json.dumps({
            "result": 0,
            "action": 1,
            "device": {"mac": mac, "type": DEVICE_TYPE_CTW3},
        }),
    }


def _connect_fail_event(mac: str = MAC, result: int = 3) -> dict:
    return {
        "event_type": "52",
        "content": json.dumps({
            "result": result,
            "action": 1,
            "device": {"mac": mac, "type": DEVICE_TYPE_CTW3},
        }),
    }


def _disconnect_done_event(mac: str = MAC) -> dict:
    return {
        "event_type": "52",
        "content": json.dumps({
            "result": 0,
            "action": 0,
            "device": {"mac": mac, "type": DEVICE_TYPE_CTW3},
        }),
    }


# --- Registry ---------------------------------------------------------------

class TestRegistry(unittest.TestCase):
    def test_register_and_lookup(self):
        s = FountainServer()
        f = _sample_fountain()
        s.register_fountain(f)
        self.assertIs(s.get_fountain(MAC), f)
        self.assertIn(MAC, s.fountains)

    def test_unknown_mac(self):
        s = FountainServer()
        self.assertIsNone(s.get_fountain("ffffffffffff"))

    def test_unregister_drops_pending(self):
        s = FountainServer()
        s.register_fountain(_sample_fountain())
        s.send_set_mode(MAC, 1, 1)
        s.unregister_fountain(MAC)
        self.assertIsNone(s.get_fountain(MAC))

    def test_ble_roster_matches_cloud_shape(self):
        s = FountainServer()
        s.register_fountain(_sample_fountain())
        roster = s.ble_roster()
        self.assertIn("list", roster)
        self.assertIn("nextTick", roster)
        self.assertEqual(len(roster["list"]), 1)
        entry = roster["list"][0]
        self.assertEqual(entry["id"], DEVICE_ID)
        self.assertEqual(entry["mac"], MAC)
        self.assertEqual(entry["secret"], "cafebabe1234")
        self.assertEqual(entry["type"], DEVICE_TYPE_CTW3)
        self.assertIn("interval", entry)


# --- Link state machine -----------------------------------------------------

class TestLinkLifecycle(unittest.TestCase):
    def setUp(self):
        self.s = FountainServer()
        # Bypass auto-sync — these tests exercise the post-sync steady state.
        _register_and_mark_synced(self.s, _sample_fountain())

    def _pop(self) -> dict:
        entry = self.s.heartbeat_content_provider()
        self.assertIsNotNone(entry, "expected a heartbeat entry")
        return json.loads(entry["content"])

    def test_idle_initially(self):
        self.assertEqual(self.s._link_state[MAC], LINK_IDLE)
        self.assertEqual(self.s.queue_depth, 0)
        self.assertIsNone(self.s.heartbeat_content_provider())

    def test_send_queues_connect_and_buffers_cmd(self):
        self.s.send_set_mode(MAC, 1, 1)
        # First entry in push queue is a connect (action=1)
        self.assertEqual(self.s._link_state[MAC], LINK_CONNECTING)
        self.assertEqual(self.s.pending_cmd_count, 1)
        self.assertEqual(self.s.queue_depth, 1)
        inner = self._pop()
        self.assertEqual(inner["type"], "connect")
        self.assertEqual(inner["payload"]["connect_action"], 1)
        self.assertEqual(inner["payload"]["device"]["mac"], MAC)

    def test_ble_cmds_withheld_until_connected(self):
        self.s.send_set_mode(MAC, 1, 1)
        self.s.send_reset_filter(MAC)
        # Only ONE push entry (the connect) — BLE cmds must wait for 52 ok
        self.assertEqual(self.s.queue_depth, 1)
        self.assertEqual(self.s.pending_cmd_count, 2)

        # Pop the connect
        self._pop()

        # Now D4 reports connect success
        self.s.handle_event_report(_connect_ok_event())
        self.assertEqual(self.s._link_state[MAC], LINK_DISCONNECTING)
        # Push queue should now contain: ble1, ble2, disconnect
        self.assertEqual(self.s.queue_depth, 3)
        self.assertEqual(self.s.pending_cmd_count, 0)

        i1 = self._pop()
        i2 = self._pop()
        i3 = self._pop()
        self.assertEqual(i1["type"], "ble")
        self.assertEqual(i1["payload"]["payload"]["cmd"], CMD_SET_MODE)
        self.assertEqual(i2["type"], "ble")
        self.assertEqual(i2["payload"]["payload"]["cmd"], CMD_RESET_FILTER)
        self.assertEqual(i3["type"], "connect")
        self.assertEqual(i3["payload"]["connect_action"], 0)

    def test_disconnect_ok_returns_to_idle(self):
        self.s.send_set_mode(MAC, 1, 1)
        # Drain connect
        self._pop()
        self.s.handle_event_report(_connect_ok_event())
        # Drain ble + disconnect (2 entries: 1 cmd + 1 disconnect)
        self._pop(); self._pop()
        # Now D4 reports disconnect done
        self.s.handle_event_report(_disconnect_done_event())
        self.assertEqual(self.s._link_state[MAC], LINK_IDLE)
        self.assertEqual(self.s.queue_depth, 0)

    def test_connect_failure_drops_pending(self):
        self.s.send_set_mode(MAC, 1, 1)
        self.s.send_reset_filter(MAC)
        self._pop()  # drain connect
        self.assertEqual(self.s.pending_cmd_count, 2)
        # D4 reports connect failed
        self.s.handle_event_report(_connect_fail_event())
        self.assertEqual(self.s._link_state[MAC], LINK_IDLE)
        self.assertEqual(self.s.pending_cmd_count, 0)
        self.assertEqual(self.s.queue_depth, 0)

    def test_new_cmds_during_disconnecting_restart_after_disconnect(self):
        self.s.send_set_mode(MAC, 1, 1)
        self._pop()  # connect
        self.s.handle_event_report(_connect_ok_event())
        # Drain ble + disconnect push
        self._pop(); self._pop()
        self.assertEqual(self.s._link_state[MAC], LINK_DISCONNECTING)
        # User submits new cmd while disconnect is in-flight
        self.s.send_reset_filter(MAC)
        # Should be buffered, not yet pushed (still DISCONNECTING)
        self.assertEqual(self.s._link_state[MAC], LINK_DISCONNECTING)
        self.assertEqual(self.s.pending_cmd_count, 1)
        # Once disconnect completes, link restarts
        self.s.handle_event_report(_disconnect_done_event())
        self.assertEqual(self.s._link_state[MAC], LINK_CONNECTING)
        # Push queue now has the new connect
        inner = self._pop()
        self.assertEqual(inner["type"], "connect")
        self.assertEqual(inner["payload"]["connect_action"], 1)


# --- High-level command helpers --------------------------------------------

class TestHighLevelCommands(unittest.TestCase):
    def test_send_set_mode_builds_valid_frame(self):
        s = FountainServer()
        s.register_fountain(_sample_fountain())
        s.send_set_mode(MAC, state_on=1, mode=2)  # NORMAL, pump on
        # Skip connect entry
        s.heartbeat_content_provider()
        s.handle_event_report(_connect_ok_event())
        entry = s.heartbeat_content_provider()
        inner = json.loads(entry["content"])
        self.assertEqual(inner["type"], "ble")
        payload = inner["payload"]["payload"]
        self.assertEqual(payload["cmd"], CMD_SET_MODE)
        # Decode BLE frame
        raw = base64.b64decode(payload["data"])
        frame = parse_frame(raw)
        self.assertEqual(frame.cmd, CMD_SET_MODE)
        # 3-byte payload: [power_status, suspend_status, mode]
        self.assertEqual(frame.data, bytes([1, 0, 2]))

    def test_send_reset_filter_builds_valid_frame(self):
        s = FountainServer()
        s.register_fountain(_sample_fountain())
        s.send_reset_filter(MAC)
        s.heartbeat_content_provider()
        s.handle_event_report(_connect_ok_event())
        entry = s.heartbeat_content_provider()
        inner = json.loads(entry["content"])
        payload = inner["payload"]["payload"]
        self.assertEqual(payload["cmd"], CMD_RESET_FILTER)
        raw = base64.b64decode(payload["data"])
        frame = parse_frame(raw)
        self.assertEqual(frame.cmd, CMD_RESET_FILTER)
        self.assertEqual(frame.data, bytes([0]))

    def test_command_to_unknown_mac_raises(self):
        s = FountainServer()
        with self.assertRaises(ValueError):
            s.send_set_mode("deadbeefdead", 1, 1)

    def test_seq_increments_across_commands(self):
        s = FountainServer()
        s.register_fountain(_sample_fountain())
        s.send_set_mode(MAC, 1, 1)
        s.send_set_mode(MAC, 0, 2)
        s.heartbeat_content_provider()  # connect
        s.handle_event_report(_connect_ok_event())
        e1 = s.heartbeat_content_provider()
        e2 = s.heartbeat_content_provider()
        f1 = parse_frame(base64.b64decode(
            json.loads(e1["content"])["payload"]["payload"]["data"]))
        f2 = parse_frame(base64.b64decode(
            json.loads(e2["content"])["payload"]["payload"]["data"]))
        self.assertEqual(f2.seq, (f1.seq + 1) & 0xFF)

    def test_optimistic_state_update(self):
        s = FountainServer()
        f = _sample_fountain()
        s.register_fountain(f)
        s.send_set_mode(MAC, state_on=1, mode=2)
        # Local state flipped immediately (before any fountain feedback)
        self.assertEqual(f.status.power_status, 1)
        self.assertEqual(f.mode, 2)


# --- Queue management -------------------------------------------------------

class TestSettingsSyncGuard(unittest.TestCase):
    """Guard against pushing HA-default config to the fountain before we've
    seen a real cmd 230 dump — bug observed live 2026-04-22 caused the
    fountain to enter Main-Unit-Failure state.
    """

    def _apply_sync_dump(self, s: FountainServer) -> None:
        """Feed a realistic cmd 230 status dump via the event_report path."""
        # Golden sample from Runde 2 capture (power=1, suspend=0, mode=2, etc.)
        hex_dump = (
            "010002020000000000000052cc640000005797"
            "001534107c640039054e05030300190e10000200000001"
        )
        s.handle_event_report({
            "event_type": "53",
            "content": json.dumps({
                "device": {"mac": MAC, "type": DEVICE_TYPE_CTW3},
                "payload": [{"cmd": 230, "data": base64.b64encode(bytes.fromhex(hex_dump)).decode()}],
            }),
        })

    def test_send_config_update_blocked_before_sync(self):
        s = FountainServer()
        s.register_fountain(_sample_fountain())
        self.assertFalse(s.is_settings_synced(MAC))
        # Drain the auto-sync connect push that register_fountain queued —
        # we're testing the config-write guard, not auto-sync behaviour.
        initial_queue_depth = s.queue_depth
        initial_pending = s.pending_cmd_count
        with self.assertRaises(RuntimeError) as cm:
            s.send_config_update(MAC, lamp_ring_brightness=3)
        self.assertIn("not yet synced", str(cm.exception))
        # Nothing extra got queued by the failed write.
        self.assertEqual(s.queue_depth, initial_queue_depth)
        self.assertEqual(s.pending_cmd_count, initial_pending)

    def test_send_config_update_allowed_after_sync(self):
        s = FountainServer()
        s.register_fountain(_sample_fountain())
        self._apply_sync_dump(s)
        self.assertTrue(s.is_settings_synced(MAC))
        # Now the call succeeds
        s.send_config_update(MAC, lamp_ring_brightness=3)
        self.assertEqual(s.pending_cmd_count, 1)

    def test_sync_flag_per_fountain(self):
        s = FountainServer()
        s.register_fountain(_sample_fountain())
        # Register a second fountain
        f2 = Ctw3State(id=999, mac="ff" * 6, secret="x", sn="x",
                       firmware=0, type_code=2)
        s.register_fountain(f2)
        self._apply_sync_dump(s)  # syncs only MAC, not f2
        self.assertTrue(s.is_settings_synced(MAC))
        self.assertFalse(s.is_settings_synced("ff" * 6))

    def test_unregister_resets_sync_flag(self):
        s = FountainServer()
        s.register_fountain(_sample_fountain())
        self._apply_sync_dump(s)
        self.assertTrue(s.is_settings_synced(MAC))
        s.unregister_fountain(MAC)
        self.assertFalse(s.is_settings_synced(MAC))

    def test_send_set_mode_not_blocked(self):
        """Power/mode/suspend commands don't need the sync guard — they
        don't read current settings, they just push the user's intent."""
        s = FountainServer()
        s.register_fountain(_sample_fountain())
        # Should work without any prior dump
        s.send_set_mode(MAC, state_on=1, mode=2)
        self.assertEqual(s.pending_cmd_count, 1)

    def test_send_reset_filter_not_blocked(self):
        s = FountainServer()
        s.register_fountain(_sample_fountain())
        s.send_reset_filter(MAC)
        self.assertEqual(s.pending_cmd_count, 1)


class TestAutoSyncOnRegister(unittest.TestCase):
    """Ensure fountain registration auto-kicks a BLE session to pull a
    cmd 230 status dump — so `f.settings` is populated from real device
    values before any cmd 221 writes can happen.
    """

    def _apply_sync_dump(self, s: FountainServer) -> None:
        hex_dump = (
            "010002020000000000000052cc640000005797"
            "001534107c640039054e05030300190e10000200000001"
        )
        s.handle_event_report({
            "event_type": "53",
            "content": json.dumps({
                "device": {"mac": MAC, "type": DEVICE_TYPE_CTW3},
                "payload": [{"cmd": 230, "data": base64.b64encode(bytes.fromhex(hex_dump)).decode()}],
            }),
        })

    def test_register_auto_requests_sync(self):
        s = FountainServer()
        s.register_fountain(_sample_fountain())
        # Sync was requested AND state machine moved to CONNECTING
        self.assertIn(MAC, s._sync_requested)
        self.assertEqual(s._link_state[MAC], LINK_CONNECTING)
        # The first thing in the push queue is the connect
        entry = s.heartbeat_content_provider()
        inner = json.loads(entry["content"])
        self.assertEqual(inner["type"], "connect")
        self.assertEqual(inner["payload"]["connect_action"], 1)

    def test_sync_reads_pushed_after_connect_success(self):
        s = FountainServer()
        s.register_fountain(_sample_fountain())
        # Drain the connect
        s.heartbeat_content_provider()
        # D4 reports BLE connected
        s.handle_event_report(_connect_ok_event())
        # State machine should have queued 2x cmd 215 reads,
        # and should NOT have queued a disconnect yet (waiting for cmd 230).
        self.assertEqual(s._link_state[MAC], LINK_CONNECTED)
        entries = []
        while True:
            entry = s.heartbeat_content_provider()
            if entry is None:
                break
            entries.append(json.loads(entry["content"]))
        self.assertEqual(len(entries), 2, "expected 2x cmd 215 reads")
        for e in entries:
            self.assertEqual(e["type"], "ble")
            self.assertEqual(e["payload"]["payload"]["cmd"], 215)
            # Empty data = read
            import base64 as _b64
            raw = _b64.b64decode(e["payload"]["payload"]["data"])
            self.assertEqual(raw[8:-1], b"")  # data portion is empty

    def test_cmd230_during_sync_triggers_disconnect(self):
        s = FountainServer()
        s.register_fountain(_sample_fountain())
        s.heartbeat_content_provider()  # drain connect
        s.handle_event_report(_connect_ok_event())
        # drain the 2 sync reads
        s.heartbeat_content_provider()
        s.heartbeat_content_provider()
        self.assertEqual(s._link_state[MAC], LINK_CONNECTED)
        # Fountain sends cmd 230 dump
        self._apply_sync_dump(s)
        # State transitions: settings_synced set, push queue has disconnect
        self.assertTrue(s.is_settings_synced(MAC))
        self.assertNotIn(MAC, s._sync_requested)
        self.assertEqual(s._link_state[MAC], LINK_DISCONNECTING)
        inner = json.loads(s.heartbeat_content_provider()["content"])
        self.assertEqual(inner["type"], "connect")
        self.assertEqual(inner["payload"]["connect_action"], 0)

    def test_already_synced_fountain_no_auto_sync(self):
        """If the fountain was synced in a previous lifecycle (not normally
        persisted across restarts but possible via register_fountain after
        existing state), no auto-sync should happen.
        """
        s = FountainServer()
        # Pre-mark as synced before registration
        f = _sample_fountain()
        s._settings_synced.add(f.mac)
        s.register_fountain(f)
        # Should NOT have queued anything
        self.assertEqual(s._link_state[MAC], LINK_IDLE)
        self.assertEqual(s.queue_depth, 0)
        self.assertNotIn(MAC, s._sync_requested)

    def test_request_sync_triggers_session(self):
        s = FountainServer()
        # Fully synced, idle
        _register_and_mark_synced(s, _sample_fountain())
        self.assertEqual(s._link_state[MAC], LINK_IDLE)
        self.assertEqual(s.queue_depth, 0)

        # Manually request a fresh sync (settings_synced stays set though)
        s._settings_synced.discard(MAC)  # simulate "stale"
        s.request_sync(MAC)
        self.assertIn(MAC, s._sync_requested)
        self.assertEqual(s._link_state[MAC], LINK_CONNECTING)
        self.assertEqual(s.queue_depth, 1)

    def test_request_sync_unknown_fountain_no_op(self):
        s = FountainServer()
        s.request_sync("ff" * 6)  # no raise, no state change
        self.assertEqual(s.queue_depth, 0)


class TestVerifyAfterWrite(unittest.TestCase):
    """After a send_config_update, the next cmd 230 dump must be compared
    against what we pushed. Mismatch = warning log.
    """

    def _make_dump_bytes(self, cfg_bytes: bytes) -> str:
        """Build a 42-byte cmd 230 dump with the given 12-byte config embedded."""
        assert len(cfg_bytes) == 12
        prefix = bytes.fromhex(
            "010002020000000000000052cc640000005797"
            "001534107c640039054e05"
        )  # 30 bytes (bytes 0-29)
        return base64.b64encode(prefix + cfg_bytes).decode()

    def _send_dump(self, s: FountainServer, cfg_bytes: bytes) -> None:
        s.handle_event_report({
            "event_type": "53",
            "content": json.dumps({
                "device": {"mac": MAC, "type": DEVICE_TYPE_CTW3},
                "payload": [{"cmd": 230, "data": self._make_dump_bytes(cfg_bytes)}],
            }),
        })

    def test_verify_logs_ok_on_match(self):
        s = FountainServer()
        _register_and_mark_synced(s, _sample_fountain())
        # Push a config change, then pretend fountain echoed same config back
        s.send_config_update(MAC, lamp_ring_brightness=3)
        # Build a dump with settings applied
        fresh = s.get_fountain(MAC).settings
        cfg_bytes = bytes([
            fresh.smart_working_time, fresh.smart_sleep_time,
            (fresh.battery_working_time >> 8) & 0xFF, fresh.battery_working_time & 0xFF,
            (fresh.battery_sleep_time >> 8) & 0xFF, fresh.battery_sleep_time & 0xFF,
            fresh.lamp_ring_switch, fresh.lamp_ring_brightness,
            fresh.no_disturbing_switch, fresh.distribution_diagram,
            fresh.smart_inductive_switch, fresh.battery_inductive_switch,
        ])
        # Capture logs
        with self.assertLogs("fountain_server", level="INFO") as logs:
            self._send_dump(s, cfg_bytes)
        self.assertTrue(
            any("verify OK" in m for m in logs.output),
            f"expected 'verify OK' in {logs.output}",
        )
        # Verification slot cleared
        self.assertNotIn(MAC, s._pending_verification)

    def test_verify_logs_mismatch(self):
        s = FountainServer()
        _register_and_mark_synced(s, _sample_fountain())
        s.send_config_update(MAC, lamp_ring_brightness=3)
        # Send a dump with WRONG brightness back
        fresh = s.get_fountain(MAC).settings
        cfg_bytes = bytes([
            fresh.smart_working_time, fresh.smart_sleep_time,
            (fresh.battery_working_time >> 8) & 0xFF, fresh.battery_working_time & 0xFF,
            (fresh.battery_sleep_time >> 8) & 0xFF, fresh.battery_sleep_time & 0xFF,
            fresh.lamp_ring_switch, 1,  # brightness=1 instead of 3
            fresh.no_disturbing_switch, fresh.distribution_diagram,
            fresh.smart_inductive_switch, fresh.battery_inductive_switch,
        ])
        with self.assertLogs("fountain_server", level="WARNING") as logs:
            self._send_dump(s, cfg_bytes)
        self.assertTrue(
            any("verify MISMATCH" in m for m in logs.output),
            f"expected 'verify MISMATCH' in {logs.output}",
        )
        self.assertTrue(
            any("lamp_ring_brightness" in m for m in logs.output),
            "expected mismatched field name in warning",
        )


class TestQueueManagement(unittest.TestCase):
    def test_clear_queue_drops_everything(self):
        s = FountainServer()
        s.register_fountain(_sample_fountain())
        s.send_set_mode(MAC, 1, 1)
        s.send_reset_filter(MAC)
        self.assertGreater(s.queue_depth + s.pending_cmd_count, 0)
        n = s.clear_queue()
        self.assertGreater(n, 0)
        self.assertEqual(s.queue_depth, 0)
        self.assertEqual(s.pending_cmd_count, 0)
        self.assertEqual(s._link_state[MAC], LINK_IDLE)

    def test_heartbeat_returns_none_when_empty(self):
        s = FountainServer()
        self.assertIsNone(s.heartbeat_content_provider())


# --- Event-report parsing ---------------------------------------------------

class TestEventReport(unittest.TestCase):
    def test_event_51_is_informational(self):
        s = FountainServer()
        # Bypass auto-sync so initial state is IDLE, not CONNECTING.
        _register_and_mark_synced(s, _sample_fountain())
        # Should not raise and should not change state
        s.handle_event_report({
            "event_type": "51",
            "content": json.dumps({
                "start_time": 1,
                "start_reason": 2,
                "action": 1,
                "device": {"mac": MAC, "type": DEVICE_TYPE_CTW3},
            }),
        })
        self.assertEqual(s._link_state[MAC], LINK_IDLE)

    def test_event_52_unknown_fountain_ignored(self):
        s = FountainServer()
        # No fountain registered — must not raise
        s.handle_event_report(_connect_ok_event(mac="ffffffffffff"))

    def test_event_53_logs_response_without_raising(self):
        s = FountainServer()
        s.register_fountain(_sample_fountain())
        s.handle_event_report({
            "event_type": "53",
            "content": json.dumps({
                "device": {"mac": MAC, "type": DEVICE_TYPE_CTW3},
                "payload": [{"cmd": 220, "data": "AQAC"}],
            }),
        })

    def test_invalid_content_ignored(self):
        s = FountainServer()
        _register_and_mark_synced(s, _sample_fountain())
        s.handle_event_report({"event_type": "52", "content": "not-json"})
        # State unchanged
        self.assertEqual(s._link_state[MAC], LINK_IDLE)


# --- CTW3 cloud-endpoint emulation -----------------------------------------

class TestCtw3EndpointEmulation(unittest.TestCase):
    def test_signup_returns_cloud_detail(self):
        s = FountainServer()
        s.register_fountain(_sample_fountain())
        resp = s.handle_ctw3_post("signup", {"mac": MAC, "sn": "EXAMPLEFAKESN001"}, b"")
        self.assertIsNotNone(resp)
        self.assertEqual(resp["id"], DEVICE_ID)
        self.assertEqual(resp["typeCode"], 2)
        self.assertIn("settings", resp)

    def test_signup_unknown_mac(self):
        s = FountainServer()
        resp = s.handle_ctw3_post("signup", {"mac": "ffffffffffff", "sn": "xxx"}, b"")
        self.assertIsNone(resp)

    def test_link_uses_id(self):
        s = FountainServer()
        s.register_fountain(_sample_fountain())
        resp = s.handle_ctw3_post("link", {"id": str(DEVICE_ID), "mac": MAC}, b"")
        self.assertIsNotNone(resp)
        self.assertEqual(resp["mac"], MAC)

    def test_device_data(self):
        s = FountainServer()
        s.register_fountain(_sample_fountain())
        resp = s.handle_ctw3_post("deviceData", {"id": str(DEVICE_ID)}, b"")
        self.assertIsNotNone(resp)
        self.assertEqual(resp["id"], DEVICE_ID)

    def test_update_applies_kv(self):
        s = FountainServer()
        s.register_fountain(_sample_fountain())
        kv = json.dumps({"mode": 1, "powerStatus": 1, "batteryPercent": 33})
        resp = s.handle_ctw3_post("update", {"id": str(DEVICE_ID), "kv": kv}, b"")
        self.assertEqual(resp, "success")
        updated = s.get_fountain(MAC)
        self.assertEqual(updated.mode, 1)
        self.assertEqual(updated.electricity.battery_percent, 33)

    def test_update_unknown_id(self):
        s = FountainServer()
        kv = json.dumps({"mode": 1})
        resp = s.handle_ctw3_post("update", {"id": "999999", "kv": kv}, b"")
        self.assertIsNone(resp)

    def test_stubs_return_empty(self):
        s = FountainServer()
        for suffix in ("addWaterRecord", "getWorkRecord", "getDrinkCompare",
                       "energyCalculation", "upgradeCheck"):
            resp = s.handle_ctw3_post(suffix, {"id": "1"}, b"")
            self.assertEqual(resp, {}, f"stub {suffix!r} should return empty dict")

    def test_unknown_suffix_returns_none(self):
        s = FountainServer()
        resp = s.handle_ctw3_post("weirdNewEndpoint", {}, b"")
        self.assertIsNone(resp)


# --- /ctw3/update state propagation ----------------------------------------

class TestStateReport(unittest.TestCase):
    def test_applies_kv_update(self):
        s = FountainServer()
        s.register_fountain(_sample_fountain())
        s.on_fountain_state_report(MAC, {
            "mode": 1,
            "powerStatus": 1,
            "suspendStatus": 0,
            "batteryPercent": 45,
            "batteryVoltage": 3650,
            "filterPercent": 50,
            "todayPumpRunTime": 200,
        })
        updated = s.get_fountain(MAC)
        self.assertEqual(updated.mode, 1)
        self.assertEqual(updated.status.power_status, 1)
        self.assertEqual(updated.electricity.battery_percent, 45)
        self.assertEqual(updated.filter_percent, 50)
        self.assertEqual(updated.today_pump_run_time, 200)

    def test_unknown_mac_ignored(self):
        s = FountainServer()
        s.on_fountain_state_report("ffffffffffff", {"mode": 1})  # no raise


# --- Listeners -------------------------------------------------------------

class TestDrinkEventTracking(unittest.TestCase):
    """Drink events are derived from cmd 230 byte 19 (motion flag)
    transitions combined with the runtime counter (bytes 11-12).

    byte 19 = 2 → motion detected (cat at fountain)
    byte 19 = 0 → no motion
    counter ticks at 1 Hz while pump runs.
    """

    # Helper to build a cmd 230 dump with custom byte 19 + counter values
    @staticmethod
    def _build_dump(motion: int, counter: int, suspend: int = 1) -> bytes:
        # 42-byte cmd 230 template, we vary only bytes 1, 11-12, 19.
        template = (
            "01000202"                      # bytes 0-3
            "0000000000000000"              # bytes 4-10 (7 bytes padding + start of counter)
            "000064"                        # bytes 11-13 (counter + battery)
            "0000000000"                    # bytes 14-18
            "00"                            # byte 19 (motion)
            "15000000"                      # bytes 20-23
            "6400000005000503030019"        # bytes 24-34
            "0e100002000000010001"          # bytes 35-41 (need exactly 7 more bytes total)
        )
        data = bytearray.fromhex(template)
        # Trim or pad to exactly 42 bytes
        if len(data) > 42:
            data = data[:42]
        elif len(data) < 42:
            data.extend(b"\x00" * (42 - len(data)))
        data[1] = suspend
        data[11] = (counter >> 8) & 0xFF
        data[12] = counter & 0xFF
        data[19] = motion
        return bytes(data)

    @staticmethod
    def _send_dump(s: FountainServer, raw: bytes) -> None:
        s.handle_event_report({
            "event_type": "53",
            "content": json.dumps({
                "device": {"mac": MAC, "type": DEVICE_TYPE_CTW3},
                "payload": [{"cmd": 230, "data": base64.b64encode(raw).decode()}],
            }),
        })

    def test_motion_off_no_event(self):
        s = FountainServer()
        _register_and_mark_synced(s, _sample_fountain())
        f = s.get_fountain(MAC)
        self._send_dump(s, self._build_dump(motion=0, counter=100))
        self._send_dump(s, self._build_dump(motion=0, counter=105))
        self.assertEqual(f.drinks_today, 0)
        self.assertEqual(f.last_drink_duration, 0)
        self.assertEqual(f.status.detect_status, 0)

    def test_single_drink_event_counts_and_durations(self):
        s = FountainServer()
        _register_and_mark_synced(s, _sample_fountain())
        f = s.get_fountain(MAC)
        # First dump: no motion, counter at 100
        self._send_dump(s, self._build_dump(motion=0, counter=100))
        # Second dump: motion starts, counter at 102
        self._send_dump(s, self._build_dump(motion=2, counter=102))
        self.assertEqual(f.status.detect_status, 1)
        self.assertEqual(f.drinks_today, 0)  # event not ended yet
        # Third dump: motion gone, counter at 115 (13s of pumping)
        self._send_dump(s, self._build_dump(motion=0, counter=115))
        self.assertEqual(f.status.detect_status, 0)
        self.assertEqual(f.drinks_today, 1)
        self.assertEqual(f.last_drink_duration, 13)
        self.assertEqual(f.total_drink_duration_today, 13)
        self.assertNotEqual(f.last_drink_at, "")

    def test_multiple_drink_events_accumulate(self):
        s = FountainServer()
        _register_and_mark_synced(s, _sample_fountain())
        f = s.get_fountain(MAC)
        # Event 1: +5s
        self._send_dump(s, self._build_dump(motion=0, counter=100))
        self._send_dump(s, self._build_dump(motion=2, counter=100))
        self._send_dump(s, self._build_dump(motion=0, counter=105))
        # Event 2: +8s
        self._send_dump(s, self._build_dump(motion=2, counter=120))
        self._send_dump(s, self._build_dump(motion=0, counter=128))
        self.assertEqual(f.drinks_today, 2)
        self.assertEqual(f.last_drink_duration, 8)
        self.assertEqual(f.total_drink_duration_today, 5 + 8)

    def test_counter_rollover_during_drink(self):
        """If the 16-bit counter rolls over (65535 → 0) during a drink,
        the duration calculation must still be correct."""
        s = FountainServer()
        _register_and_mark_synced(s, _sample_fountain())
        f = s.get_fountain(MAC)
        # Start near counter top
        self._send_dump(s, self._build_dump(motion=0, counter=65530))
        self._send_dump(s, self._build_dump(motion=2, counter=65531))
        # End after rollover
        self._send_dump(s, self._build_dump(motion=0, counter=10))
        # Expected duration: (65535 - 65531) + 1 + 10 = 15? Actually the
        # correct formula is (counter_now - start_counter) + 65536
        # if negative. So 10 - 65531 + 65536 = 15.
        self.assertEqual(f.last_drink_duration, 15)

    def test_motion_raw_value_1_treated_as_no_motion(self):
        """Only byte_19 == 2 counts as motion; other values are treated as off."""
        s = FountainServer()
        _register_and_mark_synced(s, _sample_fountain())
        f = s.get_fountain(MAC)
        self._send_dump(s, self._build_dump(motion=0, counter=100))
        self._send_dump(s, self._build_dump(motion=1, counter=102))  # value 1
        self.assertEqual(f.status.detect_status, 0)
        self.assertEqual(f.drinks_today, 0)

    def test_day_change_resets_counters(self):
        s = FountainServer()
        _register_and_mark_synced(s, _sample_fountain())
        f = s.get_fountain(MAC)
        # Simulate a drink from yesterday
        f.drinks_today = 5
        f.total_drink_duration_today = 300
        f.drinks_today_date = "2026-04-21"
        # Send a dump today — counter tracking should reset the daily counters
        self._send_dump(s, self._build_dump(motion=0, counter=100))
        self.assertEqual(f.drinks_today, 0)
        self.assertEqual(f.total_drink_duration_today, 0)
        self.assertNotEqual(f.drinks_today_date, "2026-04-21")

    def test_reset_daily_counters_if_needed_without_incoming_data(self):
        """Time-triggered reset must work even if no cmd 230 arrives.

        This is the midnight-callback path: fountain is offline across
        midnight, no status dumps come in, but the reset fires anyway via
        the HA time-change timer.
        """
        s = FountainServer()
        _register_and_mark_synced(s, _sample_fountain())
        f = s.get_fountain(MAC)
        # Set up yesterday's state
        f.drinks_today = 7
        f.total_drink_duration_today = 420
        f.drinks_today_date = "1999-01-01"  # old date, definitely not today

        reset_macs = s.reset_daily_counters_if_needed()
        self.assertEqual(reset_macs, [MAC])
        self.assertEqual(f.drinks_today, 0)
        self.assertEqual(f.total_drink_duration_today, 0)
        self.assertNotEqual(f.drinks_today_date, "1999-01-01")

    def test_reset_daily_counters_if_needed_is_noop_on_same_day(self):
        """Second call on the same day must not touch anything or notify."""
        s = FountainServer()
        _register_and_mark_synced(s, _sample_fountain())
        notifications: list = []
        s.register_update_listener(lambda: notifications.append(1))
        # First call sets today
        s.reset_daily_counters_if_needed()
        notifications.clear()
        # Second call: no changes expected
        reset_macs = s.reset_daily_counters_if_needed()
        self.assertEqual(reset_macs, [])
        self.assertEqual(notifications, [])


class TestPersistentMode(unittest.TestCase):
    """In persistent_mode, the BLE link is kept open continuously so that
    cmd 230 dumps (and therefore motion/drink events) are observed live.
    """

    @staticmethod
    def _build_minimal_230() -> bytes:
        # Minimal valid 42-byte cmd 230 dump
        return bytes.fromhex(
            "010002020000000000000052cc640000005797"
            "001534107c640039054e05030300190e10000200000001"
        )

    @staticmethod
    def _send_dump(s: FountainServer, raw: bytes) -> None:
        s.handle_event_report({
            "event_type": "53",
            "content": json.dumps({
                "device": {"mac": MAC, "type": DEVICE_TYPE_CTW3},
                "payload": [{"cmd": 230, "data": base64.b64encode(raw).decode()}],
            }),
        })

    def test_register_triggers_connect_even_without_commands(self):
        """Persistent mode should open a BLE session immediately on register,
        because we want to be connected even with no pending work."""
        s = FountainServer(persistent_mode=True)
        s.register_fountain(_sample_fountain())
        self.assertEqual(s._link_state[MAC], LINK_CONNECTING)
        self.assertEqual(s.queue_depth, 1)  # connect push

    def test_stays_connected_after_sync_dump(self):
        """After sync dump arrives in persistent mode, we STAY connected
        (session mode would disconnect)."""
        s = FountainServer(persistent_mode=True)
        s.register_fountain(_sample_fountain())
        # Drain connect
        s.heartbeat_content_provider()
        s.handle_event_report(_connect_ok_event())
        # Drain 2 sync reads
        s.heartbeat_content_provider()
        s.heartbeat_content_provider()
        self.assertEqual(s._link_state[MAC], LINK_CONNECTED)
        # Send cmd 230 dump — session mode would disconnect; persistent stays
        self._send_dump(s, self._build_minimal_230())
        self.assertEqual(s._link_state[MAC], LINK_CONNECTED)
        self.assertTrue(s.is_settings_synced(MAC))
        self.assertEqual(s.queue_depth, 0)  # no disconnect push queued

    def test_reconnects_after_unexpected_disconnect(self):
        """If fountain/D4 drops the session, persistent mode reconnects."""
        s = FountainServer(persistent_mode=True)
        s.register_fountain(_sample_fountain())
        s.heartbeat_content_provider()  # drain connect
        s.handle_event_report(_connect_ok_event())
        # Now fountain reports an unexpected disconnect
        s.handle_event_report(_disconnect_done_event())
        # Persistent mode should immediately re-queue a connect
        self.assertEqual(s._link_state[MAC], LINK_CONNECTING)

    def test_user_command_while_persistent_connected_just_pushes_cmd(self):
        """In persistent_mode, submitting a user command while CONNECTED
        doesn't open/close session — just pushes the BLE frame."""
        s = FountainServer(persistent_mode=True)
        s.register_fountain(_sample_fountain())
        # Get into CONNECTED + synced state
        s.heartbeat_content_provider()  # connect
        s.handle_event_report(_connect_ok_event())
        s.heartbeat_content_provider(); s.heartbeat_content_provider()  # 2x sync reads
        self._send_dump(s, self._build_minimal_230())
        self.assertEqual(s._link_state[MAC], LINK_CONNECTED)
        initial_queue = s.queue_depth

        # Now user toggles power
        s.send_set_mode(MAC, state_on=1, mode=2)
        # Still CONNECTED (no disconnect pushed), just one ble cmd queued
        self.assertEqual(s._link_state[MAC], LINK_CONNECTED)
        self.assertEqual(s.queue_depth, initial_queue + 1)
        # Next heartbeat returns the ble cmd (no connect/disconnect involved)
        entry = s.heartbeat_content_provider()
        inner = json.loads(entry["content"])
        self.assertEqual(inner["type"], "ble")
        self.assertEqual(inner["payload"]["payload"]["cmd"], CMD_SET_MODE)

    def test_session_mode_still_disconnects_as_before(self):
        """Verify session-mode (default) still disconnects after sync — so
        the existing behaviour is untouched for tests / callers that don't
        opt into persistent_mode."""
        s = FountainServer(persistent_mode=False)
        s.register_fountain(_sample_fountain())
        s.heartbeat_content_provider()  # connect
        s.handle_event_report(_connect_ok_event())
        s.heartbeat_content_provider(); s.heartbeat_content_provider()  # sync reads
        self._send_dump(s, self._build_minimal_230())
        # Session mode: transitions to DISCONNECTING after dump
        self.assertEqual(s._link_state[MAC], LINK_DISCONNECTING)


class TestConnectStuckRecovery(unittest.TestCase):
    """Observed live 2026-04-22: the D4 can leave us hanging in
    LINK_CONNECTING for 20+ min without any event_type=52 response (the
    fountain goes into deep sleep and ignores the connect request).
    We un-stick by timing out CONNECTING after N seconds, then retrying
    with exponential backoff.
    """

    def test_connecting_timeout_transitions_to_idle(self):
        from fountain_server import CONNECT_ATTEMPT_TIMEOUT_SEC
        s = FountainServer()
        _register_and_mark_synced(s, _sample_fountain())
        # Force a CONNECTING session and then backdate _connecting_since
        s.send_set_mode(MAC, 1, 1)
        self.assertEqual(s._link_state[MAC], LINK_CONNECTING)
        s.heartbeat_content_provider()  # drain connect push
        # Pretend we've been CONNECTING longer than the timeout
        import time as _time
        s._connecting_since[MAC] = _time.time() - CONNECT_ATTEMPT_TIMEOUT_SEC - 1
        # Next heartbeat tick notices the stuck state
        s.heartbeat_content_provider()
        # Link was bailed back to IDLE; retry-at set in the future
        self.assertEqual(s._link_state[MAC], LINK_IDLE)
        self.assertGreater(s._next_retry_at[MAC], _time.time())
        self.assertEqual(s._connect_fail_count[MAC], 1)

    def test_backoff_prevents_immediate_retry(self):
        s = FountainServer(persistent_mode=True)
        _register_and_mark_synced(s, _sample_fountain())
        # Simulate an immediate failed connect
        s.send_set_mode(MAC, 1, 1)
        s.heartbeat_content_provider()  # drain connect push
        s.handle_event_report(_connect_fail_event())  # result=3
        # Should have scheduled a retry, state IDLE
        self.assertEqual(s._link_state[MAC], LINK_IDLE)
        self.assertEqual(s._connect_fail_count[MAC], 1)
        import time as _time
        self.assertGreater(s._next_retry_at[MAC], _time.time())
        # Heartbeat should NOT try to connect yet (still in cooldown)
        initial_queue = s.queue_depth
        s.heartbeat_content_provider()
        self.assertEqual(s.queue_depth, initial_queue)  # no new connect push
        self.assertEqual(s._link_state[MAC], LINK_IDLE)

    def test_backoff_expires_and_retry_happens(self):
        s = FountainServer(persistent_mode=True)
        _register_and_mark_synced(s, _sample_fountain())
        s.send_set_mode(MAC, 1, 1)
        s.heartbeat_content_provider()  # connect
        s.handle_event_report(_connect_fail_event())
        # Fast-forward past the backoff window
        import time as _time
        s._next_retry_at[MAC] = _time.time() - 1  # already expired
        # Heartbeat should now trigger a new connect attempt
        s.heartbeat_content_provider()
        self.assertEqual(s._link_state[MAC], LINK_CONNECTING)

    def test_fail_counter_escalates_backoff(self):
        from fountain_server import CONNECT_RETRY_BACKOFF_SEC
        s = FountainServer(persistent_mode=True)
        _register_and_mark_synced(s, _sample_fountain())
        import time as _time
        for expected_idx in range(len(CONNECT_RETRY_BACKOFF_SEC) + 2):
            s.send_set_mode(MAC, 1, 1)
            s.heartbeat_content_provider()  # connect push
            s.handle_event_report(_connect_fail_event())
            idx = min(expected_idx, len(CONNECT_RETRY_BACKOFF_SEC) - 1)
            expected_delay = CONNECT_RETRY_BACKOFF_SEC[idx]
            actual_delay = s._next_retry_at[MAC] - _time.time()
            # Allow small floating-point / test-execution slack.
            self.assertAlmostEqual(actual_delay, expected_delay, delta=1.0)
            # Clear backoff for next iteration to allow re-attempt
            s._next_retry_at[MAC] = _time.time() - 1

    def test_success_resets_fail_count(self):
        s = FountainServer(persistent_mode=True)
        _register_and_mark_synced(s, _sample_fountain())
        # Two failures
        s.send_set_mode(MAC, 1, 1)
        s.heartbeat_content_provider()
        s.handle_event_report(_connect_fail_event())
        import time as _time
        s._next_retry_at[MAC] = _time.time() - 1
        s.send_set_mode(MAC, 1, 1)
        s.heartbeat_content_provider()
        s.handle_event_report(_connect_fail_event())
        self.assertEqual(s._connect_fail_count[MAC], 2)
        s._next_retry_at[MAC] = _time.time() - 1
        # Now succeed
        s.send_set_mode(MAC, 1, 1)
        s.heartbeat_content_provider()
        s.handle_event_report(_connect_ok_event())
        self.assertEqual(s._connect_fail_count[MAC], 0)
        self.assertNotIn(MAC, s._next_retry_at)


class TestSessionLiveness(unittest.TestCase):
    """Silent BLE drops (D4 keeps CONNECTED state but fountain went
    away without event 52 action=0) would leave us stuck. We detect
    this by timing out on cmd 230 dumps and force-recycling.
    """

    def test_liveness_timeout_forces_reconnect(self):
        from fountain_server import SESSION_LIVENESS_TIMEOUT_SEC
        s = FountainServer(persistent_mode=True)
        _register_and_mark_synced(s, _sample_fountain())
        # Put into a "connected" state
        s._link_state[MAC] = LINK_CONNECTED
        import time as _time
        s._last_dump_at[MAC] = _time.time() - SESSION_LIVENESS_TIMEOUT_SEC - 1
        # Heartbeat tick should notice silence + recycle through IDLE
        s.heartbeat_content_provider()
        # After liveness timeout: state back to IDLE, sync requested,
        # and a fresh connect push is queued.
        self.assertIn(MAC, s._sync_requested)
        # Drive_link ran again, state now CONNECTING
        self.assertEqual(s._link_state[MAC], LINK_CONNECTING)

    def test_liveness_does_not_fire_if_dumps_flowing(self):
        s = FountainServer(persistent_mode=True)
        _register_and_mark_synced(s, _sample_fountain())
        s._link_state[MAC] = LINK_CONNECTED
        import time as _time
        # Recent dump — well within the timeout window
        s._last_dump_at[MAC] = _time.time() - 5
        s.heartbeat_content_provider()
        # Still CONNECTED, nothing forced
        self.assertEqual(s._link_state[MAC], LINK_CONNECTED)

    def test_cmd230_dump_refreshes_liveness_timer(self):
        s = FountainServer(persistent_mode=True)
        _register_and_mark_synced(s, _sample_fountain())
        s._link_state[MAC] = LINK_CONNECTED
        import time as _time
        s._last_dump_at[MAC] = 0  # very stale
        # Simulate an incoming cmd 230 dump
        hex_dump = (
            "010002020000000000000052cc640000005797"
            "001534107c640039054e05030300190e10000200000001"
        )
        s.handle_event_report({
            "event_type": "53",
            "content": json.dumps({
                "device": {"mac": MAC, "type": DEVICE_TYPE_CTW3},
                "payload": [{"cmd": 230, "data": base64.b64encode(bytes.fromhex(hex_dump)).decode()}],
            }),
        })
        # _last_dump_at should have been refreshed to ~now
        self.assertAlmostEqual(s._last_dump_at[MAC], _time.time(), delta=1.0)


class TestListeners(unittest.TestCase):
    def test_register_fires_listener(self):
        s = FountainServer()
        events = []
        s.register_update_listener(lambda: events.append("u"))
        s.register_fountain(_sample_fountain())
        self.assertEqual(events, ["u"])

    def test_state_report_fires_listener(self):
        s = FountainServer()
        s.register_fountain(_sample_fountain())
        events = []
        s.register_update_listener(lambda: events.append("u"))
        s.on_fountain_state_report(MAC, {"mode": 1})
        self.assertEqual(len(events), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
