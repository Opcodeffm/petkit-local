"""Select platform — currently only used by optional CTW3 fountain extension.

Public feeder has no select entities. This file exists so HA loads the
platform; if the private/ folder is present, CTW3 selects (mode) are added.
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    entities: list = []

    # --- Optional CTW3 fountain entities ---
    try:
        from .ctw3_entities import get_ctw3_selects
    except ImportError:
        return

    fountain_coords = hass.data.get(DOMAIN, {}).get("_fountain_coordinators") or {}
    for mac, coord in fountain_coords.items():
        entities.extend(get_ctw3_selects(coord))

    if entities:
        async_add_entities(entities)
