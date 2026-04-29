"""Petkit cloud HTTP helper for one-shot CTW3 onboarding.

Used by the config flow to:

  1. Authenticate the user (email+password OR an existing X-Session token)
  2. Fetch the cloud-assigned `secret` for a fountain (via /ctw3/signup)
  3. Periodically refresh the session token (so it never expires)

Once a fountain is added, ZERO cloud calls happen for normal operation —
this module is only used during onboarding and the daily refresh tick.

Endpoint format and field layout were verified end-to-end against the
real Petkit-EU cloud on 2026-04-28 with a throwaway account; see
docs/CTW3_ONBOARDING.md (or the captures in docs/path_b_evidence/) for
the reference traffic.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
from typing import Any

try:
    import aiohttp
except ImportError:  # pragma: no cover — only relevant for unit tests
    aiohttp = None  # type: ignore[assignment]


_LOGGER = logging.getLogger(__name__)

# The iOS Petkit app uses /latest/. The D4 firmware uses /6/. They are the
# same backend and (for our endpoints) accept identical request shapes,
# but the iOS-style /latest/ is the better-tested path for cloud calls.
BASE_URL_EU = "https://api.eu-pet.com/latest"

# Headers — matched verbatim against captured iOS traffic. Some are
# certainly ignored by the server (User-Agent, X-Img-Version), but
# matching the app's outgoing shape avoids triggering any server-side
# heuristics that flag unfamiliar clients.
_DEFAULT_HEADERS = {
    "User-Agent": "PETKIT/04004110001 CFNetwork/3826.600.41 Darwin/24.6.0",
    "X-Img-Version": "1",
    "X-Client": "ios(18.6.2;iPhone14,4)",
    "X-Timezone": "2.0",
    "X-Api-Version": "13.6.0",
    "X-Hour": "24",
    "X-TimezoneId": "Europe/Berlin",
    "X-Locale": "de_DE",
    "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
    "Accept": "application/json",
}

# `client` JSON field embedded in login/refresh bodies — content matches
# what an iPhone 14 / iOS 18.6.2 sends. The `token` slot is the iOS push-
# notification token in the app's real flow; we pass a placeholder hex
# string. Petkit does not appear to validate it.
_CLIENT_INFO = {
    "locale": "de-DE",
    "source": "app.petkit-ios-oversea",
    "platform": "ios",
    "osVersion": "18.6.2",
    "timezone": "2.0",
    "timezoneId": "Europe/Berlin",
    "version": "13.6.0",
    "token": "0000000000000000000000000000000000000000000000000000000000000000",
    "name": "iPhone14,4",
}
_CLIENT_INFO_JSON = json.dumps(_CLIENT_INFO, separators=(",", ":"))

DEFAULT_TIMEOUT_SEC = 15


class PetkitCloudError(Exception):
    """Generic Petkit cloud error (network, HTTP, server-side error frame)."""


class PetkitAuthError(PetkitCloudError):
    """Session token is invalid / expired, or login credentials wrong."""


def _hash_password(password: str) -> str:
    """Petkit accepts MD5(password) as plain hex (lowercase)."""
    return hashlib.md5(password.encode("utf-8")).hexdigest()


def _encode_username(email: str) -> str:
    """Petkit expects the username field as base64(email)."""
    return base64.b64encode(email.encode("utf-8")).decode("ascii")


def _check_response_envelope(payload: dict[str, Any], context: str) -> dict[str, Any]:
    """Petkit always returns one of:

        {"result": <data>}     — success
        {"error":  {"code":N,"msg":"..."}}  — failure

    Raise the right exception on error frames; return the result dict on success.
    Auth-style errors (1002/1010/etc — anything mentioning session/login) become
    PetkitAuthError so the caller can prompt for re-auth.
    """
    if "error" in payload:
        err = payload.get("error") or {}
        msg = str(err.get("msg") or err)
        code = err.get("code")
        if code in (1, 5, 1002, 1010, 1011, 1012, 1013) or "session" in msg.lower() or "login" in msg.lower():
            raise PetkitAuthError(f"{context}: {msg} (code={code})")
        raise PetkitCloudError(f"{context}: {msg} (code={code})")
    if "result" not in payload:
        raise PetkitCloudError(f"{context}: malformed response (no 'result'): {str(payload)[:200]}")
    return payload["result"] if isinstance(payload["result"], dict) else {"value": payload["result"]}


async def _post(
    session: "aiohttp.ClientSession",
    path: str,
    *,
    body: dict[str, str] | None = None,
    token: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SEC,
) -> dict[str, Any]:
    """Low-level POST with the standard headers + optional X-Session.

    Returns the parsed JSON. Raises PetkitCloudError on non-200 / non-JSON.
    """
    if aiohttp is None:
        raise RuntimeError("aiohttp not available")

    headers = dict(_DEFAULT_HEADERS)
    if token:
        headers["X-Session"] = token

    url = f"{BASE_URL_EU}{path}"
    try:
        async with session.post(
            url,
            data=body or {},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise PetkitCloudError(
                    f"POST {path} returned HTTP {resp.status}: {text[:200]}"
                )
            try:
                return json.loads(text)
            except json.JSONDecodeError as e:
                raise PetkitCloudError(f"POST {path} non-JSON response: {text[:200]}") from e
    except asyncio.TimeoutError as e:
        raise PetkitCloudError(f"POST {path} timed out after {timeout}s") from e
    except aiohttp.ClientError as e:
        raise PetkitCloudError(f"POST {path} network error: {e}") from e


async def login(
    session: "aiohttp.ClientSession",
    email: str,
    password: str,
    region: str = "DE",
) -> dict[str, Any]:
    """Authenticate with email + password.

    Returns the full response dict (under "result"), which includes:
      - session: {id, userId, expiresIn, region, createdAt}
      - apiServers: [...]
      - user: {...}

    Raises PetkitAuthError on bad credentials, PetkitCloudError on
    network / server-side failure.
    """
    body = {
        "client": _CLIENT_INFO_JSON,
        "encrypt": "1",
        "oldVersion": "13.6.0",
        "password": _hash_password(password),
        "region": region,
        "username": _encode_username(email),
    }
    payload = await _post(session, "/user/login", body=body)
    return _check_response_envelope(payload, "login")


async def refresh_session(
    session: "aiohttp.ClientSession",
    token: str,
) -> dict[str, Any]:
    """Extend a session token's server-side expiry.

    Returns the refreshed session dict (same `id` is normal — Petkit
    doesn't rotate the token, just extends its TTL).

    Raises PetkitAuthError if the token is no longer valid.
    """
    body = {
        "client": _CLIENT_INFO_JSON,
        "domain": "1",
        "modules": "cs",
        "oldVersion": "13.6.0",
    }
    payload = await _post(session, "/user/refreshsession", body=body, token=token)
    return _check_response_envelope(payload, "refresh_session")


async def validate_session(
    session: "aiohttp.ClientSession",
    token: str,
) -> bool:
    """Cheap token-validity probe. Returns True iff /user/details2 returns 200
    AND the response envelope contains no error frame."""
    try:
        payload = await _post(session, "/user/details2", body={}, token=token)
        _check_response_envelope(payload, "validate_session")
        return True
    except PetkitAuthError:
        return False
    except PetkitCloudError:
        # Network errors etc. — be conservative and report invalid so caller
        # falls back to re-auth rather than papering over an outage.
        return False


async def fetch_fountain(
    session: "aiohttp.ClientSession",
    token: str,
    mac: str,
    sn: str,
) -> dict[str, Any]:
    """Fetch the cloud-assigned device data for one CTW3 by MAC + SN.

    Petkit's /ctw3/signup with `mac=<hex>&sn=<sn>` (without the `id`
    field) returns the existing record for that fountain — including
    the all-important `secret` — provided the calling session is the
    fountain's owner. Empirically a 7-day-old session still works.

    Returns a dict with at least {id, mac, secret, sn, name, firmware,
    typeCode, ...}.

    Raises PetkitAuthError on session problems, PetkitCloudError if the
    fountain isn't bound to the user's account or doesn't exist.
    """
    if not mac or not sn:
        raise ValueError("mac and sn are required")
    mac_clean = mac.replace(":", "").replace("-", "").lower().strip()
    if len(mac_clean) != 12 or any(c not in "0123456789abcdef" for c in mac_clean):
        raise ValueError(f"invalid mac: {mac!r}")
    body = {"mac": mac_clean, "sn": sn.strip()}
    payload = await _post(session, "/ctw3/signup", body=body, token=token)
    return _check_response_envelope(payload, "fetch_fountain")
