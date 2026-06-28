"""
Per-group session state and QQ-number-to-nickname bidirectional mapping.

GroupState (character, preset, chat file) is persisted to JSON on disk
so settings survive bot restarts. The nickname map is in-memory only.
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
    """Per-group bridge state: which character and preset are selected."""
    character_name: Optional[str] = None
    preset_name: Optional[str] = None
    avatar_url: Optional[str] = None   # character avatar filename
    chat_file: Optional[str] = None     # ST chat filename (without .jsonl)
    chat_metadata: dict = field(default_factory=dict)


# key = group_id (int)
_group_states: dict[int, GroupState] = {}


def get_group_state(group_id: int) -> GroupState:
    """Get or create the GroupState for a given group."""
    if group_id not in _group_states:
        _group_states[group_id] = GroupState()
    return _group_states[group_id]


# ---------------------------------------------------------------------------
# State persistence (survives bot restarts)
# ---------------------------------------------------------------------------

def _state_file_path() -> str:
    """Path to the JSON file that persists group states.

    Resolves relative to this module's location — three levels up from
    src/plugins/st_bridge/ to reach project root, then into data/.
    """
    return os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "data", "group_states.json"
    )


def save_group_states() -> None:
    """Persist all group states to disk."""
    data: dict[str, dict] = {}
    for gid, state in _group_states.items():
        data[str(gid)] = {
            "character_name": state.character_name,
            "preset_name": state.preset_name,
            "avatar_url": state.avatar_url,
            "chat_file": state.chat_file,
        }
    try:
        filepath = _state_file_path()
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.warning(f"Failed to save group states: {e}")


def load_group_states() -> None:
    """Restore group states from disk (called at startup)."""
    global _group_states
    try:
        filepath = _state_file_path()
        if not os.path.exists(filepath):
            return
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        for gid_str, fields in data.items():
            gid = int(gid_str)
            state = GroupState()
            state.character_name = fields.get("character_name")
            state.preset_name = fields.get("preset_name")
            state.avatar_url = fields.get("avatar_url")
            state.chat_file = fields.get("chat_file")
            _group_states[gid] = state
        logging.info(f"Loaded {len(data)} group state(s) from disk")
    except Exception as e:
        logging.warning(f"Failed to load group states: {e}")
        _group_states = {}


# ---------------------------------------------------------------------------
# QQ号 → 昵称 映射（用于回复中还原QQ号为昵称）
# ---------------------------------------------------------------------------

_nickname_map: dict[str, str] = {}


def remember_user(user_id: int, user_name: str) -> None:
    """记住用户的QQ号→昵称映射。"""
    _nickname_map[str(user_id)] = user_name


def replace_qq_with_nickname(text: str) -> str:
    """将文本中已知的QQ号替换回昵称。"""
    result = text
    for qq_id, nickname in _nickname_map.items():
        result = result.replace(qq_id, nickname)
    return result
