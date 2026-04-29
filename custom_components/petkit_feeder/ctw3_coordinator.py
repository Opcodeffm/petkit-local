"""Per-fountain coordinator that bridges FountainServer state changes into
Home Assistant's update loop.

Design:
    - One Ctw3Coordinator instance per fountain MAC
    - Polls FountainServer for current state on every tick
    - Also hooks into FountainServer.register_update_listener for push updates
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

try:
    from .ctw3_state import Ctw3State
    from .fountain_server import FountainServer
except ImportError:
    from ctw3_state import Ctw3State
    from fountain_server import FountainServer


_LOGGER = logging.getLogger(__name__)


class Ctw3Coordinator(DataUpdateCoordinator[Ctw3State | None]):
    """Pull state for a single CTW3 fountain from FountainServer."""

    def __init__(
        self,
        hass: HomeAssistant,
        fountain_server: FountainServer,
        fountain_mac: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"petkit_ctw3_{fountain_mac}",
            update_interval=timedelta(seconds=30),
        )
        self.fountain_server = fountain_server
        self.fountain_mac = fountain_mac
        fountain_server.register_update_listener(self._on_fountain_update)

    @callback
    def _on_fountain_update(self) -> None:
        """Invoked by FountainServer whenever any fountain changes."""
        # Only propagate if OUR fountain
        state = self.fountain_server.get_fountain(self.fountain_mac)
        if state is not None:
            self.async_set_updated_data(state)

    async def _async_update_data(self) -> Ctw3State | None:
        return self.fountain_server.get_fountain(self.fountain_mac)

    # --- Convenience accessors for entities -------------------------------

    @property
    def fountain(self) -> Ctw3State | None:
        return self.data or self.fountain_server.get_fountain(self.fountain_mac)

    @property
    def device_info_ha(self) -> dict:
        f = self.fountain_server.get_fountain(self.fountain_mac) or Ctw3State()
        return {
            "identifiers": {("petkit_feeder", f"ctw3_{self.fountain_mac}")},
            "name": f.name or "Petkit Fountain",
            "manufacturer": "Petkit",
            "model": "Eversweet Max 2 (Cordless)",
            "sw_version": str(f.firmware),
            "serial_number": f.sn or None,
        }
