"""Data update coordinator for Petkit Feeder — fully local."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN, DEFAULT_SCAN_INTERVAL
from .local_server import PetkitLocalServer

_LOGGER = logging.getLogger(__name__)


class PetkitFeederCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator that reads data from the local Petkit server."""

    def __init__(
        self,
        hass: HomeAssistant,
        server: PetkitLocalServer,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.server = server

        # Register for push updates from the server
        server.register_update_callback(self._on_server_update)

    @callback
    def _on_server_update(self) -> None:
        """Called by the local server when the feeder sends data."""
        self.async_set_updated_data(self._build_data())

    def _build_data(self) -> dict[str, Any]:
        """Build data dict from local server state."""
        info = self.server.device_info
        settings = self.server.device_settings
        state = self.server.device_state
        today_feeds = self.server.get_today_feeds()

        # Extract status fields from feeder state report
        wifi = state.get("wifi", {})
        err = state.get("err", {})

        # --- Battery + Power handling (D4 state_report fields, empirically) ---
        #   batV : battery voltage in mV. ~444 = ADC floor / no batteries.
        #          5× AA fresh alkaline ≈ 8000mV, EOL ≈ 6500mV.
        #   ubat : 1 = currently drawing from battery, 0 = running on mains (!)
        #          NOT a "battery installed" flag.
        #   DCV  : DC/USB input voltage in mV. ~5100 = USB on, ~444 = USB off.
        ubat_on_battery = bool(state.get("ubat", 0))
        batV = state.get("batV", 0)
        DCV = state.get("DCV", 0)

        # Detection: voltage above ADC noise floor → batteries present
        batteries_installed = batV > 5000

        battery_voltage: int | None = int(batV) if batteries_installed else None
        battery_level: int | None = None
        if batteries_installed:
            # Linear 6500mV (0%) → 8000mV (100%)
            pct = max(0, min(100, int((batV - 6500) / 15)))
            battery_level = pct

        # Power source: USB if DCV >= 4V, else battery
        # Values are translation keys matching translations/*.json entity.sensor.power_source.state.*
        mains_connected = DCV >= 4000
        power_source = "mains" if mains_connected else "battery"

        # Battery status — translation keys matching translations/*.json
        #   entity.sensor.battery_status.state.*
        if not batteries_installed:
            status_text = "no_batteries"
        elif ubat_on_battery:
            status_text = "on_battery"
        elif battery_level is not None and battery_level <= 20:
            status_text = "low"
        else:
            status_text = "ok"

        battery_status = 1 if ubat_on_battery else 0  # legacy field

        # Schedule summary
        schedule = self.server.schedule_summary()

        # --- Stale-based online detection ---
        # Feeder heartbeats ~every 11s. Threshold of 180s (instead of the
        # original 60s) tolerates the brief outages observed when the
        # firmware is busy with a scheduled feed dispense, BLE relay
        # session, or OTA check. Anything longer than that is likely a
        # real network/power issue.
        OFFLINE_THRESHOLD_SEC = 180
        last_hb_str = self.server.last_heartbeat
        last_hb_dt: datetime | None = None
        online: bool = False
        if last_hb_str:
            try:
                last_hb_dt = datetime.fromisoformat(last_hb_str)
                if last_hb_dt.tzinfo is None:
                    last_hb_dt = last_hb_dt.replace(tzinfo=timezone.utc)
                online = (datetime.now(timezone.utc) - last_hb_dt) < timedelta(
                    seconds=OFFLINE_THRESHOLD_SEC
                )
            except (ValueError, TypeError):
                online = False

        return {
            "detail": info,
            "settings": settings,
            "firmware": info.get("firmware", "unknown"),
            "name": info.get("name", "Petkit Feeder"),
            "sn": info.get("sn", ""),
            "mac": info.get("mac", ""),
            "wifi_ssid": wifi.get("ssid", ""),
            "wifi_rssi": wifi.get("rsq", 0),
            "online": online,
            "last_heartbeat_dt": last_hb_dt,
            # Error flags (mapped from state.err sub-object for binary_sensors)
            "error_motor": bool(err.get("moto", 0)),
            "error_rtc": bool(err.get("rtc_c", 0)),
            "error_ir": bool(err.get("ir", 0)),
            "error_dc": bool(err.get("DC", 0)),
            "error_block_door": bool(err.get("blk_d", 0)),
            "error_block_food": bool(err.get("blk_f", 0)),
            "food_state": state.get("food", 0),
            "battery_status": battery_status,
            "battery_status_text": status_text,
            "battery_power_raw": batV,
            "battery_level": battery_level,
            "battery_voltage": battery_voltage,
            "power_source": power_source,
            # Cloud-tracked (we ARE the cloud now) — firmware doesn't track this
            "desiccant_days_left": self.server.desiccant_days_left(),
            "desiccant_reset_at": self.server._desiccant_reset_at,
            "batteries_installed": batteries_installed,
            "door_state": state.get("door", 0),
            "error_dc": err.get("DC", 0),
            "error_sys": err.get("sys", 0),
            "feed_today": today_feeds,
            "feed_today_count": len(today_feeds),
            "feed_today_amount": sum(f.get("amount", 0) for f in today_feeds),
            "food_remaining_pct": self.server.food_remaining_percent(),
            "food_remaining_grams": self.server.food_remaining_grams(),
            "food_tank_capacity_g": self.server.food_tank_capacity_g,
            "food_dispensed_since_refill_g": self.server._food_dispensed_since_refill_g,
            "food_refill_at": self.server._food_refill_at,
            "heartbeat_count": self.server.heartbeat_count,
            "last_heartbeat": self.server.last_heartbeat,
            "heap_free": self.server.heap,
            "uptime": self.server.uptime,
            "schedule_count": schedule["count"],
            "schedule_text": schedule["text"],
            "schedule_entries": schedule["entries"],
            "schedule_raw": schedule["raw"],
        }

    async def _async_update_data(self) -> dict[str, Any]:
        """Periodic fallback update (in case push didn't fire)."""
        return self._build_data()

    @property
    def device_id(self) -> str:
        """Stable unique_id prefix for entity registry.

        Must NOT change across restarts, even when persistence restores
        the real Petkit device ID. Use a constant per integration instance.
        """
        return "0"

    @property
    def device_info_ha(self) -> dict:
        """Return device info for HA device registry.

        Uses a STABLE identifier — not the Petkit SN, because SN is empty
        on first boot (before signup) and populated afterwards, which
        would create a duplicate device entry. Info is still surfaced via
        name/sw_version/serial_number so user sees the real data.
        """
        info = self.server.device_info
        return {
            "identifiers": {(DOMAIN, "0")},   # stable constant — matches original device registry
            "name": info.get("name", "Petkit Feeder"),
            "manufacturer": "Petkit",
            "model": "Fresh Element Solo (D4, Local)",
            "sw_version": str(info.get("firmware", "unknown")),
            "serial_number": info.get("sn", ""),
        }
