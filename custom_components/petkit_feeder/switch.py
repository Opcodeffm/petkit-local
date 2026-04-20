"""Switch entities for Petkit Feeder settings — local."""
from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import PetkitFeederCoordinator

_LOGGER = logging.getLogger(__name__)

SWITCH_CONFIGS = [
    {"key": "feedSound",  "translation_key": "feed_sound",  "icon_on": "mdi:volume-high", "icon_off": "mdi:volume-off"},
    {"key": "manualLock", "translation_key": "manual_lock", "icon_on": "mdi:lock",        "icon_off": "mdi:lock-open"},
    {"key": "lightMode",  "translation_key": "light_mode",  "icon_on": "mdi:led-on",      "icon_off": "mdi:led-off"},
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: PetkitFeederCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([PetkitSettingSwitch(coordinator, c) for c in SWITCH_CONFIGS])


class PetkitSettingSwitch(CoordinatorEntity[PetkitFeederCoordinator], SwitchEntity):
    """Switch for a Petkit device setting — local."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: PetkitFeederCoordinator, config: dict) -> None:
        super().__init__(coordinator)
        self._setting_key = config["key"]
        self._icon_on = config["icon_on"]
        self._icon_off = config["icon_off"]
        self._attr_translation_key = config["translation_key"]
        self._attr_unique_id = f"{coordinator.device_id}_{config['key']}"
        self._attr_device_info = coordinator.device_info_ha

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("settings", {}).get(self._setting_key, 0) == 1

    @property
    def icon(self) -> str:
        return self._icon_on if self.is_on else self._icon_off

    async def async_turn_on(self, **kwargs) -> None:
        self.coordinator.server.update_setting(self._setting_key, 1)

    async def async_turn_off(self, **kwargs) -> None:
        self.coordinator.server.update_setting(self._setting_key, 0)
