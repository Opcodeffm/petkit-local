"""Petkit BLE frame builder / parser.

Frame layout (observed on W5/CTW3 via pdiegmann/ha-petkit-ble and APK analysis):

    FA FC FD  cmd  type  seq  len_lo  len_hi  [payload]  FB

Where:
    - FA FC FD  = magic header (3 bytes)
    - cmd       = command id (e.g. 220 = mode/power, 221 = config)
    - type      = 1 = request from host, 2 = response from device
    - seq       = caller-managed sequence counter (0..255, wraps)
    - len_lo    = payload length (low byte)  — pdiegmann codes it as a single byte
                  followed by a 0-byte. We keep the same convention.
    - len_hi    = always 0 for current known commands
    - payload   = len_lo bytes of command-specific data
    - FB        = terminator

Designed for use WITHOUT a live Bluetooth stack — frames are passed through
the D4 feeder as base64-encoded blobs via HTTP heartbeat commands.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

# --- Magic bytes ---
HEADER = bytes([0xFA, 0xFC, 0xFD])
TERMINATOR = 0xFB

# --- Type codes ---
TYPE_REQUEST = 1   # host -> device
TYPE_RESPONSE = 2  # device -> host

# --- Device type codes (observed) ---
DEVICE_TYPE_W5 = 14     # Cybercast W5 / Eversweet
DEVICE_TYPE_CTW3 = 24   # Eversweet Max 2 Cordless (CTW3)

# --- Command codes (observed from pdiegmann + APK analysis) ---
CMD_GET_BATTERY = 66        # response carries battery voltage + percent
CMD_INIT_DEVICE = 73        # initial handshake / secret setup
CMD_SET_DATETIME = 84       # push current time to device
CMD_GET_SYNC = 86           # sync check
CMD_GET_DEVICE_INFO = 200   # firmware / hardware info
CMD_GET_DEVICE_TYPE = 201
CMD_GET_DEVICE_STATE = 210  # pump status, filter status, errors
CMD_GET_DEVICE_CONFIG = 211 # settings (led, dnd, mode)
CMD_GET_DEVICE_DETAILS = 213
CMD_SET_LIGHT = 215         # light setting
CMD_SET_DND = 216           # do-not-disturb setting
CMD_SET_MODE = 220          # [state_on_off, mode] — 1=normal, 2=smart
CMD_SET_CONFIG = 221        # bulk config (smart times, led, dnd, lock)
CMD_RESET_FILTER = 222
CMD_UPDATE_LIGHT = 225
CMD_UPDATE_DND = 226
CMD_GET_UPDATE = 230        # poll for pushed updates from device


@dataclass(frozen=True)
class Frame:
    """Parsed representation of a Petkit BLE frame."""

    cmd: int
    type: int
    seq: int
    data: bytes

    @property
    def is_request(self) -> bool:
        return self.type == TYPE_REQUEST

    @property
    def is_response(self) -> bool:
        return self.type == TYPE_RESPONSE


def build_frame(seq: int, cmd: int, type_: int, data: Sequence[int] = ()) -> bytes:
    """Compose a Petkit BLE frame.

    Args:
        seq: sequence number (0..255, caller maintains).
        cmd: command byte (see CMD_* constants).
        type_: TYPE_REQUEST (1) or TYPE_RESPONSE (2).
        data: payload bytes.

    Returns:
        Complete frame including header + terminator.

    Raises:
        ValueError: seq/cmd/type out of byte range, or payload longer than 255.
    """
    if not 0 <= seq <= 255:
        raise ValueError(f"seq must be 0..255, got {seq}")
    if not 0 <= cmd <= 255:
        raise ValueError(f"cmd must be 0..255, got {cmd}")
    if type_ not in (TYPE_REQUEST, TYPE_RESPONSE):
        raise ValueError(f"type must be 1 or 2, got {type_}")
    if len(data) > 255:
        raise ValueError(f"data too long ({len(data)} > 255)")
    for b in data:
        if not 0 <= b <= 255:
            raise ValueError(f"data byte out of range: {b}")

    length = len(data)
    start_data = 0  # second length byte, always 0 for known commands
    return bytes(HEADER + bytes([cmd, type_, seq, length, start_data]) + bytes(data) + bytes([TERMINATOR]))


def parse_frame(raw: bytes) -> Frame:
    """Parse a Petkit BLE frame.

    Args:
        raw: complete frame bytes, including header + terminator.

    Returns:
        Frame dataclass with cmd/type/seq/data.

    Raises:
        ValueError: malformed frame (wrong header/terminator/length).
    """
    if len(raw) < 9:
        raise ValueError(f"frame too short ({len(raw)} bytes, need at least 9)")
    if raw[:3] != HEADER:
        raise ValueError(f"bad header: {raw[:3].hex()} (expected {HEADER.hex()})")
    if raw[-1] != TERMINATOR:
        raise ValueError(f"bad terminator: {raw[-1]:#04x} (expected {TERMINATOR:#04x})")

    cmd, type_, seq, length, _start = raw[3:8]
    data_end = 8 + length
    if data_end + 1 != len(raw):
        raise ValueError(
            f"length mismatch: declared {length}, actual {len(raw) - 9}"
        )
    data = raw[8:data_end]
    return Frame(cmd=cmd, type=type_, seq=seq, data=bytes(data))


# --- Helpers for payload encoding ---

def split_short(value: int) -> tuple[int, int]:
    """Split a 16-bit signed short into (hi, lo) byte tuple (big-endian)."""
    if not -32768 <= value <= 65535:
        raise ValueError(f"value out of 16-bit range: {value}")
    # Treat as unsigned for encoding
    v = value & 0xFFFF
    return ((v >> 8) & 0xFF, v & 0xFF)


def pad_left(data: Sequence[int], target_length: int) -> list[int]:
    """Pad a list on the left with zeros to reach target length."""
    if len(data) >= target_length:
        return list(data)
    return [0] * (target_length - len(data)) + list(data)


# Petkit's BLE "epoch" is 2000-01-01 UTC, not the UNIX epoch.
_PETKIT_EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)


def time_payload() -> list[int]:
    """Build the 6-byte datetime payload for CMD_SET_DATETIME.

    Byte 0 reserved (timezone offset on some devices, 0 on W5/CTW3).
    Bytes 1-4 big-endian seconds since 2000-01-01 UTC.
    Byte 5 is a trailing `13` marker (pdiegmann observed).
    """
    seconds = int((datetime.now(timezone.utc) - _PETKIT_EPOCH).total_seconds())
    return [
        0,
        (seconds >> 24) & 0xFF,
        (seconds >> 16) & 0xFF,
        (seconds >> 8) & 0xFF,
        seconds & 0xFF,
        13,
    ]


# --- Secret derivation (from pdiegmann analysis) ---

def derive_secret_from_device_id(device_id_bytes: Sequence[int]) -> list[int]:
    """Derive the secret used in CMD_INIT_DEVICE (cmd=73) from device_id.

    Petkit's pattern:
      - reverse the device_id bytes
      - if the last two bytes are zero, replace them with 13,37 (the "1337" marker)
      - left-pad to 8 bytes total

    This is the same secret negotiation pdiegmann observed in BLE captures.
    """
    arr = list(reversed(device_id_bytes))
    if len(arr) >= 2 and arr[-1] == 0 and arr[-2] == 0:
        arr[-2] = 13
        arr[-1] = 37
    return pad_left(arr, 8)


# --- Convenience: counter-free frame-building with explicit seq ---

class FrameSequencer:
    """Maintains a 0-255 wrapping sequence counter."""

    def __init__(self, start: int = 0) -> None:
        if not 0 <= start <= 255:
            raise ValueError("start must be 0..255")
        self._seq = start

    def next(self) -> int:
        s = self._seq
        self._seq = (self._seq + 1) & 0xFF
        return s

    @property
    def current(self) -> int:
        return self._seq
