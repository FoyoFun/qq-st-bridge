"""
Auto-participation: the bot spontaneously joins group discussions.

Message buffer → trigger evaluation (frequency + cooldown + probability)
→ summary built from recent messages → sent to ST as user "群聊" →
character responds → reply posted to QQ group.
"""

import asyncio
import collections
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx
from nonebot import get_bot
from nonebot.adapters.onebot.v11 import Message

from . import chat_utils
from . import config
from . import st_api
from . import state

# ---------------------------------------------------------------------------
# Per-group runtime state (in-memory only, not persisted)
# ---------------------------------------------------------------------------


@dataclass
class _GroupAutoState:
    """Ephemeral runtime state for auto-participation in a group."""

    # Ring buffer of recent messages: (user_id, user_name, text, timestamp)
    buffer: collections.deque = field(
        default_factory=lambda: collections.deque(maxlen=20)
    )
    last_trigger_time: float = 0.0  # monotonic timestamp of last auto-reply


_auto_states: dict[int, _GroupAutoState] = {}


def _get_auto_state(group_id: int) -> _GroupAutoState:
    """Get or create the ephemeral auto-participation state for a group."""
    if group_id not in _auto_states:
        _auto_states[group_id] = _GroupAutoState()
    return _auto_states[group_id]


# ---------------------------------------------------------------------------
# Feed a message into the buffer and evaluate trigger
# ---------------------------------------------------------------------------


def feed_message(
    group_id: int, user_id: int, user_name: str, text: str
) -> None:
    """Buffer a group message and check if auto-participation should trigger.

    Called for every non-@mention group message. If all trigger conditions
    are met, schedules the auto-chat flow as a background task.
    """
    gs = state.get_group_state(group_id)
    if not gs.auto_enabled:
        return

    if not gs.character_name or not gs.preset_name:
        # Auto-participate only works when a character is selected
        return

    auto = _get_auto_state(group_id)
    now = time.monotonic()
    auto.buffer.append((user_id, user_name, text, now))

    # Remove expired entries (outside the time window)
    window = gs.auto_msg_window
    while auto.buffer and (now - auto.buffer[0][3]) > window:
        auto.buffer.popleft()

    # --- Trigger conditions ---
    # ① Frequency: at least N distinct users in the window
    distinct_users = {entry[0] for entry in auto.buffer}
    if len(distinct_users) < gs.auto_msg_threshold:
        return

    # ② Cooldown
    if now - auto.last_trigger_time < gs.auto_cooldown:
        return

    # ③ Probability (roll the dice)
    if random.randint(1, 100) > gs.auto_probability:
        return

    # All conditions met — build summary and trigger
    auto.last_trigger_time = now
    summary = _build_summary(auto.buffer)
    auto.buffer.clear()  # consume the buffer

    # Schedule the ST call (don't await it — let the handler return quickly)
    asyncio.create_task(_auto_chat_flow(group_id, summary))


# ---------------------------------------------------------------------------
# Summary builder
# ---------------------------------------------------------------------------


def _build_summary(buffer: collections.deque) -> str:
    """Build a summary string from buffered messages.

    Format: one line per message, "QQ号：内容"
    """
    lines = []
    for user_id, _user_name, text, _ts in buffer:
        lines.append(f"{user_id}：{text}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Auto chat flow (load → generate → save → reply)
# ---------------------------------------------------------------------------


async def _auto_chat_flow(group_id: int, summary: str) -> None:
    """Execute the full chat pipeline for an auto-participation trigger.

    Similar to the normal chat flow, but:
    - The "user" is "群聊" (group chat itself)
    - The message is a summary of recent activity
    - Reply is sent via bot.send_group_msg() (no event to finish)
    - Errors are logged but never raised (best-effort participation)
    """
    gs = state.get_group_state(group_id)
    AUTO_USER = "群聊"

    try:
        # --- Load chat history ---
        chat_data: list[dict] = []
        if gs.chat_file:
            loaded = await st_api.load_chat(gs.avatar_url, gs.chat_file)
            if loaded:
                chat_data = loaded
        if not chat_data:
            gs.chat_file = chat_utils.new_chat_filename(gs.character_name)
            state.save_group_states()
            header = chat_utils.make_chat_header(
                AUTO_USER, gs.character_name
            )
            chat_data = [header]

        # --- Generate ---
        history = chat_utils.extract_history(chat_data)

        result = await st_api.plugin_generate(
            avatar_url=gs.avatar_url,
            preset_name=gs.preset_name,
            chat_history=history,
            user_message=summary,
            user_name=AUTO_USER,
            character_name=gs.character_name or "",
        )

        if not result.get("success"):
            logging.warning(
                f"Auto-participate: ST returned error for group {group_id}: "
                f"{result.get('error', 'unknown')}"
            )
            return

        response_text = result.get("response_text", "")
        if not response_text or not response_text.strip():
            return

        # --- Save to ST ---
        user_msg = chat_utils.make_chat_message(AUTO_USER, True, summary)
        ai_msg = chat_utils.make_chat_message(
            gs.character_name, False, response_text
        )
        if not any("chat_metadata" in m for m in chat_data):
            chat_data.insert(
                0, chat_utils.make_chat_header(AUTO_USER, gs.character_name)
            )
        chat_data.append(user_msg)
        chat_data.append(ai_msg)
        await st_api.save_chat(gs.avatar_url, gs.chat_file, chat_data)

        # --- Reply to QQ ---
        display_text = state.replace_qq_with_nickname(response_text)
        display_text = config.truncate(display_text)

        bot = get_bot()
        await bot.send_group_msg(
            group_id=group_id,
            message=Message(display_text),
        )

    except asyncio.CancelledError:
        # Task cancelled — nothing to do
        pass
    except httpx.ConnectError:
        logging.warning(
            f"Auto-participate: ST connection refused for group {group_id}"
        )
    except httpx.TimeoutException:
        logging.warning(
            f"Auto-participate: ST timeout for group {group_id}"
        )
    except RuntimeError:
        logging.warning(
            f"Auto-participate: ST retry exhausted for group {group_id}"
        )
    except Exception:
        logging.exception(
            f"Auto-participate: unexpected error for group {group_id}"
        )
