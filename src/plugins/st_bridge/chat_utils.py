"""
Pure functions for constructing and parsing SillyTavern JSONL chat records.

No side effects, no I/O, no state — these are data-format helpers.
"""

import re
from datetime import datetime, timezone


def new_chat_filename(character_name: str) -> str:
    """Generate a new chat filename for a character.

    Example: "CharacterName - 2026-06-28@10h30m45s123ms"
    """
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d@%Hh%Mm%Ss%f")[:-3] + "ms"
    safe_name = re.sub(r'[\\/:*?"<>|]', '_', character_name)
    return f"{safe_name} - {ts}"


def make_chat_header(
    user_name: str = "QQ用户",
    character_name: str = "Character",
) -> dict:
    """Create the first line (header) of a ST JSONL chat file."""
    return {
        "chat_metadata": {
            "tainted": False,
            "note_prompt": "",
            "note_interval": 1,
            "note_position": 1,
            "note_depth": 4,
            "note_role": 0,
            "timedWorldInfo": {"sticky": {}, "cooldown": {}},
            "lastInContextMessageId": 0,
        },
        "user_name": user_name,
        "character_name": character_name,
    }


def make_chat_message(name: str, is_user: bool, text: str) -> dict:
    """Create a chat message object for ST JSONL format."""
    return {
        "name": name,
        "is_user": is_user,
        "is_system": False,
        "send_date": int(datetime.now(timezone.utc).timestamp() * 1000),
        "mes": text,
        "extra": {},
    }


def extract_history(chat_data: list[dict]) -> list[dict]:
    """Extract message list from ST chat data (skip header line)."""
    history = []
    for msg in chat_data:
        if "chat_metadata" in msg:
            continue  # skip header
        if "mes" in msg and msg["mes"]:
            history.append(msg)
    return history
