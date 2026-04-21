"""Petkit Feeder Local — fully local, no cloud."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.storage import Store

from .const import DOMAIN, PLATFORMS
from .local_server import PetkitLocalServer
from .coordinator import PetkitFeederCoordinator

_LOGGER = logging.getLogger(__name__)

# Single server instance shared across entries
_server: PetkitLocalServer | None = None

# Persistent storage
_STORE_VERSION = 1
_STORE_KEY_SCHEDULE = f"{DOMAIN}_schedule"
_STORE_KEY_DEVICE = f"{DOMAIN}_device"
_store: Store | None = None
_device_store: Store | None = None

# Service names
SERVICE_SET_SCHEDULE = "set_schedule"
SERVICE_CLEAR_SCHEDULE = "clear_schedule"
SERVICE_FEED = "feed"
SERVICE_RESET_DESICCANT = "reset_desiccant"

# Schema for a single schedule entry
SCHEDULE_ENTRY_SCHEMA = vol.Schema(
    {
        vol.Required("time"): cv.string,           # "HH:MM"
        vol.Required("amount"): vol.All(int, vol.Range(min=1, max=200)),
        vol.Optional("days", default=[1, 2, 3, 4, 5, 6, 7]): vol.All(
            cv.ensure_list, [vol.Any(int, str)]
        ),
        vol.Optional("name", default=""): cv.string,
    }
)

SERVICE_SET_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Required("entries"): vol.All(cv.ensure_list, [SCHEDULE_ENTRY_SCHEMA]),
    }
)

SERVICE_FEED_SCHEMA = vol.Schema(
    {
        vol.Required("amount"): vol.All(int, vol.Range(min=1, max=200)),
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Petkit Feeder from a config entry."""
    global _server, _store, _device_store

    hass.data.setdefault(DOMAIN, {})

    # Start the local HTTP server (only once)
    if _server is None:
        _server = PetkitLocalServer()
        try:
            await _server.start()
            _LOGGER.info("Petkit local API server started")
        except OSError as err:
            _LOGGER.error("Failed to start local server: %s", err)
            _server = None
            return False

        # Propagate HA's locale/timezone/region to the server so the feeder
        # sees the host's actual locale — not a hardcoded default.
        tz_offset: float | None = None
        locale_name: str | None = hass.config.time_zone
        region: str | None = hass.config.country
        try:
            import zoneinfo
            from datetime import datetime
            if hass.config.time_zone:
                tz = zoneinfo.ZoneInfo(hass.config.time_zone)
                tz_offset = datetime.now(tz).utcoffset().total_seconds() / 3600.0
        except Exception:
            _LOGGER.debug("Couldn't derive timezone offset; using fallback", exc_info=True)
        _server.set_locale_info(
            timezone_offset=tz_offset,
            locale=locale_name,
            region=region,
        )

        # Load persisted schedule
        _store = Store(hass, _STORE_VERSION, _STORE_KEY_SCHEDULE)
        stored = await _store.async_load()
        if stored and "entries" in stored:
            try:
                _server.set_schedule(stored["entries"])
                _LOGGER.info(
                    "Restored schedule from storage: %d entries",
                    len(stored["entries"]),
                )
            except ValueError as err:
                _LOGGER.warning("Failed to restore schedule: %s", err)

        # Load persisted device state (firmware, wifi, settings, desiccant timer)
        _device_store = Store(hass, _STORE_VERSION, _STORE_KEY_DEVICE)
        device_stored = await _device_store.async_load()
        if device_stored:
            _server.load_persistent_state(device_stored)

        # Hook persist callback — server calls us on significant state changes
        def _persist_now() -> None:
            hass.async_create_task(_device_store.async_save(_server.get_persistent_state()))
        _server.register_persist_callback(_persist_now)

    # Create coordinator
    coordinator = PetkitFeederCoordinator(hass, _server)

    # Do initial data load
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Set up platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register services (idempotent — check first)
    _register_services(hass)

    return True


def _register_services(hass: HomeAssistant) -> None:
    """Register integration-wide services."""

    async def handle_set_schedule(call: ServiceCall) -> None:
        if _server is None:
            raise HomeAssistantError("Petkit server not running")
        entries = call.data["entries"]
        try:
            _server.set_schedule(entries)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
        # Persist
        if _store is not None:
            await _store.async_save({"entries": entries})
        _LOGGER.info("Schedule set via service: %d entries", len(entries))

    async def handle_clear_schedule(call: ServiceCall) -> None:
        if _server is None:
            raise HomeAssistantError("Petkit server not running")
        _server.clear_schedule()
        if _store is not None:
            await _store.async_save({"entries": []})
        _LOGGER.info("Schedule cleared via service")

    async def handle_feed(call: ServiceCall) -> None:
        if _server is None:
            raise HomeAssistantError("Petkit server not running")
        amount = call.data["amount"]
        _server.queue_feed(amount)

    async def handle_reset_desiccant(call: ServiceCall) -> None:
        if _server is None:
            raise HomeAssistantError("Petkit server not running")
        days = _server.reset_desiccant()
        _LOGGER.info("Desiccant reset → %d days", days)

    if not hass.services.has_service(DOMAIN, SERVICE_SET_SCHEDULE):
        hass.services.async_register(
            DOMAIN, SERVICE_SET_SCHEDULE, handle_set_schedule,
            schema=SERVICE_SET_SCHEDULE_SCHEMA,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_CLEAR_SCHEDULE):
        hass.services.async_register(
            DOMAIN, SERVICE_CLEAR_SCHEDULE, handle_clear_schedule,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_FEED):
        hass.services.async_register(
            DOMAIN, SERVICE_FEED, handle_feed,
            schema=SERVICE_FEED_SCHEMA,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_RESET_DESICCANT):
        hass.services.async_register(
            DOMAIN, SERVICE_RESET_DESICCANT, handle_reset_desiccant,
        )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    global _server

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

        # Stop server if no more entries
        if not hass.data[DOMAIN] and _server is not None:
            await _server.stop()
            _server = None
            # Remove services
            for svc in (SERVICE_SET_SCHEDULE, SERVICE_CLEAR_SCHEDULE, SERVICE_FEED, SERVICE_RESET_DESICCANT):
                if hass.services.has_service(DOMAIN, svc):
                    hass.services.async_remove(DOMAIN, svc)

    return unload_ok
