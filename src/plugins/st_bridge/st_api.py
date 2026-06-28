"""
High-level SillyTavern API operations.

Knows the API paths, parameter shapes, and return types. Uses st_client
for transport. Characters and presets are cached on first fetch.
"""

import asyncio
import json
import logging
from typing import Optional

import httpx

from . import config
from . import st_client

# ---------------------------------------------------------------------------
# Caches
# ---------------------------------------------------------------------------

_chars_cache: Optional[list[dict]] = None
_presets_cache: Optional[dict[str, dict]] = None  # name -> preset data


# ---------------------------------------------------------------------------
# Character / Preset queries
# ---------------------------------------------------------------------------

async def get_characters() -> list[dict]:
    """Get all characters from ST. Cached after first call."""
    global _chars_cache
    if _chars_cache is None:
        _chars_cache = await st_client.post_with_retry("/api/characters/all", {})
        if not isinstance(_chars_cache, list):
            _chars_cache = []
    return _chars_cache


async def get_character(avatar_url: str) -> dict | None:
    """Get a single character by avatar_url."""
    try:
        return await st_client.post_with_retry(
            "/api/characters/get", {"avatar_url": avatar_url}
        )
    except Exception:
        return None


async def get_presets() -> dict[str, dict]:
    """Get presets from ST. Returns {name: preset_data}. Cached."""
    global _presets_cache
    if _presets_cache is None:
        data = await st_client.post_with_retry("/api/settings/get", {})
        names = data.get("openai_setting_names", [])
        raw = data.get("openai_settings", [])
        _presets_cache = {}
        for name, preset_str in zip(names, raw):
            try:
                _presets_cache[name] = (
                    json.loads(preset_str) if isinstance(preset_str, str) else preset_str
                )
            except json.JSONDecodeError:
                pass
    return _presets_cache


# ---------------------------------------------------------------------------
# Chat file operations
# ---------------------------------------------------------------------------

async def get_chats(avatar_url: str) -> list[dict]:
    """Get list of chat files for a character."""
    try:
        return await st_client.post_with_retry(
            "/api/characters/chats", {"avatar_url": avatar_url}
        )
    except Exception:
        return []


async def load_chat(avatar_url: str, file_name: str) -> list[dict] | None:
    """Load a chat history from ST. Returns list of messages or None."""
    try:
        return await st_client.post_with_retry("/api/chats/get", {
            "avatar_url": avatar_url,
            "file_name": file_name,
        })
    except Exception:
        return None


async def save_chat(avatar_url: str, file_name: str, chat: list[dict]) -> bool:
    """Save chat history to ST. Returns True on success."""
    try:
        await st_client.post_with_retry("/api/chats/save", {
            "avatar_url": avatar_url,
            "file_name": file_name,
            "chat": chat,
        })
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Plugin generate (prompt building + AI generation in one call)
# ---------------------------------------------------------------------------

async def plugin_generate(
    avatar_url: str,
    preset_name: str,
    chat_history: list[dict],
    user_message: str,
    user_name: str = "QQ用户",
    character_name: str = "",
) -> dict:
    """Call the nb-qq-bot ST plugin to build prompt and generate AI response.

    Uses st_client.auth_post() directly (instead of post_with_retry) to
    manage its own retry loop, since the plugin endpoint may behave
    differently from simple CRUD endpoints.

    Returns: {"success": bool, "response_text": str, "error": str (if failed)}
    """
    for attempt in range(2):
        try:
            payload: dict = {
                "avatar_url": avatar_url,
                "preset_name": preset_name,
                "chat_history": chat_history,
                "user_message": user_message,
                "user_name": user_name,
                "qq_chat_behavior": config.QQ_CHAT_BEHAVIOR.format(
                    character_name=character_name or "角色"
                ),
                "max_response_length": config.ST_MAX_RESPONSE_LENGTH,
                "chat_completion_source": config.ST_CHAT_SOURCE,
                "stream": False,
            }
            # Don't send model — let ST use the preset's configured model

            resp = await st_client.auth_post(
                "/api/plugins/nb-qq-bot/generate", payload
            )
            resp.raise_for_status()
            return resp.json()

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 403 and attempt == 0:
                logging.warning(
                    "ST Bridge: CSRF rejected in plugin generate, resetting client..."
                )
                await st_client.reset_client()
                await asyncio.sleep(1)
                continue
            raise
        except (httpx.ConnectError, httpx.RemoteProtocolError) as e:
            if attempt == 0:
                logging.warning(
                    f"ST Bridge: connection lost in plugin generate ({e}), reconnecting..."
                )
                await st_client.reset_client()
                await asyncio.sleep(1)
                continue
            raise

    # Both attempts failed — let the caller show a user-friendly error
    raise RuntimeError("ST Bridge: failed to reach ST plugin after retry")
