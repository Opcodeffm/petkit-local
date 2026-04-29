"""Persistence layer for paired CTW3 fountains.

Uses Home Assistant's `Store` helper. Data shape:

    {
      "fountains": {
        "aabbcc112233": {
          "id": 400000001,
          "mac": "aabbcc112233",
          "secret": "cafebabe1234",
          "sn": "EXAMPLEFAKESN001",
          "name": "Trinkbrunnen",
          "firmware": 111,
          "last_state_kv": { ...flat state fields from /ctw3/update... }
        },
        ...
      },
      "cloud": {
        "token": "deadbeef...ybub",       # Petkit X-Session token
        "user_id": "999999998",
        "region": "DE",
        "created_at": "2026-04-28T06:24:25Z",
        "last_refresh_at": "2026-04-28T08:24:25Z"
      }
    }

The `cloud` block is shared across all fountains in this integration
(one Petkit account, possibly many fountains). It is only used during
fountain onboarding (call /ctw3/signup once to harvest the secret) and
for the daily refresh tick that keeps the token alive — there are no
cloud calls during normal fountain operation.

The `cloud` block is optional: legacy installations that only have
manually-entered (id, mac, secret) tuples will simply have no `cloud`
key, and the integration falls back to its previous local-only behavior.
"""
from __future__ import annotations

import logging
from typing import Any

try:
    from .ctw3_state import Ctw3State
except ImportError:
    from ctw3_state import Ctw3State


_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1
STORE_KEY = "petkit_feeder_fountains"


def fountain_from_stored(data: dict[str, Any]) -> Ctw3State:
    """Reconstruct a Ctw3State from a stored dict (best-effort)."""
    # The stored dict uses flat cloud-detail style, so we can rehydrate
    # via Ctw3State.from_cloud_dict.
    return Ctw3State.from_cloud_dict(data)


def fountain_to_stored(state: Ctw3State) -> dict[str, Any]:
    """Serialize a Ctw3State for persistence."""
    return state.to_cloud_detail()


# ---------------------------------------------------------------------------
# Cloud-credentials sub-block helpers
#
# These operate on the top-level dict that lives under STORE_KEY. They never
# touch the `fountains` block, so they are safe to call alongside the
# fountain-server's own listener that maintains it.
# ---------------------------------------------------------------------------

CLOUD_KEY = "cloud"


def cloud_from_stored(data: dict[str, Any] | None) -> dict[str, Any] | None:
    """Extract the cloud sub-block from raw store data, or None if absent.

    Validates that required fields exist; returns None on any malformed
    state (treat-as-missing so caller falls back to interactive auth).
    """
    if not data:
        return None
    cloud = data.get(CLOUD_KEY)
    if not isinstance(cloud, dict):
        return None
    if not cloud.get("token"):
        return None
    return {
        "token": str(cloud.get("token")),
        "user_id": str(cloud.get("user_id") or ""),
        "region": str(cloud.get("region") or "DE"),
        "created_at": str(cloud.get("created_at") or ""),
        "last_refresh_at": str(cloud.get("last_refresh_at") or ""),
    }


def cloud_to_stored(
    *,
    token: str,
    user_id: str = "",
    region: str = "DE",
    created_at: str = "",
    last_refresh_at: str = "",
) -> dict[str, Any]:
    """Build a `cloud` sub-block ready to be merged into the store dict."""
    return {
        "token": token,
        "user_id": user_id,
        "region": region,
        "created_at": created_at,
        "last_refresh_at": last_refresh_at,
    }


def merge_cloud_into_store_data(
    existing: dict[str, Any] | None, cloud: dict[str, Any]
) -> dict[str, Any]:
    """Return a copy of `existing` with the cloud block updated.

    Use this in async_save callsites to avoid clobbering the fountains map.
    """
    out: dict[str, Any] = dict(existing or {})
    out.setdefault("fountains", {})
    out[CLOUD_KEY] = dict(cloud)
    return out


def clear_cloud_from_store_data(
    existing: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return a copy of `existing` with the cloud block removed."""
    out: dict[str, Any] = dict(existing or {})
    out.setdefault("fountains", {})
    out.pop(CLOUD_KEY, None)
    return out
