"""Helpers for the MyBMW Device Code OAuth 2.0 flow."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional

import aiohttp

from .const import DEVICE_CODE_URL, TOKEN_URL

# aiohttp's default total timeout is 5 minutes, which is far too long to block
# a token refresh on. Every call here gets an explicit, short budget.
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=30)

# Bounds for the server-supplied device-flow poll interval. Without these a
# malformed or hostile `interval` either spins a tight HTTP loop against the
# token endpoint or parks the config flow on an uninterruptible sleep.
_MIN_POLL_INTERVAL = 1
_MAX_POLL_INTERVAL = 60


class CardataAuthError(Exception):
    """Raised when the BMW OAuth service rejects a request."""


def _safe_error(data: Any) -> str:
    """Summarise an OAuth error body without echoing the whole response.

    These strings reach both the config-flow UI and home-assistant.log at ERROR
    level, and logs ship inside every HA backup. Interpolating an entire
    third-party response body risks carrying back whatever the IdP chose to
    echo - including, on some providers, the rejected token itself.
    """

    if isinstance(data, dict):
        parts = [str(data.get(k))[:200] for k in ("error", "error_description") if data.get(k)]
        return " - ".join(parts) if parts else "no error detail"
    return str(data)[:200]


async def request_device_code(
    session: aiohttp.ClientSession,
    *,
    client_id: str,
    scope: str,
    code_challenge: str,
    code_challenge_method: str = "S256",
) -> Dict[str, Any]:
    """Request a device & user code pair from BMW."""

    data = {
        "client_id": client_id,
        "scope": scope,
        "response_type": "device_code",
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
    }
    async with session.post(DEVICE_CODE_URL, data=data, timeout=_HTTP_TIMEOUT) as resp:
        if resp.status != 200:
            text = await resp.text()
            raise CardataAuthError(f"Device code request failed ({resp.status}): {text}")
        return await resp.json()


async def poll_for_tokens(
    session: aiohttp.ClientSession,
    *,
    client_id: str,
    device_code: str,
    code_verifier: str,
    interval: int,
    timeout: int = 900,
    token_url: str = TOKEN_URL,
) -> Dict[str, Any]:
    """Poll the token endpoint until tokens are issued or timeout elapsed."""

    start = time.monotonic()
    payload = {
        "client_id": client_id,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": device_code,
        "code_verifier": code_verifier,
    }

    # Clamp whatever the server asked for into something sane.
    current_interval = min(max(int(interval or _MIN_POLL_INTERVAL), _MIN_POLL_INTERVAL), _MAX_POLL_INTERVAL)

    while True:
        if time.monotonic() - start > timeout:
            raise CardataAuthError("Timed out waiting for device authorization")

        async with session.post(token_url, data=payload, timeout=_HTTP_TIMEOUT) as resp:
            data = await resp.json(content_type=None)
            if resp.status == 200:
                return data

            error = data.get("error") if isinstance(data, dict) else None
            if error in {"authorization_pending", "slow_down"}:
                if error == "slow_down":
                    # RFC 8628 s3.5: the increase is cumulative and persists for
                    # the rest of the polling loop. Reverting to the original
                    # rate on the next iteration is what gets a client blocked.
                    current_interval = min(current_interval + 5, _MAX_POLL_INTERVAL)
                # Never sleep past the overall deadline.
                remaining = timeout - (time.monotonic() - start)
                if remaining <= 0:
                    raise CardataAuthError("Timed out waiting for device authorization")
                await asyncio.sleep(min(current_interval, remaining))
                continue

            raise CardataAuthError(f"Token polling failed ({resp.status}): {_safe_error(data)}")


async def refresh_tokens(
    session: aiohttp.ClientSession,
    *,
    client_id: str,
    refresh_token: str,
    scope: Optional[str] = None,
    token_url: str = TOKEN_URL,
) -> Dict[str, Any]:
    """Refresh access/ID tokens using the stored refresh token."""

    payload = {
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    if scope:
        payload["scope"] = scope

    async with session.post(token_url, data=payload, timeout=_HTTP_TIMEOUT) as resp:
        data = await resp.json(content_type=None)
        if resp.status != 200:
            raise CardataAuthError(f"Token refresh failed ({resp.status}): {_safe_error(data)}")
        return data
