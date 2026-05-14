"""Local Petkit API server — replaces api.eu-pet.com entirely.

Runs as an aiohttp web server on port 80 inside Home Assistant.
The feeder connects here thinking it's the cloud.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time as _time_mod
from datetime import datetime, timezone
from typing import Any, Callable

import aiohttp
from aiohttp import web

from .const import LOCAL_SERVER_PORT

# Real Petkit cloud for proxy mode (bypass DNS redirect with direct IP)
PETKIT_REAL_IP = "3.66.36.97"  # EU cloud IP from setup_capture
PETKIT_REAL_HOST = "api.eu-pet.com"
PROXY_MODE = False  # Enable via /proxy/on, disable via /proxy/off

# Capture mode: when True, every request (and its proxy upstream response) is
# appended as JSONL to CAPTURE_FILE. For Path B investigation — we need to see
# every byte the feeder exchanges with the cloud (and vice versa).
CAPTURE_MODE = False
CAPTURE_FILE = "/config/petkit_capture.jsonl"
_CAPTURE_LOCK: asyncio.Lock | None = None  # lazy-init inside running event loop


def _get_capture_lock() -> asyncio.Lock:
    global _CAPTURE_LOCK
    if _CAPTURE_LOCK is None:
        _CAPTURE_LOCK = asyncio.Lock()
    return _CAPTURE_LOCK

_LOGGER = logging.getLogger(__name__)


def _compact_days(days: list[str]) -> str:
    """Compact list of weekday shortcodes, e.g. [Mon,Tue,Wed,Thu,Fri] → 'Mon-Fri'."""
    if len(days) == 7:
        return "daily"
    if days == ["Mon", "Tue", "Wed", "Thu", "Fri"]:
        return "Mon-Fri"
    if days == ["Sat", "Sun"]:
        return "weekend"
    return ",".join(days)


class PetkitLocalServer:
    """Local HTTP server that emulates the Petkit Cloud API."""

    def __init__(self) -> None:
        self._app = web.Application()
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

        # Feeder state — updated by the feeder itself via heartbeats/reports
        self._device_state: dict[str, Any] = {}
        self._device_settings: dict[str, Any] = {
            "feedNotify": 1,
            "foodNotify": 1,
            "foodWarn": 0,
            "foodWarnRange": [480, 1200],
            "lowBatteryNotify": 1,
            "desiccantNotify": 1,
            "manualLock": 0,
            "lightMode": 0,          # 0 = LED off
            "lightRange": [0, 1440],
            "factor": 10,
            "lightConfig": 1,
            "lightMultiRange": [],
            "feedSound": 0,          # 0 = sound off
            "colorSetting": 0,
            "controlSettings": 0,
        }
        self._device_info: dict[str, Any] = {
            "id": 0,
            "mac": "",
            "sn": "",
            "secret": "",
            "name": "Petkit Feeder",
            "hardware": 1,
            "firmware": "unknown",
            # timezone / locale / region are overwritten from HA's config at
            # integration setup via set_locale_info(). These are fallbacks
            # only (UTC / neutral) in case the hass helpers aren't available.
            "timezone": 0.0,
            "locale": "UTC",
            "typeCode": 1,
            "btMac": "",
        }
        # region is kept separate because the feeder doesn't care about it —
        # it's only used in /user/login response for the app path.
        self._region: str = "US"
        self._feed_queue: list[dict] = []
        self._settings_push_queue: list[dict] = []  # {key, value} pairs to push via heartbeat
        self._daily_feeds: dict[str, list] = {}
        self._feed_schedule: list = []
        # --- Estimated food-tank fill tracking ----------------------------
        # The D4 firmware reports `food` as a 4-state ENUM (-1/0/1/2 — see
        # sensor.py for the mapping), not a continuous level. To give the
        # user a usable "remaining %" estimate, we count grams dispensed
        # since the last detected refill and compare against a configured
        # tank capacity.
        #
        # Refill detection: when the firmware reports food transitioning
        # 0 → (1 or 2) or -1 → (1 or 2), the tank just got more food, so we
        # zero the dispensed counter. Manual override available via the
        # `set_food_full` service.
        #
        # Tank capacity defaults to 1700 g (Petkit D4 spec is 3 L which
        # holds ~1.7-1.8 kg of typical dry kibble). User can override.
        self._food_tank_capacity_g: int = 1700
        self._food_dispensed_since_refill_g: int = 0
        self._food_refill_at: float | None = None  # unix timestamp of last refill
        self._last_food_state: int | None = None   # for transition detection
        self._heartbeat_count: int = 0
        self._last_heartbeat: str = ""
        self._feeder_online: bool = False
        self._heap: int = 0
        self._uptime: int = 0
        # Own host address as seen by the feeder — set dynamically on every
        # incoming request (request.host). Used for apiServers URLs we hand
        # back to the feeder so they always point to whatever IP/name the
        # feeder already used to reach us.
        self._own_host: str = ""
        # Desiccant reset tracking — Petkit firmware does NOT track this, cloud-side only.
        # When user replaces the desiccant bag, we set this to the current timestamp.
        # days_left = max(0, 30 - days_since_reset)
        self._desiccant_reset_at: float | None = None
        self.DESICCANT_LIFETIME_DAYS = 30
        # Callback to persist state when it changes
        self._persist_callback: Callable | None = None

        # Callbacks for HA entity updates
        self._update_callbacks: list[Callable] = []

        # Optional CTW3 fountain handler — registered by private code
        # when a fountain is configured. Called with (path_suffix, body, raw).
        self._ctw3_handler: Callable[[str, dict, bytes], "dict | None"] | None = None
        # Optional hook used by `/debug/ble_pause_*` endpoints to release or
        # re-acquire the BLE slot on paired fountains. Provided by
        # FountainServer when CTW3 support is loaded.
        self._ble_pause_control: Callable[[bool], None] | None = None
        # DEBUG: optional hook for /debug/test_dnd — pushes a cmd 226 with
        # user-supplied time window to the first registered fountain.
        # Signature: (state:int, w1_start_min:int, w1_end_min:int) -> None
        self._dnd_test_control: Callable[[int, int, int], None] | None = None

        # Heartbeat content providers — each returns an Optional[dict] that,
        # if present, becomes the single result entry in the next heartbeat
        # response instead of the default keepalive. Used by auxiliary
        # modules (e.g. the CTW3 fountain relay) to piggyback commands on
        # the D4 heartbeat cycle.
        self._heartbeat_content_providers: list[Callable[[], dict | None]] = []

        # Optional BLE-roster provider — returns the body for /d4/dev_ble_device
        # (shape: {"list":[{interval,id,secret,type,mac},...],"nextTick":n}).
        # Used by the CTW3 fountain relay to advertise paired fountains.
        self._ble_roster_provider: Callable[[], dict] | None = None

        # Optional event-report handlers — invoked for each /d4/dev_event_report
        # POST body. Used by the CTW3 fountain relay to react to BLE
        # connect/disconnect results and fountain responses.
        self._event_report_handlers: list[Callable[[dict], None]] = []

        self._setup_routes()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the HTTP server on port 80."""
        self._runner = web.AppRunner(self._app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "0.0.0.0", LOCAL_SERVER_PORT)
        try:
            await self._site.start()
            _LOGGER.info("Petkit local server started on port %d", LOCAL_SERVER_PORT)
        except OSError as err:
            _LOGGER.error("Failed to start local server on port %d: %s", LOCAL_SERVER_PORT, err)
            raise

    async def stop(self) -> None:
        """Stop the HTTP server."""
        if self._site:
            await self._site.stop()
        if self._runner:
            await self._runner.cleanup()
        _LOGGER.info("Petkit local server stopped")

    # ------------------------------------------------------------------
    # Callbacks for HA
    # ------------------------------------------------------------------

    def set_locale_info(
        self,
        timezone_offset: float | None = None,
        locale: str | None = None,
        region: str | None = None,
    ) -> None:
        """Configure timezone/locale/region reported to the feeder.

        Called once at integration setup with values from hass.config so the
        feeder sees the HA host's locale, not a hardcoded default.
        """
        if timezone_offset is not None:
            self._device_info["timezone"] = float(timezone_offset)
        if locale:
            self._device_info["locale"] = locale
        if region:
            self._region = region
        _LOGGER.info(
            "Locale configured: tz=%s locale=%s region=%s",
            self._device_info["timezone"],
            self._device_info["locale"],
            self._region,
        )

    def register_update_callback(self, callback: Callable) -> None:
        """Register a callback to be called when feeder data updates."""
        self._update_callbacks.append(callback)

    def register_ctw3_handler(
        self, handler: Callable[[str, dict, bytes], "dict | None"]
    ) -> None:
        """Register a handler for incoming CTW3 POSTs.

        Called with (path_suffix, parsed_body_dict, raw_body_bytes). May
        return a dict that becomes the JSON response `result` field, or
        None to use a default success reply.
        """
        self._ctw3_handler = handler

    def register_ble_pause_control(
        self, control: Callable[[bool], None]
    ) -> None:
        """Register a bool-callback that toggles persistent BLE mode.

        Called with True to enable persistent mode (our integration
        holds the BLE slot continuously), False to release it (so the
        Petkit app can take the slot for direct-BLE edits).

        Exposed via the HTTP debug endpoints `/debug/ble_pause_on`
        and `/debug/ble_pause_off`.
        """
        self._ble_pause_control = control

    def register_dnd_test_control(
        self, control: Callable[[int, int, int], None]
    ) -> None:
        """Register a callback that pushes a test cmd 226 DND frame.

        Invoked with (state, window1_start_min, window1_end_min).
        Used exclusively for protocol verification via
        `/debug/test_dnd?start=480&end=540&state=1`.
        """
        self._dnd_test_control = control

    def register_heartbeat_content_provider(
        self, provider: Callable[[], "dict | None"]
    ) -> None:
        """Register a callback invoked on every D4 heartbeat.

        If the callback returns a non-None dict, that dict becomes the
        single entry in the heartbeat response's `result` list instead
        of the default time-sync keepalive. Used for pushing commands
        (feed, BLE relay, settings) down to the feeder.

        Providers are tried in registration order; first non-None wins.
        """
        self._heartbeat_content_providers.append(provider)

    def register_ble_roster_provider(
        self, provider: Callable[[], dict]
    ) -> None:
        """Register a callback that supplies the /d4/dev_ble_device roster.

        The callback is invoked when the D4 asks the cloud which BLE
        devices it should relay for. Return shape:

            {"list": [{"interval":240,"id":...,"secret":...,"type":24,"mac":...}],
             "nextTick": 3600}

        Only one provider is supported (last registration wins).
        """
        self._ble_roster_provider = provider

    def register_event_report_handler(
        self, handler: Callable[[dict], None]
    ) -> None:
        """Register a callback invoked on every /d4/dev_event_report POST.

        The callback receives the parsed body dict. Used by auxiliary
        modules to react to BLE connect/disconnect results (event_type=52)
        and fountain responses (event_type=53).
        """
        self._event_report_handlers.append(handler)

    def _notify_update(self) -> None:
        """Notify all registered callbacks of data update."""
        for cb in self._update_callbacks:
            try:
                cb()
            except Exception:
                _LOGGER.exception("Error in update callback")

    # ------------------------------------------------------------------
    # Persistence (called by __init__.py via HA Store)
    # ------------------------------------------------------------------

    def register_persist_callback(self, callback: Callable) -> None:
        """Register an async save-callback (called when persistent state changes)."""
        self._persist_callback = callback

    def _persist(self) -> None:
        """Trigger async persist-save (fire and forget)."""
        if self._persist_callback:
            try:
                self._persist_callback()
            except Exception:
                _LOGGER.exception("Error in persist callback")

    def load_persistent_state(self, data: dict) -> None:
        """Load persisted device state (called on HA startup)."""
        if not data:
            return
        if "device_info" in data:
            # Only overwrite fields that were actually saved (keep defaults for missing)
            for k, v in data["device_info"].items():
                if v:  # ignore empty strings/zeros
                    self._device_info[k] = v
        if "device_state" in data:
            self._device_state = data["device_state"]
        if "device_settings" in data:
            self._device_settings.update(data["device_settings"])
        if "desiccant_reset_at" in data:
            self._desiccant_reset_at = data["desiccant_reset_at"]
        if "daily_feeds" in data:
            self._daily_feeds = data["daily_feeds"]
        if "food_tank_capacity_g" in data:
            try:
                self._food_tank_capacity_g = int(data["food_tank_capacity_g"]) or 1700
            except (ValueError, TypeError):
                pass
        if "food_dispensed_since_refill_g" in data:
            try:
                self._food_dispensed_since_refill_g = max(
                    0, int(data["food_dispensed_since_refill_g"])
                )
            except (ValueError, TypeError):
                pass
        if "food_refill_at" in data:
            try:
                self._food_refill_at = float(data["food_refill_at"]) if data["food_refill_at"] else None
            except (ValueError, TypeError):
                self._food_refill_at = None
        if "last_food_state" in data:
            try:
                self._last_food_state = int(data["last_food_state"])
            except (ValueError, TypeError):
                self._last_food_state = None
        # Restore pending feed queue, dropping items older than the max age
        # cap. Real scenario: HA restarts while a scheduled 21:00 feed is
        # waiting for the D4 to come back online — we want that feed to
        # land. But: if HA was off for 6h, the same item should NOT silently
        # fire at 03:00 when HA finally comes back. The 1h cap is the
        # compromise.
        kept = 0
        dropped = 0
        if isinstance(data.get("feed_queue"), list):
            import time as _t
            now = _t.time()
            for item in data["feed_queue"]:
                if not isinstance(item, dict):
                    continue
                try:
                    age = now - float(item.get("queued_at_unix", 0))
                except (ValueError, TypeError):
                    age = self.FEED_QUEUE_MAX_AGE_SEC + 1  # treat as expired
                if 0 <= age <= self.FEED_QUEUE_MAX_AGE_SEC:
                    self._feed_queue.append(item)
                    kept += 1
                else:
                    dropped += 1
        _LOGGER.info(
            "Restored persistent state: firmware=%s, ssid=%s, rsq=%s, "
            "desiccant_reset=%s, feed_days=%d, food_dispensed=%dg/%dg, "
            "feed_queue=%d kept (%d dropped as stale)",
            self._device_info.get("firmware"),
            self._device_state.get("wifi", {}).get("ssid"),
            self._device_state.get("wifi", {}).get("rsq"),
            self._desiccant_reset_at,
            len(self._daily_feeds),
            self._food_dispensed_since_refill_g,
            self._food_tank_capacity_g,
            kept, dropped,
        )

    def get_persistent_state(self) -> dict:
        """Return dict to be persisted. Prunes daily_feeds to last 30 days."""
        # Prune daily_feeds to last 30 days (keeps storage bounded)
        if len(self._daily_feeds) > 30:
            recent_keys = sorted(self._daily_feeds.keys())[-30:]
            self._daily_feeds = {k: self._daily_feeds[k] for k in recent_keys}
        return {
            "device_info": self._device_info,
            "device_state": self._device_state,
            "device_settings": self._device_settings,
            "desiccant_reset_at": self._desiccant_reset_at,
            "daily_feeds": self._daily_feeds,
            "food_tank_capacity_g": self._food_tank_capacity_g,
            "food_dispensed_since_refill_g": self._food_dispensed_since_refill_g,
            "food_refill_at": self._food_refill_at,
            "last_food_state": self._last_food_state,
            # Pending feeds awaiting delivery to the D4. Persisted with their
            # original queued_at_unix so we can age-prune at load time.
            "feed_queue": list(self._feed_queue),
        }

    # ------------------------------------------------------------------
    # Public API for HA integration
    # ------------------------------------------------------------------

    @property
    def feeder_online(self) -> bool:
        return self._feeder_online

    @property
    def device_state(self) -> dict:
        return self._device_state

    @property
    def device_settings(self) -> dict:
        return self._device_settings

    @property
    def device_info(self) -> dict:
        return self._device_info

    @property
    def heartbeat_count(self) -> int:
        return self._heartbeat_count

    @property
    def last_heartbeat(self) -> str:
        return self._last_heartbeat

    @property
    def heap(self) -> int:
        return self._heap

    @property
    def uptime(self) -> int:
        return self._uptime

    @property
    def daily_feeds(self) -> dict:
        return self._daily_feeds

    def get_today_feeds(self) -> list:
        today = datetime.now().strftime("%Y%m%d")
        return self._daily_feeds.get(today, [])

    # Queued feeds older than this are silently dropped on load instead of
    # being delivered. Prevents "zombie feeds" if HA was offline for hours
    # and the queued schedule item is no longer relevant — e.g. the
    # scheduled 21:00 feed should NOT trigger when HA finally comes back
    # the next morning. 1h is a tight cap that still tolerates the
    # realistic restart-during-busy-D4 race we're fixing.
    FEED_QUEUE_MAX_AGE_SEC = 3600

    def queue_feed(self, amount: int) -> None:
        """Queue a manual feed command for delivery on the next heartbeat.

        The queue survives HA restarts: items carry an absolute UNIX
        timestamp `queued_at_unix` and are persisted via
        get_persistent_state().  On reload, items older than
        FEED_QUEUE_MAX_AGE_SEC are dropped (see load_persistent_state).
        """
        import time as _t
        self._feed_queue.append({
            "amount": amount,
            "time": self._now_seconds(),         # seconds-of-day, used by some downstream code paths
            "queued_at_unix": _t.time(),         # absolute time, used for age-based pruning
        })
        _LOGGER.info("Feed queued: %d grams", amount)
        # Persist immediately — if HA crashes/restarts before the D4
        # heartbeat picks this up, we still want the feed to land.
        self._persist()

    def set_schedule_test(self, delay_seconds: int, amount: int) -> None:
        """TEST: set a schedule entry at now+delay_seconds for all 7 days."""
        feed_time = (self._now_seconds() + delay_seconds) % 86400
        items = [{
            "id": f"test{feed_time}",
            "time": feed_time,
            "amount": amount,
            "name": "ScheduleTest",
        }]
        self._feed_schedule = [
            {"suspended": 0, "repeats": i, "items": items}
            for i in range(1, 8)
        ]
        _LOGGER.info(
            "SCHEDULE TEST: feed %dg at time=%d (now+%ds) on ALL days",
            amount, feed_time, delay_seconds
        )

    def update_setting(self, key: str, value: Any) -> None:
        """Update a device setting. Queued for push via heartbeat content."""
        self._device_settings[key] = value
        self._settings_push_queue.append({"key": key, "value": value})
        _LOGGER.info("Setting updated: %s = %s  (queued for push)", key, value)
        self._notify_update()
        self._persist()

    # ------------------------------------------------------------------
    # Desiccant tracking — Petkit firmware doesn't do this, we do.
    # ------------------------------------------------------------------

    def reset_desiccant(self) -> int:
        """Reset the desiccant counter. Returns new days_left value."""
        import time as _time
        self._desiccant_reset_at = _time.time()
        _LOGGER.info("Desiccant counter reset — %d days", self.DESICCANT_LIFETIME_DAYS)
        self._notify_update()
        self._persist()
        return self.DESICCANT_LIFETIME_DAYS

    def desiccant_days_left(self) -> int | None:
        """Compute desiccant days remaining.

        Right after reset: shows full lifetime.
        After 1 full day: shows lifetime-1. Etc.
        Returns None if counter was never set.
        """
        import time as _time
        if self._desiccant_reset_at is None:
            return None
        elapsed_full_days = int((_time.time() - self._desiccant_reset_at) // 86400)
        return max(0, self.DESICCANT_LIFETIME_DAYS - elapsed_full_days)

    # ------------------------------------------------------------------
    # Estimated food-tank fill — Petkit firmware only reports a coarse
    # ENUM (-1/0/1/2). We track grams dispensed since last refill to
    # produce a finer "remaining %" estimate.
    # ------------------------------------------------------------------

    @property
    def food_tank_capacity_g(self) -> int:
        return self._food_tank_capacity_g

    def set_food_tank_capacity(self, grams: int) -> None:
        """Adjust the assumed full-tank capacity (e.g. user has a smaller hopper)."""
        if grams < 100 or grams > 5000:
            raise ValueError(f"capacity must be 100-5000g, got {grams}")
        self._food_tank_capacity_g = int(grams)
        _LOGGER.info("Food tank capacity set to %dg", self._food_tank_capacity_g)
        self._notify_update()
        self._persist()

    def record_refill(self, *, source: str = "manual") -> None:
        """Mark the tank as freshly filled. Resets the dispensed counter to 0
        and the percent-remaining indicator to 100%.

        `source` is one of 'manual' (service call), 'auto' (firmware
        food-state transition detected), 'restored' (state loaded from disk).
        """
        import time as _time
        self._food_dispensed_since_refill_g = 0
        self._food_refill_at = _time.time()
        _LOGGER.info(
            "Food tank marked as refilled (source=%s, capacity=%dg)",
            source, self._food_tank_capacity_g,
        )
        self._notify_update()
        self._persist()

    def _account_for_dispense(self, grams: int) -> None:
        """Internal: increment the dispensed-since-refill counter when a
        feed command is pushed to the feeder. Called from the heartbeat
        handler when a queued feed ships out."""
        if grams <= 0:
            return
        self._food_dispensed_since_refill_g = (
            self._food_dispensed_since_refill_g + grams
        )

    def food_remaining_grams(self) -> int | None:
        """Estimated grams remaining in the tank.

        Returns None if we have never seen a refill (= we don't know the
        baseline). After the first refill (manual or auto), this becomes
        a number between 0 and tank_capacity_g.
        """
        if self._food_refill_at is None:
            return None
        return max(
            0,
            self._food_tank_capacity_g - self._food_dispensed_since_refill_g,
        )

    def food_remaining_percent(self) -> int | None:
        """Estimated remaining tank fill, 0-100. None if no baseline."""
        grams = self.food_remaining_grams()
        if grams is None:
            return None
        return int(round(100 * grams / max(1, self._food_tank_capacity_g)))

    def _check_food_refill_transition(self, new_state: int) -> None:
        """Detect tank-refill events from the firmware's `food` ENUM.

        The firmware reports `food` as -1/0/1/2. A transition from
        empty/unknown into a non-empty state means the user just refilled.
        We auto-call record_refill so the user doesn't have to press a
        button every time.
        """
        prev = self._last_food_state
        self._last_food_state = new_state
        if prev is None:
            # First reading after restart; don't infer a refill.
            return
        # Transition from empty/unknown into "has-food" → refill detected.
        was_empty = prev in (0, -1)
        is_filled = new_state in (1, 2)
        if was_empty and is_filled:
            _LOGGER.info(
                "Auto-detected refill: food state %s → %s",
                prev, new_state,
            )
            self.record_refill(source="auto")

    # ------------------------------------------------------------------
    # Feed schedule management
    # ------------------------------------------------------------------

    MAX_ITEMS_PER_DAY = 10  # Petkit app enforces same limit (APK Feed_max_tip)

    @property
    def feed_schedule(self) -> list:
        """Return the current feed schedule (7 entries, one per weekday)."""
        return self._feed_schedule

    def set_schedule(self, entries: list[dict]) -> list[dict]:
        """Replace the full feed schedule.

        entries: list of {time: "HH:MM", amount: int, days: [1..7 or names], name?: str}

        Returns the built feedDailyList (for caller inspection).
        Raises ValueError on validation errors.
        """
        # Weekday mapping: Petkit uses repeats 1..7 where 1=Monday, 7=Sunday
        DAY_NAMES = {
            "mon": 1, "monday": 1,
            "tue": 2, "tuesday": 2,
            "wed": 3, "wednesday": 3,
            "thu": 4, "thursday": 4,
            "fri": 5, "friday": 5,
            "sat": 6, "saturday": 6,
            "sun": 7, "sunday": 7,
            "mo": 1, "di": 2, "mi": 3, "do": 4, "fr": 5, "sa": 6, "so": 7,
        }

        # Initialize empty schedule: 7 weekdays, each with empty items
        daily_list = [
            {"suspended": 0, "repeats": i, "items": []}
            for i in range(1, 8)
        ]

        for idx, e in enumerate(entries):
            # Parse time "HH:MM" or "HH:MM:SS" → seconds since midnight
            time_str = e.get("time", "")
            try:
                parts = time_str.split(":")
                if len(parts) < 2:
                    raise ValueError
                h, m = parts[0], parts[1]
                time_sec = int(h) * 3600 + int(m) * 60
                if not (0 <= time_sec < 86400):
                    raise ValueError
            except (ValueError, AttributeError):
                raise ValueError(f"entries[{idx}].time must be 'HH:MM' or 'HH:MM:SS', got {time_str!r}")

            amount = int(e.get("amount", 0))
            if amount < 1 or amount > 200:
                raise ValueError(f"entries[{idx}].amount must be 1..200g, got {amount}")

            raw_days = e.get("days", [1, 2, 3, 4, 5, 6, 7])
            day_nums: list[int] = []
            for d in raw_days:
                if isinstance(d, int):
                    if 1 <= d <= 7:
                        day_nums.append(d)
                elif isinstance(d, str):
                    n = DAY_NAMES.get(d.strip().lower())
                    if n:
                        day_nums.append(n)
            if not day_nums:
                raise ValueError(f"entries[{idx}].days has no valid weekdays")

            name = str(e.get("name", f"Plan {idx + 1}"))[:30]
            # Unique-ish id: 'hhmm_amount'
            item_id = f"s{time_sec}_{amount}"

            item = {
                "id": item_id,
                "time": time_sec,
                "amount": amount,
                "name": name,
            }

            for d in day_nums:
                day_entry = daily_list[d - 1]
                if len(day_entry["items"]) >= self.MAX_ITEMS_PER_DAY:
                    raise ValueError(
                        f"Day {d} would exceed max {self.MAX_ITEMS_PER_DAY} items"
                    )
                day_entry["items"].append(item)

        # Sort items per day by time (feeder expects chronological order)
        for d in daily_list:
            d["items"].sort(key=lambda x: x["time"])

        self._feed_schedule = daily_list
        _LOGGER.info(
            "Schedule set: %d total items across 7 days",
            sum(len(d["items"]) for d in daily_list),
        )
        self._notify_update()
        return daily_list

    def clear_schedule(self) -> None:
        """Remove all feed schedule entries."""
        self._feed_schedule = []
        _LOGGER.info("Schedule cleared")
        self._notify_update()

    def schedule_summary(self) -> dict:
        """Return a human-readable summary of the current schedule."""
        DAY_SHORT = {1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat", 7: "Sun"}
        # Group items by (time, amount) → set of days
        groups: dict[tuple[int, int, str], set[int]] = {}
        for entry in self._feed_schedule:
            day = entry.get("repeats")
            for item in entry.get("items", []):
                key = (item["time"], item["amount"], item.get("name", ""))
                groups.setdefault(key, set()).add(day)

        lines = []
        for (time_sec, amount, name), days in sorted(groups.items()):
            hh = time_sec // 3600
            mm = (time_sec % 3600) // 60
            day_str = _compact_days([DAY_SHORT[d] for d in sorted(days)])
            lines.append(f"{hh:02d}:{mm:02d} {amount}g {day_str}" + (f" ({name})" if name else ""))

        total = sum(len(e.get("items", [])) for e in self._feed_schedule)
        return {
            "count": total,
            "text": " · ".join(lines) if lines else "",
            "entries": lines,
            "raw": self._feed_schedule,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _today() -> str:
        return datetime.now().strftime("%Y%m%d")

    @staticmethod
    def _now_seconds() -> int:
        n = datetime.now()
        return n.hour * 3600 + n.minute * 60 + n.second

    @staticmethod
    def _ok(result: Any) -> web.Response:
        return web.json_response({"result": result})

    async def _read_body(self, request: web.Request) -> dict:
        """Parse URL-encoded or JSON body."""
        body = await request.text()
        params = {}
        if not body:
            return params
        # Try URL-encoded first
        for pair in body.split("&"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                from urllib.parse import unquote
                params[unquote(k)] = unquote(v)
        return params

    def _normalize_path(self, path: str) -> str:
        """Normalize /6/d4/... and /latest/d4/... to /d4/..."""
        if path.startswith("/6/"):
            return "/" + path[3:]
        if path.startswith("/latest/"):
            return "/" + path[8:]
        return path

    # ------------------------------------------------------------------
    # Capture (Path B investigation — dumps every request as JSONL)
    # ------------------------------------------------------------------

    async def _capture_write(self, entry: dict) -> None:
        """Append one capture entry to CAPTURE_FILE. Lock-protected."""
        if not CAPTURE_MODE:
            return
        try:
            async with _get_capture_lock():
                loop = asyncio.get_event_loop()
                line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
                await loop.run_in_executor(
                    None,
                    lambda: open(CAPTURE_FILE, "a", encoding="utf-8").write(line),
                )
        except Exception:
            _LOGGER.exception("capture write failed")

    @staticmethod
    def _decode_body_bytes(raw: bytes) -> Any:
        """Best-effort decode: JSON → dict, else URL-encoded → dict, else str/hex."""
        if not raw:
            return ""
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return {"__hex": raw.hex(), "__len": len(raw)}
        # Try JSON first
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            pass
        # Try URL-encoded
        if "=" in text and ("&" in text or not text.startswith("{")):
            from urllib.parse import unquote
            parsed = {}
            for pair in text.split("&"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    parsed[unquote(k)] = unquote(v)
            if parsed:
                return parsed
        return text

    # ------------------------------------------------------------------
    # Route setup
    # ------------------------------------------------------------------

    def _setup_routes(self) -> None:
        self._app.router.add_route("POST", "/{path:.*}", self._handle_post)
        self._app.router.add_route("GET", "/{path:.*}", self._handle_get)

    # ------------------------------------------------------------------
    # GET handler
    # ------------------------------------------------------------------

    async def _handle_get(self, request: web.Request) -> web.Response:
        global PROXY_MODE, CAPTURE_MODE
        path = request.path
        # Capture everything except our own control/health endpoints
        if CAPTURE_MODE and not (
            path == "/" or path == "/health"
            or path.startswith("/proxy/") or path.startswith("/capture/") or path.startswith("/debug/")
            or path.startswith("/test/")
        ):
            await self._capture_write({
                "ts": _time_mod.time(),
                "dir": "in",
                "method": "GET",
                "path": path,
                "query": dict(request.query),
                "host": request.host,
                "remote": request.remote,
                "headers": dict(request.headers),
                "proxy_mode": PROXY_MODE,
            })
        if path == "/" or path == "/health":
            return web.json_response({
                "status": "ok",
                "server": "petkit-local-ha",
                "feeder_online": self._feeder_online,
                "heartbeats": self._heartbeat_count,
                "last_heartbeat": self._last_heartbeat,
                "feed_schedule_items": sum(len(e.get("items", [])) for e in self._feed_schedule),
            })
        if path == "/proxy/on":
            PROXY_MODE = True
            _LOGGER.warning("🔀 PROXY MODE ENABLED — forwarding to real Petkit cloud")
            return web.json_response({"proxy_mode": True})
        if path == "/proxy/off":
            PROXY_MODE = False
            _LOGGER.warning("🛑 PROXY MODE DISABLED")
            return web.json_response({"proxy_mode": False})
        if path == "/capture/on":
            CAPTURE_MODE = True
            _LOGGER.warning("🎥 CAPTURE MODE ENABLED → %s", CAPTURE_FILE)
            return web.json_response({"capture_mode": True, "file": CAPTURE_FILE})
        if path == "/capture/off":
            CAPTURE_MODE = False
            _LOGGER.warning("⏹ CAPTURE MODE DISABLED")
            return web.json_response({"capture_mode": False})
        if path == "/capture/status":
            # Run the file I/O off-loop to avoid blocking the event loop
            # (HA warns on synchronous open() calls inside async handlers).
            def _read_stats() -> tuple[int, int]:
                size = 0
                lines = 0
                if os.path.exists(CAPTURE_FILE):
                    size = os.path.getsize(CAPTURE_FILE)
                    with open(CAPTURE_FILE, "rb") as f:
                        lines = sum(1 for _ in f)
                return size, lines
            try:
                size, lines = await asyncio.to_thread(_read_stats)
            except Exception as err:
                return web.json_response({"error": str(err)}, status=500)
            return web.json_response({
                "capture_mode": CAPTURE_MODE,
                "proxy_mode": PROXY_MODE,
                "file": CAPTURE_FILE,
                "bytes": size,
                "entries": lines,
            })
        if path == "/capture/clear":
            try:
                if os.path.exists(CAPTURE_FILE):
                    os.remove(CAPTURE_FILE)
                _LOGGER.warning("🗑 CAPTURE FILE CLEARED")
                return web.json_response({"cleared": True})
            except Exception as err:
                return web.json_response({"error": str(err)}, status=500)
        if path == "/capture/download":
            try:
                if not os.path.exists(CAPTURE_FILE):
                    return web.json_response({"error": "no capture file"}, status=404)
                return web.FileResponse(
                    CAPTURE_FILE,
                    headers={
                        "Content-Type": "application/x-ndjson",
                        "Content-Disposition": 'attachment; filename="petkit_capture.jsonl"',
                    },
                )
            except Exception as err:
                return web.json_response({"error": str(err)}, status=500)
        # BLE-pause toggle: /debug/ble_pause_on disables our persistent
        # BLE mode and drops active sessions so the Petkit app can claim
        # the fountain's BLE slot directly (needed for DND-time edits
        # and other app-driven config). /debug/ble_pause_off re-enables.
        if path in ("/debug/ble_pause_on", "/debug/ble_pause_off"):
            if self._ble_pause_control is None:
                return web.json_response(
                    {"error": "no BLE fountains registered"}, status=404,
                )
            pause = (path.endswith("_on"))
            try:
                # pause=True means persistent_mode should be OFF
                self._ble_pause_control(not pause)
            except Exception as err:
                return web.json_response({"error": str(err)}, status=500)
            _LOGGER.warning(
                "🔒 BLE PAUSE %s (persistent_mode=%s)",
                "ON" if pause else "OFF", not pause,
            )
            return web.json_response({"ble_paused": pause})
        # DEBUG: /debug/test_dnd?start=MIN&end=MIN&state=N
        # Pushes a cmd 226 DND time-window frame to the fountain for
        # protocol verification. All params optional; defaults to
        # start=480 (08:00), end=540 (09:00), state=1 (DND on).
        if path == "/debug/test_dnd":
            if self._dnd_test_control is None:
                return web.json_response(
                    {"error": "no BLE fountains registered"}, status=404,
                )
            try:
                start = int(request.query.get("start", "480"))
                end = int(request.query.get("end", "540"))
                state = int(request.query.get("state", "1"))
            except ValueError as err:
                return web.json_response({"error": str(err)}, status=400)
            try:
                self._dnd_test_control(state, start, end)
            except Exception as err:
                return web.json_response({"error": str(err)}, status=500)
            return web.json_response({
                "sent": True,
                "state": state,
                "window1_start_min": start,
                "window1_end_min": end,
                "window1_human": f"{start//60:02d}:{start%60:02d}-{end//60:02d}:{end%60:02d}",
            })
        # Proxy GETs too when PROXY_MODE is active (some app endpoints are GET)
        if PROXY_MODE and not (
            path == "/" or path == "/health"
            or path.startswith("/proxy/") or path.startswith("/capture/") or path.startswith("/debug/")
            or path.startswith("/test/")
        ):
            return await self._proxy_to_petkit(request, b"")
        # TEST: /test/schedule/<delay_seconds>/<amount_grams>
        if path.startswith("/test/schedule/"):
            try:
                parts = path.strip("/").split("/")
                delay = int(parts[2])
                amount = int(parts[3])
                self.set_schedule_test(delay, amount)
                return web.json_response({
                    "ok": True,
                    "scheduled_in_seconds": delay,
                    "amount": amount,
                })
            except (ValueError, IndexError) as err:
                return web.json_response({"error": str(err)}, status=400)
        return self._ok("success")

    # ------------------------------------------------------------------
    # POST handler — main routing
    # ------------------------------------------------------------------

    async def _proxy_to_petkit(self, request: web.Request, raw_body: bytes) -> web.Response:
        """Forward the feeder's request to real Petkit cloud and return its response."""
        url = f"http://{PETKIT_REAL_IP}{request.path}"
        if request.query_string:
            url += "?" + request.query_string

        fwd_headers = {}
        for k, v in request.headers.items():
            kl = k.lower()
            if kl in ("host", "content-length"):
                continue
            fwd_headers[k] = v
        fwd_headers["Host"] = PETKIT_REAL_HOST

        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(
                    request.method, url, headers=fwd_headers, data=raw_body,
                    allow_redirects=False,
                ) as resp:
                    resp_body = await resp.read()
                    _LOGGER.info(
                        "PROXY %s %s -> %d (%d bytes): %s",
                        request.method, request.path, resp.status,
                        len(resp_body), resp_body[:500].decode("utf-8", errors="replace"),
                    )
                    hdrs = {
                        k: v for k, v in resp.headers.items()
                        if k.lower() not in ("transfer-encoding", "connection", "content-length")
                    }
                    if CAPTURE_MODE:
                        await self._capture_write({
                            "ts": _time_mod.time(),
                            "dir": "upstream_resp",
                            "method": request.method,
                            "path": request.path,
                            "status": resp.status,
                            "resp_headers": dict(resp.headers),
                            "resp_body_len": len(resp_body),
                            "resp_body": self._decode_body_bytes(resp_body),
                        })
                    return web.Response(
                        status=resp.status,
                        headers=hdrs,
                        body=resp_body,
                    )
        except Exception as err:
            _LOGGER.error("PROXY failed for %s: %s", request.path, err)
            if CAPTURE_MODE:
                await self._capture_write({
                    "ts": _time_mod.time(),
                    "dir": "upstream_err",
                    "method": request.method,
                    "path": request.path,
                    "error": str(err),
                })
            return self._ok("success")

    async def _handle_post(self, request: web.Request) -> web.Response:
        path = request.path
        norm = self._normalize_path(path)
        # Remember whatever host the feeder used to reach us — we reflect
        # it back in responses that need to tell the feeder our server URL.
        if request.host:
            self._own_host = request.host

        # Capture raw body ONCE up front so we can both log it and forward/parse it.
        # The D4 firmware occasionally RSTs the TCP connection mid-request
        # (~180/day observed): aggressive socket-close pattern likely from the
        # ESP32 lwIP stack, possibly correlated with internal heap-watchdog or
        # BLE-relay context-switches. Not user-actionable — the next heartbeat
        # comes through cleanly. Suppress aiohttp's default ERROR-level logging
        # (which spammed 500+ tracebacks per few days) and just return cheaply.
        try:
            raw_body = await request.read()
        except (ConnectionResetError, ConnectionError,
                asyncio.IncompleteReadError) as e:
            _LOGGER.debug(
                "Client %s closed connection mid-request: %s",
                request.remote, e,
            )
            return web.Response(status=200, body=b"")

        if CAPTURE_MODE:
            await self._capture_write({
                "ts": _time_mod.time(),
                "dir": "in",
                "method": "POST",
                "path": path,
                "query": dict(request.query),
                "host": request.host,
                "remote": request.remote,
                "headers": dict(request.headers),
                "body_raw_len": len(raw_body),
                "body": self._decode_body_bytes(raw_body),
                "proxy_mode": PROXY_MODE,
            })

        # If proxy mode active, forward ALL requests to real cloud
        # EXCEPT dev_serverinfo — that one we ALWAYS answer locally so the
        # feeder keeps pointing at us (otherwise real cloud hands back its
        # real URL and the feeder escapes our proxy on next request).
        if PROXY_MODE and norm.endswith("/d4/dev_serverinfo"):
            _LOGGER.info("dev_serverinfo intercepted (PROXY bypassed) — keeping feeder glued to HA")
            return await self._handle_serverinfo(await self._read_body(request))
        # dev_iot_device_info: real cloud returns MQTT creds → feeder tries MQTT →
        # blocked by firewall → feeder stuck in retry loop and stops HTTP heartbeats.
        # Short-circuit with empty response so feeder stays in HTTP mode.
        if PROXY_MODE and norm.endswith("/d4/dev_iot_device_info"):
            _LOGGER.info("dev_iot_device_info intercepted (PROXY bypassed) — preventing MQTT attempt")
            return self._ok({"sn": self._device_info.get("sn", "")})
        if PROXY_MODE:
            try:
                body_preview = raw_body.decode("utf-8", errors="replace")[:300]
            except Exception:
                body_preview = f"<{len(raw_body)}b>"
            _LOGGER.info("PROXY IN: POST %s body=%s", path, body_preview)
            return await self._proxy_to_petkit(request, raw_body)

        # Parse body for local handlers (URL-encoded → dict)
        body = {}
        if raw_body:
            text = raw_body.decode("utf-8", errors="replace")
            from urllib.parse import unquote
            for pair in text.split("&"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    body[unquote(k)] = unquote(v)

        _LOGGER.debug("POST %s body=%s", path, str(body)[:300])

        # ---- Feeder device endpoints (from the feeder itself) ----

        if norm.endswith("/d4/dev_signup"):
            return await self._handle_dev_signup(body)

        if norm.endswith("/d4/dev_state_report"):
            return await self._handle_state_report(body)

        if norm.endswith("/poll/d4/heartbeat"):
            return await self._handle_heartbeat(body)

        if norm.endswith("/d4/dev_serverinfo"):
            return await self._handle_serverinfo(body)

        if norm.endswith("/d4/dev_iot_device_info"):
            return self._ok({"sn": self._device_info.get("sn", "")})

        if norm.endswith("/d4/dev_ble_device"):
            if self._ble_roster_provider is not None:
                try:
                    roster = self._ble_roster_provider()
                except Exception:
                    _LOGGER.exception("ble_roster_provider failed")
                    roster = {"list": [], "nextTick": 3600}
                return self._ok(roster)
            return self._ok({"list": [], "nextTick": 3600})

        if norm.endswith("/d4/dev_feed_get"):
            return await self._handle_dev_feed_get(body)

        if norm.endswith("/d4/dev_multi_config"):
            return await self._handle_dev_multi_config(body)

        if norm.endswith("/d4/dev_event_report"):
            return await self._handle_dev_event_report(body)

        if norm.endswith("/d4/dev_device_info"):
            return await self._handle_dev_device_info(body)

        if norm.endswith("/d4/dev_ota_check"):
            return self._ok({"hasNewVersion": False})

        # ---- App/HA endpoints (from our integration) ----

        if norm.endswith("/user/login"):
            return await self._handle_login(body)

        if norm.endswith("/d4/device_detail"):
            return await self._handle_device_detail(body)

        if norm.endswith("/d4/refreshHomeV2"):
            return await self._handle_refresh_home(body)

        if norm.endswith("/d4/saveDailyFeed"):
            return await self._handle_save_daily_feed(body)

        if norm.endswith("/d4/feed"):
            return await self._handle_get_feed(body)

        if norm.endswith("/d4/saveFeed"):
            return await self._handle_save_feed(body)

        if norm.endswith("/d4/dailyFeeds"):
            return await self._handle_daily_feeds(body)

        if norm.endswith("/d4/feedStatistic"):
            return await self._handle_feed_statistic(body)

        if norm.endswith("/d4/updateSettings"):
            return await self._handle_update_settings(body)

        if norm.endswith("/d4/desiccantReset"):
            new_days = self.reset_desiccant()
            return self._ok(new_days)

        if norm.endswith("/d4/ota_check"):
            return self._ok({"hasNewVersion": False, "version": self._device_info.get("firmware", "1.255")})

        # ---- CTW3 fountain endpoints (optional — only if a handler is registered) ----
        # The D4 (when acting as BLE relay) or the app posts fountain state/config
        # here. We delegate everything under /ctw3/ to an optional registered
        # handler; if none present we still respond 200 so the D4 doesn't retry.
        if "/ctw3/" in norm:
            return await self._handle_ctw3_request(norm, body, raw_body)

        if norm.endswith("/d4/ota_status"):
            return self._ok({"ota": 0})

        if norm.endswith("/device/linkStatus"):
            return self._ok({"online": 1 if self._feeder_online else 0})

        if norm.endswith("/discovery/device_roster_v2"):
            return self._ok([{
                "devices": [{
                    "id": self._device_info.get("id", 0),
                    "name": self._device_info.get("name", "Petkit Feeder"),
                    "type": "D4",
                    "typeCode": 1,
                    "mac": self._device_info.get("mac", ""),
                }]
            }])

        # Catch-all
        _LOGGER.debug("Unknown endpoint: %s", norm)
        return self._ok("success")

    # ------------------------------------------------------------------
    # Feeder handlers
    # ------------------------------------------------------------------

    async def _handle_dev_signup(self, body: dict) -> web.Response:
        """Feeder registration / check-in."""
        if "firmware" in body:
            self._device_info["firmware"] = body["firmware"]
        if "mac" in body:
            self._device_info["mac"] = body["mac"]
        if "sn" in body:
            self._device_info["sn"] = body["sn"]
        if "id" in body:
            self._device_info["id"] = int(body["id"])
        if "bt_mac" in body:
            self._device_info["btMac"] = body["bt_mac"]
        if "hardware" in body:
            self._device_info["hardware"] = int(body["hardware"])

        self._feeder_online = True
        self._notify_update()
        self._persist()

        _LOGGER.info("Feeder registered: %s (SN: %s)", self._device_info["mac"], self._device_info["sn"])

        # Mirror real Petkit signup_status response format as closely as possible
        return self._ok({
            "id": self._device_info["id"],
            "mac": self._device_info["mac"],
            "sn": self._device_info["sn"],
            "secret": self._device_info.get("secret", ""),
            "createdAt": "2026-01-24T12:00:00.000+0000",
            "signupAt": "2026-01-24T12:00:00.000+0000",
            "name": self._device_info.get("name", "Petkit Feeder"),
            "hardware": self._device_info["hardware"],
            "firmware": self._device_info["firmware"],
            "firmwareDetails": [
                {"module": "userbin", "version": 2531005},
                {"module": "lans", "version": 1001},
            ],
            "timezone": self._device_info["timezone"],
            "locale": self._device_info["locale"],
            "shareOpen": 0,
            "autoUpgrade": 0,
            "btMac": self._device_info.get("btMac", ""),
            "typeCode": self._device_info.get("typeCode", 1),
            "settings": self._device_settings,
            "desc": "No feeding schedule.",
            "multiFeed": True,
            "multiConfig": True,
            "state": {
                "wifi": {"ssid": "", "rsq": 0, "bssid": ""},
                "pim": 1,
                "ota": 0,
                "overall": 1,
                "batteryStatus": 0,
                "runtime": 0,
                "batteryPower": 0,
                "food": 1,
                "desiccantLeftDays": 30,
                "door": 1,
                "feeding": 0,
                "desiccantTime": 0,
            },
        })

    async def _handle_state_report(self, body: dict) -> web.Response:
        """Feeder sends its full status."""
        state_raw = body.get("state", "{}")
        try:
            state = json.loads(state_raw) if isinstance(state_raw, str) else state_raw
            self._device_state = state
            self._feeder_online = True
            # Detect auto-refill from food-state transitions (0/-1 → 1/2).
            # Has to happen BEFORE _persist + _notify_update so callers see
            # the freshly-reset dispensed counter on this same tick.
            food_now = state.get("food")
            if isinstance(food_now, int):
                self._check_food_refill_transition(food_now)
            self._notify_update()
            self._persist()
            # Full JSON — no truncation — so we see battery/voltage/pim/etc exactly as sent
            _LOGGER.info("Feeder state report: %s", json.dumps(state, sort_keys=True))
        except (json.JSONDecodeError, TypeError) as err:
            _LOGGER.warning("Failed to parse state report: %s", err)

        return self._ok("success")

    async def _handle_heartbeat(self, body: dict) -> web.Response:
        """Feeder heartbeat — every ~10 seconds.

        Response format (decoded from Petkit cloud via proxy capture):

          Normal keepalive:
            {"result": [{"time": <unix_ms>}]}

          With feed command pushed:
            {"result": [{
                "content": "<escaped JSON>",
                "time": <unix_ms>,
                "timestamp": <unix_sec>
            }]}

          Where content is a JSON string:
            {"msgType":2,
             "payload":{"amount":<g>, "id":"<feed_id>"},
             "type":"feed_realtime",
             "timestamp":<unix_sec>}

          feed_id format: r_YYYYMMDD_<ss>_<ss>-<seq>  (ss = seconds since midnight)
        """
        import time as _time
        # Diagnostic: log when a heartbeat arrives after an unusually
        # long gap. D4 normally heartbeats every ~11 s. With the BLE
        # relay (persistent_mode=True) holding the radio for the
        # fountain, gaps of 90-150 s are routine and expected — every
        # BLE session blocks the heartbeat for its duration. We only
        # call out gaps above the coordinator's offline threshold
        # (180 s) since that's the one that actually means "something
        # went wrong" rather than "BLE was busy". Lower verbosity
        # downgraded from WARNING to INFO — these are normal-mode
        # observations, not user-actionable.
        _GAP_THRESHOLD_SEC = 180
        if self._last_heartbeat:
            try:
                prev = datetime.fromisoformat(self._last_heartbeat)
                gap_sec = (datetime.now(timezone.utc) - prev).total_seconds()
                if gap_sec > _GAP_THRESHOLD_SEC:
                    _LOGGER.info(
                        "D4 heartbeat gap of %.0f s (>%d s) — feeder was "
                        "likely busy or briefly offline; queued feeds "
                        "delivered now",
                        gap_sec, _GAP_THRESHOLD_SEC,
                    )
            except Exception:
                pass
        self._heartbeat_count += 1
        self._last_heartbeat = datetime.now(timezone.utc).isoformat()
        self._feeder_online = True
        self._heap = int(body.get("heap", 0))
        self._uptime = int(body.get("rt", 0))

        now_sec = int(_time.time())
        now_ms = int(_time.time() * 1000)

        # Default: time-sync entry
        result_list = [{"time": now_ms}]

        if self._feed_queue:
            cmd = self._feed_queue.pop(0)
            amount = cmd["amount"]
            today_str = datetime.now().strftime("%Y%m%d")
            sss = self._now_seconds()
            feed_id = f"r_{today_str}_{sss}_{sss}-1"

            inner = {
                "msgType": 2,
                "payload": {"amount": amount, "id": feed_id},
                "type": "feed_realtime",
                "timestamp": now_sec,
            }
            content_string = json.dumps(inner, separators=(",", ":"))

            # Replace result with feed command (matches cloud format: 1 entry with everything)
            result_list = [{
                "content": content_string,
                "time": now_ms,
                "timestamp": now_sec,
            }]

            # Record locally
            today = self._today()
            if today not in self._daily_feeds:
                self._daily_feeds[today] = []
            self._daily_feeds[today].append({
                "id": feed_id,
                "time": sss,
                "amount": amount,
                "src": 3,
                "status": 0,
                "isExecuted": 1,
            })
            # Decrement the estimated tank-fill counter — best-effort
            # ("estimated" because we don't know if the feeder actually
            # succeeded in dispensing; a jammed motor would leave food
            # in the tank but we'd count it as gone).
            self._account_for_dispense(amount)
            _LOGGER.info("FEED CMD pushed to feeder: %dg  id=%s", amount, feed_id)
            self._persist()

        elif self._settings_push_queue:
            # Push all pending setting changes in one go
            payload = {}
            while self._settings_push_queue:
                s = self._settings_push_queue.pop(0)
                payload[s["key"]] = s["value"]

            inner = {
                "msgType": 1,
                "payload": payload,
                "type": "update_settings",
                "timestamp": now_sec,
            }
            content_string = json.dumps(inner, separators=(",", ":"))
            result_list = [{
                "content": content_string,
                "time": now_ms,
                "timestamp": now_sec,
            }]
            _LOGGER.info("SETTINGS push to feeder: %s", payload)

        else:
            # No feed/settings pending — ask any registered content providers
            # (e.g. CTW3 fountain relay) whether they have something to push.
            for provider in self._heartbeat_content_providers:
                try:
                    entry = provider()
                except Exception:  # pragma: no cover
                    _LOGGER.exception("heartbeat content provider failed")
                    continue
                if entry:
                    result_list = [entry]
                    _LOGGER.info(
                        "heartbeat carrying provider content (type=%s)",
                        (json.loads(entry.get("content", "{}")).get("type", "?")
                         if isinstance(entry.get("content"), str) else "?"),
                    )
                    break

        response: dict = {"result": result_list}

        # Periodic update notification (every 6 heartbeats = ~60s)
        if self._heartbeat_count % 6 == 0:
            self._notify_update()

        return web.json_response(response)

    def _server_urls(self) -> dict:
        """Return apiServers / ipServers pointing to whatever host the feeder used."""
        host = self._own_host or "0.0.0.0"
        # Strip port if present — we always serve on LOCAL_SERVER_PORT
        host_no_port = host.split(":")[0]
        return {
            "apiServers": [f"http://{host_no_port}/6/"],
            "ipServers": [f"http://{host_no_port}:{LOCAL_SERVER_PORT}/6/"],
        }

    async def _handle_serverinfo(self, body: dict) -> web.Response:
        """Tell the feeder where to connect."""
        return self._ok(self._server_urls())

    async def _handle_dev_feed_get(self, body: dict) -> web.Response:
        """Feeder asks for its feed schedule.

        Format mirrors the cloud's /d4/feed response: feedDailyList with 7 entries
        (one per weekday, repeats=1..7). Items have {id, time, amount, name}.
        time = seconds since midnight (local time).

        Strategy 2026-04-25 — HA-driven schedule:
        We DO NOT push the user's saved schedule to the feeder's flash
        anymore. Reason: D4 firmware sometimes hangs (heap drift) and
        misses scheduled feeds entirely. Instead HA tracks the schedule
        and queues a manual feed via `queue_feed()` at the right time;
        if the D4 happens to be hanging at that moment the feed sits in
        our queue and fires when it next polls — typically <2 min later.

        So this response is ALWAYS just empty schedule slots plus any
        manually-queued feeds (from buttons, services, or HA-driven
        scheduler) injected at now+10s.
        """
        from datetime import datetime
        now_sec = self._now_seconds()

        # Base empty schedule: 7 days, no items.  HA-driven mode never
        # populates these days from `_feed_schedule` — that's only used
        # by the time-change listener inside the HA integration setup.
        daily_list = [
            {"suspended": 0, "repeats": i, "items": []}
            for i in range(1, 8)
        ]

        # Queued one-shot feeds (manual buttons, scheduler, services).
        # Inject into all 7 days at now+10s so the feeder fires it on
        # whichever weekday it currently thinks today is.
        if self._feed_queue:
            items = []
            for i, cmd in enumerate(self._feed_queue):
                feed_time = max(0, now_sec + 10 + i * 2)
                items.append({
                    "id": f"r{feed_time}-{i+1}",
                    "time": feed_time,
                    "amount": cmd["amount"],
                    "name": "Manual",
                })
            for entry in daily_list:
                entry["items"] = items
            _LOGGER.debug("dev_feed_get: returning %d immediate feed items (all days)", len(items))

        _LOGGER.debug("dev_feed_get response: %s", json.dumps(daily_list)[:500])
        return self._ok({
            "feedDailyList": daily_list,
            "isExecuted": 1,
            "userId": "local",
        })

    async def _handle_dev_multi_config(self, body: dict) -> web.Response:
        """Feeder asks for multi-config / multi-feeder capabilities."""
        return self._ok({
            "multiFeed": True,
            "multiConfig": True,
            "feedDailyList": [
                {"suspended": 0, "repeats": i, "items": []} for i in range(1, 8)
            ],
        })

    async def _handle_dev_event_report(self, body: dict) -> web.Response:
        """Feeder reports events (e.g. manual button press, errors, feed executed).

        Also forwarded to any registered event-report handlers (e.g. the
        CTW3 fountain relay, which watches for event_type=51/52/53 to
        drive its BLE link state machine).
        """
        _LOGGER.info("Feeder event: %s", str(body)[:500])
        for handler in self._event_report_handlers:
            try:
                handler(body)
            except Exception:
                _LOGGER.exception("event_report handler failed")
        return self._ok("success")

    async def _handle_dev_device_info(self, body: dict) -> web.Response:
        """Feeder requests its own device info."""
        return self._ok({
            **self._device_info,
            "settings": self._device_settings,
            "autoUpgrade": 0,
            "shareOpen": 0,
        })

    # ------------------------------------------------------------------
    # App/HA handlers
    # ------------------------------------------------------------------

    async def _handle_login(self, body: dict) -> web.Response:
        return self._ok({
            "session": {
                "id": "local_session_token",
                "userId": "local",
                "expiresIn": 999999,
                "region": self._region,
                "createdAt": datetime.now(timezone.utc).isoformat(),
            },
            "apiServers": self._server_urls()["apiServers"],
            "user": {"id": "local", "nick": "Local"},
        })

    async def _handle_device_detail(self, body: dict) -> web.Response:
        return self._ok({
            **self._device_info,
            "name": self._device_info.get("name", "Petkit Feeder"),
            "settings": self._device_settings,
            "createdAt": "2026-04-16T09:36:46.578+0000",
            "user": {"id": "local", "nick": "Local"},
            "shareOpen": 0,
            "autoUpgrade": 0,
            "relation": {"userId": "local"},
        })

    async def _handle_refresh_home(self, body: dict) -> web.Response:
        today = self._today()
        items = self._daily_feeds.get(today, [])
        return self._ok({
            "devices": [{
                "data": {
                    "name": self._device_info.get("name", "Petkit Feeder"),
                    "id": self._device_info.get("id", 0),
                    "state": 1,
                    "controlSettings": self._device_settings.get("controlSettings", 0),
                    "factor": self._device_settings.get("factor", 10),
                    "relation": {"userId": "local"},
                    "status": self._device_state if self._device_state else {
                        "wifi": {"ssid": "", "rsq": 0, "bssid": ""},
                        "pim": 1 if self._feeder_online else 0,
                        "food": 1,
                        "batteryStatus": 0,
                        "batteryPower": 0,
                        "desiccantLeftDays": 30,
                        "door": 1,
                    },
                    "dailyFeed": {
                        "items": items,
                        "day": int(today),
                        "realAmount": sum(f.get("amount", 0) for f in items),
                    },
                }
            }],
        })

    async def _handle_save_daily_feed(self, body: dict) -> web.Response:
        """Manual feed from HA."""
        amount = int(body.get("amount", 10))
        self.queue_feed(amount)

        feed_time = self._now_seconds()
        return self._ok({
            "id": f"r{feed_time}-1",
            "time": feed_time,
            "amount": amount,
            "src": 3,
            "status": 0,
            "isExecuted": 1,
        })

    async def _handle_get_feed(self, body: dict) -> web.Response:
        # HA-driven schedule: feeder flash schedule is intentionally
        # empty so the feeder never auto-fires. HA queues feeds via
        # the time-change listener instead. See _handle_dev_feed_get.
        empty = [
            {"suspended": 0, "repeats": i, "items": []} for i in range(1, 8)
        ]
        return self._ok({"feedDailyList": empty})

    async def _handle_save_feed(self, body: dict) -> web.Response:
        raw = body.get("feedDailyList", "[]")
        try:
            self._feed_schedule = json.loads(raw)
        except json.JSONDecodeError:
            pass
        return self._ok("success")

    async def _handle_daily_feeds(self, body: dict) -> web.Response:
        day = body.get("days", self._today())
        items = self._daily_feeds.get(day, [])
        return self._ok({
            "feed": [{
                "items": items,
                "day": int(day),
                "planAmount": 0,
                "addAmount": 0,
                "realAmount": sum(f.get("amount", 0) for f in items),
                "deviceId": self._device_info.get("id", 0),
                "amount": 0,
            }]
        })

    async def _handle_feed_statistic(self, body: dict) -> web.Response:
        day = body.get("date", self._today())
        items = self._daily_feeds.get(day, [])
        return self._ok({
            day: {},
            "realAmount": sum(f.get("amount", 0) for f in items),
        })

    async def _handle_update_settings(self, body: dict) -> web.Response:
        kv_raw = body.get("kv", "{}")
        try:
            kv = json.loads(kv_raw)
            self._device_settings.update(kv)
            _LOGGER.info("Settings updated: %s", kv)
            self._notify_update()
        except json.JSONDecodeError:
            pass
        return self._ok("success")

    async def _handle_ctw3_request(
        self, norm_path: str, body: dict, raw_body: bytes
    ) -> web.Response:
        """Dispatch a CTW3 fountain POST to the registered private handler.

        Falls back to a generic `"success"` response if no handler is
        attached — keeps the D4 (or app) happy without knowing what the
        request was about.
        """
        # Extract path suffix after /ctw3/ for convenience: "update", "link", ...
        suffix = norm_path.split("/ctw3/", 1)[1] if "/ctw3/" in norm_path else norm_path
        _LOGGER.info(
            "CTW3 request: %s (body keys: %s, %d bytes raw)",
            norm_path, list(body.keys())[:8], len(raw_body),
        )
        if self._ctw3_handler is None:
            return self._ok("success")
        try:
            result = self._ctw3_handler(suffix, body, raw_body)
        except Exception:  # pragma: no cover
            _LOGGER.exception("CTW3 handler raised")
            return self._ok("success")
        if result is None:
            return self._ok("success")
        return self._ok(result)
