"""FountainServer — private extension that plugs into PetkitLocalServer
to enable CTW3 fountain control via D4 BLE relay.

Protocol lifecycle (observed live 2026-04-21):
  1. Cloud pushes `type:"connect"` with connect_action=1 via D4 heartbeat.
  2. D4 attempts BLE connection, reports `event_type=51` (started) then
     `event_type=52` with result=0 (success) or result=3 (failed).
  3. Once CONNECTED, cloud pushes one or more `type:"ble"` command
     entries via subsequent heartbeats.
  4. Fountain responses come back as `event_type=53` POSTs.
  5. Cloud pushes `type:"connect"` with connect_action=0 to close.

This module manages that state machine per-fountain: commands submitted
via send_* methods are buffered until the BLE link is CONNECTED, then
flushed, then the link is torn down.

This module does NOT talk to HA directly. It's a plain Python service
consumed by `private/coordinator.py` and the HA platforms.
"""
from __future__ import annotations

import logging
from collections import deque
from typing import Callable, Optional

try:
    from .ble_control import (
        BleControlCommand,
        wrap_ble,
        wrap_connect,
    )
    from .ble_frame import (
        CMD_RESET_FILTER,
        CMD_SET_CONFIG,
        CMD_SET_LIGHT,
        CMD_SET_MODE,
        DEVICE_TYPE_CTW3,
        FrameSequencer,
        build_frame,
    )
    from .ctw3_decode import Cmd230Status, FountainConfig
    from .ctw3_state import Ctw3State
except ImportError:
    from ble_control import (
        BleControlCommand,
        wrap_ble,
        wrap_connect,
    )
    from ble_frame import (
        CMD_RESET_FILTER,
        CMD_SET_CONFIG,
        CMD_SET_LIGHT,
        CMD_SET_MODE,
        DEVICE_TYPE_CTW3,
        FrameSequencer,
        build_frame,
    )
    from ctw3_decode import Cmd230Status, FountainConfig
    from ctw3_state import Ctw3State


_LOGGER = logging.getLogger(__name__)


# --- Per-fountain link states (D4 ↔ fountain BLE channel) -------------------

LINK_IDLE = 0          # no BLE link attempted
LINK_CONNECTING = 1    # connect push sent, waiting for event_type=52
LINK_CONNECTED = 2     # D4 reports BLE up, we can push BLE cmds
LINK_DISCONNECTING = 3  # disconnect push sent, waiting for event_type=52


# BLE-connect reliability knobs.
# If we stay in LINK_CONNECTING longer than this without an event_type=52
# result, assume the attempt silently failed (fountain asleep, out of range,
# firmware bug) and recycle the state back to IDLE.
CONNECT_ATTEMPT_TIMEOUT_SEC = 60.0
# After consecutive connect failures, wait progressively longer before
# retrying. Final cap prevents spamming BLE pushes when the fountain is
# simply off.
CONNECT_RETRY_BACKOFF_SEC = [10.0, 30.0, 120.0, 300.0]
# Liveness check for LINK_CONNECTED sessions: if no cmd 230 status dump
# has been received for this long while we "are" connected, assume the
# fountain/D4 has silently dropped the BLE link. We then force-disconnect
# and reconnect so persistent mode recovers. Chosen slightly longer than
# the observed ~60 s spontaneous dump cadence so we don't false-positive
# on a slow dump window.
SESSION_LIVENESS_TIMEOUT_SEC = 90.0


def _link_name(state: int) -> str:
    return {
        LINK_IDLE: "IDLE",
        LINK_CONNECTING: "CONNECTING",
        LINK_CONNECTED: "CONNECTED",
        LINK_DISCONNECTING: "DISCONNECTING",
    }.get(state, f"?({state})")


class FountainServer:
    """Per-HA-instance service managing all paired CTW3 fountains."""

    def __init__(self, persistent_mode: bool = False) -> None:
        """FountainServer.

        persistent_mode: when True, keep the BLE session to each fountain
        open indefinitely so motion-sensor transitions (byte 19 of cmd 230)
        are observed live as they happen. In session-based mode (False),
        the server connects only for specific commands and disconnects
        immediately — which means short drink events between commands are
        missed entirely.

        Implications of persistent_mode=True:
          - iPhone cannot connect to the fountain directly via BT while we
            hold the BLE slot. The Petkit app still works via the cloud if
            you use it, but direct-BT pairing (used for initial setup) is
            blocked until we disconnect.
          - Slightly higher energy use on the fountain (confirmed by
            pdiegmann's W5 integration: ~1.5 months of battery life with
            10-second polling, so the CTW3 should be fine on battery for
            weeks even in persistent mode).
        """
        self._persistent_mode = persistent_mode
        self._fountains: dict[str, Ctw3State] = {}          # mac -> state
        # Per-fountain link state + queue of commands waiting for link-up.
        self._link_state: dict[str, int] = {}               # mac -> LINK_*
        self._pending_cmds: dict[str, deque[BleControlCommand]] = {}  # mac -> queue
        # Stream of heartbeat result-entries ready to inject on next D4 poll.
        self._push_queue: deque[dict] = deque()
        self._sequencer = FrameSequencer()
        self._update_listeners: list[Callable[[], None]] = []
        # Fountains whose `f.settings` has been refreshed from a real
        # cmd 230 status dump since this FountainServer was created. Writes
        # that depend on read-modify-write of f.settings (e.g. send_config_update)
        # are blocked until the mac appears in this set, otherwise we'd push
        # HA-default config values to the fountain and overwrite whatever the
        # user had configured via the Petkit app. (Observed live 2026-04-22:
        # a premature send_config_update caused Main-Unit-Failure blinking.)
        self._settings_synced: set[str] = set()
        # Fountains that should get an auto-sync BLE session on next
        # opportunity (drives _drive_link to open a session even with
        # empty pending queue, then reads cmd 215 to trigger a cmd 230
        # status dump from the fountain — same pattern as the real cloud).
        self._sync_requested: set[str] = set()
        # Verification: tracks the config we pushed via send_config_update,
        # so the next incoming cmd 230 dump can be compared field-by-field.
        self._pending_verification: dict[str, FountainConfig] = {}
        # Drink-event state machine per fountain, tracks transitions of
        # cmd 230 byte 19 (motion flag) and uses counter_runtime delta to
        # measure each drink's duration. Purely local — we don't rely on
        # any Petkit-cloud event (confirmed: the cloud never posts drink
        # events back to the D4).
        # Schema per mac: {"last_b19": int, "start_counter": int|None,
        #                  "start_ts": float|None}
        self._drink_state: dict[str, dict] = {}
        # Connect-attempt timeout / retry-backoff tracking. The D4 can
        # leave us hanging in LINK_CONNECTING forever if the fountain
        # ignores a BLE connect request (observed after ~20 min fountain
        # idle → goes into deep sleep). On every heartbeat we check for
        # stuck CONNECTING and bail out. Failed attempts back off so we
        # don't spam connect pushes.
        self._connecting_since: dict[str, float] = {}     # mac -> ts entered CONNECTING
        self._connect_fail_count: dict[str, int] = {}     # mac -> consecutive fails
        self._next_retry_at: dict[str, float] = {}        # mac -> unix_ts, no connect before this
        # Liveness tracker: timestamp of the most recent cmd 230 dump
        # (or of transition INTO CONNECTED). If too long without a dump
        # while CONNECTED, we assume silent link drop and force reconnect.
        self._last_dump_at: dict[str, float] = {}         # mac -> unix_ts

    # --- Fountain registry --------------------------------------------------

    def register_fountain(self, state: Ctw3State) -> None:
        """Add or replace a fountain in the registry.

        Automatically requests an initial sync — the FountainServer will
        open a BLE session at the next opportunity and pull a cmd 230
        status dump to initialise `f.settings` from real fountain values
        (instead of HA defaults, which would cause cmd 221 pushes to
        clobber the fountain's actual config).
        """
        self._fountains[state.mac] = state
        self._link_state.setdefault(state.mac, LINK_IDLE)
        self._pending_cmds.setdefault(state.mac, deque())
        if state.mac not in self._settings_synced:
            self._sync_requested.add(state.mac)
        _LOGGER.info(
            "Fountain registered: mac=%s sn=%s name=%s  (sync %s)",
            state.mac, state.sn, state.name or "(unnamed)",
            "queued" if state.mac in self._sync_requested else "already done",
        )
        # Kick the state machine so the sync session starts immediately
        # (instead of waiting for the first user command).
        self._drive_link(state.mac)
        self._notify()

    def unregister_fountain(self, mac: str) -> None:
        """Remove a fountain and drop any pending commands for it."""
        mac = mac.lower().replace(":", "")
        self._fountains.pop(mac, None)
        self._link_state.pop(mac, None)
        self._pending_cmds.pop(mac, None)
        self._settings_synced.discard(mac)
        self._sync_requested.discard(mac)
        self._pending_verification.pop(mac, None)
        self._connecting_since.pop(mac, None)
        self._connect_fail_count.pop(mac, None)
        self._next_retry_at.pop(mac, None)
        self._last_dump_at.pop(mac, None)
        self._notify()

    def request_sync(self, mac: str) -> None:
        """Request an initial / fresh cmd 230 status dump from a fountain.

        Marks the fountain as "needs sync" — the state machine will open a
        BLE session on the next opportunity, push cmd 215 reads (mirroring
        the real cloud's post-connect behaviour), and wait for the fountain
        to respond with a cmd 230 dump, which resets `f.settings`.

        Useful if the cached settings might be stale after a user change
        through the Petkit app, or for a manual resync service.
        """
        if mac not in self._fountains:
            return
        self._sync_requested.add(mac)
        self._drive_link(mac)

    def is_settings_synced(self, mac: str) -> bool:
        """True once we've applied a real cmd 230 status dump for this fountain.

        Settings-modifying commands (cmd 221) must not run before this is
        True — otherwise we'd send HA-default values instead of the
        fountain's actual config, clobbering whatever the user set in the
        Petkit app.
        """
        return mac in self._settings_synced

    def get_fountain(self, mac: str) -> Optional[Ctw3State]:
        return self._fountains.get(mac)

    @property
    def fountains(self) -> dict[str, Ctw3State]:
        return dict(self._fountains)

    def ble_roster(self, default_interval: int = 240, next_tick: int = 3600) -> dict:
        """Return a `/d4/dev_ble_device` response body advertising all fountains.

        Matches the real cloud response shape:
            {"list":[{"interval":240,"id":<id>,"secret":"...","type":24,"mac":"..."}],
             "nextTick":3600}
        """
        return {
            "list": [
                {
                    "interval": default_interval,
                    "id": f.id,
                    "secret": f.secret,
                    "type": DEVICE_TYPE_CTW3,
                    "mac": f.mac,
                }
                for f in self._fountains.values()
            ],
            "nextTick": next_tick,
        }

    # --- Update listeners ---------------------------------------------------

    def register_update_listener(self, cb: Callable[[], None]) -> None:
        self._update_listeners.append(cb)

    def set_persistent_mode(self, enabled: bool) -> None:
        """Toggle persistent-BLE-session mode at runtime.

        Used by debug/capture endpoints to temporarily release the BLE
        slot to the Petkit app (which needs a direct BLE connection to
        the fountain for e.g. DND time-window edits).

        When disabling while a session is open, queue a disconnect for
        the next heartbeat so the link actually drops — otherwise we'd
        stay connected forever since no new command would kick
        `_drive_link` into action.
        """
        if self._persistent_mode == enabled:
            return
        self._persistent_mode = enabled
        _LOGGER.warning(
            "Persistent BLE mode %s",
            "ENABLED" if enabled else "DISABLED (BLE slot released)",
        )
        if not enabled:
            # Force any CONNECTED links to disconnect so the slot is free
            # for the Petkit app / phone BLE direct.
            import time as _time
            for mac, state in list(self._link_state.items()):
                if state == LINK_CONNECTED:
                    # Push an explicit "close" entry onto the D4 heartbeat
                    # queue — mirrors how a real cloud-initiated disconnect
                    # looks.
                    content = {
                        "msgType": 2,
                        "type": "connect",
                        "payload": {
                            "connect_action": 0,  # 0 = close
                            "device": {"type": 24, "mac": mac},
                            "timestamp": int(_time.time()),
                        },
                        "timestamp": int(_time.time()),
                    }
                    import json as _json
                    self._push_queue.append({
                        "content": _json.dumps(content),
                        "time": int(_time.time() * 1000),
                        "timestamp": int(_time.time()),
                    })
                    self._link_state[mac] = LINK_DISCONNECTING
                    _LOGGER.info("Link %s: forced disconnect queued (pause_ble)", mac)

    def _notify(self) -> None:
        for cb in self._update_listeners:
            try:
                cb()
            except Exception:
                _LOGGER.exception("update listener error")

    # --- Command submission -------------------------------------------------

    def queue_command(self, cmd: BleControlCommand) -> None:
        """Submit a BLE command. Buffers until the link is CONNECTED."""
        mac = cmd.fountain_mac
        if mac not in self._fountains:
            raise ValueError(f"fountain not registered: mac={mac!r}")
        self._pending_cmds.setdefault(mac, deque()).append(cmd)
        _LOGGER.info(
            "BLE cmd queued: mac=%s cmd=%d frame=%d bytes (pending: %d, link=%s)",
            mac, cmd.cmd_code, len(cmd.frame_bytes),
            len(self._pending_cmds[mac]),
            _link_name(self._link_state.get(mac, LINK_IDLE)),
        )
        self._drive_link(mac)

    def _sync_needed(self, mac: str) -> bool:
        """True if this fountain was flagged for sync and hasn't dumped yet."""
        return mac in self._sync_requested and mac not in self._settings_synced

    def _queue_sync_reads(self, mac: str) -> None:
        """Push two cmd 215 (LED status) READ frames into the push queue.

        Empty-data cmd 215 is a read request — the fountain replies with
        current LED state AND spontaneously emits a cmd 230 full-status
        dump shortly after. This is exactly what the real Petkit cloud
        does immediately after a connect (observed 2026-04-21).

        Used to trigger the initial sync without needing a user action.
        """
        f = self._fountains.get(mac)
        if f is None:
            return
        for _ in range(2):
            frame = build_frame(
                seq=self._sequencer.next(),
                cmd=CMD_SET_LIGHT,  # 215 — empty payload = read
                type_=1,
                data=[],
            )
            self._push_queue.append(wrap_ble(BleControlCommand(
                fountain_id=f.id,
                fountain_mac=f.mac,
                cmd_code=CMD_SET_LIGHT,
                frame_bytes=frame,
                device_type=DEVICE_TYPE_CTW3,
            )))

    def _drive_link(self, mac: str) -> None:
        """Advance the link state machine for `mac`.

        Called whenever pending-queue, link-state, or sync-request changes.
        Emits push entries to `_push_queue` as appropriate.

        In persistent_mode, we always want the link UP — never push a
        disconnect. If the D4 or fountain drops the session, we observe
        it via event_type=52 action=0 and auto-reconnect in
        `_on_connect_result`.
        """
        state = self._link_state.get(mac, LINK_IDLE)
        pending = self._pending_cmds.get(mac, deque())
        needs_sync = self._sync_needed(mac)

        # Reason to hold/open the link: pending cmds, sync requested, OR
        # persistent mode (we want to observe cmd 230 dumps live).
        want_connected = bool(pending) or needs_sync or self._persistent_mode

        if state == LINK_IDLE and want_connected:
            # Respect retry backoff: if a prior attempt failed, don't retry
            # until the scheduled cool-down has elapsed.
            import time as _time
            now = _time.time()
            retry_at = self._next_retry_at.get(mac, 0.0)
            if now < retry_at:
                # Still in cooldown — skip this attempt silently.
                return
            # Open a BLE session.
            self._push_queue.append(
                wrap_connect(mac, DEVICE_TYPE_CTW3, action=1)
            )
            self._link_state[mac] = LINK_CONNECTING
            self._connecting_since[mac] = now
            _LOGGER.info(
                "Link %s -> CONNECTING (pending=%d, sync_needed=%s, persistent=%s, fail#%d)",
                mac, len(pending), needs_sync, self._persistent_mode,
                self._connect_fail_count.get(mac, 0),
            )

        elif state == LINK_CONNECTED:
            # Drain any pending user cmds first.
            n = 0
            while pending:
                cmd = pending.popleft()
                self._push_queue.append(wrap_ble(cmd))
                n += 1

            if needs_sync:
                # Queue READ commands to trigger a cmd 230 dump. In
                # session mode we wait for the dump then disconnect; in
                # persistent mode we just stay connected afterwards.
                self._queue_sync_reads(mac)
                _LOGGER.info(
                    "Link %s CONNECTED (drained %d, sync reads queued) — "
                    "awaiting cmd 230 dump",
                    mac, n,
                )
            elif self._persistent_mode:
                # Stay connected — the fountain will stream cmd 230 dumps
                # spontaneously while the link is up.
                if n:
                    _LOGGER.info(
                        "Link %s CONNECTED (drained %d, staying connected)",
                        mac, n,
                    )
            else:
                # Session mode, fully synced — clean disconnect.
                self._push_queue.append(
                    wrap_connect(mac, DEVICE_TYPE_CTW3, action=0)
                )
                self._link_state[mac] = LINK_DISCONNECTING
                _LOGGER.info("Link %s -> DISCONNECTING (pushed %d BLE cmd(s))", mac, n)

        # LINK_CONNECTING / LINK_DISCONNECTING: we wait for event_type=52.

    @property
    def queue_depth(self) -> int:
        """Total heartbeat-entries waiting to be injected (push stream)."""
        return len(self._push_queue)

    @property
    def pending_cmd_count(self) -> int:
        """Total BLE commands buffered across all fountains."""
        return sum(len(q) for q in self._pending_cmds.values())

    def clear_queue(self) -> int:
        """Drop all push-stream entries AND all buffered commands."""
        n = len(self._push_queue) + sum(len(q) for q in self._pending_cmds.values())
        self._push_queue.clear()
        for q in self._pending_cmds.values():
            q.clear()
        # Reset link states back to IDLE so caller can re-queue cleanly.
        for mac in list(self._link_state.keys()):
            self._link_state[mac] = LINK_IDLE
        if n:
            _LOGGER.info("Cleared %d pending entries/commands", n)
        return n

    # --- High-level command helpers (build BLE frame + queue) ---------------

    def send_set_mode(
        self,
        fountain_mac: str,
        state_on: int,
        mode: int,
        suspend: int | None = None,
    ) -> None:
        """Push mode/power command (cmd 220).

        data layout: [powerStatus, suspendStatus, mode]
          powerStatus: 0=pump off, 1=pump on
          suspendStatus: 0=running, 1=paused (pump on but not actively pumping)
          mode: 1=SMART, 2=NORMAL, 3=INTERMITTENT

        If `suspend` is None, the current fountain suspend_status is preserved.
        """
        f = self._require_fountain(fountain_mac)
        if suspend is None:
            suspend = f.status.suspend_status
        frame = build_frame(
            seq=self._sequencer.next(),
            cmd=CMD_SET_MODE,
            type_=1,
            data=[state_on, suspend, mode],
        )
        self.queue_command(BleControlCommand(
            fountain_id=f.id,
            fountain_mac=f.mac,
            cmd_code=CMD_SET_MODE,
            frame_bytes=frame,
            device_type=DEVICE_TYPE_CTW3,
        ))
        # Optimistic local state update — HA switch stays in the new position
        # without waiting for device feedback. Will be corrected by real
        # feedback if/when it arrives via event_type=53 / /ctw3/update.
        f.status.power_status = state_on
        f.status.suspend_status = suspend
        f.mode = mode
        self._notify()

    def send_config(self, fountain_mac: str, cfg: FountainConfig) -> None:
        """Push full device config (cmd 221, 12 bytes).

        Use `FountainConfig.parse(existing.raw[30:42])` from the most
        recent status dump as a starting point and modify fields before
        submitting.
        """
        f = self._require_fountain(fountain_mac)
        data = list(cfg.to_bytes())
        frame = build_frame(
            seq=self._sequencer.next(),
            cmd=CMD_SET_CONFIG,
            type_=1,
            data=data,
        )
        self.queue_command(BleControlCommand(
            fountain_id=f.id,
            fountain_mac=f.mac,
            cmd_code=CMD_SET_CONFIG,
            frame_bytes=frame,
            device_type=DEVICE_TYPE_CTW3,
        ))

    def send_config_update(self, fountain_mac: str, **changes: int) -> None:
        """Convenience: build the new cmd 221 payload from current `f.settings`
        plus the supplied keyword overrides, push it, and mirror the change
        optimistically into `f.settings`.

        Accepted kwargs match FountainConfig field names:
          smart_working_time, smart_sleep_time,
          battery_working_time, battery_sleep_time,
          lamp_ring_switch, lamp_ring_brightness,
          no_disturbing_switch, distribution_diagram,
          smart_inductive_switch, battery_inductive_switch.

        Raises RuntimeError if the fountain's settings haven't been refreshed
        from a real cmd 230 status dump yet — pushing a cmd 221 before that
        would silently overwrite the fountain's real config with HA defaults
        (observed 2026-04-22: caused Main-Unit-Failure blinking).
        """
        f = self._require_fountain(fountain_mac)
        if not self.is_settings_synced(fountain_mac):
            raise RuntimeError(
                f"Fountain {fountain_mac} settings not yet synced from device. "
                "Wait for first cmd 230 status dump (triggered by any BLE "
                "session — e.g. toggle the Power switch once) before changing "
                "config fields."
            )
        s = f.settings
        # Build current snapshot.
        cfg = FountainConfig(
            smart_working_time=s.smart_working_time,
            smart_sleep_time=s.smart_sleep_time,
            battery_working_time=s.battery_working_time,
            battery_sleep_time=s.battery_sleep_time,
            lamp_ring_switch=s.lamp_ring_switch,
            lamp_ring_brightness=s.lamp_ring_brightness,
            no_disturbing_switch=s.no_disturbing_switch,
            distribution_diagram=s.distribution_diagram,
            smart_inductive_switch=s.smart_inductive_switch,
            battery_inductive_switch=s.battery_inductive_switch,
        )
        # Apply overrides.
        for field_name, value in changes.items():
            if not hasattr(cfg, field_name):
                raise ValueError(f"unknown config field: {field_name!r}")
            setattr(cfg, field_name, int(value))
        # Remember for verify-after-write: next incoming cmd 230 dump will
        # be compared against this expected config.
        self._pending_verification[fountain_mac] = FountainConfig(
            smart_working_time=cfg.smart_working_time,
            smart_sleep_time=cfg.smart_sleep_time,
            battery_working_time=cfg.battery_working_time,
            battery_sleep_time=cfg.battery_sleep_time,
            lamp_ring_switch=cfg.lamp_ring_switch,
            lamp_ring_brightness=cfg.lamp_ring_brightness,
            no_disturbing_switch=cfg.no_disturbing_switch,
            distribution_diagram=cfg.distribution_diagram,
            smart_inductive_switch=cfg.smart_inductive_switch,
            battery_inductive_switch=cfg.battery_inductive_switch,
        )
        # Push.
        self.send_config(fountain_mac, cfg)
        # Mirror optimistically.
        for field_name, value in changes.items():
            setattr(s, field_name, int(value))
        self._notify()

    def send_dnd_test_raw(
        self,
        fountain_mac: str,
        state: int,
        window1_start_min: int,
        window1_end_min: int,
        window2_start_min: int = 0x0468,  # 1128 = 18:48 — seen as "placeholder" in captures
        window2_end_min: int = 0x0558,    # 1368 = 22:48 — same
        window1_enabled: bool = True,
        window2_enabled: bool = False,
    ) -> None:
        """TEST / DEBUG: push cmd 226 DND time-window config to fountain.

        Payload layout (31 bytes, decoded from 6-frame capture 2026-04-24):
          0:     0x00
          1:     DND state flag (0=off, 1=on, 2=active-during-window)
          2-5:   0x00 0x00 0x00 0x00
          6-7:   window1 start time, u16 BE, minutes from midnight
          8-9:   window1 end time, u16 BE
          10:    window1 enabled (0xff = yes, 0x00 = no)
          11-12: window2 start
          13-14: window2 end
          15:    window2 enabled (0xff / 0x00)
          16-30: trailer — 15 bytes observed constant across all captures.
                 Exact meaning unknown (device-binding? firmware version?)
                 but constant across payload changes, so it's safe to copy.
        """
        f = self._require_fountain(fountain_mac)
        # Constant trailer observed in every captured cmd 226 push (2026-04-24).
        TRAILER = bytes.fromhex("c097592f0100000024008e4dbb65df")
        assert len(TRAILER) == 15
        payload = (
            bytes([0x00, state & 0xff, 0x00, 0x00, 0x00, 0x00])
            + window1_start_min.to_bytes(2, "big")
            + window1_end_min.to_bytes(2, "big")
            + bytes([0xff if window1_enabled else 0x00])
            + window2_start_min.to_bytes(2, "big")
            + window2_end_min.to_bytes(2, "big")
            + bytes([0xff if window2_enabled else 0x00])
            + TRAILER
        )
        assert len(payload) == 31, f"payload len {len(payload)} != 31"
        frame = build_frame(
            seq=self._sequencer.next(),
            cmd=226,
            type_=1,
            data=list(payload),
        )
        _LOGGER.warning(
            "cmd 226 SEND: state=%d w1=%d-%d(en=%d) w2=%d-%d(en=%d) frame=%s",
            state, window1_start_min, window1_end_min, window1_enabled,
            window2_start_min, window2_end_min, window2_enabled,
            frame.hex(),
        )
        self.queue_command(BleControlCommand(
            fountain_id=f.id,
            fountain_mac=f.mac,
            cmd_code=226,
            frame_bytes=frame,
            device_type=DEVICE_TYPE_CTW3,
        ))
        # Cache what we wrote so HA entities can reflect it. The fountain
        # does not echo DND times back in cmd 230 dumps, so this is the
        # only source of truth for UI display until the next write.
        f.dnd_window1_start_min = window1_start_min
        f.dnd_window1_end_min = window1_end_min
        f.dnd_window1_enabled = bool(window1_enabled)
        self._notify()

    def send_dnd_config(
        self,
        fountain_mac: str,
        window1_start_min: int,
        window1_end_min: int,
        window1_enabled: bool = True,
        state: int = 1,
    ) -> None:
        """Write DND time window 1 to the fountain (cmd 226).

        Thin wrapper around `send_dnd_test_raw` for the common single-
        window case. Window 2 is not user-configurable yet — the
        placeholder values from the original cloud captures are reused.
        """
        self.send_dnd_test_raw(
            fountain_mac=fountain_mac,
            state=state,
            window1_start_min=window1_start_min,
            window1_end_min=window1_end_min,
            window1_enabled=window1_enabled,
        )

    def send_reset_filter(self, fountain_mac: str) -> None:
        """Push filter-reset command (cmd 222)."""
        f = self._require_fountain(fountain_mac)
        frame = build_frame(
            seq=self._sequencer.next(),
            cmd=CMD_RESET_FILTER,
            type_=1,
            data=[0],
        )
        self.queue_command(BleControlCommand(
            fountain_id=f.id,
            fountain_mac=f.mac,
            cmd_code=CMD_RESET_FILTER,
            frame_bytes=frame,
            device_type=DEVICE_TYPE_CTW3,
        ))

    def _require_fountain(self, mac: str) -> Ctw3State:
        f = self.get_fountain(mac)
        if not f:
            raise ValueError(f"fountain not registered: mac={mac!r}")
        return f

    # --- Heartbeat callback (plugs into PetkitLocalServer) -----------------

    def _note_connect_failure(self, mac: str, reason: str = "") -> None:
        """Record a failed connect + schedule exponentially-backed-off retry."""
        import time as _time
        self._connect_fail_count[mac] = self._connect_fail_count.get(mac, 0) + 1
        idx = min(
            self._connect_fail_count[mac] - 1,
            len(CONNECT_RETRY_BACKOFF_SEC) - 1,
        )
        self._next_retry_at[mac] = _time.time() + CONNECT_RETRY_BACKOFF_SEC[idx]

    def _check_stuck_connecting(self) -> None:
        """Detect CONNECTING states that never received event_type=52.

        Observed 2026-04-22: the D4 will silently never send event 52 if
        the fountain doesn't respond to the BLE connect (sleep / out-of-
        range). We time out after CONNECT_ATTEMPT_TIMEOUT_SEC seconds and
        bail the state back to IDLE with a backoff so the next iteration
        can try again.
        """
        import time as _time
        now = _time.time()
        for mac, started in list(self._connecting_since.items()):
            if self._link_state.get(mac) != LINK_CONNECTING:
                # State changed without clearing the timestamp — clean up.
                self._connecting_since.pop(mac, None)
                continue
            if now - started < CONNECT_ATTEMPT_TIMEOUT_SEC:
                continue
            _LOGGER.info(
                "Link %s: CONNECTING timed out after %.0fs — assuming fail "
                "(expected when fountain sleeps / out of range; backoff will retry)",
                mac, now - started,
            )
            self._connecting_since.pop(mac, None)
            self._link_state[mac] = LINK_IDLE
            self._note_connect_failure(mac, reason="timeout")
            # Give _drive_link a chance to try again once cooldown passes.

    def _check_session_liveness(self) -> None:
        """Detect silently-dropped BLE sessions.

        Observed 2026-04-23: the D4 can end up in LINK_CONNECTED in our
        bookkeeping but the actual BLE link is dead — no more cmd 230
        dumps arrive, and no event_type=52 action=0 disconnect is reported.
        Without intervention we'd stay "connected" forever.

        On every heartbeat we check how long since the last cmd 230 dump
        for each CONNECTED fountain. Past SESSION_LIVENESS_TIMEOUT_SEC,
        we consider the link dead and force-recycle through IDLE.
        """
        import time as _time
        now = _time.time()
        for mac in list(self._fountains.keys()):
            if self._link_state.get(mac) != LINK_CONNECTED:
                continue
            last = self._last_dump_at.get(mac, 0.0)
            silent_for = now - last
            if silent_for < SESSION_LIVENESS_TIMEOUT_SEC:
                continue
            _LOGGER.info(
                "Link %s: session silent for %.0fs (no cmd 230 dump) — "
                "force reconnect (normal recovery from silent BLE drop)",
                mac, silent_for,
            )
            # Force the state machine back to IDLE without queuing a
            # disconnect push (the D4 may already think it's disconnected
            # and a spurious disconnect would just get ignored).
            self._link_state[mac] = LINK_IDLE
            self._last_dump_at.pop(mac, None)
            # Mark sync-needed so _drive_link reliably triggers a reconnect
            # even without pending user commands.
            self._sync_requested.add(mac)

    def heartbeat_content_provider(self) -> Optional[dict]:
        """Invoked on every D4 heartbeat. Returns one result-entry or None.

        Pops from the push stream (connect/ble/disconnect entries that
        were prepared by `_drive_link`). Opportunistically checks for
        stuck CONNECTING states and re-drives idle fountains.
        """
        # Check for any CONNECTING attempts that silently timed out.
        self._check_stuck_connecting()
        # Check for CONNECTED sessions that have gone silent.
        self._check_session_liveness()
        # Re-drive any IDLE fountain that should be connected — the retry
        # backoff gate inside `_drive_link` handles the cooldown timing.
        for mac in list(self._fountains.keys()):
            if self._link_state.get(mac) == LINK_IDLE:
                self._drive_link(mac)
        if not self._push_queue:
            return None
        entry = self._push_queue.popleft()
        try:
            import json as _json
            t = _json.loads(entry.get("content", "{}")).get("type", "?")
        except Exception:
            t = "?"
        _LOGGER.info(
            "Injecting heartbeat entry (type=%s, remaining: %d)",
            t, len(self._push_queue),
        )
        return entry

    # --- Event-report handler (plugs into PetkitLocalServer) ---------------

    def handle_event_report(self, body: dict) -> None:
        """Process a /d4/dev_event_report body.

        Event types we care about:
          51: BLE connect/disconnect attempt started (informational)
          52: BLE connect/disconnect attempt result
              content.action: 1=connect, 0=disconnect
              content.result: 0=success, 3=failed (others possible)
          53: Fountain response (D4 received BLE data from fountain)
              content.payload: [{cmd, data}]
              content.device.mac / .type
        """
        import json as _json
        try:
            event_type = int(body.get("event_type", 0))
        except (TypeError, ValueError):
            return
        # Only 51/52/53 carry JSON content we understand. D4 sends many
        # other event types (own feed-events, rtc_c errors, etc.) whose
        # content is not valid JSON — silently skip those instead of
        # noisy warnings in the HA log.
        if event_type not in (51, 52, 53):
            return
        content_raw = body.get("content", "")
        try:
            content = _json.loads(content_raw) if isinstance(content_raw, str) else (content_raw or {})
        except (_json.JSONDecodeError, TypeError):
            _LOGGER.debug("event_report: invalid content for type=%d: %r", event_type, content_raw[:100] if isinstance(content_raw, str) else content_raw)
            return

        if event_type == 51:
            mac = str(content.get("device", {}).get("mac", "")).lower()
            action = content.get("action")
            _LOGGER.info("event 51 (start) mac=%s action=%s", mac, action)

        elif event_type == 52:
            mac = str(content.get("device", {}).get("mac", "")).lower()
            action = content.get("action")
            result = content.get("result")
            _LOGGER.info(
                "event 52 (result) mac=%s action=%s result=%s",
                mac, action, result,
            )
            self._on_connect_result(mac, action, result)

        elif event_type == 53:
            mac = str(content.get("device", {}).get("mac", "")).lower()
            payload = content.get("payload", [])
            if not isinstance(payload, list):
                return
            _LOGGER.info(
                "event 53 (response) mac=%s entries=%d",
                mac, len(payload),
            )
            for entry in payload:
                if not isinstance(entry, dict):
                    continue
                try:
                    self._on_fountain_response(
                        mac,
                        int(entry.get("cmd", 0)),
                        entry.get("data", ""),
                    )
                except Exception:
                    _LOGGER.exception("event 53 entry failed: %r", entry)

    def _on_connect_result(self, mac: str, action, result) -> None:
        """Transition link state based on D4's event_type=52 report."""
        f = self._fountains.get(mac)
        if not f:
            # Unknown fountain — ignore.
            return

        try:
            action_i = int(action)
            result_i = int(result)
        except (TypeError, ValueError):
            return

        old_state = self._link_state.get(mac, LINK_IDLE)

        if action_i == 1:
            # Connect attempt result
            self._connecting_since.pop(mac, None)  # no longer pending
            if result_i == 0:
                import time as _time
                self._link_state[mac] = LINK_CONNECTED
                self._connect_fail_count[mac] = 0  # reset on success
                self._next_retry_at.pop(mac, None)
                # Seed the liveness timer — a cmd 230 dump is expected soon.
                self._last_dump_at[mac] = _time.time()
                _LOGGER.info("Link %s: CONNECTING -> CONNECTED", mac)
                self._drive_link(mac)  # drain pending commands
            else:
                # Connect failed — drop pending commands, back to IDLE,
                # schedule a retry with backoff.
                dropped = len(self._pending_cmds.get(mac, deque()))
                self._pending_cmds.get(mac, deque()).clear()
                self._link_state[mac] = LINK_IDLE
                self._note_connect_failure(mac, reason=f"result={result_i}")
                # result=6 is "fountain not ready yet" (common right after
                # a disconnect). Log at info unless it's a different code or
                # we've failed multiple times in a row — then warn.
                fail_count = self._connect_fail_count.get(mac, 0)
                level = (
                    logging.WARNING
                    if (result_i != 6 or fail_count >= 3)
                    else logging.INFO
                )
                _LOGGER.log(
                    level,
                    "Link %s: connect FAILED (result=%d, attempt=%d), "
                    "dropped %d cmd(s); backoff=%ds",
                    mac, result_i, fail_count, dropped,
                    int(self._next_retry_at.get(mac, 0) - __import__("time").time()),
                )
        elif action_i == 0:
            # Disconnect completed (success or not, we go idle)
            self._link_state[mac] = LINK_IDLE
            _LOGGER.info(
                "Link %s: %s -> IDLE (disconnect result=%d)",
                mac, _link_name(old_state), result_i,
            )
            # Trigger a new connect if:
            # - there are pending cmds to deliver, OR
            # - a sync is outstanding, OR
            # - we're in persistent mode (link should always stay up)
            # `_drive_link` will no-op if already CONNECTING due to its
            # state check.
            if (
                self._pending_cmds.get(mac)
                or self._sync_needed(mac)
                or self._persistent_mode
            ):
                self._drive_link(mac)

    def reset_daily_counters_if_needed(self) -> list[str]:
        """Roll drink counters forward at local-date change.

        Returns the list of mac addresses that were actually reset (so
        callers can persist / trigger coordinator refreshes only when
        something changed).

        Called from two places:
          1. `_update_drink_tracking` on each cmd 230 dump — handles the
             case where the fountain is online across midnight.
          2. From a midnight time-change callback registered in
             `__init__.py` — handles the case where the fountain is
             offline across midnight, so counters still reset at 00:00
             even without any incoming data.
        """
        from datetime import datetime as _dt
        today = _dt.now().astimezone().strftime("%Y-%m-%d")
        reset_macs: list[str] = []
        for mac, f in self._fountains.items():
            if f.drinks_today_date == today:
                continue
            if f.drinks_today or f.total_drink_duration_today:
                _LOGGER.info(
                    "Resetting drink counts for %s (new day %s, yesterday: %d drinks, %ds)",
                    mac, today, f.drinks_today, f.total_drink_duration_today,
                )
            f.drinks_today = 0
            f.total_drink_duration_today = 0
            f.drinks_today_date = today
            reset_macs.append(mac)
        if reset_macs:
            self._notify()
        return reset_macs

    def _update_drink_tracking(self, mac: str, status: Cmd230Status) -> None:
        """Track drink events from cmd 230 byte-19 (motion flag) transitions.

        Semantics (confirmed live 2026-04-22):
          - byte 19 = 2: cat is at the fountain NOW
          - byte 19 = 0: no cat present
        We consider a "drink event" to start on a 0→2 transition and end
        on the next 2→0 transition. Duration = delta of counter_runtime
        between start and end (accurate to the second; not affected by
        heartbeat jitter). If the counter rolls over (65535 → 0) during
        a drink event, we add 65536 to correct.

        Daily counts (drinks_today, total duration) reset when the local
        date changes — delegated to `reset_daily_counters_if_needed` so
        the logic is shared with the midnight timer callback.
        """
        import time as _time
        from datetime import datetime as _dt

        f = self._fountains.get(mac)
        if f is None:
            return

        dstate = self._drink_state.setdefault(mac, {
            "last_b19": 0,
            "start_counter": None,
            "start_ts": None,
        })
        prev_b19 = dstate["last_b19"]
        cur_b19 = status.motion_raw
        now = _time.time()

        # Roll counters forward if we've crossed midnight since the last dump.
        self.reset_daily_counters_if_needed()

        if prev_b19 != 2 and cur_b19 == 2:
            # 0 (or other) → 2: drink started
            dstate["start_counter"] = status.counter_runtime
            dstate["start_ts"] = now
            _LOGGER.info("Drink started for %s", mac)
        elif prev_b19 == 2 and cur_b19 != 2:
            # 2 → 0: drink ended — compute duration
            start_counter = dstate["start_counter"]
            start_ts = dstate["start_ts"]
            dstate["start_counter"] = None
            dstate["start_ts"] = None
            if start_counter is None:
                # We missed the start (first dump was already mid-event).
                # Fall back to wall-clock delta if we at least have a timestamp.
                duration_sec = 0
            else:
                counter_delta = status.counter_runtime - start_counter
                if counter_delta < 0:
                    counter_delta += 65536  # 16-bit rollover
                duration_sec = counter_delta
            f.drinks_today += 1
            f.total_drink_duration_today += duration_sec
            f.last_drink_duration = duration_sec
            f.last_drink_at = _dt.now().astimezone().isoformat(timespec="seconds")
            _LOGGER.info(
                "Drink #%d ended for %s: duration=%ds, today: %d drinks / %ds total",
                f.drinks_today, mac, duration_sec,
                f.drinks_today, f.total_drink_duration_today,
            )

        dstate["last_b19"] = cur_b19

    def _on_fountain_response(self, mac: str, cmd: int, b64_data: str) -> None:
        """Apply a fountain BLE response (from event_type=53).

        cmd 230 (42 bytes): full status dump — mirrored into Ctw3State.
        cmd 220/221/222 (1 byte): command ACK (0x01 = OK). Logged only.
        cmd 215/216 (11-16 bytes): LED/DND settings — logged only for now.
        """
        import base64
        try:
            data = base64.b64decode(b64_data) if b64_data else b""
        except Exception:
            data = b""
        _LOGGER.info(
            "Fountain response: mac=%s cmd=%d data=%s",
            mac, cmd, data.hex() if data else "(empty)",
        )

        f = self._fountains.get(mac)
        if f is None:
            return

        if cmd == 230 and len(data) >= 42:
            try:
                status = Cmd230Status.parse(data)
            except Exception:
                _LOGGER.exception("cmd 230 parse failed: %s", data.hex())
                return
            # Refresh liveness timer — we just heard from the fountain.
            import time as _time
            self._last_dump_at[mac] = _time.time()
            # --- Drink-event tracking (must run BEFORE overwriting fields) ---
            self._update_drink_tracking(mac, status)
            # --- Mirror runtime state ---
            f.status.power_status = status.power_status
            f.status.suspend_status = status.suspend_status
            f.status.detect_status = 1 if status.motion_detected else 0
            f.mode = status.mode
            f.electricity.battery_percent = status.battery_percent
            f.filter_percent = status.filter_percent
            # --- Mirror config block (bytes 30-41) into f.settings so HA
            #     entities have a single source of truth. ---
            cfg = status.config
            f.settings.smart_working_time = cfg.smart_working_time
            f.settings.smart_sleep_time = cfg.smart_sleep_time
            f.settings.battery_working_time = cfg.battery_working_time
            f.settings.battery_sleep_time = cfg.battery_sleep_time
            f.settings.lamp_ring_switch = cfg.lamp_ring_switch
            f.settings.lamp_ring_brightness = cfg.lamp_ring_brightness
            f.settings.no_disturbing_switch = cfg.no_disturbing_switch
            f.settings.distribution_diagram = cfg.distribution_diagram
            f.settings.smart_inductive_switch = cfg.smart_inductive_switch
            f.settings.battery_inductive_switch = cfg.battery_inductive_switch
            # --- Non-config runtime extras (not yet part of Ctw3State, stored as attrs) ---
            f._ctw3_wifi_rssi = -status.wifi_rssi_magnitude  # type: ignore[attr-defined]
            f._ctw3_counter_runtime = status.counter_runtime  # type: ignore[attr-defined]
            f._ctw3_counter_uptime = status.counter_uptime  # type: ignore[attr-defined]
            # counter_runtime ticks 1 Hz only while the pump is actually
            # running (confirmed by comparing byte 11-12 delta against
            # suspend_status across Runde 2 capture). That's exactly what
            # "today_pump_run_time" is supposed to be.
            f.today_pump_run_time = status.counter_runtime
            # Mark as synced — now safe to allow config writes.
            newly_synced = mac not in self._settings_synced
            self._settings_synced.add(mac)
            self._sync_requested.discard(mac)
            if newly_synced:
                _LOGGER.info(
                    "Fountain %s settings now in sync with device — "
                    "config writes enabled.",
                    mac,
                )

            # Verify-after-write: compare against what we pushed.
            expected = self._pending_verification.pop(mac, None)
            if expected is not None:
                actual = status.config
                if expected == actual:
                    _LOGGER.info(
                        "Config verify OK for %s: all 12 bytes match pushed cmd 221.",
                        mac,
                    )
                else:
                    # Diff out the mismatched fields for a useful message.
                    from dataclasses import fields as _fields
                    bad = [
                        fld.name for fld in _fields(FountainConfig)
                        if getattr(expected, fld.name) != getattr(actual, fld.name)
                    ]
                    _LOGGER.warning(
                        "Config verify MISMATCH for %s: fields=%s expected=%s actual=%s",
                        mac, bad, expected, actual,
                    )

            # In session-mode, the dump was what we were waiting for —
            # close the link now. In persistent_mode we stay connected so
            # motion events can be observed live.
            if (
                self._link_state.get(mac) == LINK_CONNECTED
                and not self._persistent_mode
            ):
                self._push_queue.append(
                    wrap_connect(mac, DEVICE_TYPE_CTW3, action=0)
                )
                self._link_state[mac] = LINK_DISCONNECTING
                _LOGGER.info(
                    "Link %s: sync dump received, transitioning to DISCONNECTING",
                    mac,
                )
            _LOGGER.info(
                "Status dump applied: mac=%s power=%d suspend=%d mode=%d "
                "battery=%d%% filter=%d%% rssi=%ddBm cfg=%s",
                mac, status.power_status, status.suspend_status, status.mode,
                status.battery_percent, status.filter_percent,
                -status.wifi_rssi_magnitude, cfg,
            )
            self._notify()

    # --- Cloud-endpoint emulation (registered with PetkitLocalServer) ------

    def handle_ctw3_post(
        self, suffix: str, body: dict, raw_body: bytes
    ) -> dict | None:
        """Respond to all /ctw3/<suffix> POST requests.

        Called by local_server when a D4 or app hits our cloud-emulated
        CTW3 endpoints. We dispatch on suffix; unknown suffixes yield
        None (local_server then returns generic `success`).
        """
        if suffix.startswith("update"):
            return self._handle_update(body)
        if suffix.startswith("deviceData"):
            return self._handle_device_data(body)
        if suffix.startswith("refreshHomeV2"):
            return self._handle_device_data(body)
        if suffix.startswith("link"):
            return self._handle_link(body)
        if suffix.startswith("signup"):
            return self._handle_signup(body)
        if suffix in ("addWaterRecord", "getWorkRecord", "getDrinkCompare",
                      "energyCalculation", "getDistributionDiagram",
                      "upgradeCheck", "saveLog"):
            # Stubs — return empty data to keep the app/D4 happy
            return {}
        _LOGGER.debug("CTW3 handler: unhandled suffix %r", suffix)
        return None

    def _fountain_by_id(self, device_id: int) -> Ctw3State | None:
        for f in self._fountains.values():
            if f.id == device_id:
                return f
        return None

    def _fountain_by_mac(self, mac: str) -> Ctw3State | None:
        return self._fountains.get(mac.lower().replace(":", ""))

    def _handle_signup(self, body: dict) -> dict | None:
        mac = str(body.get("mac", "")).lower()
        f = self._fountain_by_mac(mac)
        if not f:
            _LOGGER.warning("signup for unregistered fountain mac=%s", mac)
            return None
        return f.to_cloud_detail()

    def _handle_link(self, body: dict) -> dict | None:
        try:
            device_id = int(body.get("id", 0))
        except (TypeError, ValueError):
            return None
        f = self._fountain_by_id(device_id)
        if not f:
            return None
        return f.to_cloud_detail()

    def _handle_device_data(self, body: dict) -> dict | None:
        try:
            device_id = int(body.get("id", 0))
        except (TypeError, ValueError):
            return None
        f = self._fountain_by_id(device_id)
        if not f:
            return None
        return f.to_cloud_detail()

    def _handle_update(self, body: dict) -> dict | None:
        """Apply a `kv`-encoded state update (from D4-relay or app)."""
        import json as _json
        kv_raw = body.get("kv", "")
        if not kv_raw:
            return {"result": "success"}
        try:
            kv = _json.loads(kv_raw) if isinstance(kv_raw, str) else kv_raw
        except (_json.JSONDecodeError, TypeError):
            _LOGGER.warning("ctw3/update: invalid kv=%r", kv_raw[:100])
            return None

        device_id = int(body.get("id", kv.get("id", 0)) or 0)
        f = self._fountain_by_id(device_id)
        if not f:
            _LOGGER.warning("ctw3/update: unknown device id=%s", device_id)
            return None

        self.on_fountain_state_report(f.mac, kv)
        return "success"

    # --- State updates (from D4 callback endpoint) -------------------------

    def on_fountain_state_report(self, mac: str, flat_kv: dict) -> None:
        """Apply a flat /ctw3/update-style payload as a state update."""
        f = self.get_fountain(mac)
        if not f:
            _LOGGER.warning("state report for unknown fountain: %s", mac)
            return
        # Apply known fields (best-effort)
        if "mode" in flat_kv: f.mode = int(flat_kv["mode"])
        if "powerStatus" in flat_kv: f.status.power_status = int(flat_kv["powerStatus"])
        if "suspendStatus" in flat_kv: f.status.suspend_status = int(flat_kv["suspendStatus"])
        if "runStatus" in flat_kv: f.status.run_status = int(flat_kv["runStatus"])
        if "detectStatus" in flat_kv: f.status.detect_status = int(flat_kv["detectStatus"])
        if "electricStatus" in flat_kv: f.status.electric_status = int(flat_kv["electricStatus"])
        if "batteryPercent" in flat_kv: f.electricity.battery_percent = int(flat_kv["batteryPercent"])
        if "batteryVoltage" in flat_kv: f.electricity.battery_voltage = int(flat_kv["batteryVoltage"])
        if "supplyVoltage" in flat_kv: f.electricity.supply_voltage = int(flat_kv["supplyVoltage"])
        if "filterPercent" in flat_kv: f.filter_percent = int(flat_kv["filterPercent"])
        if "todayPumpRunTime" in flat_kv: f.today_pump_run_time = int(flat_kv["todayPumpRunTime"])
        if "filterWarning" in flat_kv: f.filter_warning = int(flat_kv["filterWarning"])
        if "lackWarning" in flat_kv: f.lack_warning = int(flat_kv["lackWarning"])
        if "lowBattery" in flat_kv: f.low_battery = int(flat_kv["lowBattery"])
        if "breakdownWarning" in flat_kv: f.breakdown_warning = int(flat_kv["breakdownWarning"])
        _LOGGER.debug("Updated fountain state for %s", mac)
        self._notify()
