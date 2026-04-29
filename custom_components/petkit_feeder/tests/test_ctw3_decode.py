"""Tests for CTW3 BLE-payload decoders.

Golden samples are taken verbatim from the live capture 2026-04-21
(docs/path_b_evidence/ha_capture_full_protocol.jsonl) and tied to
the cmd 220 / cmd 221 pushes that preceded them, so the expected
power/suspend/mode values and the embedded config block are known
independently of this module's implementation.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ctw3_decode import (
    Cmd230Status,
    FountainConfig,
    MODE_NORMAL,
    MODE_SMART,
    ModePayload,
)


class TestFountainConfig(unittest.TestCase):
    def test_parse_default_config(self):
        # From capture Runde 1 t=0: `030300190e10000200000001`
        # Bytes 6-7 = 00 02 → lamp_switch=0 (off), brightness=2 (medium)
        raw = bytes.fromhex("030300190e10000200000001")
        cfg = FountainConfig.parse(raw)
        self.assertEqual(cfg.smart_working_time, 3)
        self.assertEqual(cfg.smart_sleep_time, 3)
        self.assertEqual(cfg.battery_working_time, 0x0019)   # 25 seconds
        self.assertEqual(cfg.battery_sleep_time, 0x0e10)     # 3600 seconds (1h)
        self.assertEqual(cfg.lamp_ring_switch, 0)
        self.assertEqual(cfg.lamp_ring_brightness, 2)
        self.assertEqual(cfg.no_disturbing_switch, 0)
        self.assertEqual(cfg.distribution_diagram, 0)
        self.assertEqual(cfg.smart_inductive_switch, 0)
        self.assertEqual(cfg.battery_inductive_switch, 1)

    def test_parse_after_cmd221_changes(self):
        # From Runde 1 t=1233: smart=4/4 min, batt_work=30s, batt_sleep=3000s,
        # lamp_off, brightness=2, smart_inductive=1, battery_inductive=1
        raw = bytes.fromhex("0404001e0bb8000200000101")
        cfg = FountainConfig.parse(raw)
        self.assertEqual(cfg.smart_working_time, 4)
        self.assertEqual(cfg.smart_sleep_time, 4)
        self.assertEqual(cfg.battery_working_time, 30)
        self.assertEqual(cfg.battery_sleep_time, 3000)
        self.assertEqual(cfg.lamp_ring_switch, 0)
        self.assertEqual(cfg.lamp_ring_brightness, 2)
        self.assertEqual(cfg.smart_inductive_switch, 1)
        self.assertEqual(cfg.battery_inductive_switch, 1)

    def test_parse_lamp_on_bright_medium(self):
        # From Runde 2 t=1413: user turned on display light (byte 6 → 01)
        raw = bytes.fromhex("040400320e10010200000101")
        cfg = FountainConfig.parse(raw)
        self.assertEqual(cfg.lamp_ring_switch, 1)
        self.assertEqual(cfg.lamp_ring_brightness, 2)

    def test_parse_lamp_on_bright_high(self):
        # From Runde 2 t=1434: user set brightness to HIGH (byte 7 → 03)
        raw = bytes.fromhex("040400320e10010300000101")
        cfg = FountainConfig.parse(raw)
        self.assertEqual(cfg.lamp_ring_switch, 1)
        self.assertEqual(cfg.lamp_ring_brightness, 3)

    def test_parse_lamp_off_brightness_remembered(self):
        # From Runde 2 t=1456: user turned lamp OFF — brightness 3 stays
        raw = bytes.fromhex("040400320e10000300000101")
        cfg = FountainConfig.parse(raw)
        self.assertEqual(cfg.lamp_ring_switch, 0)
        self.assertEqual(cfg.lamp_ring_brightness, 3)

    def test_parse_final_energy_settings(self):
        # From Runde 2 t=1552: smart=6/7 min (user changed via Energieverwaltung)
        raw = bytes.fromhex("060700320e10000300000101")
        cfg = FountainConfig.parse(raw)
        self.assertEqual(cfg.smart_working_time, 6)
        self.assertEqual(cfg.smart_sleep_time, 7)
        self.assertEqual(cfg.battery_working_time, 50)
        self.assertEqual(cfg.battery_sleep_time, 3600)

    def test_roundtrip(self):
        # Every byte must survive parse -> to_bytes.
        # Golden samples from both captures.
        samples = [
            "030300190e10000200000001",         # Runde 1 default
            "0303001e0bb8000200000001",         # Runde 1 after battery change
            "0303001e0bb8000201000001",         # Runde 1 noDisturbing=1
            "0303001e0bb8000200000101",         # Runde 1 smartInductive=1
            "0404001e0bb8000200000101",         # Runde 1 smart=4/4
            "040400320e10010200000101",         # Runde 2 lamp on, bright=2
            "040400320e10010300000101",         # Runde 2 lamp on, bright=3 (HIGH)
            "040400320e10000300000101",         # Runde 2 lamp off, bright=3 remembered
            "060700320e10000300000101",         # Runde 2 smart=6/7
        ]
        for hex_s in samples:
            raw = bytes.fromhex(hex_s)
            cfg = FountainConfig.parse(raw)
            self.assertEqual(cfg.to_bytes(), raw, f"roundtrip failed for {hex_s}")

    def test_too_short_raises(self):
        with self.assertRaises(ValueError):
            FountainConfig.parse(b"\x00" * 11)


class TestModePayload(unittest.TestCase):
    def test_roundtrip(self):
        for power, suspend, mode in [(1, 0, 2), (1, 1, 2), (1, 1, 1), (0, 0, 2)]:
            p = ModePayload(power, suspend, mode)
            self.assertEqual(ModePayload.parse(p.to_bytes()), p)

    def test_too_short_raises(self):
        with self.assertRaises(ValueError):
            ModePayload.parse(b"\x01\x00")


class TestCmd230Status(unittest.TestCase):
    # Golden sample from capture t=362.8s, directly after cmd 220 push [1,0,2]
    # (power on, suspend=0, mode=NORMAL). Expected core fields:
    #   power_status=1, suspend_status=0, mode=NORMAL, battery=100%, filter=100%
    GOLDEN_HEX = (
        "010002020000000000000052cc640000005797"
        "001534107c640039054e05030300190e10000200000001"
    )

    def test_parse_golden_sample(self):
        raw = bytes.fromhex(self.GOLDEN_HEX)
        self.assertEqual(len(raw), 42)
        s = Cmd230Status.parse(raw)
        self.assertEqual(s.power_status, 1)
        self.assertEqual(s.suspend_status, 0)
        self.assertEqual(s.mode, MODE_NORMAL)
        self.assertEqual(s.default_mode, MODE_NORMAL)
        self.assertEqual(s.battery_percent, 100)
        self.assertEqual(s.filter_percent, 100)
        self.assertEqual(s.wifi_rssi_magnitude, 0x34)
        self.assertEqual(s.wifi_rssi_dbm(), -52)
        # Embedded config must match the known pre-test defaults.
        self.assertEqual(s.config.smart_working_time, 3)
        self.assertEqual(s.config.smart_sleep_time, 3)
        self.assertEqual(s.config.lamp_ring_switch, 0)     # lamp off
        self.assertEqual(s.config.lamp_ring_brightness, 2)  # medium
        self.assertEqual(s.config.battery_inductive_switch, 1)

    def test_parse_sample_with_smart_mode(self):
        # From capture t=383.9s, after cmd 220 push [1,1,1] (SMART + paused)
        raw = bytes.fromhex(
            "010101020000000000000052d66401000057a100"
            "153d107c64003905330503030000190e10000200000001"[:84]
        )
        # Trim to exactly 42 bytes
        raw = raw[:42]
        s = Cmd230Status.parse(raw)
        self.assertEqual(s.power_status, 1)
        self.assertEqual(s.suspend_status, 1)
        self.assertEqual(s.mode, MODE_SMART)

    def test_parse_sample_with_changed_config(self):
        # From capture t=1232.9s, config changed to smart=4/4 + smartInductive=1
        raw = bytes.fromhex(
            "0101020200000000000000551f6400000059ea"
            "00153a107f6400390530050404001e0bb8000200000101"
        )
        self.assertEqual(len(raw), 42)
        s = Cmd230Status.parse(raw)
        self.assertEqual(s.config.smart_working_time, 4)
        self.assertEqual(s.config.smart_sleep_time, 4)
        self.assertEqual(s.config.smart_inductive_switch, 1)
        self.assertEqual(s.config.battery_inductive_switch, 1)

    def test_too_short_raises(self):
        with self.assertRaises(ValueError):
            Cmd230Status.parse(b"\x00" * 41)

    def test_motion_raw_and_motion_detected(self):
        """Byte 19 = 2 means motion detected (confirmed 2026-04-22 live trigger)."""
        # Make a dump with byte_19 = 2
        raw = bytearray.fromhex(self.GOLDEN_HEX)
        raw[19] = 2
        s = Cmd230Status.parse(bytes(raw))
        self.assertEqual(s.motion_raw, 2)
        self.assertTrue(s.motion_detected)

        # And with byte_19 = 0
        raw[19] = 0
        s = Cmd230Status.parse(bytes(raw))
        self.assertEqual(s.motion_raw, 0)
        self.assertFalse(s.motion_detected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
