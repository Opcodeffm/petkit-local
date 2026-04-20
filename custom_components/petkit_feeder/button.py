"""Button entities for Petkit Feeder — local."""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import PetkitFeederCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: PetkitFeederCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities([
        PetkitFeedButton(coordinator, amount=10, translation_key="feed_1_portion"),
        PetkitFeedButton(coordinator, amount=20, translation_key="feed_2_portions"),
        PetkitFeedButton(coordinator, amount=50, translation_key="feed_5_portions"),
        PetkitResetDesiccantButton(coordinator),
    ])


class PetkitFeedButton(CoordinatorEntity[PetkitFeederCoordinator], ButtonEntity):
    """Button to trigger manual feeding — fully local."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:food-drumstick"

    def __init__(self, coordinator: PetkitFeederCoordinator, amount: int, translation_key: str) -> None:
        super().__init__(coordinator)
        self._amount = amount
        self._attr_translation_key = translation_key
        self._attr_unique_id = f"{coordinator.device_id}_feed_{amount}"
        self._attr_device_info = coordinator.device_info_ha

    async def async_press(self) -> None:
        """Queue feed command — delivered to feeder on next heartbeat."""
        self.coordinator.server.queue_feed(self._amount)
        _LOGGER.info("Feed queued: %d grams (delivered on next heartbeat)", self._amount)


class PetkitResetDesiccantButton(CoordinatorEntity[PetkitFeederCoordinator], ButtonEntity):
    """Button to reset the desiccant counter (after replacing the silica gel pack)."""

    _attr_has_entity_name = True
    _attr_translation_key = "reset_desiccant"
    _attr_icon = "mdi:restart"

    def __init__(self, coordinator: PetkitFeederCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.device_id}_reset_desiccant"
        self._attr_device_info = coordinator.device_info_ha

    async def async_press(self) -> None:
        days = self.coordinator.server.reset_desiccant()
        _LOGGER.info("Desiccant reset → %d days", days)
