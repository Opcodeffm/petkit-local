"""Tests for BLE control command wrapping.

Format is fixed by live-captured evidence (see
`docs/path_b_evidence/ha_capture_full_protocol.jsonl`): the cloud pushes
`type:"connect"` (with connect_action) and `type:"ble"` (with nested
payload.payload.{data,cmd}) heartbeat-result entries.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import unittest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ble_control import (
    BleControlCommand,
    build_content_ble,
    build_content_connect,
    wrap_ble,
    wrap_connect,
)
from ble_frame import DEVICE_TYPE_CTW3, build_frame, CMD_SET_MODE, TYPE_REQUEST


def _sample_cmd(seq: int = 0) -> BleControlCommand:
    """Deterministic CTW3 'set mode to SMART, pump on' command."""
    frame = build_frame(seq=seq, cmd=CMD_SET_MODE, type_=TYPE_REQUEST, data=[1, 1])
    return BleControlCommand(
        fountain_id=400000001,
        fountain_mac="aabbcc112233",
        cmd_code=CMD_SET_MODE,
        frame_bytes=frame,
        device_type=DEVICE_TYPE_CTW3,
        timestamp=1776770000.0,
    )


class TestBuildContentBle(unittest.TestCase):
    """Match the exact shape observed on the wire."""

    def test_matches_observed_envelope(self):
        s = build_content_ble(_sample_cmd())
        obj = json.loads(s)

        # Outer envelope
        self.assertEqual(obj["msgType"], 2)
        self.assertEqual(obj["type"], "ble")
        self.assertEqual(obj["timestamp"], 1776770000)
        self.assertIn("payload", obj)

        # Outer payload: device + inner payload + timestamp
        outer_payload = obj["payload"]
        self.assertEqual(outer_payload["device"]["type"], DEVICE_TYPE_CTW3)
        self.assertEqual(outer_payload["device"]["mac"], "aabbcc112233")
        self.assertEqual(outer_payload["timestamp"], 1776770000)

        # Nested payload.payload: cmd + base64 data
        inner = outer_payload["payload"]
        self.assertEqual(inner["cmd"], CMD_SET_MODE)
        self.assertIn("data", inner)

    def test_data_is_base64_of_raw_frame(self):
        cmd = _sample_cmd(seq=42)
        s = build_content_ble(cmd)
        obj = json.loads(s)
        decoded = base64.b64decode(obj["payload"]["payload"]["data"])
        self.assertEqual(decoded, cmd.frame_bytes)

    def test_observed_sample_roundtrip(self):
        """Mirror the real cmd 215 push from the capture line-for-line."""
        # Real captured base64: "+vz91wEBAAD7" for cmd=215 read
        # Frame: fa fc fd d7 01 01 00 00 fb (9 bytes, seq=1)
        real_bytes = base64.b64decode("+vz91wEBAAD7")
        cmd = BleControlCommand(
            fountain_id=400000001,
            fountain_mac="aabbcc112233",
            cmd_code=215,
            frame_bytes=real_bytes,
            device_type=24,
            timestamp=1776789723.0,
        )
        s = build_content_ble(cmd)
        obj = json.loads(s)
        self.assertEqual(obj["type"], "ble")
        self.assertEqual(obj["timestamp"], 1776789723)
        self.assertEqual(obj["payload"]["payload"]["cmd"], 215)
        self.assertEqual(obj["payload"]["payload"]["data"], "+vz91wEBAAD7")
        self.assertEqual(obj["payload"]["device"]["type"], 24)
        self.assertEqual(obj["payload"]["device"]["mac"], "aabbcc112233")


class TestBuildContentConnect(unittest.TestCase):
    def test_connect_action_start(self):
        s = build_content_connect("aabbcc112233", DEVICE_TYPE_CTW3, action=1)
        obj = json.loads(s)
        self.assertEqual(obj["msgType"], 2)
        self.assertEqual(obj["type"], "connect")
        self.assertEqual(obj["payload"]["connect_action"], 1)
        self.assertEqual(obj["payload"]["device"]["type"], DEVICE_TYPE_CTW3)
        self.assertEqual(obj["payload"]["device"]["mac"], "aabbcc112233")
        self.assertIn("timestamp", obj)
        self.assertIn("timestamp", obj["payload"])

    def test_connect_action_stop(self):
        s = build_content_connect("aabbcc112233", DEVICE_TYPE_CTW3, action=0)
        obj = json.loads(s)
        self.assertEqual(obj["payload"]["connect_action"], 0)


class TestWrappers(unittest.TestCase):
    def test_wrap_ble_entry_shape(self):
        cmd = _sample_cmd()
        entry = wrap_ble(cmd)
        self.assertEqual(set(entry.keys()), {"content", "time", "timestamp"})
        # time is in milliseconds, timestamp in seconds
        self.assertEqual(entry["timestamp"], 1776770000)
        self.assertEqual(entry["time"], 1776770000 * 1000)
        inner = json.loads(entry["content"])
        self.assertEqual(inner["type"], "ble")

    def test_wrap_connect_entry_shape(self):
        entry = wrap_connect("aabbcc112233", DEVICE_TYPE_CTW3, action=1)
        self.assertEqual(set(entry.keys()), {"content", "time", "timestamp"})
        inner = json.loads(entry["content"])
        self.assertEqual(inner["type"], "connect")
        self.assertEqual(inner["payload"]["connect_action"], 1)
        # time is ms version of timestamp
        self.assertEqual(entry["time"], entry["timestamp"] * 1000)


class TestBleControlCommand(unittest.TestCase):
    def test_auto_timestamp(self):
        import time as _time
        before = _time.time()
        cmd = BleControlCommand(
            fountain_id=1,
            fountain_mac="aa" * 6,
            cmd_code=220,
            frame_bytes=b"\xFA\xFC\xFD\xDC\x01\x00\x00\x00\xFB",
        )
        after = _time.time()
        self.assertGreaterEqual(cmd.timestamp, before)
        self.assertLessEqual(cmd.timestamp, after + 1)

    def test_explicit_timestamp_preserved(self):
        cmd = BleControlCommand(
            fountain_id=1,
            fountain_mac="aa" * 6,
            cmd_code=220,
            frame_bytes=b"\x00",
            timestamp=12345.0,
        )
        self.assertEqual(cmd.timestamp, 12345.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
