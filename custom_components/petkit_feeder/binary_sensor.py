"""Binary sensors for Petkit Feeder Local — connectivity + error flags."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import PetkitFeederCoordinator


BINARY_SENSORS: list[BinarySensorEntityDescription] = [
    BinarySensorEntityDescription(
        key="online",
        translation_key="online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
    ),
    BinarySensorEntityDescription(
        key="error_motor",
        translation_key="motor_error",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BinarySensorEntityDescription(
        key="error_rtc",
        translation_key="rtc_error",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BinarySensorEntityDescription(
        key="error_ir",
        translation_key="ir_sensor_error",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    BinarySensorEntityDescription(
        key="error_dc",
        translation_key="dc_error",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: PetkitFeederCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list = [PetkitBinarySensor(coordinator, desc) for desc in BINARY_SENSORS]

    # Optional CTW3 fountain binary sensors
    try:
        from .ctw3_entities import get_ctw3_binary_sensors
        for coord in (hass.data.get(DOMAIN, {}).get("_fountain_coordinators") or {}).values():
            entities.extend(get_ctw3_binary_sensors(coord))
    except ImportError:
        pass

    async_add_entities(entities)


class PetkitBinarySensor(CoordinatorEntity[PetkitFeederCoordinator], BinarySensorEntity):
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PetkitFeederCoordinator,
        description: BinarySensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.device_id}_bin_{description.key}"
        self._attr_device_info = coordinator.device_info_ha

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        return bool(self.coordinator.data.get(self.entity_description.key))
