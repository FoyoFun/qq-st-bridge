"""
SillyTavern Bridge Plugin for NoneBot2
========================================
Bridges QQ group chat to SillyTavern AI character chat.
Users @mention the bot to interact with ST characters.

Commands:
  /chars    - List available characters
  /presets  - List available presets
  /char     - Select a character
  /preset   - Select a preset
  /status   - Show current binding
  /newchat  - Start a new chat
  /clear    - Clear conversation history
  /help     - Show help
"""

import logging

from nonebot import get_driver

# Import sub-modules (triggers NoneBot2 handler registration as a side effect)
from . import chat_handler  # noqa: F401  — registers on_message matcher
from . import chat_utils    # noqa: F401
from . import concurrency   # noqa: F401
from . import config        # noqa: F401
from . import handlers      # noqa: F401
from . import st_api        # noqa: F401
from . import st_client     # noqa: F401
from . import state         # noqa: F401

# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

driver = get_driver()


@driver.on_startup
async def _on_startup():
    """Load config, restore persisted state, and pre-fetch caches."""
    _driver_config = driver.config

    # Load runtime config into config module
    config.ST_BASE_URL = getattr(_driver_config, "st_base_url", "http://127.0.0.1:8000")
    config.ST_CHAT_SOURCE = getattr(_driver_config, "st_chat_source", "deepseek")
    config.ST_MODEL = getattr(_driver_config, "st_model", "")
    config.ST_TIMEOUT = int(getattr(_driver_config, "st_timeout", 120))
    config.ST_MAX_RESPONSE_LENGTH = int(
        getattr(_driver_config, "st_max_response_length", 800)
    )
    config.ST_DEFAULT_PRESET = getattr(_driver_config, "st_default_preset", "")
    config.ST_DEFAULT_CHARACTER = getattr(_driver_config, "st_default_character", "")

    # Reset cached base URL
    config.reset_base_url()

    logging.info(
        f"ST Bridge loaded: base={config.ST_BASE_URL}, "
        f"source={config.ST_CHAT_SOURCE}, "
        f"model={config.ST_MODEL or 'from preset'}, "
        f"timeout={config.ST_TIMEOUT}s"
    )

    # Restore persisted group states from disk
    state.load_group_states()

    # Pre-fetch caches
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
    """Clean up HTTP client."""
    await st_client.close_client()
    logging.info("ST Bridge: client closed")
