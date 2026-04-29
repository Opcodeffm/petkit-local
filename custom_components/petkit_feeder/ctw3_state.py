"""CTW3 (Eversweet Max 2 Cordless) device state model.

Maps Petkit's cloud JSON representation into a typed dataclass.

Schema derived from observed `/ctw3/signup`, `/ctw3/link`, `/ctw3/deviceData`,
and `/ctw3/update` flows (2026-04-21 capture).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import IntEnum
from typing import Any


class Mode(IntEnum):
    """CTW3 operating mode (observed on Eversweet Max 2 Cordless)."""
    SMART = 1           # On-demand pump, motion-triggered
    NORMAL = 2          # Continuous pump
    INTERMITTENT = 3    # Scheduled on/off cycles (observed as default)


class LampBrightness(IntEnum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3


class DisturbConfig(IntEnum):
    """Night-mode behaviour selector."""
    OFF = 0
    SINGLE_WINDOW = 1
    MULTI_WINDOW = 2


class LightConfig(IntEnum):
    ALWAYS_ON = 1
    MULTI_WINDOW = 2


@dataclass
class TimeWindow:
    """A repeating time window (used for disturbMultiTime, lightMultiTime)."""
    time: tuple[int, int]       # (start_minutes_from_midnight, end_minutes_from_midnight)
    repeats: str = "1"          # Comma-separated weekday numbers (Petkit format)

    def to_dict(self) -> dict[str, Any]:
        return {"time": list(self.time), "repeats": self.repeats}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TimeWindow":
        t = d.get("time", [0, 0])
        return cls(time=(int(t[0]), int(t[1])), repeats=str(d.get("repeats", "1")))


@dataclass
class Settings:
    """Mutable settings pushed by the app (sent via `/ctw3/update` `kv=`)."""

    # Lamp ring
    lamp_ring_brightness: int = LampBrightness.MEDIUM.value   # 1/2/3
    lamp_ring_switch: int = 1                                 # 0/1
    light_config: int = LightConfig.MULTI_WINDOW.value        # 1/2
    light_multi_time: list[TimeWindow] = field(default_factory=list)

    # Do-not-disturb
    no_disturbing_switch: int = 0                             # 0/1
    disturb_config: int = DisturbConfig.MULTI_WINDOW.value    # 0/1/2
    disturb_multi_time: list[TimeWindow] = field(default_factory=list)

    # Smart sensor timings (minutes) — pump behaviour when motion detected
    smart_sleep_time: int = 3
    smart_working_time: int = 3
    smart_inductive_switch: int = 0                           # 0=off, 1=on

    # Battery-saving timings (seconds + minutes respectively)
    battery_sleep_time: int = 3600
    battery_working_time: int = 25
    battery_inductive_switch: int = 1                         # 0/1

    # Display toggle (cordless model shows drink stats on base)
    distribution_diagram: int = 1                             # 0/1

    # --- (de)serialization -----------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "lampRingBrightness": self.lamp_ring_brightness,
            "lampRingSwitch": self.lamp_ring_switch,
            "lightConfig": self.light_config,
            "lightMultiTime": [w.to_dict() for w in self.light_multi_time],
            "noDisturbingSwitch": self.no_disturbing_switch,
            "disturbConfig": self.disturb_config,
            "disturbMultiTime": [w.to_dict() for w in self.disturb_multi_time],
            "smartSleepTime": self.smart_sleep_time,
            "smartWorkingTime": self.smart_working_time,
            "smartInductiveSwitch": self.smart_inductive_switch,
            "batterySleepTime": self.battery_sleep_time,
            "batteryWorkingTime": self.battery_working_time,
            "batteryInductiveSwitch": self.battery_inductive_switch,
            "distributionDiagram": self.distribution_diagram,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Settings":
        return cls(
            lamp_ring_brightness=int(d.get("lampRingBrightness", 2)),
            lamp_ring_switch=int(d.get("lampRingSwitch", 1)),
            light_config=int(d.get("lightConfig", LightConfig.MULTI_WINDOW.value)),
            light_multi_time=[TimeWindow.from_dict(w) for w in d.get("lightMultiTime", [])],
            no_disturbing_switch=int(d.get("noDisturbingSwitch", 0)),
            disturb_config=int(d.get("disturbConfig", DisturbConfig.MULTI_WINDOW.value)),
            disturb_multi_time=[TimeWindow.from_dict(w) for w in d.get("disturbMultiTime", [])],
            smart_sleep_time=int(d.get("smartSleepTime", 3)),
            smart_working_time=int(d.get("smartWorkingTime", 3)),
            smart_inductive_switch=int(d.get("smartInductiveSwitch", 0)),
            battery_sleep_time=int(d.get("batterySleepTime", 3600)),
            battery_working_time=int(d.get("batteryWorkingTime", 25)),
            battery_inductive_switch=int(d.get("batteryInductiveSwitch", 1)),
            distribution_diagram=int(d.get("distributionDiagram", 1)),
        )


@dataclass
class Status:
    """Dynamic runtime flags (device-reported, read-only from HA)."""
    power_status: int = 0        # 0=off, 1=on
    suspend_status: int = 0      # 0=running, 1=paused
    run_status: int = 0          # 0=idle, 1=pumping
    detect_status: int = 0       # 0=no motion, 1=motion detected
    electric_status: int = 0     # AC electric connected 0/1

    def to_dict(self) -> dict[str, Any]:
        return {
            "powerStatus": self.power_status,
            "suspendStatus": self.suspend_status,
            "runStatus": self.run_status,
            "detectStatus": self.detect_status,
            "electricStatus": self.electric_status,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Status":
        return cls(
            power_status=int(d.get("powerStatus", 0)),
            suspend_status=int(d.get("suspendStatus", 0)),
            run_status=int(d.get("runStatus", 0)),
            detect_status=int(d.get("detectStatus", 0)),
            electric_status=int(d.get("electricStatus", 0)),
        )


@dataclass
class Electricity:
    """Power/battery readings."""
    supply_voltage: int = 0        # mV (USB adapter if plugged)
    battery_voltage: int = 0       # mV
    battery_percent: int = 0       # 0-100

    def to_dict(self) -> dict[str, Any]:
        return {
            "supplyVoltage": self.supply_voltage,
            "batteryVoltage": self.battery_voltage,
            "batteryPercent": self.battery_percent,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Electricity":
        return cls(
            supply_voltage=int(d.get("supplyVoltage", 0)),
            battery_voltage=int(d.get("batteryVoltage", 0)),
            battery_percent=int(d.get("batteryPercent", 0)),
        )


@dataclass
class Ctw3State:
    """Complete device state for one CTW3 fountain.

    Combines identity, settings, runtime status, and runtime metrics.
    Maps to the Petkit cloud `device_detail` / `deviceData` JSON.
    """

    # --- Identity (immutable after binding) ---
    id: int = 0
    mac: str = ""                       # "aabbcc112233" (no colons)
    secret: str = ""                    # 12-char hex
    sn: str = ""                        # "EXAMPLEFAKESN001"
    name: str = ""                      # user-chosen friendly name
    hardware: int = 1
    firmware: int = 0                   # numeric fw version (e.g. 111)
    type_code: int = 2                  # always 2 for CTW3

    # --- Mode ---
    mode: int = Mode.INTERMITTENT.value

    # --- Runtime ---
    status: Status = field(default_factory=Status)
    electricity: Electricity = field(default_factory=Electricity)

    # --- Warnings / flags ---
    breakdown_warning: int = 0
    filter_warning: int = 0
    lack_warning: int = 0
    low_battery: int = 0
    is_night_no_disturbing: int = 0
    module_status: int = 0

    # --- Filter / pump telemetry ---
    water_pump_run_time: int = -1      # cumulative seconds or -1 if unknown
    filter_percent: int = 100          # 0-100
    filter_expected_days: int = -1
    today_pump_run_time: int = 0       # seconds pumped today
    today_clean_water: int = -1        # mL purified today
    today_use_electricity: float = -1.0
    expected_clean_water: int = -1
    expected_use_electricity: float = -1.0
    record_automatic_add_water: int = 1

    # --- Drink-event tracking (derived locally from cmd 230 byte 19 +
    # pump runtime counter, no Petkit cloud involvement) ---
    drinks_today: int = 0              # count reset at midnight (local time)
    drinks_today_date: str = ""        # ISO date that drinks_today / total belong to
    total_drink_duration_today: int = 0  # sum of drink durations today, seconds
    last_drink_at: str = ""            # ISO timestamp of most recent completed drink
    last_drink_duration: int = 0       # seconds of most recent completed drink

    # --- DND time window (cmd 226, last-write-wins since fountain does not
    # echo DND times back in cmd 230 dumps). -1 means "not yet written from HA".
    # Times are in minutes from midnight (0..1439).
    dnd_window1_start_min: int = -1
    dnd_window1_end_min: int = -1
    dnd_window1_enabled: bool = False

    # --- Settings (user-controllable) ---
    settings: Settings = field(default_factory=Settings)

    # --- Metadata ---
    user_id: str = ""
    timezone: float = 0.0
    locale: str = "UTC"
    sync_time: str = ""            # ISO timestamp of last device-initiated sync
    update_at: str = ""
    created_at: str = ""
    family_id: int = 0

    # --- Parsing ---

    @classmethod
    def from_cloud_dict(cls, d: dict[str, Any]) -> "Ctw3State":
        """Parse a Petkit cloud JSON dict (from /ctw3/signup|link|deviceData)."""
        # Some endpoints wrap in {"result": {...}} — unwrap for convenience
        if "result" in d and isinstance(d["result"], dict):
            d = d["result"]

        return cls(
            id=int(d.get("id", 0)),
            mac=str(d.get("mac", "")),
            secret=str(d.get("secret", "")),
            sn=str(d.get("sn", "")),
            name=str(d.get("name", "")),
            hardware=int(d.get("hardware", 1)),
            firmware=int(d.get("firmware", 0)),
            type_code=int(d.get("typeCode", 2)),
            mode=int(d.get("mode", Mode.INTERMITTENT.value)),
            status=Status.from_dict(d.get("status", {}) or {}),
            electricity=Electricity.from_dict(d.get("electricity", {}) or {}),
            breakdown_warning=int(d.get("breakdownWarning", 0)),
            filter_warning=int(d.get("filterWarning", 0)),
            lack_warning=int(d.get("lackWarning", 0)),
            low_battery=int(d.get("lowBattery", 0)),
            is_night_no_disturbing=int(d.get("isNightNoDisturbing", 0)),
            module_status=int(d.get("moduleStatus", 0)),
            water_pump_run_time=int(d.get("waterPumpRunTime", -1)),
            filter_percent=int(d.get("filterPercent", 100)),
            filter_expected_days=int(d.get("filterExpectedDays", -1)),
            today_pump_run_time=int(d.get("todayPumpRunTime", 0)),
            today_clean_water=int(d.get("todayCleanWater", -1)),
            today_use_electricity=float(d.get("todayUseElectricity", -1.0)),
            expected_clean_water=int(d.get("expectedCleanWater", -1)),
            expected_use_electricity=float(d.get("expectedUseElectricity", -1.0)),
            record_automatic_add_water=int(d.get("recordAutomaticAddWater", 1)),
            settings=Settings.from_dict(d.get("settings", {}) or {}),
            user_id=str(d.get("userId", "")),
            timezone=float(d.get("timezone", 0.0)),
            locale=str(d.get("locale", "UTC")),
            sync_time=str(d.get("syncTime", "")),
            update_at=str(d.get("updateAt", "")),
            created_at=str(d.get("createdAt", "")),
            family_id=int(d.get("familyId", 0)),
        )

    def to_update_kv(self) -> dict[str, Any]:
        """Return the flat dict used by `/ctw3/update` `kv=...` payload.

        Mirrors the 35-field structure observed during pairing when the app
        pushed initial state. Used when OUR local_server acts as the cloud
        and we want to synthesize a device-like state response.
        """
        flat = {
            "id": str(self.id),
            "hardware": self.hardware,
            "firmware": self.firmware,
            "mode": self.mode,
            "powerStatus": self.status.power_status,
            "suspendStatus": self.status.suspend_status,
            "runStatus": self.status.run_status,
            "detectStatus": self.status.detect_status,
            "electricStatus": self.status.electric_status,
            "breakdownWarning": self.breakdown_warning,
            "filterWarning": self.filter_warning,
            "lackWarning": self.lack_warning,
            "lowBattery": self.low_battery,
            "isNightNoDisturbing": self.is_night_no_disturbing,
            "moduleStatus": self.module_status,
            "waterPumpRunTime": self.water_pump_run_time,
            "filterPercent": self.filter_percent,
            "todayPumpRunTime": self.today_pump_run_time,
            "supplyVoltage": self.electricity.supply_voltage,
            "batteryVoltage": self.electricity.battery_voltage,
            "batteryPercent": self.electricity.battery_percent,
        }
        flat.update(self.settings.to_dict())
        # Re-type lists using to_dict to be json-serializable
        return flat

    def to_cloud_detail(self) -> dict[str, Any]:
        """Serialize to the nested cloud JSON (for our local_server to return)."""
        return {
            "id": self.id,
            "mac": self.mac,
            "secret": self.secret,
            "sn": self.sn,
            "name": self.name,
            "hardware": self.hardware,
            "firmware": self.firmware,
            "typeCode": self.type_code,
            "mode": self.mode,
            "status": self.status.to_dict(),
            "breakdownWarning": self.breakdown_warning,
            "filterWarning": self.filter_warning,
            "lackWarning": self.lack_warning,
            "lowBattery": self.low_battery,
            "electricity": self.electricity.to_dict(),
            "settings": self.settings.to_dict(),
            "isNightNoDisturbing": self.is_night_no_disturbing,
            "waterPumpRunTime": self.water_pump_run_time,
            "filterPercent": self.filter_percent,
            "todayPumpRunTime": self.today_pump_run_time,
            "todayCleanWater": self.today_clean_water,
            "todayUseElectricity": self.today_use_electricity,
            "expectedCleanWater": self.expected_clean_water,
            "expectedUseElectricity": self.expected_use_electricity,
            "filterExpectedDays": self.filter_expected_days,
            "recordAutomaticAddWater": self.record_automatic_add_water,
            "relation": {"userId": self.user_id} if self.user_id else {},
            "userId": self.user_id,
            "timezone": self.timezone,
            "locale": self.locale,
            "syncTime": self.sync_time,
            "updateAt": self.update_at,
            "createdAt": self.created_at,
            "familyId": self.family_id,
            "moduleStatus": self.module_status,
        }


# --- Convenience helpers -----------------------------------------------------

def mac_to_hex_no_colons(mac: str) -> str:
    """Convert 'aa:bb:cc:11:22:33' or 'A4-C1-...' or 'a4c1...' to lowercase hex
    without separators."""
    return mac.replace(":", "").replace("-", "").replace(" ", "").lower()


def mac_to_bytes(mac: str) -> bytes:
    """Convert MAC string to 6-byte representation."""
    hexstr = mac_to_hex_no_colons(mac)
    if len(hexstr) != 12:
        raise ValueError(f"invalid MAC: {mac!r}")
    return bytes.fromhex(hexstr)
