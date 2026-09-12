"""
Context builder — assembles what the character "sees" each turn.

Combines the rolling buffer (every line stamped with its own [HH:MM]),
a bridge-computed atmosphere read (读空气 signals: density, who spoke
last, whether her own last line got answered, unfinished sentences),
the group roster (codenames + impressions), the sticker catalog digest,
and the turn instruction into one observation block used as the user
message for ST generation. The character/preset/protocol live ST-side;
this module only renders the ever-changing world state.
"""

import re
import time
from datetime import datetime

from . import aliases
from . import collector
from . import config
from . import stickers

# ---------------------------------------------------------------------------
# Records rendering
# ---------------------------------------------------------------------------

def _hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def render_records(messages: list[collector.ConvMsg]) -> str:
    """Render buffered messages, one [HH:MM]-stamped line per message.

    Every message carries its own timestamp so the character can judge
    when each line was said (pace, pauses, who answered whom). Two
    explicit markers remove all ambiguity about who is who:
    - her own entries get「（你）」after the codename
    - messages that @'d the bot get「（@你）」— visible even when the @
      is buried under newer chatter
    """
    lines: list[str] = []
    for m in messages:
        if m.is_self:
            tag = "（你）"
        elif m.is_at:
            tag = "（@你）"
        else:
            tag = ""
        lines.append(f"[{_hhmm(m.ts)}] {m.alias}{tag}：{m.text}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Atmosphere (读空气) signals — computed facts, not opinions
# ---------------------------------------------------------------------------

# Heuristics ported from qq-bridge v2-wait.js: "message tail suggests the
# speaker isn't finished".
_UNFINISHED_TAIL_RE = re.compile(
    r"(?:你知道|等一下|我跟你讲|其实吧|但是|所以说|然后|那个|就是|我想说|对了|等我|"
    r"等等|我看看|还有|再说|主要是|毕竟|因为|所以|但是吧|回头|待会|晚点|等会)$"
)
_UNFINISHED_PUNCT_RE = re.compile(r"[，、；：,;:]$")
_FINISHED_TAIL_RE = re.compile(r"[。！？!?…～~]+$")


def looks_unfinished(text: str) -> bool:
    """True when a message tail suggests the speaker isn't done talking."""
    s = (text or "").strip()
    if not s:
        return False
    if _FINISHED_TAIL_RE.search(s):
        return False
    if _UNFINISHED_TAIL_RE.search(s):
        return True
    return bool(_UNFINISHED_PUNCT_RE.search(s))


def build_atmosphere(conv: str, char_name: str) -> str:
    """Compute a compact room-reading digest for group conversations.

    Returns "" for private chats (nothing to read) or empty buffers.
    Only verifiable facts go in — the judgment stays with the model.
    """
    if conv.startswith("private:"):
        return ""
    msgs = collector.recent(conv, limit=config.ST_CONTEXT_WINDOW)
    if not msgs:
        return ""
    now = time.time()

    lines: list[str] = []
    window = [m for m in msgs if now - m.ts <= 600]
    others = [m for m in window if not m.is_self]

    if others:
        speakers = len({m.alias for m in others})
        pace = "很热闹" if len(others) >= 10 else ("在聊天" if len(others) >= 3 else "有点安静")
        lines.append(f"最近10分钟{speakers}个人说了{len(others)}句话（{pace}）")
        last = msgs[-1]
        ago = max(0, int(now - last.ts))
        when = f"{ago}秒前" if ago < 3600 else f"{ago // 3600}小时前"
        tail = "，话像没说完，可能在等下文" if looks_unfinished(last.text) else ""
        lines.append(f"最后一条是 {last.alias} 在{when}说的{tail}")
    else:
        lines.append("最近10分钟没人说话")

    mine = [m for m in msgs if m.is_self]
    if mine:
        my_last = mine[-1]
        since = int(now - my_last.ts)
        replies = [m for m in msgs if m.ts > my_last.ts and not m.is_self]
        when = f"{since}秒前" if since < 120 else f"{since // 60}分钟前"
        if replies:
            lines.append(f"你上次发言是{when}，之后大家又说了{len(replies)}句话")
        else:
            lines.append(f"你上次发言是{when}，之后没人接你的话")
    else:
        lines.append("你最近没说过话")

    if any(m.is_at for m in others):
        lines.append("有人@了你")

    return "【气氛观察】\n- " + "\n- ".join(lines)


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

def _world_block() -> str:
    """Dynamic world-state preamble: time, roster, sticker catalog."""
    now = datetime.now()
    weekdays = "一二三四五六日"
    parts = [
        f"【当前时间】{now.strftime('%Y-%m-%d %H:%M')}（周{weekdays[now.weekday()]}）",
    ]
    roster = aliases.roster_lines(limit=20)
    if roster:
        parts.append("【群友名册】\n" + "\n".join(roster))
    catalog = stickers.catalog_summary()
    if catalog:
        parts.append("【可用表情】\n" + catalog)
    return "\n\n".join(parts)


def build_observation(
    conv: str,
    char_name: str,
    instruction: str,
    window: int | None = None,
) -> tuple[str, str]:
    """Build the observation user-message for one turn.

    Returns (full_prompt, records_digest):
    - full_prompt: world block + atmosphere + records + instruction
    - records_digest: just the record block (saved into ST history so the
      character remembers what the group talked about in this turn)
    """
    limit = window or config.ST_CONTEXT_WINDOW
    messages = collector.recent(conv, limit=limit)
    records = render_records(messages)
    if not records:
        records = "（最近没有新消息）"

    atmosphere = build_atmosphere(conv, char_name)
    body = f"【群聊记录】\n{records}"
    if atmosphere:
        body = f"{body}\n\n{atmosphere}"
    body = f"{body}\n\n{instruction}"

    full = f"{_world_block()}\n\n{body}"
    return full, records


def build_instruction(tier: str, reason: str, extra: str = "") -> str:
    """Per-turn instruction line appended after the records.

    Kept short and situational — the marker protocol itself lives in the
    post-history contract, not here. Wording pushes room-reading: judge
    the moment first, speaking is optional.
    """
    if reason == "at":
        text = ("记录中标了「（@你）」的消息在等你回应，请优先回应它；"
                "它之后大家聊的新话题如果你也想接，可以接着聊。")
    elif reason == "poke":
        text = "有人在群里戳了戳你。可以回应一句，也可以不理会。"
    elif reason == "private":
        text = "（这是私聊）对方在和你单独聊天，请回应。"
    elif reason == "keyword":
        text = "记录里出现了你感兴趣的话题。先看看这个话题还在不在进行，想接就自然地接一句。"
    elif reason == "batch":
        text = "看一眼大家聊到哪了：判断现在适不适合插话、有没有人在等你说话。不适合开口就 [SILENT]。"
    elif reason == "wake":
        text = "你之前说先潜水，现在时间到了。看看近况，有想接的话再说话，否则 [SILENT]。"
    elif reason == "wait":
        text = "你在等下文，看看有没有新消息。想接话就接，还没等到就 [SILENT] 或继续 [WAIT]。"
    elif reason == "probe":
        text = "群里安静了一会儿。想试探性地说一句就说，不想说就 [SILENT]。"
    elif reason == "exit":
        text = "你聊了一阵子了，自然地说一句收尾的话（比如先去忙/先潜了），之后就安静下来。"
    else:
        text = "看一眼群聊，判断要不要说话；不说话就 [SILENT]。"

    if tier == "light" and reason not in ("at", "private"):
        text += "（简短为要）"
    if extra:
        text += "\n" + extra
    return text
