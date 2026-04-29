"""Tests for ctw3_state using real /ctw3/signup and /ctw3/deviceData bodies
from today's mitmproxy capture.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ctw3_state import (
    Ctw3State,
    DisturbConfig,
    Electricity,
    LampBrightness,
    LightConfig,
    Mode,
    Settings,
    Status,
    TimeWindow,
    mac_to_bytes,
    mac_to_hex_no_colons,
)


# Verbatim /ctw3/signup response body from today's capture (device freshly bound)
SIGNUP_RESPONSE = {
    "result": {
        "id": 400000001,
        "mac": "aabbcc112233",
        "secret": "cafebabe1234",
        "sn": "EXAMPLEFAKESN001",
        "hardware": 1,
        "firmware": 111,
        "typeCode": 2,
        "mode": 3,
        "status": {
            "powerStatus": 1,
            "suspendStatus": 1,
            "runStatus": 0,
            "detectStatus": 0,
            "electricStatus": 0,
        },
        "breakdownWarning": 0,
        "filterWarning": 0,
        "lackWarning": 0,
        "lowBattery": 0,
        "electricity": {
            "supplyVoltage": 0,
            "batteryVoltage": 3782,
            "batteryPercent": 75,
        },
        "settings": {
            "lampRingBrightness": 2,
            "lampRingSwitch": 1,
            "noDisturbingSwitch": 0,
            "smartSleepTime": 3,
            "smartWorkingTime": 3,
            "batterySleepTime": 3600,
            "batteryWorkingTime": 25,
            "disturbConfig": 2,
            "disturbMultiTime": [{"time": [1320, 360], "repeats": "1"}],
            "lightConfig": 2,
            "lightMultiTime": [{"time": [0, 1440], "repeats": "1"}],
            "distributionDiagram": 1,
            "smartInductiveSwitch": 0,
            "batteryInductiveSwitch": 1,
        },
        "isNightNoDisturbing": 0,
        "waterPumpRunTime": -1,
        "filterPercent": 98,
        "todayPumpRunTime": 709,
        "todayCleanWater": -1,
        "todayUseElectricity": -1.0,
        "expectedCleanWater": -1,
        "expectedUseElectricity": -1.0,
        "filterExpectedDays": -1,
        "recordAutomaticAddWater": 1,
        "syncTime": "2026-03-24T07:09:30.795+0000",
        "timezone": 1.0,
        "updateAt": "2026-03-24T07:09:36.865+0000",
        "createdAt": "2026-02-21T14:57:44.559+0000",
        "moduleStatus": 0,
        "locale": "Europe/Berlin",
    }
}

# /ctw3/link response — adds userId/relation/familyId
LINK_RESPONSE_EXTRA = {
    "name": "EVERSWEET MAX 2 (CORDLESS)1",
    "userId": "999999999",
    "relation": {"userId": "999999999"},
}


class TestParseSignupResponse(unittest.TestCase):
    def test_parse_signup(self):
        state = Ctw3State.from_cloud_dict(SIGNUP_RESPONSE)
        self.assertEqual(state.id, 400000001)
        self.assertEqual(state.mac, "aabbcc112233")
        self.assertEqual(state.secret, "cafebabe1234")
        self.assertEqual(state.sn, "EXAMPLEFAKESN001")
        self.assertEqual(state.firmware, 111)
        self.assertEqual(state.type_code, 2)
        self.assertEqual(state.mode, Mode.INTERMITTENT.value)
        self.assertEqual(state.filter_percent, 98)
        self.assertEqual(state.today_pump_run_time, 709)
        # Status
        self.assertEqual(state.status.power_status, 1)
        self.assertEqual(state.status.suspend_status, 1)  # paused
        self.assertEqual(state.status.run_status, 0)
        # Battery
        self.assertEqual(state.electricity.battery_voltage, 3782)
        self.assertEqual(state.electricity.battery_percent, 75)
        # Settings
        self.assertEqual(state.settings.lamp_ring_brightness, LampBrightness.MEDIUM.value)
        self.assertEqual(state.settings.lamp_ring_switch, 1)
        self.assertEqual(state.settings.disturb_config, DisturbConfig.MULTI_WINDOW.value)
        self.assertEqual(len(state.settings.disturb_multi_time), 1)
        self.assertEqual(state.settings.disturb_multi_time[0].time, (1320, 360))
        self.assertEqual(state.settings.light_config, LightConfig.MULTI_WINDOW.value)
        self.assertEqual(len(state.settings.light_multi_time), 1)
        self.assertEqual(state.settings.light_multi_time[0].time, (0, 1440))

    def test_parse_link_overlay(self):
        # Simulate /ctw3/link response: signup-like but with userId/name
        combined = {**SIGNUP_RESPONSE["result"], **LINK_RESPONSE_EXTRA}
        state = Ctw3State.from_cloud_dict({"result": combined})
        self.assertEqual(state.name, "EVERSWEET MAX 2 (CORDLESS)1")
        self.assertEqual(state.user_id, "999999999")


class TestSerialization(unittest.TestCase):
    def test_roundtrip_signup(self):
        state = Ctw3State.from_cloud_dict(SIGNUP_RESPONSE)
        detail = state.to_cloud_detail()
        # Critical fields survive
        self.assertEqual(detail["id"], 400000001)
        self.assertEqual(detail["mode"], 3)
        self.assertEqual(detail["status"]["powerStatus"], 1)
        self.assertEqual(detail["electricity"]["batteryPercent"], 75)
        self.assertEqual(detail["settings"]["lampRingBrightness"], 2)
        # Embedded window lists survive
        self.assertEqual(detail["settings"]["disturbMultiTime"][0]["time"], [1320, 360])

    def test_update_kv_flattened(self):
        state = Ctw3State.from_cloud_dict(SIGNUP_RESPONSE)
        kv = state.to_update_kv()
        self.assertEqual(kv["id"], "400000001")
        self.assertEqual(kv["mode"], 3)
        self.assertEqual(kv["powerStatus"], 1)
        self.assertEqual(kv["batteryPercent"], 75)
        self.assertEqual(kv["filterPercent"], 98)
        # Settings fields flattened into top-level kv
        self.assertEqual(kv["lampRingBrightness"], 2)
        self.assertEqual(kv["batteryInductiveSwitch"], 1)


class TestDefaults(unittest.TestCase):
    def test_empty_state(self):
        state = Ctw3State()
        self.assertEqual(state.type_code, 2)  # default for CTW3
        self.assertEqual(state.mode, Mode.INTERMITTENT.value)
        self.assertEqual(state.settings.lamp_ring_brightness, LampBrightness.MEDIUM.value)
        self.assertEqual(state.filter_percent, 100)

    def test_settings_defaults_roundtrip(self):
        s = Settings()
        d = s.to_dict()
        s2 = Settings.from_dict(d)
        self.assertEqual(s.lamp_ring_brightness, s2.lamp_ring_brightness)
        self.assertEqual(s.disturb_config, s2.disturb_config)
        # Empty lists survive
        self.assertEqual(s.light_multi_time, s2.light_multi_time)


class TestHelpers(unittest.TestCase):
    def test_mac_to_hex_colons(self):
        self.assertEqual(mac_to_hex_no_colons("AA:BB:CC:11:22:33"), "aabbcc112233")

    def test_mac_to_hex_dashes(self):
        self.assertEqual(mac_to_hex_no_colons("AA-BB-CC-11-22-33"), "aabbcc112233")

    def test_mac_to_hex_plain(self):
        self.assertEqual(mac_to_hex_no_colons("aabbcc112233"), "aabbcc112233")

    def test_mac_to_bytes(self):
        self.assertEqual(mac_to_bytes("AA:BB:CC:11:22:33"), bytes.fromhex("aabbcc112233"))

    def test_mac_to_bytes_invalid(self):
        with self.assertRaises(ValueError):
            mac_to_bytes("abc")


class TestTimeWindow(unittest.TestCase):
    def test_roundtrip(self):
        w = TimeWindow(time=(1320, 360), repeats="1,2,3")
        d = w.to_dict()
        self.assertEqual(d, {"time": [1320, 360], "repeats": "1,2,3"})
        w2 = TimeWindow.from_dict(d)
        self.assertEqual(w2.time, (1320, 360))
        self.assertEqual(w2.repeats, "1,2,3")


if __name__ == "__main__":
    unittest.main(verbosity=2)
