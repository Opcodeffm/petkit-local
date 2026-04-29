"""Build the heartbeat-response `content` strings that make a D4 feeder
act as a BLE relay to a CTW3 fountain.

Protocol is **observed live** (capture 2026-04-21,
`docs/path_b_evidence/ha_capture_full_protocol.jsonl`).
The real Petkit cloud pushes two kinds of messages to the D4 via the
`/poll/d4/heartbeat` response `result` list:

1. `type: "connect"` — open (action=1) or close (action=0) a BLE channel
   to a specific fountain.
2. `type: "ble"` — send one BLE command frame. Only valid AFTER the D4
   has reported a successful connect (event_type=52, result=0).

The BLE frame itself is built by `ble_frame.build_frame()` and
base64-encoded inside `payload.payload.data`.
"""
from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass

try:
    from .ble_frame import DEVICE_TYPE_CTW3
except ImportError:  # when loaded as a standalone script for tests
    from ble_frame import DEVICE_TYPE_CTW3


@dataclass
class BleControlCommand:
    """One pending BLE command to relay through the D4."""

    fountain_id: int              # Petkit cloud device id (e.g. 400000001)
    fountain_mac: str             # hex no colons, e.g. "aabbcc112233"
    cmd_code: int                 # BLE command byte (e.g. 220 for mode/power)
    frame_bytes: bytes            # complete BLE frame from ble_frame.build_frame()
    device_type: int = DEVICE_TYPE_CTW3
    timestamp: float = 0.0        # time.time() when queued

    def __post_init__(self) -> None:
        if self.timestamp == 0.0:
            self.timestamp = time.time()


def _b64(frame_bytes: bytes) -> str:
    """Base64-encode a raw BLE frame; matches observed cloud pattern."""
    return base64.b64encode(frame_bytes).decode("ascii")


# --- Content builders (stringified JSON) ------------------------------------

def build_content_ble(cmd: BleControlCommand) -> str:
    """Build the stringified JSON for a BLE-command push.

    Exact observed format (capture 2026-04-21):

        {"msgType":2,
         "payload":{
           "payload":{"data":"<base64 frame>","cmd":220},
           "device":{"type":24,"mac":"aabbcc112233"},
           "timestamp":1776789746},
         "type":"ble",
         "timestamp":1776789746}

    Note the nested `payload.payload` — this is intentional; the D4
    forwards the inner `payload` (data+cmd) as-is over BLE and uses the
    outer `device` field for its own routing.
    """
    ts_sec = int(cmd.timestamp or time.time())
    inner = {
        "msgType": 2,
        "payload": {
            "payload": {
                "data": _b64(cmd.frame_bytes),
                "cmd": cmd.cmd_code,
            },
            "device": {
                "type": cmd.device_type,
                "mac": cmd.fountain_mac,
            },
            "timestamp": ts_sec,
        },
        "type": "ble",
        "timestamp": ts_sec,
    }
    return json.dumps(inner, separators=(",", ":"))


def build_content_connect(fountain_mac: str, device_type: int, action: int) -> str:
    """Build the stringified JSON for a connect/disconnect push.

    action: 1 = start BLE connection, 0 = end it.

    Exact observed format (capture 2026-04-21):

        {"msgType":2,
         "payload":{
           "connect_action":1,
           "device":{"type":24,"mac":"aabbcc112233"},
           "timestamp":1776788746},
         "type":"connect",
         "timestamp":1776788746}
    """
    ts_sec = int(time.time())
    inner = {
        "msgType": 2,
        "payload": {
            "connect_action": action,
            "device": {"type": device_type, "mac": fountain_mac},
            "timestamp": ts_sec,
        },
        "type": "connect",
        "timestamp": ts_sec,
    }
    return json.dumps(inner, separators=(",", ":"))


# --- Heartbeat result-entry wrappers ----------------------------------------

def _wrap_entry(content: str, ts_sec: int | None = None) -> dict:
    """Wrap a stringified content into a heartbeat result-entry.

    The observed envelope is:
        {"content": "<stringified JSON>",
         "time": <unix_millis>,
         "timestamp": <unix_sec>}
    """
    if ts_sec is None:
        ts_sec = int(time.time())
    return {
        "content": content,
        "time": ts_sec * 1000,
        "timestamp": ts_sec,
    }


def wrap_ble(cmd: BleControlCommand) -> dict:
    """Build a heartbeat result-entry carrying a BLE command push."""
    ts_sec = int(cmd.timestamp or time.time())
    return _wrap_entry(build_content_ble(cmd), ts_sec)


def wrap_connect(fountain_mac: str, device_type: int, action: int) -> dict:
    """Build a heartbeat result-entry for a connect/disconnect push."""
    ts_sec = int(time.time())
    return _wrap_entry(
        build_content_connect(fountain_mac, device_type, action),
        ts_sec,
    )
