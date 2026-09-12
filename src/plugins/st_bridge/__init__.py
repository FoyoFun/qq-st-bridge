"""
SillyTavern Bridge Plugin for NoneBot2
========================================
Bridges QQ conversations to SillyTavern AI characters with a social
strategy layer: the character participates in group chat like a real
member (watch / active / probe / exit state machine), instead of only
answering @mentions.

Architecture (bridge = body/behavior, ST = brain/persona):
  collector       normalize messages -> clean codenames -> rolling buffer
  participation   state machine + checkpoint triggers + WAKE/WAIT timers
  context_builder observation blocks (records + roster + sticker catalog)
  action_loop     call ST -> parse marker protocol -> dispatch actions
  sender          bubble splitting + human-like delays + stickers + pokes
  aliases         stable member codenames + impressions roster
  stickers        sticker catalog for [STICKER: tag]
  state           per-conversation settings, persisted

Commands: /help /chars /presets /char /preset /status /newchat /clear
          /social /stickers /sticker /note
"""

import logging
import os

from nonebot import get_driver

# Import sub-modules (triggers NoneBot2 handler registration as a side effect)
from . import action_loop       # noqa: F401
from . import aliases           # noqa: F401
from . import chat_handler      # noqa: F401  — registers matchers
from . import chat_utils        # noqa: F401
from . import collector         # noqa: F401
from . import concurrency       # noqa: F401
from . import config            # noqa: F401
from . import context_builder   # noqa: F401
from . import handlers          # noqa: F401
from . import participation     # noqa: F401
from . import sender            # noqa: F401
from . import st_api            # noqa: F401
from . import st_client         # noqa: F401
from . import state             # noqa: F401
from . import stickers          # noqa: F401
from . import tracelog          # noqa: F401

# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

driver = get_driver()


def _cfg(name: str, default):
    return getattr(driver.config, name, default)


@driver.on_startup
async def _on_startup():
    """Load config, restore persisted state, start engine + sender."""
    config.ST_BASE_URL = str(_cfg("st_base_url", "http://127.0.0.1:8000"))
    config.ST_CHAT_SOURCE = str(_cfg("st_chat_source", "deepseek"))
    config.ST_MODEL = str(_cfg("st_model", ""))
    config.ST_TIMEOUT = int(_cfg("st_timeout", 120))
    config.ST_MAX_RESPONSE_LENGTH = int(_cfg("st_max_response_length", 250))
    config.ST_DEFAULT_PRESET = str(_cfg("st_default_preset", ""))
    config.ST_DEFAULT_CHARACTER = str(_cfg("st_default_character", ""))

    # Social engine parameters
    config.ST_SOCIAL_ENABLED = str(_cfg("st_social_enabled", "false")).lower() == "true"
    config.ST_CONTEXT_WINDOW = int(_cfg("st_context_window", 50))

    config.ST_CHECKPOINT_THRESHOLD = int(_cfg("st_checkpoint_threshold", 3))
    config.ST_CHECKPOINT_COOLDOWN = int(_cfg("st_checkpoint_cooldown", 90))
    config.ST_TICK_INTERVAL = float(_cfg("st_tick_interval", 2.0))

    config.ST_ACTIVE_MIN = int(_cfg("st_active_min", 300))
    config.ST_ACTIVE_MAX = int(_cfg("st_active_max", 600))
    config.ST_ACTIVE_CHECK_MIN = int(_cfg("st_active_check_min", 10))
    config.ST_ACTIVE_CHECK_MAX = int(_cfg("st_active_check_max", 30))
    config.ST_COLD_WINDOW = int(_cfg("st_cold_window", 360))
    config.ST_PROBE_PROBABILITY = int(_cfg("st_probe_probability", 30))
    config.ST_PROBE_COOLDOWN = int(_cfg("st_probe_cooldown", 1200))
    config.ST_PROBE_WAIT_MIN = int(_cfg("st_probe_wait_min", 120))
    config.ST_PROBE_WAIT_MAX = int(_cfg("st_probe_wait_max", 300))

    config.ST_REPLY_DELAY_MIN = float(_cfg("st_reply_delay_min", 2.0))
    config.ST_REPLY_DELAY_MAX = float(_cfg("st_reply_delay_max", 8.0))
    config.ST_BURST_MIN = float(_cfg("st_burst_min", 1.0))
    config.ST_BURST_MAX = float(_cfg("st_burst_max", 3.0))
    config.ST_BURST_LONG_PROBABILITY = float(_cfg("st_burst_long_probability", 0.2))
    config.ST_BURST_LONG_MIN = float(_cfg("st_burst_long_min", 8.0))
    config.ST_BURST_LONG_MAX = float(_cfg("st_burst_long_max", 10.0))
    config.ST_MAX_MSG_CHARS = int(_cfg("st_max_msg_chars", 500))
    config.ST_MAX_BUBBLES = int(_cfg("st_max_bubbles", 6))

    config.ST_LIGHT_TOKENS = int(_cfg("st_light_tokens", 80))
    config.ST_HEAVY_TOKENS = int(_cfg("st_heavy_tokens", 250))
    config.ST_WAKE_MIN = int(_cfg("st_wake_min", 600))
    config.ST_WAKE_MAX = int(_cfg("st_wake_max", 7200))
    config.ST_WAKE_DAILY_LIMIT = int(_cfg("st_wake_daily_limit", 30))
    config.ST_MAX_STEPS = int(_cfg("st_max_steps", 5))

    raw_keywords = str(_cfg("st_interest_keywords", ""))
    config.ST_INTEREST_KEYWORDS = [
        k.strip() for k in raw_keywords.replace("，", ",").split(",") if k.strip()
    ]

    # I/O trace log
    config.ST_TRACE_ENABLED = str(_cfg("st_trace_enabled", "true")).lower() == "true"
    config.ST_TRACE_MAX_CHARS = int(_cfg("st_trace_max_chars", 4000))
    config.ST_TRACE_KEEP_DAYS = int(_cfg("st_trace_keep_days", 7))

    state.set_default_social_enabled(config.ST_SOCIAL_ENABLED)
    config.reset_base_url()

    logging.info(
        f"ST Bridge loaded: base={config.ST_BASE_URL}, "
        f"source={config.ST_CHAT_SOURCE}, "
        f"timeout={config.ST_TIMEOUT}s, "
        f"social={'on' if config.ST_SOCIAL_ENABLED else 'off'}, "
        f"keywords={config.ST_INTEREST_KEYWORDS}"
    )

    # Restore persisted stores
    state.load_states()
    aliases.load()
    stickers.load()
    participation.load_snapshots()

    # Start I/O trace (logs/trace.log, daily rotation + retention)
    tracelog.setup(
        logs_dir=os.path.join(
            os.path.dirname(__file__), "..", "..", "..", "logs"
        ),
        trace_enabled=config.ST_TRACE_ENABLED,
        trace_max_chars=config.ST_TRACE_MAX_CHARS,
        keep_days=config.ST_TRACE_KEEP_DAYS,
    )

    # Start background machinery
    sender.start_worker()
    participation.start_engine()

    # Pre-fetch ST caches (best-effort)
    try:
        chars = await st_api.get_characters()
        logging.info(f"ST Bridge: {len(chars)} characters loaded")
    except Exception as e:
        logging.warning(f"ST Bridge: failed to preload characters: {e}")

    try:
        presets = await st_api.get_presets()
        logging.info(f"ST Bridge: {len(presets)} presets loaded")
    except Exception as e:
        logging.warning(f"ST Bridge: failed to preload presets: {e}")


@driver.on_shutdown
async def _on_shutdown():
    """Stop engine/sender and clean up HTTP client."""
    await participation.stop_engine()
    await sender.stop_worker()
    participation.save_snapshots()
    await st_client.close_client()
    logging.info("ST Bridge: shutdown complete")
