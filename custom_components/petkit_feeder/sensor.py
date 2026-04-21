"""Sensor entities for Petkit Feeder — local."""
from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    UnitOfElectricPotential,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import PetkitFeederCoordinator

SENSOR_DESCRIPTIONS: list[SensorEntityDescription] = [
    # --- Main sensors (main device view) ---
    SensorEntityDescription(
        key="food_state",
        translation_key="food_container",
        icon="mdi:food-drumstick",
        device_class=SensorDeviceClass.ENUM,
        options=["ok", "empty"],
    ),
    SensorEntityDescription(
        key="desiccant_days_left",
        translation_key="desiccant_days_left",
        icon="mdi:water-opacity",
        native_unit_of_measurement="d",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="feed_today_count",
        translation_key="feedings_today",
        icon="mdi:counter",
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    SensorEntityDescription(
        key="feed_today_amount",
        translation_key="food_amount_today",
        icon="mdi:scale",
        native_unit_of_measurement="g",
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    SensorEntityDescription(
        key="battery_level",
        translation_key="battery",
        icon="mdi:battery",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="battery_status_text",
        translation_key="battery_status",
        icon="mdi:battery-alert",
        device_class=SensorDeviceClass.ENUM,
        options=["ok", "low", "critical", "no_batteries", "on_battery"],
    ),
    SensorEntityDescription(
        key="power_source",
        translation_key="power_source",
        icon="mdi:power-plug",
        device_class=SensorDeviceClass.ENUM,
        options=["mains", "battery"],
    ),

    # --- Diagnostic sensors ---
    SensorEntityDescription(
        key="firmware",
        translation_key="firmware",
        icon="mdi:chip",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="wifi_rssi",
        translation_key="wifi_signal",
        icon="mdi:wifi",
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="last_heartbeat_dt",
        translation_key="last_heartbeat",
        icon="mdi:pulse",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="uptime",
        translation_key="uptime",
        icon="mdi:clock-outline",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        device_class=SensorDeviceClass.DURATION,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="heartbeat_count",
        translation_key="heartbeat_count",
        icon="mdi:counter",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    SensorEntityDescription(
        key="battery_voltage",
        translation_key="battery_voltage",
        icon="mdi:flash",
        native_unit_of_measurement=UnitOfElectricPotential.MILLIVOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    SensorEntityDescription(
        key="heap_free",
        translation_key="heap_free",
        icon="mdi:memory",
        native_unit_of_measurement="B",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Petkit Feeder sensors."""
    coordinator: PetkitFeederCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        PetkitFeederSensor(coordinator, description)
        for description in SENSOR_DESCRIPTIONS
    ]
    entities.append(PetkitScheduleSensor(coordinator))
    async_add_entities(entities)


class PetkitFeederSensor(CoordinatorEntity[PetkitFeederCoordinator], SensorEntity):
    """Sensor for Petkit Feeder data."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: PetkitFeederCoordinator, description: SensorEntityDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.device_id}_{description.key}"
        self._attr_device_info = coordinator.device_info_ha

    @property
    def native_value(self):
        if self.coordinator.data is None:
            return None
        value = self.coordinator.data.get(self.entity_description.key)
        key = self.entity_description.key
        if key == "food_state":
            # Returns translation key — matches entity.sensor.food_container.state.{ok,empty}
            return "ok" if value == 1 else "empty"
        if key == "wifi_rssi" and value == 0:
            return None
        if key == "firmware" and (value in (None, "", "unknown")):
            return None
        if key in ("battery_level", "battery_voltage") and value is None:
            return None
        if key == "desiccant_days_left" and value is None:
            return None
        return value


class PetkitScheduleSensor(CoordinatorEntity[PetkitFeederCoordinator], SensorEntity):
    """Shows the currently active feed schedule as human-readable text + attributes."""

    _attr_has_entity_name = True
    _attr_translation_key = "feed_schedule"
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator: PetkitFeederCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.device_id}_schedule"
        self._attr_device_info = coordinator.device_info_ha

    @property
    def native_value(self):
        data = self.coordinator.data or {}
        return data.get("schedule_text", "")

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data or {}
        return {
            "count": data.get("schedule_count", 0),
            "entries": data.get("schedule_entries", []),
            "raw": data.get("schedule_raw", []),
        }
