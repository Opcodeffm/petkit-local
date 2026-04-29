"""Petkit Feeder Local — fully local, no cloud."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store

from .const import DOMAIN, PLATFORMS
from .local_server import PetkitLocalServer
from .coordinator import PetkitFeederCoordinator

_LOGGER = logging.getLogger(__name__)

# CTW3 entity unique_id suffixes that were renamed between versions.
# Any registry entry matching one of these (for a given fountain mac)
# is no longer backed by a live entity and shows as "unavailable" —
# we clean them up automatically on integration setup.
_CTW3_ORPHANED_UNIQUE_SUFFIXES = (
    # Renamed 2026-04-22: smart/battery working/sleep → intermittent/battery pump on/off
    "smart_working_time",
    "smart_sleep_time",
    "battery_working_time",
    "battery_sleep_time",
    # Renamed 2026-04-22: today_pump_runtime → today_pump_runtime_min
    # (forced new entity to change stored unit from seconds to minutes)
    "today_pump_runtime",
    # Removed 2026-04-22: distribution_diagram is a Petkit-app-only toggle,
    # no effect when using HA — we drop the switch entirely.
    "distribution_diagram",
    # Re-enabled 2026-04-22: motion_detected was created with
    # entity_registry_enabled_default=False in older versions; removing
    # from the registry here lets the integration re-create it enabled.
    # Later removed entirely 2026-04-23 (see get_ctw3_binary_sensors):
    # sensor was too delayed to be useful; drink-event sensors replace it.
    "motion_detected",
    # 2026-04-23: In production the registered unique_id turned out to be
    # `motion_detected_v2` (added during an earlier rename attempt), not
    # plain `motion_detected`. Add the _v2 suffix too so the cleanup
    # actually matches and removes the stale "unavailable" entity.
    "motion_detected_v2",
    # 2026-04-24: DND window was briefly exposed as two Number entities
    # (minutes-from-midnight) — replaced same-day by proper Time entities
    # with HH:MM picker. Remove the stale Number-platform registrations.
    "dnd_start_min",
    "dnd_end_min",
    # 2026-04-24: also renamed Time entities to drop the "_time" suffix and
    # switch from "end" to "stop" so both entity_id and friendly_name sort
    # correctly in HA UI (start < stop alphabetically, start < end was
    # backwards).
    "dnd_start_time",
    "dnd_end_time",
    # 2026-04-24 evening: DND Time entities disabled entirely — see
    # Ctw3DndStartTime/EndTime note. Sending cmd 226 silences the
    # fountain's cmd 230 status dump flow until power-cycle. Until we
    # have a reliable recovery sequence, these registry entries must
    # be cleaned up so stale-"unknown" entities don't linger in the UI.
    "dnd_start",
    "dnd_stop",
)


def _migrate_orphaned_ctw3_entities(hass: HomeAssistant, macs) -> None:
    """Remove entity registry entries left behind by unique_id renames.

    HA doesn't auto-clean up orphaned entities — they stay visible in the
    UI as greyed-out / "unavailable" rows. This walks the registry for
    each known fountain mac and removes any entry whose unique_id matches
    a known-renamed suffix.
    """
    try:
        registry = er.async_get(hass)
    except Exception:  # pragma: no cover
        _LOGGER.debug("entity_registry not available, skipping migration")
        return
    removed = 0
    for mac in macs:
        for suffix in _CTW3_ORPHANED_UNIQUE_SUFFIXES:
            old_unique_id = f"ctw3_{mac}_{suffix}"
            for platform in ("number", "switch", "select", "sensor", "binary_sensor", "button", "time"):
                entity_id = registry.async_get_entity_id(platform, DOMAIN, old_unique_id)
                if entity_id is not None:
                    _LOGGER.info(
                        "Removing orphaned entity %s (old unique_id=%s)",
                        entity_id, old_unique_id,
                    )
                    registry.async_remove(entity_id)
                    removed += 1
    if removed:
        _LOGGER.info("Cleaned up %d orphaned CTW3 entities", removed)


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
SERVICE_SET_FOOD_FULL = "set_food_full"
SERVICE_SET_TANK_CAPACITY = "set_food_tank_capacity"

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

SERVICE_SET_TANK_CAPACITY_SCHEMA = vol.Schema(
    {
        vol.Required("grams"): vol.All(int, vol.Range(min=100, max=5000)),
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

        # --- HA-driven feed scheduler ----------------------------------------
        # 2026-04-25: D4 firmware misses scheduled feeds when its heap
        # drifts (~7 h uptime). To make scheduled feedings reliable we
        # don't push the schedule to the feeder's flash anymore (see
        # local_server._handle_dev_feed_get / _handle_get_feed). Instead
        # we tick every minute, check if any schedule item matches the
        # current local minute + weekday, and queue a manual feed. The
        # feeder dispenses on its next heartbeat (typically <15 s; up
        # to ~2 min if the feeder is briefly hanging).
        @callback
        def _ha_driven_schedule_tick(now) -> None:
            try:
                schedule = _server.feed_schedule or []
                if not schedule:
                    return
                # HA gives us local time. Petkit weekdays: 1=Mon … 7=Sun.
                weekday = now.isoweekday()
                minute_of_day = now.hour * 60 + now.minute
                for daily_entry in schedule:
                    if daily_entry.get("repeats") != weekday:
                        continue
                    if daily_entry.get("suspended"):
                        continue
                    for item in daily_entry.get("items", []):
                        item_time_sec = int(item.get("time", -1))
                        if item_time_sec < 0:
                            continue
                        if item_time_sec // 60 == minute_of_day:
                            amount = int(item.get("amount", 0))
                            if amount <= 0:
                                continue
                            _LOGGER.info(
                                "HA schedule trigger: %d g (%s) at %02d:%02d wkd=%d",
                                amount, item.get("name", "?"),
                                now.hour, now.minute, weekday,
                            )
                            _server.queue_feed(amount)
            except Exception:
                _LOGGER.exception("schedule tick failed")

        cancel_schedule = async_track_time_change(
            hass, _ha_driven_schedule_tick, second=0,
        )
        hass.data[DOMAIN]["_schedule_cancel"] = cancel_schedule

        # --- Private: CTW3 fountain extension (optional, not publicly released) ---
        # Only loaded if the private/ folder exists; otherwise a no-op.
        try:
            from .fountain_server import FountainServer
            from .fountain_store import STORE_KEY as _FOUNTAIN_STORE_KEY, fountain_from_stored
        except ImportError:
            FountainServer = None  # type: ignore

        if FountainServer is not None:
            # Persistent-mode default 2026-04-27: back to **True**.
            #
            # Story so far:
            #  - 2026-04-25: switched to False because D4 firmware leaks
            #    heap when we cycle BLE sessions, eventually hangs and
            #    misses schedule feeds.
            #  - 2026-04-27: switched back to True after analysis of the
            #    decompiled Petkit Android APK confirmed Petkit has NO
            #    soft-reboot mechanism (no HTTP reboot endpoint, no BLE
            #    reboot command, no MQTT control method, app explicitly
            #    tells users to physically restart on errors).
            #
            # The actual recovery in Petkit's design is a firmware-internal
            # heap watchdog: D4 detects low heap and reboots itself. We
            # observed this live (2026-04-25 12:53, runtime jumped from
            # 28000s to 2s without intervention).
            #
            # With our HA-driven feed schedule (queue persists across D4
            # hangs and fires when D4 reconnects), the user experience is
            # actually BETTER than Petkit's own app — feeds are resilient
            # to silent D4 reboots. Persistent mode gives us live drink
            # events. The trade-off is gone.
            fs = FountainServer(persistent_mode=True)
            fountain_store = Store(hass, _STORE_VERSION, _FOUNTAIN_STORE_KEY)
            stored_fountains = await fountain_store.async_load() or {}
            for mac, stored_dict in (stored_fountains.get("fountains") or {}).items():
                try:
                    fs.register_fountain(fountain_from_stored(stored_dict))
                except Exception:  # pragma: no cover
                    _LOGGER.exception("Failed to load fountain %s", mac)
            # Migration: remove orphaned registry entries from entity unique_ids
            # that no longer exist after renames in 2026-04-22 release.
            _migrate_orphaned_ctw3_entities(hass, fs.fountains.keys())
            _server.register_heartbeat_content_provider(fs.heartbeat_content_provider)
            _server.register_ctw3_handler(fs.handle_ctw3_post)
            # Feed /d4/dev_ble_device roster queries from the fountain registry
            # so the D4 knows which fountain(s) to relay for.
            if hasattr(_server, "register_ble_roster_provider"):
                _server.register_ble_roster_provider(fs.ble_roster)
            # Forward /d4/dev_event_report so FountainServer can react to
            # event_type=51/52/53 (BLE connect lifecycle + fountain responses).
            if hasattr(_server, "register_event_report_handler"):
                _server.register_event_report_handler(fs.handle_event_report)
            # Expose persistent-mode toggle via /debug/ble_pause_{on,off} —
            # lets us temporarily release the BLE slot so the Petkit app
            # can claim a direct-BLE session for config edits that aren't
            # exposed via the cloud API (DND time windows, etc.).
            if hasattr(_server, "register_ble_pause_control"):
                _server.register_ble_pause_control(fs.set_persistent_mode)
            # 2026-04-24: /debug/test_dnd endpoint DISABLED. Confirmed that
            # cmd 226 SEND puts the fountain into a state where it stops
            # volunteering cmd 230 status dumps until power-cycled. Leave
            # the plumbing in place for future research but don't wire
            # the callback — prevents accidental sends while we don't
            # have a reliable recovery sequence.
            # if hasattr(_server, "register_dnd_test_control"):
            #     def _send_test_dnd(state: int, w1_start: int, w1_end: int) -> None:
            #         macs = list(fs.fountains.keys())
            #         if not macs:
            #             raise RuntimeError("no fountain registered")
            #         fs.send_dnd_test_raw(macs[0], state, w1_start, w1_end)
            #     _server.register_dnd_test_control(_send_test_dnd)

            def _persist_fountains() -> None:
                data = {
                    "fountains": {
                        mac: f.to_cloud_detail()
                        for mac, f in fs.fountains.items()
                    }
                }
                hass.async_create_task(fountain_store.async_save(data))

            fs.register_update_listener(_persist_fountains)
            hass.data[DOMAIN]["_fountain_server"] = fs

            # --- Periodic auto-resync --------------------------------------
            # Our BLE relay is session-based: we open a BLE link, pull a
            # status dump, then disconnect. Between sessions we're blind to
            # motion-sensor transitions + pump cycles. To catch those, we
            # schedule a resync every few minutes. The interval is a trade-off
            # between freshness (motion events / drink counts) and BLE-slot
            # contention (each session locks out the Petkit iPhone app's
            # direct-BLE connect for ~30 s).
            _RESYNC_INTERVAL = timedelta(minutes=3)

            @callback
            def _periodic_resync(_now) -> None:
                for mac in list(fs.fountains.keys()):
                    # request_sync is a no-op if a session is already open.
                    fs.request_sync(mac)

            cancel_resync = async_track_time_interval(
                hass, _periodic_resync, _RESYNC_INTERVAL,
            )
            hass.data[DOMAIN]["_fountain_resync_cancel"] = cancel_resync

            # --- Midnight daily-reset --------------------------------------
            # Drink counters (drinks_today, total_drink_duration_today) are
            # normally rolled over by `_update_drink_tracking` whenever a
            # cmd 230 dump arrives on a new local date. That works fine
            # while the fountain is online — but if the fountain is
            # offline across midnight (e.g. battery dead, out of range),
            # the counters would otherwise stay at yesterday's values
            # until the next dump, which could be hours or a full day
            # later. A time-triggered reset at 00:00:05 local time makes
            # the rollover reliable regardless of BLE activity.
            @callback
            def _midnight_rollover(_now) -> None:
                reset_macs = fs.reset_daily_counters_if_needed()
                if not reset_macs:
                    return
                # Refresh the per-fountain coordinators so entities
                # display the freshly-zeroed values immediately.
                coords = hass.data.get(DOMAIN, {}).get("_fountain_coordinators") or {}
                for mac in reset_macs:
                    c = coords.get(mac)
                    if c is not None:
                        hass.async_create_task(c.async_request_refresh())

            cancel_midnight = async_track_time_change(
                hass, _midnight_rollover, hour=0, minute=0, second=5,
            )
            hass.data[DOMAIN]["_fountain_midnight_cancel"] = cancel_midnight

            # Also run once at integration startup — catches the case
            # where HA was down across midnight.
            fs.reset_daily_counters_if_needed()

            # --- Daily cloud-session refresh -------------------------------
            # Petkit X-Session tokens have a nominal server-side TTL of 36 h
            # (per /user/refreshsession response). Empirically they live much
            # longer, but we don't rely on that — a daily call to
            # /user/refreshsession extends the TTL and keeps the token valid
            # indefinitely. Refresh runs at 03:30 local time (low-traffic
            # window). On auth failure we surface a persistent notification
            # asking the user to re-authenticate via the options flow.
            #
            # The token itself is only used for one-time onboarding calls
            # (/ctw3/signup); the refresh task is the only ongoing cloud
            # interaction the integration makes after startup.
            async def _refresh_cloud_token() -> None:
                try:
                    from .fountain_store import (
                        cloud_from_stored,
                        cloud_to_stored,
                        merge_cloud_into_store_data,
                    )
                    from . import petkit_cloud
                    from homeassistant.helpers.aiohttp_client import (
                        async_get_clientsession,
                    )
                except ImportError:
                    return

                data = await fountain_store.async_load() or {}
                cloud = cloud_from_stored(data)
                if not cloud:
                    return  # no token stored — nothing to refresh

                sess = async_get_clientsession(hass)
                try:
                    result = await petkit_cloud.refresh_session(sess, cloud["token"])
                except petkit_cloud.PetkitAuthError as e:
                    _LOGGER.warning(
                        "Petkit cloud session expired during refresh: %s — "
                        "user re-auth required",
                        e,
                    )
                    try:
                        from homeassistant.components import persistent_notification
                        persistent_notification.async_create(
                            hass,
                            "Your Petkit cloud session has expired. Local "
                            "fountain operation is unaffected (id+secret are "
                            "cached locally), but adding new fountains will "
                            "require re-authentication. Open Settings → "
                            "Devices & Services → Petkit Feeder Local → "
                            "Configure → Add fountain to re-authenticate.",
                            title="Petkit cloud session expired",
                            notification_id="petkit_feeder_session_expired",
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    return
                except petkit_cloud.PetkitCloudError as e:
                    _LOGGER.info(
                        "Petkit cloud refresh failed (will retry tomorrow): %s", e,
                    )
                    return

                new_session = result.get("session") or {}
                new_token = new_session.get("id") or cloud["token"]
                now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                updated = cloud_to_stored(
                    token=str(new_token),
                    user_id=str(new_session.get("userId") or cloud.get("user_id", "")),
                    region=str(new_session.get("region") or cloud.get("region", "DE")),
                    created_at=str(
                        new_session.get("createdAt") or cloud.get("created_at", "")
                    ),
                    last_refresh_at=now_iso,
                )
                await fountain_store.async_save(
                    merge_cloud_into_store_data(data, updated)
                )
                _LOGGER.info(
                    "Petkit cloud session refreshed (token rotated: %s)",
                    str(new_token) != cloud["token"],
                )

            @callback
            def _refresh_cloud_tick(_now) -> None:
                hass.async_create_task(_refresh_cloud_token())

            cancel_cloud_refresh = async_track_time_change(
                hass, _refresh_cloud_tick, hour=3, minute=30, second=0,
            )
            hass.data[DOMAIN]["_fountain_cloud_refresh_cancel"] = cancel_cloud_refresh

            # Startup-staleness check: if the token hasn't been refreshed in
            # the last 24 h (e.g. HA was down for a week), do it now —
            # delayed by 5 min so HA's own startup completes first.
            async def _initial_cloud_refresh_check() -> None:
                import asyncio as _asyncio
                await _asyncio.sleep(300)
                try:
                    from .fountain_store import cloud_from_stored
                except ImportError:
                    return
                data = await fountain_store.async_load() or {}
                cloud = cloud_from_stored(data)
                if not cloud:
                    return
                last_str = cloud.get("last_refresh_at", "")
                stale = True
                try:
                    last_dt = datetime.fromisoformat(
                        last_str.replace("Z", "+00:00")
                    )
                    age_sec = (
                        datetime.now(timezone.utc) - last_dt
                    ).total_seconds()
                    stale = age_sec > 86400
                except (ValueError, TypeError):
                    pass
                if stale:
                    _LOGGER.info(
                        "Cloud token last refreshed >24h ago — refreshing now",
                    )
                    await _refresh_cloud_token()

            hass.async_create_task(_initial_cloud_refresh_check())

            _LOGGER.info(
                "CTW3 fountain extension loaded (%d paired, resync every %s, daily rollover at 00:00:05, cloud refresh at 03:30)",
                len(fs.fountains), _RESYNC_INTERVAL,
            )

    # Create coordinator
    coordinator = PetkitFeederCoordinator(hass, _server)

    # Do initial data load
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = coordinator

    # --- Private: per-fountain coordinators ---
    fs = hass.data[DOMAIN].get("_fountain_server")
    if fs is not None:
        try:
            from .ctw3_coordinator import Ctw3Coordinator
            fountain_coords: dict[str, Ctw3Coordinator] = {}
            for mac in list(fs.fountains):
                c = Ctw3Coordinator(hass, fs, mac)
                await c.async_config_entry_first_refresh()
                fountain_coords[mac] = c
            hass.data[DOMAIN]["_fountain_coordinators"] = fountain_coords
        except ImportError:
            pass

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

    async def handle_set_food_full(call: ServiceCall) -> None:
        """Mark the food tank as freshly filled.

        Resets the dispensed-since-refill counter, so the
        food_remaining_pct sensor jumps back to 100%. Use this after
        physically refilling the tank if the auto-detection (which only
        fires when the firmware ENUM transitions empty→non-empty) didn't
        catch it.
        """
        if _server is None:
            raise HomeAssistantError("Petkit server not running")
        _server.record_refill(source="manual")

    async def handle_set_food_tank_capacity(call: ServiceCall) -> None:
        """Override the assumed full-tank capacity in grams (default 1700)."""
        if _server is None:
            raise HomeAssistantError("Petkit server not running")
        _server.set_food_tank_capacity(int(call.data["grams"]))

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
    if not hass.services.has_service(DOMAIN, SERVICE_SET_FOOD_FULL):
        hass.services.async_register(
            DOMAIN, SERVICE_SET_FOOD_FULL, handle_set_food_full,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_SET_TANK_CAPACITY):
        hass.services.async_register(
            DOMAIN, SERVICE_SET_TANK_CAPACITY, handle_set_food_tank_capacity,
            schema=SERVICE_SET_TANK_CAPACITY_SCHEMA,
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

        # Cancel the fountain-resync timer if this was the last entry.
        cancel = hass.data[DOMAIN].pop("_fountain_resync_cancel", None)
        if callable(cancel):
            cancel()

        # Cancel the midnight daily-reset timer.
        cancel_midnight = hass.data[DOMAIN].pop("_fountain_midnight_cancel", None)
        if callable(cancel_midnight):
            cancel_midnight()

        # Cancel the daily Petkit cloud-token refresh.
        cancel_cloud_refresh = hass.data[DOMAIN].pop(
            "_fountain_cloud_refresh_cancel", None,
        )
        if callable(cancel_cloud_refresh):
            cancel_cloud_refresh()

        # Cancel the HA-driven schedule listener.
        cancel_schedule = hass.data[DOMAIN].pop("_schedule_cancel", None)
        if callable(cancel_schedule):
            cancel_schedule()

        # Stop server if no more entries
        if not hass.data[DOMAIN] and _server is not None:
            await _server.stop()
            _server = None
            # Remove services
            for svc in (SERVICE_SET_SCHEDULE, SERVICE_CLEAR_SCHEDULE, SERVICE_FEED, SERVICE_RESET_DESICCANT, SERVICE_SET_FOOD_FULL, SERVICE_SET_TANK_CAPACITY):
                if hass.services.has_service(DOMAIN, svc):
                    hass.services.async_remove(DOMAIN, svc)

    return unload_ok
