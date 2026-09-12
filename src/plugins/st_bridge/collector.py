"""
Message collector — the bridge's eyes.

Normalizes every incoming conversation message into a ConvMsg with a clean
codename (via aliases), renders OneBot message segments into readable
placeholders ([图片] / @代号 / （回复 …）), and writes it into a per-
conversation rolling buffer (default 50 entries). Zero generation cost —
collection never talks to ST.
"""

import collections
import logging
import time
from dataclasses import dataclass

from . import aliases
from . import tracelog

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ConvMsg:
    """One normalized conversation message."""
    conv: str            # "group:123" / "private:456"
    qq: str              # sender QQ ("" for self messages)
    alias: str           # clean codename of the sender
    text: str            # rendered readable text (segments resolved)
    ts: float            # unix timestamp
    is_self: bool = False
    is_at: bool = False  # the message explicitly @'d the bot


# ---------------------------------------------------------------------------
# Rolling buffers
# ---------------------------------------------------------------------------

_BUFFERS: dict[str, collections.deque] = {}
_BUFFER_MAX = 50


def _buffer(conv: str) -> collections.deque:
    if conv not in _BUFFERS:
        _BUFFERS[conv] = collections.deque(maxlen=_BUFFER_MAX)
    return _BUFFERS[conv]


def group_conv(group_id: int | str) -> str:
    return f"group:{group_id}"


def private_conv(user_id: int | str) -> str:
    return f"private:{user_id}"


# ---------------------------------------------------------------------------
# Segment rendering
# ---------------------------------------------------------------------------

def _render_segments(message) -> tuple[str, bool]:
    """Render a OneBot v11 Message into readable text.

    Returns (text, has_content). @ segments become @代号, images become
    [图片], replies become （回复 代号：片段） prefixes, faces become QQ
    表情 placeholders. Pure function — no side effects.
    """
    from nonebot.adapters.onebot.v11 import MessageSegment  # local: adapter types

    parts: list[str] = []
    reply_prefix = ""
    for seg in message:
        try:
            if seg.type == "text":
                parts.append(str(seg.data.get("text", "")))
            elif seg.type == "at":
                qq = str(seg.data.get("qq", ""))
                if qq == "all":
                    parts.append("@全体成员")
                else:
                    member = aliases.get_member(qq)
                    name = member.alias if member else (qq or "某人")
                    parts.append(f"@{name}")
            elif seg.type == "image":
                parts.append("[图片]")
            elif seg.type == "face":
                parts.append(f"[QQ表情{seg.data.get('id', '')}]")
            elif seg.type == "reply":
                ref_qq = str(seg.data.get("qq", ""))
                member = aliases.get_member(ref_qq) if ref_qq else None
                ref_name = member.alias if member else (ref_qq or "消息")
                snippet = str(seg.data.get("text", "") or "")[:20]
                reply_prefix = f"（回复 {ref_name}：{snippet}）" if snippet else f"（回复 {ref_name}）"
            elif seg.type in ("record", "video"):
                parts.append("[语音]" if seg.type == "record" else "[视频]")
            elif seg.type == "json":
                parts.append("[卡片]")
            # other segment types are silently dropped
        except Exception:
            continue
    text = reply_prefix + "".join(parts)
    text = "\n".join(line.strip() for line in text.splitlines()).strip()
    return text, bool(text)


# ---------------------------------------------------------------------------
# Collection API
# ---------------------------------------------------------------------------

def collect_group(event, text_override: str = "", is_at: bool = False) -> ConvMsg | None:
    """Normalize one group message into the buffer.

    Resolves/creates the sender's codename, records roster activity (with
    the current display name, so nickname changes stay traceable), and
    appends to the rolling buffer. Returns the ConvMsg, or None when the
    message has no readable content.
    """
    conv = group_conv(event.group_id)
    qq = str(event.user_id)

    display = ""
    try:
        display = event.sender.card or event.sender.nickname or qq
    except Exception:
        display = qq

    if text_override:
        text = text_override.strip()
    else:
        text, _ = _render_segments(event.message)

    if not text:
        return None

    alias = aliases.get_alias(qq, display)
    aliases.record_seen(qq, display)

    msg = ConvMsg(conv=conv, qq=qq, alias=alias, text=text, ts=time.time(),
                  is_at=is_at)
    _buffer(conv).append(msg)
    tracelog.qq_in(conv, alias, text)
    return msg


def collect_private(event) -> ConvMsg | None:
    """Normalize one private message into the buffer (conv private:<qq>)."""
    conv = private_conv(event.user_id)
    qq = str(event.user_id)
    display = ""
    try:
        display = event.sender.card or event.sender.nickname or qq
    except Exception:
        display = qq

    text, _ = _render_segments(event.message)
    if not text:
        return None

    alias = aliases.get_alias(qq, display)
    aliases.record_seen(qq, display)

    msg = ConvMsg(conv=conv, qq=qq, alias=alias, text=text, ts=time.time())
    _buffer(conv).append(msg)
    tracelog.qq_in(conv, alias, text)
    return msg


def record_self(conv: str, text: str, char_name: str) -> None:
    """Record one of the bot's own outgoing messages into the buffer.

    Called by the sender after a successful send so the model sees its own
    recent utterances in the next context window.
    """
    if not text:
        return
    msg = ConvMsg(
        conv=conv, qq="", alias=char_name, text=text,
        ts=time.time(), is_self=True,
    )
    _buffer(conv).append(msg)


def add_event(conv: str, alias: str, text: str) -> None:
    """Record a non-message social event (e.g. someone poked the bot).

    Rendered into the record block so the model can perceive it.
    """
    msg = ConvMsg(conv=conv, qq="", alias=alias, text=text, ts=time.time())
    _buffer(conv).append(msg)
    tracelog.qq_in(conv, alias, f"（事件）{text}")


# ---------------------------------------------------------------------------
# Buffer queries
# ---------------------------------------------------------------------------

def recent(conv: str, limit: int = 50) -> list[ConvMsg]:
    """The most recent `limit` messages of a conversation, oldest first."""
    return list(_buffer(conv))[-max(1, limit):]


def messages_since(conv: str, ts: float) -> list[ConvMsg]:
    """Messages newer than `ts` (used by the active-period batch check)."""
    return [m for m in _buffer(conv) if m.ts > ts]


def last(conv: str) -> ConvMsg | None:
    """The newest buffered message, or None."""
    buf = _buffer(conv)
    return buf[-1] if buf else None
