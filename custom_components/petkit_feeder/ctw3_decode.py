"""CTW3 BLE-payload decoders.

These decode the *data* portions of BLE response frames (cmd 210, 215,
216, 220, 221, 222, 226, 230) received from a CTW3 fountain via the D4
relay and reported by the D4 as event_type=53.

Byte layouts were reverse-engineered from a live capture on 2026-04-21
(docs/path_b_evidence/ha_capture_full_protocol.jsonl) by diffing status
dumps against the known cmd 220 / cmd 221 pushes that triggered them.

High-confidence fields are exposed as named attributes; uncertain bytes
are kept as raw values so a future capture can refine the layout
without breaking callers.
"""
from __future__ import annotations

from dataclasses import dataclass


def _u16be(b: bytes, off: int) -> int:
    """Big-endian 16-bit at offset off."""
    return (b[off] << 8) | b[off + 1]


# --- Mode constants (confirmed via observed cmd 220 pushes) ----------------

MODE_SMART = 1
MODE_NORMAL = 2
MODE_INTERMITTENT = 3


# --- cmd 221 / cmd 230 config portion (shared layout) ----------------------

@dataclass
class FountainConfig:
    """12-byte config block — appears both as cmd 221 payload and as
    bytes 30-41 of the cmd 230 status dump (exact mirror).

    Byte layout confirmed by live capture 2026-04-22 (Runde 2): bytes 6-7
    are TWO separate bytes, not a single u16 — proven by toggling the
    display-light switch independently of brightness.
    """

    smart_working_time: int      # minutes (byte 0)
    smart_sleep_time: int        # minutes (byte 1)
    battery_working_time: int    # seconds, 16-bit BE (bytes 2-3)
    battery_sleep_time: int      # seconds, 16-bit BE (bytes 4-5)
    lamp_ring_switch: int        # 0=off, 1=on (byte 6)
    lamp_ring_brightness: int    # 1=LOW, 2=MEDIUM, 3=HIGH (byte 7)
    no_disturbing_switch: int    # 0/1 (byte 8)
    distribution_diagram: int    # 0/1 (byte 9)
    smart_inductive_switch: int  # 0/1 (byte 10)
    battery_inductive_switch: int  # 0/1 (byte 11)

    @classmethod
    def parse(cls, data: bytes) -> "FountainConfig":
        if len(data) < 12:
            raise ValueError(f"config block too short: {len(data)}")
        return cls(
            smart_working_time=data[0],
            smart_sleep_time=data[1],
            battery_working_time=_u16be(data, 2),
            battery_sleep_time=_u16be(data, 4),
            lamp_ring_switch=data[6],
            lamp_ring_brightness=data[7],
            no_disturbing_switch=data[8],
            distribution_diagram=data[9],
            smart_inductive_switch=data[10],
            battery_inductive_switch=data[11],
        )

    def to_bytes(self) -> bytes:
        return bytes([
            self.smart_working_time & 0xFF,
            self.smart_sleep_time & 0xFF,
            (self.battery_working_time >> 8) & 0xFF,
            self.battery_working_time & 0xFF,
            (self.battery_sleep_time >> 8) & 0xFF,
            self.battery_sleep_time & 0xFF,
            self.lamp_ring_switch & 0xFF,
            self.lamp_ring_brightness & 0xFF,
            self.no_disturbing_switch & 0xFF,
            self.distribution_diagram & 0xFF,
            self.smart_inductive_switch & 0xFF,
            self.battery_inductive_switch & 0xFF,
        ])


# --- cmd 220 SET_MODE payload (host->device, 3 bytes) ----------------------

@dataclass
class ModePayload:
    """cmd 220 data — observed [powerStatus, runStatus, mode].

    Byte semantics (confirmed via runtime-counter correlation, Runde 2
    capture): runStatus=1 means the pump is RUNNING (the 16-bit counter
    in cmd 230 bytes 11-12 ticks once per second). runStatus=0 means
    PAUSED (counter frozen). The name `suspend_status` was used
    initially but its semantics were inverted — kept for backwards
    compatibility, but the interpretation is "1 = running, 0 = paused".
    """
    power_status: int
    suspend_status: int   # 1 = running, 0 = paused  (note inverted from the name)
    mode: int

    def to_bytes(self) -> bytes:
        return bytes([self.power_status & 0xFF, self.suspend_status & 0xFF, self.mode & 0xFF])

    @classmethod
    def parse(cls, data: bytes) -> "ModePayload":
        if len(data) < 3:
            raise ValueError(f"mode payload too short: {len(data)}")
        return cls(power_status=data[0], suspend_status=data[1], mode=data[2])


# --- cmd 230 status dump (42 bytes, device->host) --------------------------

@dataclass
class Cmd230Status:
    """Decoded cmd 230 status dump (42 bytes).

    Uncertainty notes:
      - bytes 11-12 and 17-18: both are monotonic 16-bit counters with a
        constant offset of 0x04CB between them. Likely "today pump
        runtime (s)" vs "total uptime (s)" but not positively identified.
      - byte 21: varies 49-64 — read as wifi_rssi_magnitude (|dBm|).
      - bytes 23 and 28: non-monotonic raw sensor readings — values 74 vs
        77 etc correlate with b19=0/2. Likely proximity/light values from
        the motion sensor itself. Exposed as raw for future refinement.
      - byte 4: appears to mirror the current "dnd_active" state
        (ticks from 0 to 1 shortly after a cmd 221 sets no_disturbing=1).
    """
    # Core state (confirmed via cmd 220 push correlation + counter-tick
    # correlation: byte 1 drives the 16-bit runtime counter b11-12 — ticks
    # at 1 Hz when byte 1 = 1, frozen when byte 1 = 0).
    power_status: int            # byte 0 (0=off, 1=on)
    suspend_status: int          # byte 1 — ACTUALLY a run flag: 1=RUNNING, 0=PAUSED
    mode: int                    # byte 2 (CTW3: 1=Standard/continuous, 2=Intermittent)
    default_mode: int            # byte 3 (always 2 in captures)

    dnd_active: int              # byte 4 (dnd currently enforced)

    battery_percent: int         # byte 13
    wifi_rssi_magnitude: int     # byte 21 (|rssi_dbm|)
    filter_percent: int          # byte 24

    # byte 19 — motion-sensor flag, 0=no motion, 2=motion detected.
    # Confirmed live 2026-04-22: user triggered sensor at 15:54:30 and
    # the exact-matching cmd 230 dump showed b19=2 (vs b19=0 in preceding
    # and succeeding dumps). Value 1 never observed — either unused or
    # reserved.
    motion_raw: int              # byte 19 (2 = motion, 0 = none)

    # Counters (16-bit BE, meaning TBD)
    counter_runtime: int         # bytes 11-12
    counter_uptime: int          # bytes 17-18

    # Raw analog bytes, meanings unconfirmed — exposed so callers can
    # refine interpretation without re-parsing.
    byte_14: int
    byte_23: int
    byte_28: int
    byte_29: int

    # Embedded config (mirror of cmd 221 data)
    config: FountainConfig       # bytes 30-41

    # Raw frame data for debugging
    raw: bytes

    @classmethod
    def parse(cls, data: bytes) -> "Cmd230Status":
        if len(data) < 42:
            raise ValueError(f"cmd 230 dump too short: {len(data)}")
        return cls(
            power_status=data[0],
            suspend_status=data[1],
            mode=data[2],
            default_mode=data[3],
            dnd_active=data[4],
            battery_percent=data[13],
            wifi_rssi_magnitude=data[21],
            filter_percent=data[24],
            motion_raw=data[19],
            counter_runtime=_u16be(data, 11),
            counter_uptime=_u16be(data, 17),
            byte_14=data[14],
            byte_23=data[23],
            byte_28=data[28],
            byte_29=data[29],
            config=FountainConfig.parse(data[30:42]),
            raw=bytes(data[:42]),
        )

    @property
    def motion_detected(self) -> bool:
        """True while the fountain reports motion (pet near the sensor)."""
        return self.motion_raw == 2

    def wifi_rssi_dbm(self) -> int:
        """Signed dBm (negative) — convenience."""
        return -self.wifi_rssi_magnitude
