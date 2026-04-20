"""Config flow + Options flow for Petkit Feeder Local."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import DOMAIN, CONF_DEVICE_ID, CONF_DEVICE_NAME

_LOGGER = logging.getLogger(__name__)

DAY_OPTIONS = [
    {"value": "mon", "label": "Montag"},
    {"value": "tue", "label": "Dienstag"},
    {"value": "wed", "label": "Mittwoch"},
    {"value": "thu", "label": "Donnerstag"},
    {"value": "fri", "label": "Freitag"},
    {"value": "sat", "label": "Samstag"},
    {"value": "sun", "label": "Sonntag"},
]


# ----------------------------------------------------------------------
# Initial config flow
# ----------------------------------------------------------------------

class PetkitFeederConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow — just confirm, feeder auto-registers."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            name = user_input.get(CONF_DEVICE_NAME, "Futterautomat")
            return self.async_create_entry(
                title=name,
                data={
                    CONF_DEVICE_ID: 0,
                    CONF_DEVICE_NAME: name,
                },
            )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Optional(CONF_DEVICE_NAME, default="Futterautomat"): str,
            }),
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return PetkitFeederOptionsFlow(config_entry)


# ----------------------------------------------------------------------
# Options flow — schedule editor
# ----------------------------------------------------------------------

class PetkitFeederOptionsFlow(config_entries.OptionsFlow):
    """Menu-based schedule editor.

    Flow:
      init → menu
        • add        → add form → back to init
        • delete     → pick form → back to init
        • clear      → confirms, wipes, back to init
        • save       → commit to server + Store, exit
        • cancel     → discard changes, exit
    """

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        # NOTE: do NOT assign self.config_entry — HA provides it as a property
        # in newer versions and assigning raises AttributeError.
        self._entries: list[dict] = []
        self._loaded = False

    async def _load(self) -> None:
        """Load current entries from the server (canonical) on first open."""
        if self._loaded:
            return
        server = self._get_server()
        if server is None:
            self._entries = []
        else:
            # Reverse-engineer flat entries from the feedDailyList stored
            # in the server. Group items by (time, amount, name) across weekdays.
            groups: dict[tuple[int, int, str], set[int]] = {}
            for entry in server.feed_schedule:
                day = entry.get("repeats")
                for it in entry.get("items", []):
                    key = (it["time"], it["amount"], it.get("name", ""))
                    groups.setdefault(key, set()).add(day)
            day_map = {1: "mon", 2: "tue", 3: "wed", 4: "thu",
                       5: "fri", 6: "sat", 7: "sun"}
            flat = []
            for (time_sec, amount, name), day_nums in groups.items():
                hh = time_sec // 3600
                mm = (time_sec % 3600) // 60
                flat.append({
                    "time": f"{hh:02d}:{mm:02d}:00",
                    "amount": amount,
                    "days": [day_map[d] for d in sorted(day_nums)],
                    "name": name,
                })
            self._entries = sorted(flat, key=lambda e: e["time"])
        self._loaded = True

    def _get_server(self):
        from . import _server
        return _server

    def _summary(self) -> str:
        if not self._entries:
            return "— Kein Eintrag —"
        lines = []
        for i, e in enumerate(self._entries):
            hm = e["time"][:5]
            days = ",".join(e.get("days", []))
            lines.append(f"{i + 1}. {hm}  {e['amount']}g  [{days}]  {e.get('name', '')}")
        return "\n".join(lines)

    # --------- init (menu) ---------

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        await self._load()

        # Render menu with current plan shown in description
        return self.async_show_menu(
            step_id="init",
            menu_options={
                "add": "Eintrag hinzufügen",
                "delete": "Eintrag entfernen",
                "clear": "Alle löschen",
                "save": "Speichern und schließen",
            },
            description_placeholders={"plan": self._summary()},
        )

    # --------- add ---------

    async def async_step_add(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            # HA time selector returns "HH:MM:SS"
            time_val = user_input["time"]
            if len(time_val) >= 5:
                time_val = time_val[:5] + ":00"
            entry = {
                "time": time_val,
                "amount": int(user_input["amount"]),
                "days": list(user_input["days"]),
                "name": user_input.get("name", "") or "",
            }
            self._entries.append(entry)
            self._entries.sort(key=lambda e: e["time"])
            return await self.async_step_init()

        schema = vol.Schema({
            vol.Required("time"): selector.TimeSelector(),
            vol.Required("amount", default=10): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=200, step=1,
                    unit_of_measurement="g",
                    mode=selector.NumberSelectorMode.SLIDER,
                )
            ),
            vol.Required("days", default=["mon", "tue", "wed", "thu", "fri", "sat", "sun"]):
                selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=DAY_OPTIONS,
                        multiple=True,
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
            vol.Optional("name", default=""): str,
        })

        return self.async_show_form(
            step_id="add",
            data_schema=schema,
            errors=errors,
            description_placeholders={"plan": self._summary()},
        )

    # --------- delete ---------

    async def async_step_delete(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if not self._entries:
            return await self.async_step_init()

        if user_input is not None:
            idx = int(user_input["index"])
            if 0 <= idx < len(self._entries):
                removed = self._entries.pop(idx)
                _LOGGER.info("Removed schedule entry: %s", removed)
            return await self.async_step_init()

        options = [
            {
                "value": str(i),
                "label": f"{e['time'][:5]}  {e['amount']}g  [{','.join(e.get('days', []))}]  {e.get('name', '')}",
            }
            for i, e in enumerate(self._entries)
        ]

        schema = vol.Schema({
            vol.Required("index"): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=options,
                    multiple=False,
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
        })
        return self.async_show_form(
            step_id="delete",
            data_schema=schema,
            description_placeholders={"plan": self._summary()},
        )

    # --------- clear ---------

    async def async_step_clear(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        self._entries = []
        _LOGGER.info("Schedule cleared via options flow")
        return await self.async_step_init()

    # --------- save ---------

    async def async_step_save(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        server = self._get_server()
        if server is not None:
            try:
                server.set_schedule(self._entries)
            except ValueError as err:
                _LOGGER.warning("Invalid schedule on save: %s", err)
                return self.async_abort(reason="invalid_schedule")

        # Persist also via our Store (so user-set service + options flow share storage)
        from . import _store
        if _store is not None:
            await _store.async_save({"entries": self._entries})

        _LOGGER.info("Saved schedule: %d entries", len(self._entries))
        return self.async_create_entry(title="", data={"entries": self._entries})
