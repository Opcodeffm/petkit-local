"""Unit tests for ble_frame. Run from repo root:

    python3 -m unittest custom_components.petkit_feeder.test_ble_frame

or directly:

    python3 custom_components/petkit_feeder/test_ble_frame.py
"""
from __future__ import annotations

import unittest

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ble_frame import (
    CMD_GET_BATTERY,
    CMD_RESET_FILTER,
    CMD_SET_MODE,
    Frame,
    FrameSequencer,
    TYPE_REQUEST,
    TYPE_RESPONSE,
    build_frame,
    derive_secret_from_device_id,
    pad_left,
    parse_frame,
    split_short,
    time_payload,
)


class TestBuildFrame(unittest.TestCase):
    def test_empty_payload_mode_cmd(self):
        # pdiegmann-observed pattern: cmd 220 for mode/power on CTW3
        # state=1 (on), mode=2 (smart)
        frame = build_frame(seq=5, cmd=CMD_SET_MODE, type_=TYPE_REQUEST, data=[1, 2])
        expected = bytes([
            0xFA, 0xFC, 0xFD,       # header
            220, 1, 5,              # cmd, type, seq
            2, 0,                   # len=2, start=0
            1, 2,                   # payload
            0xFB                    # terminator
        ])
        self.assertEqual(frame, expected)

    def test_get_battery_frame(self):
        # cmd 66, type=1, seq=0, data=[0, 0]
        frame = build_frame(seq=0, cmd=CMD_GET_BATTERY, type_=TYPE_REQUEST, data=[0, 0])
        expected = bytes([
            0xFA, 0xFC, 0xFD,
            66, 1, 0,
            2, 0,
            0, 0,
            0xFB
        ])
        self.assertEqual(frame, expected)

    def test_reset_filter_frame(self):
        # cmd 222, single-byte data=[0]
        frame = build_frame(seq=42, cmd=CMD_RESET_FILTER, type_=TYPE_REQUEST, data=[0])
        # Frame: FA FC FD 222 1 42 1 0 0 FB  (10 bytes total)
        self.assertEqual(len(frame), 10)
        self.assertEqual(frame[3], 222)  # cmd
        self.assertEqual(frame[4], 1)    # type
        self.assertEqual(frame[5], 42)   # seq
        self.assertEqual(frame[6], 1)    # len
        self.assertEqual(frame[7], 0)    # start
        self.assertEqual(frame[8], 0)    # data[0]
        self.assertEqual(frame[-1], 0xFB)  # terminator

    def test_empty_data_allowed(self):
        frame = build_frame(seq=0, cmd=200, type_=TYPE_REQUEST, data=[])
        self.assertEqual(frame, bytes([0xFA, 0xFC, 0xFD, 200, 1, 0, 0, 0, 0xFB]))

    def test_max_seq(self):
        frame = build_frame(seq=255, cmd=200, type_=TYPE_REQUEST, data=[])
        self.assertEqual(frame[5], 255)

    def test_invalid_seq_too_big(self):
        with self.assertRaises(ValueError):
            build_frame(seq=256, cmd=200, type_=TYPE_REQUEST)

    def test_invalid_seq_negative(self):
        with self.assertRaises(ValueError):
            build_frame(seq=-1, cmd=200, type_=TYPE_REQUEST)

    def test_invalid_type(self):
        with self.assertRaises(ValueError):
            build_frame(seq=0, cmd=200, type_=3)

    def test_invalid_payload_too_long(self):
        with self.assertRaises(ValueError):
            build_frame(seq=0, cmd=200, type_=TYPE_REQUEST, data=[0] * 256)

    def test_invalid_payload_byte(self):
        with self.assertRaises(ValueError):
            build_frame(seq=0, cmd=200, type_=TYPE_REQUEST, data=[256])


class TestParseFrame(unittest.TestCase):
    def test_roundtrip_mode_cmd(self):
        original = build_frame(seq=5, cmd=220, type_=TYPE_REQUEST, data=[1, 2])
        parsed = parse_frame(original)
        self.assertEqual(parsed.cmd, 220)
        self.assertEqual(parsed.type, 1)
        self.assertEqual(parsed.seq, 5)
        self.assertEqual(parsed.data, bytes([1, 2]))
        self.assertTrue(parsed.is_request)
        self.assertFalse(parsed.is_response)

    def test_roundtrip_empty_data(self):
        original = build_frame(seq=100, cmd=200, type_=TYPE_RESPONSE, data=[])
        parsed = parse_frame(original)
        self.assertEqual(parsed.cmd, 200)
        self.assertEqual(parsed.type, 2)
        self.assertEqual(parsed.seq, 100)
        self.assertEqual(parsed.data, b"")
        self.assertTrue(parsed.is_response)

    def test_parse_bad_header(self):
        bad = bytes([0xAA, 0xBB, 0xCC, 200, 1, 0, 0, 0, 0xFB])
        with self.assertRaisesRegex(ValueError, "bad header"):
            parse_frame(bad)

    def test_parse_bad_terminator(self):
        bad = bytes([0xFA, 0xFC, 0xFD, 200, 1, 0, 0, 0, 0xFF])
        with self.assertRaisesRegex(ValueError, "bad terminator"):
            parse_frame(bad)

    def test_parse_too_short(self):
        with self.assertRaisesRegex(ValueError, "too short"):
            parse_frame(b"\xFA\xFC\xFD")

    def test_parse_length_mismatch(self):
        # Declared length=5 but only 2 bytes of payload present
        bad = bytes([0xFA, 0xFC, 0xFD, 200, 1, 0, 5, 0, 0xAA, 0xBB, 0xFB])
        with self.assertRaisesRegex(ValueError, "length mismatch"):
            parse_frame(bad)


class TestHelpers(unittest.TestCase):
    def test_split_short(self):
        self.assertEqual(split_short(0), (0, 0))
        self.assertEqual(split_short(0x0100), (1, 0))
        self.assertEqual(split_short(0xFFFF), (0xFF, 0xFF))
        self.assertEqual(split_short(256), (1, 0))
        self.assertEqual(split_short(-1), (0xFF, 0xFF))  # two's complement

    def test_split_short_out_of_range(self):
        with self.assertRaises(ValueError):
            split_short(0x10000)

    def test_pad_left(self):
        self.assertEqual(pad_left([1, 2], 4), [0, 0, 1, 2])
        self.assertEqual(pad_left([1, 2, 3, 4], 4), [1, 2, 3, 4])
        self.assertEqual(pad_left([1, 2, 3, 4, 5], 4), [1, 2, 3, 4, 5])  # no truncation
        self.assertEqual(pad_left([], 3), [0, 0, 0])

    def test_time_payload_structure(self):
        p = time_payload()
        self.assertEqual(len(p), 6)
        self.assertEqual(p[0], 0)    # tz reserved
        self.assertEqual(p[5], 13)   # trailing marker
        # Bytes 1-4: big-endian seconds since 2000-01-01 UTC
        seconds = (p[1] << 24) | (p[2] << 16) | (p[3] << 8) | p[4]
        # Should be well into the 2020s, so > 2**29 = 536870912 (year 2017)
        self.assertGreater(seconds, 2**29)

    def test_derive_secret_from_device_id_no_zero_tail(self):
        # device_id = [1, 2, 3, 4] (no trailing zeros)
        secret = derive_secret_from_device_id([1, 2, 3, 4])
        # Reversed: [4, 3, 2, 1] — last two are 2, 1 (no replacement)
        # Padded to 8: [0, 0, 0, 0, 4, 3, 2, 1]
        self.assertEqual(secret, [0, 0, 0, 0, 4, 3, 2, 1])

    def test_derive_secret_from_device_id_zero_tail(self):
        # device_id = [1, 2, 0, 0] — after reverse becomes [0, 0, 2, 1]
        # Last two (2, 1) are NOT zero, so no replacement
        secret = derive_secret_from_device_id([1, 2, 0, 0])
        self.assertEqual(secret, [0, 0, 0, 0, 0, 0, 2, 1])

    def test_derive_secret_zero_ends_replaced(self):
        # device_id = [0, 0, 1, 2] — reversed: [2, 1, 0, 0]
        # Last two ARE zero → replaced with 13, 37
        secret = derive_secret_from_device_id([0, 0, 1, 2])
        self.assertEqual(secret, [0, 0, 0, 0, 2, 1, 13, 37])


class TestFrameSequencer(unittest.TestCase):
    def test_increment(self):
        seq = FrameSequencer()
        self.assertEqual(seq.next(), 0)
        self.assertEqual(seq.next(), 1)
        self.assertEqual(seq.next(), 2)
        self.assertEqual(seq.current, 3)

    def test_wrap(self):
        seq = FrameSequencer(start=254)
        self.assertEqual(seq.next(), 254)
        self.assertEqual(seq.next(), 255)
        self.assertEqual(seq.next(), 0)

    def test_invalid_start(self):
        with self.assertRaises(ValueError):
            FrameSequencer(start=256)


if __name__ == "__main__":
    unittest.main(verbosity=2)
