"""Home Assistant entities for CTW3 fountain (Eversweet Max 2 Cordless).

Scope (after cmd 221 decode on 2026-04-22):
  - switch.power            : cmd 220 power byte
  - switch.suspend          : cmd 220 suspend byte (pause while power on)
  - switch.no_disturb       : cmd 221 byte 8
  - switch.smart_inductive  : cmd 221 byte 10 (motion sensor in SMART mode)
  - switch.battery_inductive: cmd 221 byte 11 (motion sensor in battery mode)
  - switch.distribution_diagram: cmd 221 byte 9 (base-plate stats display)
  - select.mode             : cmd 220 mode byte (SMART/NORMAL/INTERMITTENT)
  - select.lamp_brightness  : cmd 221 bytes 6-7 (1=LOW, 2=MED, 3=HIGH)
  - number.smart_working_time (min)  : cmd 221 byte 0
  - number.smart_sleep_time (min)    : cmd 221 byte 1
  - number.battery_working_time (s)  : cmd 221 bytes 2-3
  - number.battery_sleep_time (s)    : cmd 221 bytes 4-5
  - button.reset_filter     : cmd 222
  - sensors: filter %, battery %, battery voltage, today pump runtime
  - binary_sensors: low battery, filter warning, lack warning,
                    breakdown warning, is_running (pump activity)

Still deferred (need BLE capture analysis):
  - DND time windows (cmd 226, 31 bytes — layout unknown)
  - Per-window LED settings (cmd 215 read layout unknown)
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.components.button import ButtonEntity
from homeassistant.components.number import (
    NumberEntity,
    NumberMode,
)
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.const import (
    PERCENTAGE,
    UnitOfElectricPotential,
    UnitOfTime,
    EntityCategory,
)
from datetime import time, timedelta

from homeassistant.components.time import TimeEntity

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity

if TYPE_CHECKING:
    from .ctw3_coordinator import Ctw3Coordinator

try:
    from .ctw3_state import Ctw3State, Mode
except ImportError:
    from ctw3_state import Ctw3State, Mode


_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class Ctw3BaseEntity(CoordinatorEntity):
    """Shared behavior for all CTW3 entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: "Ctw3Coordinator", unique_suffix: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"ctw3_{coordinator.fountain_mac}_{unique_suffix}"
        self._attr_device_info = coordinator.device_info_ha

    @property
    def fountain(self) -> Ctw3State | None:
        return self.coordinator.fountain

    @property
    def available(self) -> bool:
        return self.fountain is not None


# ---------------------------------------------------------------------------
# Switch: power on/off (uses cmd 220, mode kept)
# ---------------------------------------------------------------------------


class Ctw3PowerSwitch(Ctw3BaseEntity, SwitchEntity):
    _attr_translation_key = "ctw3_power"
    _attr_icon = "mdi:power"
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(self, coordinator: "Ctw3Coordinator") -> None:
        super().__init__(coordinator, "power")

    @property
    def is_on(self) -> bool | None:
        f = self.fountain
        if not f:
            return None
        return f.status.power_status == 1

    async def async_turn_on(self, **_: object) -> None:
        f = self.fountain
        if not f:
            return
        self.coordinator.fountain_server.send_set_mode(
            f.mac, state_on=1, mode=f.mode
        )

    async def async_turn_off(self, **_: object) -> None:
        f = self.fountain
        if not f:
            return
        self.coordinator.fountain_server.send_set_mode(
            f.mac, state_on=0, mode=f.mode
        )


# ---------------------------------------------------------------------------
# Select: mode (SMART / NORMAL / INTERMITTENT)
# ---------------------------------------------------------------------------


_MODE_OPTIONS = ("smart", "normal", "intermittent")
_MODE_VALUE_TO_KEY = {
    Mode.SMART.value: "smart",
    Mode.NORMAL.value: "normal",
    Mode.INTERMITTENT.value: "intermittent",
}
_MODE_KEY_TO_VALUE = {v: k for k, v in _MODE_VALUE_TO_KEY.items()}


class Ctw3ModeSelect(Ctw3BaseEntity, SelectEntity):
    _attr_translation_key = "ctw3_mode"
    _attr_icon = "mdi:cog"
    _attr_options = list(_MODE_OPTIONS)

    def __init__(self, coordinator: "Ctw3Coordinator") -> None:
        super().__init__(coordinator, "mode")

    @property
    def current_option(self) -> str | None:
        f = self.fountain
        if not f:
            return None
        return _MODE_VALUE_TO_KEY.get(f.mode)

    async def async_select_option(self, option: str) -> None:
        mode_val = _MODE_KEY_TO_VALUE.get(option)
        if mode_val is None:
            _LOGGER.warning("Unknown mode option: %r", option)
            return
        f = self.fountain
        if not f:
            return
        self.coordinator.fountain_server.send_set_mode(
            f.mac, state_on=f.status.power_status, mode=mode_val
        )


# ---------------------------------------------------------------------------
# Button: reset filter
# ---------------------------------------------------------------------------


class Ctw3ResetFilterButton(Ctw3BaseEntity, ButtonEntity):
    _attr_translation_key = "ctw3_reset_filter"
    _attr_icon = "mdi:filter-remove"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: "Ctw3Coordinator") -> None:
        super().__init__(coordinator, "reset_filter")

    async def async_press(self) -> None:
        f = self.fountain
        if not f:
            return
        self.coordinator.fountain_server.send_reset_filter(f.mac)


# ---------------------------------------------------------------------------
# Sensors
# ---------------------------------------------------------------------------


class Ctw3FilterPercentSensor(Ctw3BaseEntity, SensorEntity):
    _attr_translation_key = "ctw3_filter_percent"
    _attr_icon = "mdi:filter"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: "Ctw3Coordinator") -> None:
        super().__init__(coordinator, "filter_percent")

    @property
    def native_value(self) -> int | None:
        f = self.fountain
        return f.filter_percent if f else None


class Ctw3BatteryPercentSensor(Ctw3BaseEntity, SensorEntity):
    _attr_translation_key = "ctw3_battery_percent"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: "Ctw3Coordinator") -> None:
        super().__init__(coordinator, "battery_percent")

    @property
    def native_value(self) -> int | None:
        f = self.fountain
        return f.electricity.battery_percent if f else None


class Ctw3BatteryVoltageSensor(Ctw3BaseEntity, SensorEntity):
    _attr_translation_key = "ctw3_battery_voltage"
    _attr_device_class = SensorDeviceClass.VOLTAGE
    _attr_native_unit_of_measurement = UnitOfElectricPotential.MILLIVOLT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False  # advanced / debug

    def __init__(self, coordinator: "Ctw3Coordinator") -> None:
        super().__init__(coordinator, "battery_voltage")

    @property
    def native_value(self) -> int | None:
        f = self.fountain
        return f.electricity.battery_voltage if f else None


class Ctw3TodayPumpRuntimeSensor(Ctw3BaseEntity, SensorEntity):
    """Pump runtime today — displayed in minutes.

    Underlying fountain counter is in seconds (16-bit, rolls over after
    ~18 h of actual pumping). Converted to minutes with 1-decimal
    precision so users see `89.3 min` instead of `5,359 s`.
    """
    _attr_translation_key = "ctw3_today_pump_runtime"
    _attr_icon = "mdi:pump"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_suggested_display_precision = 1
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: "Ctw3Coordinator") -> None:
        # unique_id suffix changed from "today_pump_runtime" (seconds) to
        # "_min" variant because HA bakes the unit into the entity registry
        # on first creation — we need a fresh entity to get MINUTES.
        # The old unique_id is in _CTW3_ORPHANED_UNIQUE_SUFFIXES for cleanup.
        super().__init__(coordinator, "today_pump_runtime_min")

    @property
    def native_value(self) -> float | None:
        f = self.fountain
        if f is None:
            return None
        return round(f.today_pump_run_time / 60.0, 1)


class Ctw3DrinksTodaySensor(Ctw3BaseEntity, SensorEntity):
    """How often the pet drank today (counter resets at local midnight)."""
    _attr_translation_key = "ctw3_drinks_today"
    _attr_icon = "mdi:cup-water"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, coordinator: "Ctw3Coordinator") -> None:
        super().__init__(coordinator, "drinks_today")

    @property
    def native_value(self) -> int | None:
        f = self.fountain
        return f.drinks_today if f else None


class Ctw3LastDrinkDurationSensor(Ctw3BaseEntity, SensorEntity):
    """Duration of the most recently completed drink event, in seconds."""
    _attr_translation_key = "ctw3_last_drink_duration"
    _attr_icon = "mdi:timer-outline"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: "Ctw3Coordinator") -> None:
        super().__init__(coordinator, "last_drink_duration")

    @property
    def native_value(self) -> int | None:
        f = self.fountain
        if f is None:
            return None
        return f.last_drink_duration if f.last_drink_duration > 0 else None


class Ctw3LastDrinkAtSensor(Ctw3BaseEntity, SensorEntity):
    """Timestamp of the most recently completed drink event."""
    _attr_translation_key = "ctw3_last_drink_at"
    _attr_icon = "mdi:clock-outline"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: "Ctw3Coordinator") -> None:
        super().__init__(coordinator, "last_drink_at")

    @property
    def native_value(self):
        f = self.fountain
        if f is None or not f.last_drink_at:
            return None
        # HA expects a datetime for TIMESTAMP device class.
        from datetime import datetime
        try:
            return datetime.fromisoformat(f.last_drink_at)
        except (ValueError, TypeError):
            return None


class Ctw3TotalDrinkDurationTodaySensor(Ctw3BaseEntity, SensorEntity):
    """Total drinking time today, displayed in minutes."""
    _attr_translation_key = "ctw3_total_drink_time_today"
    _attr_icon = "mdi:timer-sand"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_suggested_display_precision = 1
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, coordinator: "Ctw3Coordinator") -> None:
        super().__init__(coordinator, "total_drink_time_today")

    @property
    def native_value(self) -> float | None:
        f = self.fountain
        if f is None:
            return None
        return round(f.total_drink_duration_today / 60.0, 1)


class Ctw3ModeSensor(Ctw3BaseEntity, SensorEntity):
    """Read-only mirror of the mode select (for dashboards)."""

    _attr_translation_key = "ctw3_mode_sensor"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(_MODE_OPTIONS)
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: "Ctw3Coordinator") -> None:
        super().__init__(coordinator, "mode_state")

    @property
    def native_value(self) -> str | None:
        f = self.fountain
        if not f:
            return None
        return _MODE_VALUE_TO_KEY.get(f.mode)


# ---------------------------------------------------------------------------
# Binary Sensors
# ---------------------------------------------------------------------------


class Ctw3PumpRunningBinarySensor(Ctw3BaseEntity, BinarySensorEntity):
    """True while the pump is actively running (byte 1 of cmd 230 = 1)."""

    _attr_translation_key = "ctw3_pump_running"
    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_icon = "mdi:pump"

    def __init__(self, coordinator: "Ctw3Coordinator") -> None:
        super().__init__(coordinator, "pump_running")

    @property
    def is_on(self) -> bool | None:
        f = self.fountain
        if not f:
            return None
        # suspend_status byte is actually a run flag: 1=RUNNING, 0=PAUSED.
        # Also require power_status=1 so we don't report "running" when
        # the fountain is globally off.
        return f.status.power_status == 1 and f.status.suspend_status == 1


class Ctw3MotionDetectedBinarySensor(Ctw3BaseEntity, BinarySensorEntity):
    """Live motion-sensor state — True while the fountain reports a pet
    near the sensor (cmd 230 byte 19 = 2). Confirmed 2026-04-22 via
    targeted live trigger.
    """
    _attr_translation_key = "ctw3_motion_detected"
    _attr_device_class = BinarySensorDeviceClass.MOTION
    _attr_entity_registry_enabled_default = True  # explicitly, in case default differs

    def __init__(self, coordinator: "Ctw3Coordinator") -> None:
        # unique_suffix bumped to "v2" to sidestep HA's sticky disabled-by
        # cache for the original "motion_detected" suffix (was created with
        # enabled_default=False in older versions; HA refused to re-enable
        # even after registry deletion).
        super().__init__(coordinator, "motion_detected_v2")

    @property
    def is_on(self) -> bool | None:
        f = self.fountain
        if not f:
            return None
        return f.status.detect_status == 1


class Ctw3LowBatteryBinarySensor(Ctw3BaseEntity, BinarySensorEntity):
    _attr_translation_key = "ctw3_low_battery"
    _attr_device_class = BinarySensorDeviceClass.BATTERY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: "Ctw3Coordinator") -> None:
        super().__init__(coordinator, "low_battery")

    @property
    def is_on(self) -> bool | None:
        f = self.fountain
        if not f:
            return None
        return f.low_battery == 1


class Ctw3FilterWarningBinarySensor(Ctw3BaseEntity, BinarySensorEntity):
    _attr_translation_key = "ctw3_filter_warning"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:filter-remove"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: "Ctw3Coordinator") -> None:
        super().__init__(coordinator, "filter_warning")

    @property
    def is_on(self) -> bool | None:
        f = self.fountain
        if not f:
            return None
        return f.filter_warning == 1


class Ctw3LackWarningBinarySensor(Ctw3BaseEntity, BinarySensorEntity):
    """Low-water warning."""

    _attr_translation_key = "ctw3_lack_warning"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:water-off"

    def __init__(self, coordinator: "Ctw3Coordinator") -> None:
        super().__init__(coordinator, "lack_warning")

    @property
    def is_on(self) -> bool | None:
        f = self.fountain
        if not f:
            return None
        return f.lack_warning == 1


class Ctw3BreakdownWarningBinarySensor(Ctw3BaseEntity, BinarySensorEntity):
    _attr_translation_key = "ctw3_breakdown_warning"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: "Ctw3Coordinator") -> None:
        super().__init__(coordinator, "breakdown_warning")

    @property
    def is_on(self) -> bool | None:
        f = self.fountain
        if not f:
            return None
        return f.breakdown_warning == 1


# ---------------------------------------------------------------------------
# Switch: suspend / pause (uses cmd 220 suspend byte, power stays on)
# ---------------------------------------------------------------------------


class Ctw3SuspendSwitch(Ctw3BaseEntity, SwitchEntity):
    """Pause the pump without cutting power. Maps to cmd 220 byte 1.

    Byte-1 semantics (confirmed via runtime-counter correlation, 2026-04-22):
      byte 1 = 1  → pump RUNNING (counter ticks)
      byte 1 = 0  → pump PAUSED (counter frozen)
    The legacy field name `suspend_status` is the opposite of what its
    name suggests; this switch inverts it so the UI behaviour matches
    user expectation ("Pause pump" ON = paused).

    The fountain firmware auto-unpauses after ~10 minutes — if we did
    nothing, HA would show a stale "paused" state long after the pump
    had resumed. We schedule a re-sync 10.5 minutes after activating
    pause so HA's state catches up automatically.
    """

    _AUTO_UNPAUSE_DELAY = timedelta(minutes=10, seconds=30)

    _attr_translation_key = "ctw3_suspend"
    _attr_icon = "mdi:pause-circle"

    def __init__(self, coordinator: "Ctw3Coordinator") -> None:
        super().__init__(coordinator, "suspend")
        self._cancel_auto_unpause_resync = None  # callable to cancel pending timer

    @property
    def is_on(self) -> bool | None:
        """True when pump is paused (Pause switch ON)."""
        f = self.fountain
        if not f:
            return None
        # suspend_status==0 means paused → Pause switch ON
        return f.status.suspend_status == 0

    async def async_turn_on(self, **_: object) -> None:
        """User wants pause ON → send byte 1 = 0 (pause)."""
        f = self.fountain
        if not f:
            return
        self.coordinator.fountain_server.send_set_mode(
            f.mac, state_on=f.status.power_status, mode=f.mode, suspend=0,
        )
        # Fountain auto-unpauses after ~10 min — schedule a re-sync so
        # HA picks up the real state when that happens.
        self._schedule_auto_unpause_resync()

    async def async_turn_off(self, **_: object) -> None:
        """User wants pause OFF → send byte 1 = 1 (run)."""
        f = self.fountain
        if not f:
            return
        self.coordinator.fountain_server.send_set_mode(
            f.mac, state_on=f.status.power_status, mode=f.mode, suspend=1,
        )
        # User explicitly unpaused → cancel any pending auto-unpause sync.
        self._cancel_scheduled_resync()

    def _schedule_auto_unpause_resync(self) -> None:
        # Cancel any previous pending timer first.
        self._cancel_scheduled_resync()
        self._cancel_auto_unpause_resync = async_call_later(
            self.hass, self._AUTO_UNPAUSE_DELAY, self._do_auto_unpause_resync,
        )

    def _cancel_scheduled_resync(self) -> None:
        if self._cancel_auto_unpause_resync is not None:
            try:
                self._cancel_auto_unpause_resync()
            except Exception:  # pragma: no cover
                pass
            self._cancel_auto_unpause_resync = None

    async def _do_auto_unpause_resync(self, _now) -> None:
        """Fires ~10min after pause-ON — refresh state from the fountain."""
        self._cancel_auto_unpause_resync = None
        f = self.fountain
        if not f:
            return
        _LOGGER.info(
            "Auto-unpause window elapsed for %s; requesting resync.", f.mac,
        )
        self.coordinator.fountain_server.request_sync(f.mac)

    async def async_will_remove_from_hass(self) -> None:
        # Entity being torn down (reload, unload) — cancel timer.
        self._cancel_scheduled_resync()
        await super().async_will_remove_from_hass()


# ---------------------------------------------------------------------------
# Config switches — all use cmd 221 (bulk config), one boolean field each
# ---------------------------------------------------------------------------


_NOT_SYNCED_MESSAGE = (
    "Einstellung kann erst geändert werden nachdem der Brunnen einmal "
    "seinen aktuellen Status gemeldet hat. Drücke einmal den Power-Schalter "
    "(ein/aus), dann kurz warten bis die BLE-Session läuft — danach "
    "funktioniert diese Einstellung."
)


def _call_config_update(fountain_server, mac: str, **changes: int) -> None:
    """Wrap FountainServer.send_config_update to translate the internal
    RuntimeError into a user-facing HomeAssistantError.
    """
    try:
        fountain_server.send_config_update(mac, **changes)
    except RuntimeError as err:
        if "not yet synced" in str(err):
            raise HomeAssistantError(_NOT_SYNCED_MESSAGE) from err
        raise


class _Ctw3ConfigSwitch(Ctw3BaseEntity, SwitchEntity):
    """Base class for cmd-221 boolean switches.

    Subclasses set `_attr_translation_key`, `_attr_icon`, provide a
    unique_suffix via `__init__`, and override `_settings_field` with
    the FountainConfig / Settings attribute name.

    Shown as unavailable in HA until the fountain has sent a real cmd 230
    status dump — so users can't flip values that would get clobbered by
    HA defaults in the subsequent cmd 221 push.
    """
    _settings_field: str = ""
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def available(self) -> bool:
        f = self.fountain
        if not f:
            return False
        return self.coordinator.fountain_server.is_settings_synced(f.mac)

    @property
    def is_on(self) -> bool | None:
        f = self.fountain
        if not f:
            return None
        return getattr(f.settings, self._settings_field) == 1

    async def async_turn_on(self, **_: object) -> None:
        f = self.fountain
        if not f:
            return
        _call_config_update(
            self.coordinator.fountain_server, f.mac,
            **{self._settings_field: 1},
        )

    async def async_turn_off(self, **_: object) -> None:
        f = self.fountain
        if not f:
            return
        _call_config_update(
            self.coordinator.fountain_server, f.mac,
            **{self._settings_field: 0},
        )


class Ctw3NoDisturbSwitch(_Ctw3ConfigSwitch):
    _attr_translation_key = "ctw3_no_disturb"
    _attr_icon = "mdi:bell-sleep"
    _settings_field = "no_disturbing_switch"

    def __init__(self, coordinator: "Ctw3Coordinator") -> None:
        super().__init__(coordinator, "no_disturb")


class Ctw3SmartInductiveSwitch(_Ctw3ConfigSwitch):
    _attr_translation_key = "ctw3_smart_inductive"
    _attr_icon = "mdi:motion-sensor"
    _settings_field = "smart_inductive_switch"

    def __init__(self, coordinator: "Ctw3Coordinator") -> None:
        super().__init__(coordinator, "smart_inductive")


class Ctw3BatteryInductiveSwitch(_Ctw3ConfigSwitch):
    _attr_translation_key = "ctw3_battery_inductive"
    _attr_icon = "mdi:motion-sensor"
    _settings_field = "battery_inductive_switch"

    def __init__(self, coordinator: "Ctw3Coordinator") -> None:
        super().__init__(coordinator, "battery_inductive")


# Ctw3DistributionDiagramSwitch removed — cmd 221 byte 9 is a flag only
# consumed by the Petkit app (toggles a drinking-statistics chart in their
# UI). Has no effect when using HA; we don't expose it to avoid UI clutter.
# The underlying bit is still preserved in cmd 221 payloads via f.settings
# default (value 1) so we don't accidentally clobber it.


class Ctw3LampSwitch(_Ctw3ConfigSwitch):
    """Display light (top-mounted LED, on when USB-powered). cmd 221 byte 6."""

    _attr_translation_key = "ctw3_lamp"
    _attr_icon = "mdi:lightbulb"
    _settings_field = "lamp_ring_switch"

    def __init__(self, coordinator: "Ctw3Coordinator") -> None:
        super().__init__(coordinator, "lamp")


# ---------------------------------------------------------------------------
# Select: lamp ring brightness (1=LOW, 2=MEDIUM, 3=HIGH)
# ---------------------------------------------------------------------------


_LAMP_OPTIONS = ("low", "medium", "high")
_LAMP_KEY_TO_VALUE = {"low": 1, "medium": 2, "high": 3}
_LAMP_VALUE_TO_KEY = {v: k for k, v in _LAMP_KEY_TO_VALUE.items()}


class Ctw3LampBrightnessSelect(Ctw3BaseEntity, SelectEntity):
    _attr_translation_key = "ctw3_lamp_brightness"
    _attr_icon = "mdi:led-on"
    _attr_options = list(_LAMP_OPTIONS)
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: "Ctw3Coordinator") -> None:
        super().__init__(coordinator, "lamp_brightness")

    @property
    def available(self) -> bool:
        f = self.fountain
        if not f:
            return False
        return self.coordinator.fountain_server.is_settings_synced(f.mac)

    @property
    def current_option(self) -> str | None:
        f = self.fountain
        if not f:
            return None
        return _LAMP_VALUE_TO_KEY.get(f.settings.lamp_ring_brightness)

    async def async_select_option(self, option: str) -> None:
        value = _LAMP_KEY_TO_VALUE.get(option)
        if value is None:
            _LOGGER.warning("Unknown lamp brightness option: %r", option)
            return
        f = self.fountain
        if not f:
            return
        _call_config_update(
            self.coordinator.fountain_server, f.mac,
            lamp_ring_brightness=value,
        )


# ---------------------------------------------------------------------------
# Number entities: smart + battery working/sleep times (cmd 221)
# ---------------------------------------------------------------------------


class _Ctw3ConfigNumber(Ctw3BaseEntity, NumberEntity):
    """Base class for cmd-221 numeric settings.

    Unavailable until the fountain has reported a real cmd 230 status dump
    (see _Ctw3ConfigSwitch for reasoning).
    """

    _settings_field: str = ""
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX

    @property
    def available(self) -> bool:
        f = self.fountain
        if not f:
            return False
        return self.coordinator.fountain_server.is_settings_synced(f.mac)

    @property
    def native_value(self) -> float | None:
        f = self.fountain
        if not f:
            return None
        return float(getattr(f.settings, self._settings_field))

    async def async_set_native_value(self, value: float) -> None:
        f = self.fountain
        if not f:
            return
        _call_config_update(
            self.coordinator.fountain_server, f.mac,
            **{self._settings_field: int(value)},
        )


class Ctw3IntermittentPumpOnTimeNumber(_Ctw3ConfigNumber):
    """Pump-on duration during Intermittent mode (AC power).

    Maps to cmd 221 byte 0 (stored in minutes as-is on the fountain).
    In the Petkit app this lives under "Energieverwaltung → Wasserflusszeit".
    """
    _attr_translation_key = "ctw3_intermittent_pump_on_time"
    _attr_icon = "mdi:timer-play"
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_native_min_value = 1
    _attr_native_max_value = 60
    _attr_native_step = 1
    _settings_field = "smart_working_time"  # underlying state field name unchanged

    def __init__(self, coordinator: "Ctw3Coordinator") -> None:
        super().__init__(coordinator, "intermittent_pump_on_time")


class Ctw3IntermittentPumpOffTimeNumber(_Ctw3ConfigNumber):
    """Pump-off duration during Intermittent mode (AC power).

    Maps to cmd 221 byte 1 (stored in minutes on the fountain).
    Petkit app: "Energieverwaltung → Bereitschaftsmoduszeit".
    """
    _attr_translation_key = "ctw3_intermittent_pump_off_time"
    _attr_icon = "mdi:timer-pause"
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_native_min_value = 1
    _attr_native_max_value = 60
    _attr_native_step = 1
    _settings_field = "smart_sleep_time"

    def __init__(self, coordinator: "Ctw3Coordinator") -> None:
        super().__init__(coordinator, "intermittent_pump_off_time")


class Ctw3BatteryPumpOnTimeNumber(_Ctw3ConfigNumber):
    """Pump-on duration when running on battery (cmd 221 bytes 2-3).

    Exposed in SECONDS to match the Petkit app UI — the fountain supports
    short cycle times like 30 s on / 20 min off that would be awkward to
    express in minutes.
    Petkit app: "Batteriemodus → Wasserflusszeit".
    """
    _attr_translation_key = "ctw3_battery_pump_on_time"
    _attr_icon = "mdi:timer-play-outline"
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_native_min_value = 5
    _attr_native_max_value = 600
    _attr_native_step = 1
    _settings_field = "battery_working_time"

    def __init__(self, coordinator: "Ctw3Coordinator") -> None:
        super().__init__(coordinator, "battery_pump_on_time")


class Ctw3BatteryPumpOffTimeNumber(_Ctw3ConfigNumber):
    """Pump-off duration when running on battery (cmd 221 bytes 4-5).

    Exposed in SECONDS to match the Petkit app. Typical range is 60 s
    up to a few hours.
    Petkit app: "Batteriemodus → Bereitschaftsmoduszeit".
    """
    _attr_translation_key = "ctw3_battery_pump_off_time"
    _attr_icon = "mdi:timer-pause-outline"
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_native_min_value = 60
    _attr_native_max_value = 7200
    _attr_native_step = 10
    _settings_field = "battery_sleep_time"

    def __init__(self, coordinator: "Ctw3Coordinator") -> None:
        super().__init__(coordinator, "battery_pump_off_time")


# ---------------------------------------------------------------------------
# DND time-window entities (cmd 226) — exposed as HA Time entities so the
# UI shows a proper HH:MM picker instead of a minute counter.
# ---------------------------------------------------------------------------


class _Ctw3DndTime(Ctw3BaseEntity, TimeEntity):
    """Base for DND time-window Time entities.

    Writes go through FountainServer.send_dnd_config (cmd 226). Reads use
    the last-write-wins cache on Ctw3State — fountain does not echo DND
    times in cmd 230 dumps, so there's no read-back path.

    Internal storage is minutes-from-midnight (int). The TimeEntity API
    uses `datetime.time` objects — we convert on the boundary.

    Available even before cmd 230 sync; DND config is independent of the
    cmd 221 sync-guard. Value is None until the user writes something.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _state_field: str = ""  # subclass sets

    @property
    def native_value(self) -> time | None:
        f = self.fountain
        if not f:
            return None
        v = getattr(f, self._state_field, -1)
        if v is None or v < 0:
            return None
        v = max(0, min(1439, int(v)))
        return time(hour=v // 60, minute=v % 60)

    async def async_set_value(self, value: time) -> None:
        f = self.fountain
        if not f:
            return
        new_min = value.hour * 60 + value.minute
        # Pair with the partner field to send the full cmd 226.
        if self._state_field == "dnd_window1_start_min":
            start = new_min
            end = max(0, int(f.dnd_window1_end_min))
        else:
            start = max(0, int(f.dnd_window1_start_min))
            end = new_min
        try:
            self.coordinator.fountain_server.send_dnd_config(
                f.mac,
                window1_start_min=start,
                window1_end_min=end,
                window1_enabled=True,
                state=1,
            )
        except RuntimeError as err:
            raise HomeAssistantError(
                f"DND-Schreibvorgang fehlgeschlagen: {err}"
            ) from err


class Ctw3DndStartTime(_Ctw3DndTime):
    """DND window 1 start time.

    Unique suffix `dnd_start` pairs with `dnd_stop` so both entity_id
    and friendly_name sort start→stop correctly in HA UI (start < stop).
    """
    _attr_translation_key = "ctw3_dnd_start"
    _attr_icon = "mdi:weather-night"
    _state_field = "dnd_window1_start_min"

    def __init__(self, coordinator: "Ctw3Coordinator") -> None:
        super().__init__(coordinator, "dnd_start")


class Ctw3DndEndTime(_Ctw3DndTime):
    """DND window 1 end time (suffix 'stop' for correct alphabetical order)."""
    _attr_translation_key = "ctw3_dnd_stop"
    _attr_icon = "mdi:weather-sunset-up"
    _state_field = "dnd_window1_end_min"

    def __init__(self, coordinator: "Ctw3Coordinator") -> None:
        super().__init__(coordinator, "dnd_stop")


# ---------------------------------------------------------------------------
# Factory entry-points — called by public platform files if private/ exists
# ---------------------------------------------------------------------------


def get_ctw3_switches(coordinator: "Ctw3Coordinator") -> list:
    return [
        Ctw3PowerSwitch(coordinator),
        Ctw3SuspendSwitch(coordinator),
        Ctw3LampSwitch(coordinator),
        Ctw3NoDisturbSwitch(coordinator),
        Ctw3SmartInductiveSwitch(coordinator),
        Ctw3BatteryInductiveSwitch(coordinator),
    ]


def get_ctw3_selects(coordinator: "Ctw3Coordinator") -> list:
    return [
        Ctw3ModeSelect(coordinator),
        Ctw3LampBrightnessSelect(coordinator),
    ]


def get_ctw3_buttons(coordinator: "Ctw3Coordinator") -> list:
    return [Ctw3ResetFilterButton(coordinator)]


def get_ctw3_numbers(coordinator: "Ctw3Coordinator") -> list:
    return [
        Ctw3IntermittentPumpOnTimeNumber(coordinator),
        Ctw3IntermittentPumpOffTimeNumber(coordinator),
        Ctw3BatteryPumpOnTimeNumber(coordinator),
        Ctw3BatteryPumpOffTimeNumber(coordinator),
    ]


def get_ctw3_times(coordinator: "Ctw3Coordinator") -> list:
    # 2026-04-24: DND time entities DISABLED. The fountain accepts our
    # cmd 226 send and ACKs with 0x01, but stops volunteering cmd 230
    # status dumps afterwards — HA entities freeze stale until the
    # fountain is power-cycled. Root cause unknown (maybe state byte
    # semantics, maybe trailer bytes, maybe fountain-side bug).
    # The Ctw3DndStartTime/Ctw3DndEndTime classes are kept for future
    # research but not instantiated until we have a reliable "recover"
    # sequence after the write.
    return []


def get_ctw3_sensors(coordinator: "Ctw3Coordinator") -> list:
    return [
        Ctw3FilterPercentSensor(coordinator),
        Ctw3BatteryPercentSensor(coordinator),
        Ctw3BatteryVoltageSensor(coordinator),
        Ctw3TodayPumpRuntimeSensor(coordinator),
        Ctw3DrinksTodaySensor(coordinator),
        Ctw3LastDrinkDurationSensor(coordinator),
        Ctw3LastDrinkAtSensor(coordinator),
        Ctw3TotalDrinkDurationTodaySensor(coordinator),
        Ctw3ModeSensor(coordinator),
    ]


def get_ctw3_binary_sensors(coordinator: "Ctw3Coordinator") -> list:
    # Note: Ctw3MotionDetectedBinarySensor removed 2026-04-23.
    # Motion state is derived from cmd 230 byte 19 (= same source as drink
    # events), but cmd 230 dumps only arrive every ~60 s or longer during
    # a BLE session — by the time HA reflects "motion ON" the cat is
    # long done drinking. The drink-event sensors (count / last time /
    # duration / total time today) carry all the useful info with
    # tolerable delay, so the real-time motion flag was misleading.
    return [
        Ctw3PumpRunningBinarySensor(coordinator),
        Ctw3LowBatteryBinarySensor(coordinator),
        Ctw3FilterWarningBinarySensor(coordinator),
        Ctw3LackWarningBinarySensor(coordinator),
        Ctw3BreakdownWarningBinarySensor(coordinator),
    ]
