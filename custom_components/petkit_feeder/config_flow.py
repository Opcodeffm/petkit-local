"""Config flow + Options flow for Petkit Feeder Local."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import DOMAIN, CONF_DEVICE_ID, CONF_DEVICE_NAME

_LOGGER = logging.getLogger(__name__)

# Values only — display labels come from translations (selector.weekday.options.*)
DAY_VALUES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _fountain_select_schema() -> vol.Schema:
    """Schema for the MAC + SN input on the fountain_select step.

    Defined at module level so the same shape is reused in error-paths
    (where the user submitted bad input and we re-render the form).
    """
    return vol.Schema({
        vol.Required("mac"): str,
        vol.Required("sn"): str,
    })


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
            name = user_input.get(CONF_DEVICE_NAME, "Petkit Feeder")
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
                vol.Optional(CONF_DEVICE_NAME, default="Petkit Feeder"): str,
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

        # CTW3 onboarding state — held across sub-steps of "Add fountain".
        self._cloud_token: str | None = None
        self._cloud_user_id: str = ""
        self._cloud_region: str = "DE"
        self._fetched_fountain: dict[str, Any] | None = None

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
            return "— no entries —"
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

        # Render menu — menu_options as a list uses translation keys from
        # strings.json → options.step.init.menu_options.*
        # "add_fountain" only shown when private/ is present.
        menu_options = ["add", "delete", "clear", "save"]
        try:
            from . import fountain_server  # noqa: F401
            menu_options = ["add", "delete", "clear", "add_fountain", "remove_fountain", "save"]
        except ImportError:
            pass

        return self.async_show_menu(
            step_id="init",
            menu_options=menu_options,
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
            # Amount is restricted to the three sizes the feeder's
            # motor actually dispenses reliably: 10 g (1 portion),
            # 20 g (2 portions), 50 g (5 portions). A free slider would
            # suggest gram-precise control that the hardware doesn't
            # have — the auger rotates in discrete ~10 g steps.
            vol.Required("amount", default="10"): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        {"value": "10", "label": "10 g (1 portion)"},
                        {"value": "20", "label": "20 g (2 portions)"},
                        {"value": "50", "label": "50 g (5 portions)"},
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required("days", default=["mon", "tue", "wed", "thu", "fri", "sat", "sun"]):
                selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=DAY_VALUES,
                        multiple=True,
                        mode=selector.SelectSelectorMode.LIST,
                        translation_key="weekday",
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

    # ------------------------------------------------------------------
    # CTW3 fountain management (only active when private/ is loaded)
    # ------------------------------------------------------------------

    def _get_fountain_server(self):
        try:
            from homeassistant.core import async_get_hass  # type: ignore
            hass = async_get_hass()
        except Exception:
            return None
        return (hass.data.get(DOMAIN) or {}).get("_fountain_server")

    # ----- add_fountain: top-level chooser -----

    async def async_step_add_fountain(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Pick the authentication method, then flow into the matching sub-step.

        Three paths:
          * fountain_login   — Email/Password Petkit account → we log in for them
          * fountain_token   — User pastes their X-Session token (Apple/Google
                               sign-in users, or anyone with mitm capture)
          * fountain_manual  — Direct (id, mac, secret) entry for users who
                               already have those values and want zero cloud calls
        """
        try:
            from . import petkit_cloud  # noqa: F401
        except ImportError:
            return self.async_abort(reason="ctw3_not_available")

        # Try to reuse a stored cloud token first — common case after first
        # onboarding, when the user adds a 2nd fountain to the same account.
        if self._cloud_token is None:
            stored = await self._load_stored_cloud()
            if stored is not None:
                self._cloud_token = stored["token"]
                self._cloud_user_id = stored.get("user_id", "")
                self._cloud_region = stored.get("region", "DE")
                _LOGGER.debug("Reusing stored Petkit cloud token for fountain onboarding")
                return await self.async_step_fountain_select()

        return self.async_show_menu(
            step_id="add_fountain",
            menu_options=["fountain_login", "fountain_token", "fountain_manual"],
        )

    # ----- fountain_login: email + password -----

    async def async_step_fountain_login(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        from . import petkit_cloud
        from homeassistant.helpers.aiohttp_client import async_get_clientsession
        from homeassistant.core import async_get_hass

        errors: dict[str, str] = {}

        if user_input is not None:
            email = (user_input.get("email") or "").strip()
            password = user_input.get("password") or ""
            region = (user_input.get("region") or "DE").strip().upper()

            if not email or "@" not in email:
                errors["email"] = "invalid_email"
            if not password:
                errors["password"] = "invalid_password"

            if not errors:
                try:
                    hass = async_get_hass()
                    sess = async_get_clientsession(hass)
                    result = await petkit_cloud.login(sess, email, password, region=region)
                except petkit_cloud.PetkitAuthError as e:
                    _LOGGER.warning("Petkit login rejected: %s", e)
                    errors["base"] = "invalid_auth"
                except petkit_cloud.PetkitCloudError as e:
                    _LOGGER.warning("Petkit cloud error during login: %s", e)
                    errors["base"] = "cloud_unreachable"
                else:
                    session_block = result.get("session") or {}
                    self._cloud_token = session_block.get("id")
                    self._cloud_user_id = str(session_block.get("userId") or "")
                    self._cloud_region = str(session_block.get("region") or region)
                    if not self._cloud_token:
                        errors["base"] = "no_session_returned"
                    else:
                        _LOGGER.info(
                            "Petkit login OK (userId=%s, region=%s)",
                            self._cloud_user_id, self._cloud_region,
                        )
                        await self._persist_cloud_token(
                            session_block.get("createdAt", "")
                        )
                        return await self.async_step_fountain_select()

        schema = vol.Schema({
            vol.Required("email"): str,
            vol.Required("password"): str,
            vol.Optional("region", default="DE"): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        {"value": "DE", "label": "Europe (DE)"},
                        {"value": "US", "label": "United States"},
                        {"value": "CN", "label": "China"},
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
        })
        return self.async_show_form(
            step_id="fountain_login",
            data_schema=schema,
            errors=errors,
        )

    # ----- fountain_token: paste X-Session token -----

    async def async_step_fountain_token(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        from . import petkit_cloud
        from homeassistant.helpers.aiohttp_client import async_get_clientsession
        from homeassistant.core import async_get_hass

        errors: dict[str, str] = {}

        if user_input is not None:
            token = (user_input.get("token") or "").strip()
            region = (user_input.get("region") or "DE").strip().upper()

            if not token or len(token) < 16 or len(token) > 256:
                errors["token"] = "invalid_token_length"
            else:
                try:
                    hass = async_get_hass()
                    sess = async_get_clientsession(hass)
                    is_valid = await petkit_cloud.validate_session(sess, token)
                except Exception:
                    _LOGGER.exception("Cloud validation crashed")
                    errors["base"] = "cloud_unreachable"
                else:
                    if not is_valid:
                        errors["token"] = "invalid_token"
                    else:
                        self._cloud_token = token
                        self._cloud_region = region
                        # user_id we don't have unless we extract from
                        # /user/details2 — keep blank, refresh task will fill
                        self._cloud_user_id = ""
                        _LOGGER.info("Petkit session token validated; persisting")
                        await self._persist_cloud_token("")
                        return await self.async_step_fountain_select()

        schema = vol.Schema({
            vol.Required("token"): str,
            vol.Optional("region", default="DE"): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        {"value": "DE", "label": "Europe (DE)"},
                        {"value": "US", "label": "United States"},
                        {"value": "CN", "label": "China"},
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
        })
        return self.async_show_form(
            step_id="fountain_token",
            data_schema=schema,
            errors=errors,
        )

    # ----- fountain_manual: power-user direct entry -----

    async def async_step_fountain_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                from .ctw3_state import Ctw3State, mac_to_hex_no_colons
            except ImportError:
                return self.async_abort(reason="ctw3_not_available")

            mac_input = user_input["mac"].strip()
            try:
                mac = mac_to_hex_no_colons(mac_input)
                if len(mac) != 12:
                    raise ValueError("mac length")
                int(mac, 16)
            except (ValueError, TypeError):
                errors["mac"] = "invalid_mac"

            secret = user_input["secret"].strip().lower()
            if not secret or any(c not in "0123456789abcdef" for c in secret):
                errors["secret"] = "invalid_secret"

            try:
                device_id = int(user_input["device_id"])
                if device_id <= 0:
                    raise ValueError
            except (ValueError, TypeError):
                errors["device_id"] = "invalid_device_id"

            if not errors:
                return await self._register_fountain(
                    device_id=device_id,
                    mac=mac,
                    secret=secret,
                    sn=user_input.get("sn", "").strip(),
                    name=user_input.get("name", "").strip() or "Petkit Fountain",
                    firmware=int(user_input.get("firmware", 111) or 111),
                )

        schema = vol.Schema({
            vol.Required("mac"): str,
            vol.Required("secret"): str,
            vol.Required("device_id"): vol.All(vol.Coerce(int), vol.Range(min=1)),
            vol.Optional("sn", default=""): str,
            vol.Optional("name", default="Petkit Fountain"): str,
            vol.Optional("firmware", default=111): vol.All(vol.Coerce(int), vol.Range(min=1, max=999)),
        })
        return self.async_show_form(
            step_id="fountain_manual",
            data_schema=schema,
            errors=errors,
        )

    # ----- fountain_select: ask for MAC+SN, fetch from cloud, confirm -----

    async def async_step_fountain_select(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """User has a valid cloud token. Ask for MAC + SN of one fountain,
        call /ctw3/signup, present a confirmation card with the extracted
        secret, and on confirm register the fountain in HA.

        We don't auto-discover the fountain list because /ctw3/owndevices
        was empirically empty for accounts that pair via the unified
        device_roster_v2 endpoint, and because asking for MAC+SN keeps
        the user in control if their account holds multiple devices."""
        from . import petkit_cloud
        from homeassistant.helpers.aiohttp_client import async_get_clientsession
        from homeassistant.core import async_get_hass

        if not self._cloud_token:
            # Lost the cloud token (e.g., HA restart mid-flow) — bounce back
            return await self.async_step_add_fountain()

        errors: dict[str, str] = {}

        if user_input is not None:
            # If we already have a fetched fountain in state, this is the
            # confirm-step submission.
            if self._fetched_fountain is not None:
                if user_input.get("confirm"):
                    f = self._fetched_fountain
                    return await self._register_fountain(
                        device_id=int(f.get("id") or 0),
                        mac=str(f.get("mac") or "").lower(),
                        secret=str(f.get("secret") or ""),
                        sn=str(f.get("sn") or ""),
                        name=user_input.get("name") or str(f.get("name") or "Petkit Fountain"),
                        firmware=int(f.get("firmware") or 111),
                    )
                # User declined — drop fetched, ask again
                self._fetched_fountain = None

            else:
                mac_in = (user_input.get("mac") or "").strip()
                sn_in = (user_input.get("sn") or "").strip()
                try:
                    hass = async_get_hass()
                    sess = async_get_clientsession(hass)
                    fetched = await petkit_cloud.fetch_fountain(
                        sess, self._cloud_token, mac_in, sn_in
                    )
                except ValueError:
                    errors["mac"] = "invalid_mac"
                except petkit_cloud.PetkitAuthError as e:
                    _LOGGER.warning("Cloud token invalidated mid-flow: %s", e)
                    self._cloud_token = None
                    return self.async_show_form(
                        step_id="fountain_select",
                        data_schema=_fountain_select_schema(),
                        errors={"base": "session_lost"},
                    )
                except petkit_cloud.PetkitCloudError as e:
                    _LOGGER.warning("Cloud signup failed: %s", e)
                    errors["base"] = "fountain_lookup_failed"
                else:
                    if not fetched.get("secret"):
                        errors["base"] = "fountain_no_secret"
                    else:
                        self._fetched_fountain = fetched
                        _LOGGER.info(
                            "Fetched fountain via cloud: id=%s mac=%s sn=%s",
                            fetched.get("id"), fetched.get("mac"), fetched.get("sn"),
                        )

        # Render: either the input form, or the confirm card
        if self._fetched_fountain is not None:
            f = self._fetched_fountain
            sec = str(f.get("secret") or "")
            secret_short = f"{sec[:4]}…{sec[-2:]}" if len(sec) >= 6 else "***"
            confirm_schema = vol.Schema({
                vol.Optional(
                    "name",
                    default=str(f.get("name") or "Petkit Fountain"),
                ): str,
                vol.Required("confirm", default=True): bool,
            })
            return self.async_show_form(
                step_id="fountain_select",
                data_schema=confirm_schema,
                description_placeholders={
                    "id": str(f.get("id") or "?"),
                    "mac": str(f.get("mac") or "?"),
                    "sn": str(f.get("sn") or "?"),
                    "secret": secret_short,
                    "firmware": str(f.get("firmware") or "?"),
                    "default_name": str(f.get("name") or "?"),
                },
                errors=errors,
            )

        return self.async_show_form(
            step_id="fountain_select",
            data_schema=_fountain_select_schema(),
            errors=errors,
        )

    # ----- shared register-and-reload helper -----

    async def _register_fountain(
        self,
        *,
        device_id: int,
        mac: str,
        secret: str,
        sn: str,
        name: str,
        firmware: int,
    ) -> config_entries.ConfigFlowResult:
        """Register a Ctw3State in the running FountainServer and reload
        the integration so entities are created."""
        try:
            from .ctw3_state import Ctw3State
        except ImportError:
            return self.async_abort(reason="ctw3_not_available")

        fs = self._get_fountain_server()
        if fs is None:
            return self.async_abort(reason="ctw3_not_available")

        state = Ctw3State(
            id=device_id,
            mac=mac,
            secret=secret,
            sn=sn,
            name=name,
            firmware=firmware,
            type_code=2,
        )
        fs.register_fountain(state)
        _LOGGER.info(
            "Added fountain via options flow: mac=%s id=%d name=%r",
            mac, device_id, name,
        )

        from homeassistant.core import async_get_hass
        try:
            hass = async_get_hass()
            entries = hass.config_entries.async_entries(DOMAIN)
            if entries:
                await hass.config_entries.async_reload(entries[0].entry_id)
                _LOGGER.info("Reloaded petkit_feeder to create fountain entities")
        except Exception:
            _LOGGER.exception("Failed to reload integration after fountain add")

        # Reset transient state so a 2nd "Add fountain" in same options flow works
        self._fetched_fountain = None
        return self.async_create_entry(title="", data={"entries": self._entries})

    # ----- cloud token persistence helpers -----

    async def _load_stored_cloud(self) -> dict[str, Any] | None:
        """Read the cloud sub-block from the fountain store, if present."""
        try:
            from .fountain_store import (
                STORE_KEY,
                STORE_VERSION,
                cloud_from_stored,
            )
        except ImportError:
            return None
        try:
            from homeassistant.core import async_get_hass
            from homeassistant.helpers.storage import Store
            hass = async_get_hass()
            store = Store(hass, STORE_VERSION, STORE_KEY)
            data = await store.async_load()
            return cloud_from_stored(data)
        except Exception:
            _LOGGER.exception("Failed to load stored cloud token")
            return None

    async def _persist_cloud_token(self, created_at: str) -> None:
        """Write the current self._cloud_* state into the fountain store."""
        try:
            from .fountain_store import (
                STORE_KEY,
                STORE_VERSION,
                cloud_to_stored,
                merge_cloud_into_store_data,
            )
        except ImportError:
            return
        try:
            from homeassistant.core import async_get_hass
            from homeassistant.helpers.storage import Store
            hass = async_get_hass()
            store = Store(hass, STORE_VERSION, STORE_KEY)
            data = await store.async_load() or {}
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            cloud = cloud_to_stored(
                token=self._cloud_token or "",
                user_id=self._cloud_user_id,
                region=self._cloud_region,
                created_at=created_at or now_iso,
                last_refresh_at=now_iso,
            )
            await store.async_save(merge_cloud_into_store_data(data, cloud))
        except Exception:
            _LOGGER.exception("Failed to persist cloud token")

    async def async_step_remove_fountain(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        fs = self._get_fountain_server()
        if fs is None or not fs.fountains:
            return await self.async_step_init()

        if user_input is not None:
            mac = user_input["mac"]
            # Direct removal from fountains dict
            if mac in fs._fountains:
                removed = fs._fountains.pop(mac)
                _LOGGER.info(
                    "Removed fountain: mac=%s sn=%s",
                    removed.mac, removed.sn,
                )
                fs._notify()

            # Reload so stale entities get dropped
            from homeassistant.core import async_get_hass  # type: ignore
            try:
                hass = async_get_hass()
                entries = hass.config_entries.async_entries(DOMAIN)
                if entries:
                    await hass.config_entries.async_reload(entries[0].entry_id)
            except Exception:
                _LOGGER.exception("Failed to reload integration after fountain remove")
            return self.async_create_entry(title="", data={"entries": self._entries})

        options = [
            {
                "value": mac,
                "label": f"{f.name or '(unnamed)'}  [{mac}]  sn={f.sn or '-'}",
            }
            for mac, f in fs.fountains.items()
        ]
        schema = vol.Schema({
            vol.Required("mac"): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=options,
                    multiple=False,
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
        })
        return self.async_show_form(
            step_id="remove_fountain",
            data_schema=schema,
        )
