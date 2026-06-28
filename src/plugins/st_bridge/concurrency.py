"""
Global ST operation lock — ensures only one SillyTavern request at a time.

All groups share a single lock. This prevents:
- Multiple concurrent AI generation calls hitting ST (and its backend)
- ST internal state conflicts from overlapping chat read/write cycles
- DeepSeek API rate-limit issues

Fast local-only operations (like /status, /help) do NOT acquire this lock.
"""

import asyncio
import logging

# ---------------------------------------------------------------------------
# Global ST operation lock (shared across all groups)
# ---------------------------------------------------------------------------

_st_lock = asyncio.Lock()
"""Single global lock — only one ST operation in flight at a time."""

DEFAULT_ST_LOCK_TIMEOUT: float = 120.0
"""Seconds to wait for the ST lock before rejecting.

Matches ST_TIMEOUT since the longest operation (AI generation) can take
up to that long. A waiting caller would time out alongside the in-flight
request, or a bit sooner if there's queue buildup.
"""


async def acquire_st_lock(timeout: float = DEFAULT_ST_LOCK_TIMEOUT) -> bool:
    """Try to acquire the global ST operation lock.

    Returns True on success, False if the timeout expires.
    """
    try:
        acquired = await asyncio.wait_for(_st_lock.acquire(), timeout=timeout)
        return acquired
    except asyncio.TimeoutError:
        logging.warning(
            f"ST lock: acquisition timed out after {timeout}s "
            f"(ST may be overloaded or unresponsive)"
        )
        return False


def release_st_lock() -> None:
    """Release the global ST operation lock.

    Safe to call even if the lock was not held (RuntimeError suppressed).
    """
    try:
        _st_lock.release()
    except RuntimeError:
        # Lock was not held — nothing to release
        pass
