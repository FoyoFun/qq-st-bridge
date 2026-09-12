"""
Conversation state and persistence.

GroupState (character, preset, chat file, social switch) is persisted to
data/group_states.json so settings survive restarts. States are keyed by
conversation key ("group:<gid>" / "private:<uid>"). Old int-keyed files
(legacy group states) are migrated on load.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# GroupState
# ---------------------------------------------------------------------------

@dataclass
class GroupState:
    """Per-conversation bridge state."""
    character_name: Optional[str] = None
    preset_name: Optional[str] = None
    avatar_url: Optional[str] = None   # character avatar filename
    chat_file: Optional[str] = None    # ST chat filename (without .jsonl)

    # --- Social engine switch (per-conversation; params are global) ---
    social_enabled: bool = False
    interest_keywords: list[str] = field(default_factory=list)


# key = conversation key ("group:123" / "private:456")
_states: dict[str, GroupState] = {}

# Default social switch for newly encountered groups (set from .env at startup)
_default_social_enabled: bool = False


def set_default_social_enabled(enabled: bool) -> None:
    global _default_social_enabled
    _default_social_enabled = enabled


def group_conv(group_id: int) -> str:
    return f"group:{group_id}"


def private_conv(user_id: int) -> str:
    return f"private:{user_id}"


def get_state(conv: str) -> GroupState:
    """Get or create the state for a conversation key."""
    if conv not in _states:
        gs = GroupState()
        gs.social_enabled = _default_social_enabled
        _states[conv] = gs
    return _states[conv]


# Backward-compatible helpers (used by handlers that only know group ids)
def get_group_state(group_id: int) -> GroupState:
    return get_state(group_conv(group_id))


# ---------------------------------------------------------------------------
# Persistence (survives bot restarts)
# ---------------------------------------------------------------------------

def _state_file_path() -> str:
    return os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "data", "group_states.json"
    )


def save_states() -> None:
    """Persist all conversation states to disk."""
    data: dict[str, dict] = {}
    for conv, gs in _states.items():
        data[conv] = {
            "character_name": gs.character_name,
            "preset_name": gs.preset_name,
            "avatar_url": gs.avatar_url,
            "chat_file": gs.chat_file,
            "social_enabled": gs.social_enabled,
            "interest_keywords": gs.interest_keywords,
        }
    try:
        filepath = _state_file_path()
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.warning(f"Failed to save group states: {e}")


def load_states() -> None:
    """Restore conversation states from disk (called at startup)."""
    global _states
    try:
        filepath = _state_file_path()
        if not os.path.exists(filepath):
            return
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        _states = {}
        for key, fields in data.items():
            # Legacy files were keyed by bare int group ids
            conv = key if str(key).startswith(("group:", "private:")) else group_conv(int(key))
            gs = GroupState()
            gs.character_name = fields.get("character_name")
            gs.preset_name = fields.get("preset_name")
            gs.avatar_url = fields.get("avatar_url")
            gs.chat_file = fields.get("chat_file")
            gs.social_enabled = bool(fields.get("social_enabled", False))
            gs.interest_keywords = list(fields.get("interest_keywords", []))
            _states[conv] = gs
        logging.info(f"Loaded {len(_states)} conversation state(s) from disk")
    except Exception as e:
        logging.warning(f"Failed to load group states: {e}")
        _states = {}
