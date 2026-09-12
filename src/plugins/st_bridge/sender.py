"""
Sender — the bridge's mouth.

Single serialized send queue for all conversations. Text replies are split
into QQ-style burst bubbles (AI uses spaces as split signals), each bubble
goes out with human-like random delays: first bubble 2~8s (typing feel),
between bubbles 1~3s with a 20% chance of an 8~10s long gap. Stickers go
out as their own image bubble; pokes use the OneBot group_poke action.

Every successfully sent text is recorded back into the collector buffer so
the model sees its own utterances next turn.
"""

import asyncio
import logging
import random
from dataclasses import dataclass, field

from . import collector
from . import config
from . import tracelog

# ---------------------------------------------------------------------------
# Splitting (ported from qq-bridge planSocialTimeline)
# ---------------------------------------------------------------------------

_CJK_PUNCT = set("，。！？、；：""''「」『』（）…—～·")


def _is_cjk(ch: str) -> bool:
    """True for Han characters and CJK punctuation.

    Deliberately excludes ASCII — a space between two latin words is
    typography, not a split signal.
    """
    if not ch:
        return False
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF
        or 0x3400 <= code <= 0x4DBF
        or 0xF900 <= code <= 0xFAFF
        or ch in _CJK_PUNCT
    )


def split_by_spaces(src: str) -> list[str]:
    """Split on intentional spaces: a space with CJK on either side is a
    bubble boundary; spaces inside latin runs are kept."""
    tokens = [t.strip() for t in (src or "").split()] if src else []
    tokens = [t for t in tokens if t]
    if len(tokens) <= 1:
        return tokens
    groups: list[str] = []
    current = tokens[0]
    for token in tokens[1:]:
        if _is_cjk(current[-1]) or _is_cjk(token[0]):
            groups.append(current)
            current = token
        else:
            current = f"{current} {token}"
    if current:
        groups.append(current)
    return groups


def split_long(text: str, max_chars: int) -> list[str]:
    """Hard-split one bubble that exceeds the length guard."""
    text = text.strip()
    if len(text) <= max_chars:
        return [text] if text else []
    chunks = []
    for i in range(0, len(text), max_chars):
        chunks.append(text[i : i + max_chars])
    return chunks


def plan_bubbles(text: str, max_chars: int | None = None) -> list[str]:
    """Full split pipeline: space signals first, then length hard-split.

    Hard-capped at config.ST_MAX_BUBBLES bubbles to prevent runaways.
    """
    max_chars = max_chars or config.ST_MAX_MSG_CHARS
    bubbles: list[str] = []
    for part in split_by_spaces(text):
        bubbles.extend(split_long(part, max_chars))
    return bubbles[: config.ST_MAX_BUBBLES]


# ---------------------------------------------------------------------------
# Send queue
# ---------------------------------------------------------------------------

@dataclass
class _SendItem:
    conv: str
    kind: str            # "text" | "sticker" | "poke"
    pre_delay: float     # seconds to sleep BEFORE sending
    payload: dict = field(default_factory=dict)
    char_name: str = ""  # for self-recording of text bubbles
    record_text: str = ""  # burst self-record: full text, set on the LAST bubble


_queue: asyncio.Queue = asyncio.Queue()
_worker_task: asyncio.Task | None = None


def rand_reply_delay() -> float:
    return random.uniform(config.ST_REPLY_DELAY_MIN, config.ST_REPLY_DELAY_MAX)


def enqueue_text(conv: str, text: str, char_name: str) -> int:
    """Queue a text reply as a burst of bubbles with human-like pacing.

    One turn = one buffer record: the full burst text (joined with \n) is
    attached to the LAST bubble and recorded once after it is sent, so the
    model sees her own reply as a single unit and self-messages can never
    re-trigger the engine.
    """
    bubbles = plan_bubbles(text)
    if not bubbles:
        return 0
    full_text = "\n".join(bubbles)
    delay = rand_reply_delay()
    for i, bubble in enumerate(bubbles):
        if i > 0:
            if random.random() < config.ST_BURST_LONG_PROBABILITY:
                delay = random.uniform(config.ST_BURST_LONG_MIN, config.ST_BURST_LONG_MAX)
            else:
                delay = random.uniform(config.ST_BURST_MIN, config.ST_BURST_MAX)
        is_last = i == len(bubbles) - 1
        _queue.put_nowait(_SendItem(
            conv=conv, kind="text", pre_delay=delay,
            payload={"text": bubble}, char_name=char_name,
            record_text=full_text if is_last else "",
        ))
    return len(bubbles)


def enqueue_sticker(conv: str, uri: str, tag: str, char_name: str) -> None:
    """Queue a sticker as its own image bubble (short natural delay)."""
    _queue.put_nowait(_SendItem(
        conv=conv, kind="sticker", pre_delay=rand_reply_delay(),
        payload={"uri": uri, "tag": tag}, char_name=char_name,
    ))


def enqueue_poke(conv: str, target_qq: str) -> None:
    """Queue a group poke."""
    _queue.put_nowait(_SendItem(
        conv=conv, kind="poke", pre_delay=random.uniform(0.5, 2.0),
        payload={"qq": str(target_qq)},
    ))


def pending_count() -> int:
    """Number of unsent items (used to skip triggers while backed up)."""
    return _queue.qsize()


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

async def _resolve_bot():
    from nonebot import get_bot
    return get_bot()


async def _send_one(bot, item: _SendItem) -> None:
    if item.kind == "text":
        conv_type, conv_id = item.conv.split(":", 1)
        try:
            if conv_type == "group":
                await bot.send_group_msg(group_id=int(conv_id), message=str(item.payload["text"]))
            else:
                await bot.send_private_msg(user_id=int(conv_id), message=str(item.payload["text"]))
        except Exception:
            tracelog.qq_out(item.conv, "text", str(item.payload["text"]), ok=False, detail="send failed")
            raise
        tracelog.qq_out(item.conv, "text", item.payload["text"])
        # One buffer entry per burst (the last bubble carries the full text)
        if item.record_text:
            collector.record_self(item.conv, item.record_text, item.char_name)
    elif item.kind == "sticker":
        from nonebot.adapters.onebot.v11 import Message, MessageSegment
        message = Message(MessageSegment.image(item.payload["uri"]))
        tag = item.payload.get("tag", "")
        conv_type, conv_id = item.conv.split(":", 1)
        try:
            if conv_type == "group":
                await bot.send_group_msg(group_id=int(conv_id), message=message)
            else:
                await bot.send_private_msg(user_id=int(conv_id), message=message)
        except Exception:
            tracelog.qq_out(item.conv, "sticker", f"[{tag}] {item.payload['uri']}", ok=False, detail="send failed")
            raise
        tracelog.qq_out(item.conv, "sticker", f"[{tag}]")
        collector.record_self(item.conv, f"[表情：{tag}]", item.char_name)
        from . import stickers as _stickers
        entry = _stickers.find(tag)
        if entry is not None:
            _stickers.mark_used(entry)
    elif item.kind == "poke":
        conv_type, conv_id = item.conv.split(":", 1)
        if conv_type != "group":
            return  # pokes are group-only
        try:
            await bot.call_api(
                "group_poke", group_id=int(conv_id), user_id=int(item.payload["qq"])
            )
        except Exception:
            tracelog.qq_out(item.conv, "poke", item.payload["qq"], ok=False, detail="poke failed")
            raise
        tracelog.qq_out(item.conv, "poke", item.payload["qq"])


async def _worker() -> None:
    """Drain the send queue serially, honoring each item's pre-delay."""
    while True:
        item = await _queue.get()
        try:
            await asyncio.sleep(item.pre_delay)
            bot = await _resolve_bot()
            await _send_one(bot, item)
            logging.info(
                f"Sender: sent {item.kind} to {item.conv}"
                + (f" '{str(item.payload.get('text', ''))[:30]}'" if item.kind == "text" else "")
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.warning(f"Sender: failed to send {item.kind} to {item.conv}: {e}")
        finally:
            _queue.task_done()


def start_worker() -> None:
    """Start the send-queue worker (called at startup)."""
    global _worker_task
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_worker())
        logging.info("Sender: worker started")


async def stop_worker() -> None:
    """Stop the worker and drop any unsent items (called at shutdown)."""
    global _worker_task
    if _worker_task is not None:
        _worker_task.cancel()
        try:
            await _worker_task
        except asyncio.CancelledError:
            pass
        _worker_task = None
    while not _queue.empty():
        try:
            _queue.get_nowait()
            _queue.task_done()
        except asyncio.QueueEmpty:
            break
