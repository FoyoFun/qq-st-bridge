"""
Per-group concurrency control with acquisition timeout.

Prevents race conditions when two messages arrive simultaneously for the
same group: without this, both handlers read the same chat history, both
generate responses, and the last to save overwrites the first — losing
an entire interaction.
"""

import asyncio
import logging

# ---------------------------------------------------------------------------
# Per-group locks
# ---------------------------------------------------------------------------

_group_locks: dict[int, asyncio.Lock] = {}
"""One asyncio.Lock per active group, lazily created."""

DEFAULT_LOCK_TIMEOUT: float = 30.0
"""Seconds to wait for a per-group lock before rejecting the request."""


def _get_lock(group_id: int) -> asyncio.Lock:
    """Get or create the per-group lock."""
    if group_id not in _group_locks:
        _group_locks[group_id] = asyncio.Lock()
    return _group_locks[group_id]


async def acquire_group_lock(
    group_id: int,
    timeout: float = DEFAULT_LOCK_TIMEOUT,
) -> bool:
    """Try to acquire the per-group lock.

    Returns True on success, False if the timeout expires.
    """
    lock = _get_lock(group_id)
    try:
        acquired = await asyncio.wait_for(lock.acquire(), timeout=timeout)
        return acquired
    except asyncio.TimeoutError:
        logging.warning(
            f"Group {group_id}: lock acquisition timed out after {timeout}s"
        )
        return False


def release_group_lock(group_id: int) -> None:
    """Release the per-group lock.

    Safe to call even if the lock was not acquired (RuntimeError suppressed).
    """
    lock = _group_locks.get(group_id)
    if lock is None:
        return
    try:
        lock.release()
    except RuntimeError:
        # Lock was not held — nothing to release
        pass
