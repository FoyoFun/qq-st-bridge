"""
Low-level HTTP transport to SillyTavern.

Manages the httpx.AsyncClient singleton, CSRF token lifecycle, and
retry-on-failure logic. Does NOT know about ST API semantics — it just
provides authenticated POST primitives for st_api.py to build on.

Key refactoring: _auth_post() is the single CSRF-lock-guarded primitive
shared by both simple API calls (via post_with_retry) and the plugin
generate flow (via direct call with its own retry loop in st_api.py).
This eliminates ~20 lines of duplicated CSRF+retry code.
"""

import asyncio
import logging
from typing import Optional

import httpx

from . import config

# ---------------------------------------------------------------------------
# HTTP Client singleton
# ---------------------------------------------------------------------------

_client: Optional[httpx.AsyncClient] = None


async def get_client() -> httpx.AsyncClient:
    """Get or create the shared httpx.AsyncClient (lazy-init)."""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(config.ST_TIMEOUT),
            follow_redirects=True,
        )
    return _client


async def close_client() -> None:
    """Close and discard the shared HTTP client."""
    global _client
    if _client:
        await _client.aclose()
        _client = None


async def reset_client() -> None:
    """Close and recreate the HTTP client (clears stale cookies/sessions)."""
    logging.info("ST Bridge: resetting HTTP client (stale session cleared)")
    await close_client()
    # get_client() will lazily create a new client on next use


# ---------------------------------------------------------------------------
# CSRF token management
# ---------------------------------------------------------------------------

# Lock to serialize CSRF token fetches across concurrent handlers.
# Without it, two concurrent coroutines may get the same CSRF token;
# the first POST consumes it, causing the second to fail with 403.
_csrf_lock = asyncio.Lock()


async def _fetch_csrf_token(client: httpx.AsyncClient) -> str:
    """Fetch a fresh CSRF token from SillyTavern."""
    resp = await client.get(f"{config.get_base_url()}/csrf-token")
    resp.raise_for_status()
    return str(resp.json()["token"])


# ---------------------------------------------------------------------------
# Authenticated POST primitives
# ---------------------------------------------------------------------------

async def auth_post(path: str, body: dict) -> httpx.Response:
    """CSRF-lock-guarded POST to SillyTavern. Returns raw httpx.Response.

    Acquires the global CSRF lock, fetches a fresh token, and dispatches
    a POST with the X-CSRF-Token header. The caller is responsible for
    reading/parsing the response body and handling errors.

    The lock is held only for the token fetch + POST dispatch; response
    body parsing happens outside the lock so other handlers can proceed.
    """
    async with _csrf_lock:
        client = await get_client()
        token = await _fetch_csrf_token(client)
        resp = await client.post(
            f"{config.get_base_url()}{path}",
            json=body,
            headers={"X-CSRF-Token": token},
        )
    # Response body is parsed outside the lock
    return resp


async def post_with_retry(path: str, body: dict) -> dict:
    """Authenticated POST with auto-reconnect retry for simple API calls.

    Retries once on CSRF rejection (403) or connection errors by
    resetting the HTTP client (clearing stale cookies/session).
    Returns parsed JSON. Raises RuntimeError if both attempts fail.
    """
    for attempt in range(2):
        try:
            resp = await auth_post(path, body)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 403 and attempt == 0:
                logging.warning(
                    f"ST Bridge: CSRF rejected (403) on {path}, resetting client..."
                )
                await reset_client()
                await asyncio.sleep(1)
                continue
            raise
        except (httpx.ConnectError, httpx.RemoteProtocolError) as e:
            if attempt == 0:
                logging.warning(
                    f"ST Bridge: connection lost on {path} ({e}), reconnecting..."
                )
                await reset_client()
                await asyncio.sleep(1)
                continue
            raise

    # Both attempts failed
    raise RuntimeError(f"ST Bridge: failed to reach ST after retry ({path})")
