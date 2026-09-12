"""
Clean codename registry for group members.

Every QQ member gets a stable, readable codename (清洁代号) on first sight.
The codename never changes for the lifetime of the mapping, so the AI sees
consistent names in chat records instead of raw QQ numbers or fluctuating
nicknames. Persisted to data/aliases.json (QQ -> codename, lifetime stable)
and data/impressions.json (roster stats + manual notes).
"""

import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class MemberInfo:
    """Per-member roster entry (impressions layer)."""
    qq: str
    alias: str
    first_seen: float = 0.0
    last_seen: float = 0.0
    msg_count: int = 0
    note: str = ""          # manual impression note (/note command)


# qq(str) -> MemberInfo
_members: dict[str, MemberInfo] = {}
# alias(lower) -> qq(str), reverse index for [POKE: 代号] resolution
_alias_index: dict[str, str] = {}

_alias_file: str = ""
_impression_file: str = ""

_FALLBACK_PREFIX = "群友"
_MAX_ALIAS_LEN = 12


def _data_dir() -> str:
    return os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "data"
    )


def _sanitize(raw: str) -> str:
    """Reduce a nickname/group card to a safe short codename candidate.

    Keeps CJK letters digits and a few safe separators; strips everything
    else (emoji, QQ-number-ish spam, control chars). Empty result means
    the caller must fall back to a generated codename.
    """
    text = re.sub(r"\s+", "", raw or "")
    text = re.sub(r"[^\w\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af-]", "", text)
    # Drop long digit runs (raw QQ numbers are not readable codenames)
    text = re.sub(r"\d{5,}", "", text)
    return text[:_MAX_ALIAS_LEN]


def _generate_fallback() -> str:
    return f"{_FALLBACK_PREFIX}{secrets.token_hex(2)}"


def _unique_alias(candidate: str) -> str:
    """Ensure the alias does not collide with an existing one."""
    base = candidate or _generate_fallback()
    alias = base
    n = 2
    while alias.lower() in _alias_index:
        suffix = str(n)
        alias = base[: _MAX_ALIAS_LEN - len(suffix)] + suffix
        n += 1
    return alias


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def load() -> None:
    """Load aliases + impressions from disk (called at startup)."""
    global _members, _alias_index, _alias_file, _impression_file
    data_dir = _data_dir()
    _alias_file = os.path.join(data_dir, "aliases.json")
    _impression_file = os.path.join(data_dir, "impressions.json")

    aliases: dict[str, str] = {}
    try:
        if os.path.exists(_alias_file):
            with open(_alias_file, "r", encoding="utf-8") as f:
                aliases = json.load(f)
    except Exception as e:
        logging.warning(f"Aliases: failed to load {_alias_file}: {e}")
        aliases = {}

    impressions: dict[str, dict] = {}
    try:
        if os.path.exists(_impression_file):
            with open(_impression_file, "r", encoding="utf-8") as f:
                impressions = json.load(f)
    except Exception as e:
        logging.warning(f"Aliases: failed to load {_impression_file}: {e}")
        impressions = {}

    _members = {}
    _alias_index = {}
    for qq, alias in aliases.items():
        info_raw = impressions.get(qq, {})
        member = MemberInfo(
            qq=str(qq),
            alias=str(alias),
            first_seen=float(info_raw.get("first_seen", 0.0)),
            last_seen=float(info_raw.get("last_seen", 0.0)),
            msg_count=int(info_raw.get("msg_count", 0)),
            note=str(info_raw.get("note", "")),
        )
        _members[member.qq] = member
        _alias_index.setdefault(member.alias.lower(), member.qq)
    logging.info(f"Aliases: loaded {len(_members)} member codename(s)")


def save() -> None:
    """Persist aliases + impressions to disk."""
    global _alias_file, _impression_file
    if not _alias_file or not _impression_file:
        data_dir = _data_dir()
        _alias_file = os.path.join(data_dir, "aliases.json")
        _impression_file = os.path.join(data_dir, "impressions.json")
    try:
        os.makedirs(_data_dir(), exist_ok=True)
        alias_map = {m.qq: m.alias for m in _members.values()}
        with open(_alias_file, "w", encoding="utf-8") as f:
            json.dump(alias_map, f, ensure_ascii=False, indent=2)
        imp_map = {
            m.qq: {
                "first_seen": m.first_seen,
                "last_seen": m.last_seen,
                "msg_count": m.msg_count,
                "note": m.note,
            }
            for m in _members.values()
        }
        with open(_impression_file, "w", encoding="utf-8") as f:
            json.dump(imp_map, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.warning(f"Aliases: failed to save: {e}")


# ---------------------------------------------------------------------------
# Lookup / registration
# ---------------------------------------------------------------------------

def get_member(qq: int | str) -> Optional[MemberInfo]:
    """Get the roster entry for a QQ number, or None if never seen."""
    return _members.get(str(qq))


def get_alias(qq: int | str, display_name: str = "") -> str:
    """Get the stable codename for a QQ number, creating one on first sight.

    The codename is derived from the display name once and then frozen:
    later nickname changes do NOT rewrite it (lifetime stability).
    """
    key = str(qq)
    member = _members.get(key)
    if member is not None:
        return member.alias

    candidate = _sanitize(display_name)
    alias = _unique_alias(candidate if len(candidate) >= 2 else _generate_fallback())
    now = time.time()
    member = MemberInfo(qq=key, alias=alias, first_seen=now, last_seen=now)
    _members[key] = member
    _alias_index[alias.lower()] = key
    save()
    logging.info(f"Aliases: QQ {key} -> codename '{alias}'")
    return alias


def record_seen(qq: int | str, is_self: bool = False) -> None:
    """Update last-seen timestamp and message counter for a member."""
    member = _members.get(str(qq))
    if member is None:
        return
    member.last_seen = time.time()
    if not is_self:
        member.msg_count += 1


def resolve_qq(alias: str) -> Optional[str]:
    """Reverse lookup: codename (or exact alias text) -> QQ string."""
    return _alias_index.get((alias or "").strip().lower())


def set_note(qq: int | str, note: str) -> bool:
    """Set a manual impression note for a member. Returns True if known."""
    member = _members.get(str(qq))
    if member is None:
        return False
    member.note = note.strip()
    save()
    return True


def roster_lines(limit: int = 20) -> list[str]:
    """Render the roster for prompt injection, most recently active first.

    Lines look like: "- 阿伟：聊过35次，最近22:30；印象：爱聊游戏"
    """
    now = time.time()
    members = sorted(_members.values(), key=lambda m: m.last_seen, reverse=True)
    lines = []
    for m in members[: max(1, limit)]:
        stats = f"聊过{m.msg_count}次"
        if m.last_seen:
            delta = now - m.last_seen
            if delta < 3600:
                recent = f"{int(delta // 60)}分钟前活跃"
            elif delta < 86400:
                recent = f"{int(delta // 3600)}小时前活跃"
            else:
                recent = f"{int(delta // 86400)}天前见过"
        else:
            recent = "暂无活动"
        extra = f"；印象：{m.note}" if m.note else ""
        lines.append(f"- {m.alias}：{stats}，{recent}{extra}")
    return lines
