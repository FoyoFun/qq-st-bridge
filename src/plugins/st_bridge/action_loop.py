"""
Action loop — one "look at the group" lifetime.

Pipeline: build observation -> call ST (light/heavy tier) -> parse the
marker protocol -> dispatch actions (text burst / sticker / poke /
[WAIT] / [WAKE]) -> record the result into the participation engine ->
save a compact exchange into ST chat history as long-term memory.

Loop guard: at most config.ST_MAX_STEPS generation calls per turn; the
per-conversation lock (participation.lock_for) guarantees one turn at a
time, and the global ST lock in st_client serializes actual generation.
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field

from . import chat_utils
from . import config
from . import context_builder
from . import participation
from . import sender
from . import state
from . import st_api
from . import stickers
from . import aliases

# ---------------------------------------------------------------------------
# Marker protocol parsing
# ---------------------------------------------------------------------------

_SILENT_RE = re.compile(r"\[\s*SILENT\s*\]", re.IGNORECASE)
_WAIT_RE = re.compile(r"\[\s*WAIT\s+(\d+)\s*([sm分钟秒h时]?)\s*\]", re.IGNORECASE)
_WAKE_RE = re.compile(r"\[\s*WAKE\s+(\d+)\s*([sm分钟秒h时]?)\s*\]", re.IGNORECASE)
_STICKER_RE = re.compile(r"\[\s*STICKER\s*[:：]\s*([^\]]+?)\s*\]", re.IGNORECASE)
_POKE_RE = re.compile(r"\[\s*POKE\s*[:：]\s*([^\]]+?)\s*\]", re.IGNORECASE)
_MARKER_RES = (_SILENT_RE, _WAIT_RE, _WAKE_RE, _STICKER_RE, _POKE_RE)

_UNIT_SECONDS = {
    "": 60.0, "s": 1.0, "秒": 1.0,
    "m": 60.0, "分": 60.0, "分钟": 60.0,
    "h": 3600.0, "时": 3600.0, "小时": 3600.0,
}


@dataclass
class Actions:
    """Parsed model output."""
    silent: bool = False
    wait_s: float = 0.0
    wake_s: float = 0.0
    stickers: list[str] = field(default_factory=list)
    pokes: list[str] = field(default_factory=list)
    text: str = ""

    @property
    def spoke(self) -> bool:
        return bool(self.text.strip() or self.stickers)


def _to_seconds(num: str, unit: str) -> float:
    return float(num) * _UNIT_SECONDS.get(unit, 60.0)


def parse_actions(raw: str) -> Actions:
    """Extract all protocol markers and the remaining spoken text."""
    actions = Actions()
    text = raw or ""

    if _SILENT_RE.search(text):
        actions.silent = True

    wait = _WAIT_RE.search(text)
    if wait:
        actions.wait_s = min(_to_seconds(wait.group(1), wait.group(2)), 3600.0)
    wake = _WAKE_RE.search(text)
    if wake:
        # Cost guardrails: WAKE is clamped to [.env ST_WAKE_MIN, ST_WAKE_MAX]
        raw_s = _to_seconds(wake.group(1), wake.group(2))
        actions.wake_s = max(
            float(config.ST_WAKE_MIN),
            min(raw_s, float(config.ST_WAKE_MAX)),
        )

    actions.stickers = [m.strip() for m in _STICKER_RE.findall(text)]
    actions.pokes = [m.strip() for m in _POKE_RE.findall(text)]

    for pattern in _MARKER_RES:
        text = pattern.sub("", text)
    actions.text = text.strip()
    return actions


# ---------------------------------------------------------------------------
# ST chat history helpers
# ---------------------------------------------------------------------------

_HISTORY_KEEP = 80          # max entries sent to the plugin
_AUTO_USER = "群聊"


async def _load_history(gs) -> list[dict]:
    """Load (or start) the ST chat file, trimmed for context."""
    chat_data: list[dict] = []
    if gs.chat_file:
        loaded = await st_api.load_chat(gs.avatar_url, gs.chat_file)
        if loaded:
            chat_data = loaded
    if not chat_data:
        gs.chat_file = chat_utils.new_chat_filename(gs.character_name)
        state.save_states()
        chat_data = [chat_utils.make_chat_header(_AUTO_USER, gs.character_name)]
    return chat_data


def _trim_history(chat_data: list[dict]) -> list[dict]:
    """Keep the header plus the most recent messages."""
    header = [m for m in chat_data if "chat_metadata" in m]
    messages = [m for m in chat_data if "chat_metadata" not in m]
    return header + messages[-_HISTORY_KEEP:]


# ---------------------------------------------------------------------------
# Turn execution
# ---------------------------------------------------------------------------

async def run_turn(conv: str, tier: str, reason: str) -> None:
    """Execute one full turn (single generation + action dispatch)."""
    async with participation.lock_for(conv):
        await _run_turn_locked(conv, tier, reason)


async def _run_turn_locked(conv: str, tier: str, reason: str) -> None:
    gs = state.get_state(conv)
    if not (gs.character_name and gs.preset_name):
        return

    max_tokens = config.ST_LIGHT_TOKENS if tier == "light" else config.ST_HEAVY_TOKENS
    instruction = context_builder.build_instruction(tier, reason)
    observation, digest = context_builder.build_observation(
        conv, gs.character_name, instruction
    )
    history = _trim_history(await _load_history(gs))

    try:
        result = await st_api.plugin_generate(
            avatar_url=gs.avatar_url,
            preset_name=gs.preset_name,
            chat_history=history,
            user_message=observation,
            user_name=_AUTO_USER,
            character_name=gs.character_name or "",
            max_response_length=max_tokens,
            post_history=config.POST_HISTORY_CONTRACT,
            conv=conv,
            tier=tier,
            reason=reason,
        )
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logging.warning(f"ActionLoop: ST call failed on {conv} ({reason}): {e}")
        if reason in ("at", "private", "poke"):
            await _send_notice(conv, "（刚刚走神了没听到……再叫我一次？）")
        return

    if not result.get("success"):
        logging.warning(
            f"ActionLoop: ST error on {conv} ({reason}): {result.get('error')}"
        )
        if reason in ("at", "private", "poke"):
            await _send_notice(conv, "（刚刚走神了没听到……再叫我一次？）")
        return

    raw = result.get("response_text", "") or ""
    actions = parse_actions(raw)
    logging.info(
        f"ActionLoop: turn on {conv} ({reason}, {tier}) -> "
        f"silent={actions.silent} text={bool(actions.text)} "
        f"stickers={actions.stickers} pokes={actions.pokes} "
        f"wait={actions.wait_s} wake={actions.wake_s}"
    )

    # --- dispatch actions ---
    spoken_parts: list[str] = []

    if actions.text:
        n = sender.enqueue_text(conv, actions.text, gs.character_name)
        spoken_parts.extend(_last_bubbles(n, actions.text))

    for tag in actions.stickers:
        entry = stickers.find(tag)
        if entry is not None:
            sender.enqueue_sticker(
                conv, stickers.file_uri(entry), entry.tag, gs.character_name
            )
            spoken_parts.append(f"[表情：{entry.tag}]")
            stickers.mark_used(entry)
        else:
            logging.info(f"ActionLoop: sticker tag '{tag}' not found, skipped")

    for alias in actions.pokes:
        qq = aliases.resolve_qq(alias)
        if qq is not None:
            sender.enqueue_poke(conv, qq)
            spoken_parts.append(f"（拍了拍 {alias}）")
        else:
            logging.info(f"ActionLoop: poke alias '{alias}' unresolved, skipped")

    # --- update the state machine ---
    participation.record_result(
        conv,
        spoke=actions.spoke,
        silent=actions.silent and not actions.spoke,
        wait_s=actions.wait_s,
        wake_s=actions.wake_s,
    )

    # --- persist the exchange into ST chat memory ---
    if actions.spoke or actions.silent:
        await _save_exchange(gs, digest, spoken_parts, actions, history)


def _last_bubbles(n: int, text: str) -> list[str]:
    """Recover the planned bubbles for history recording."""
    from .sender import plan_bubbles
    return plan_bubbles(text)[:n]


async def _save_exchange(
    gs, digest: str, spoken_parts: list[str], actions: Actions,
    history: list[dict],
) -> None:
    """Append user digest + assistant reply to the ST chat file.

    The assistant entry stores what she actually "said" (markers stripped),
    so long-term memory stays dialog-shaped.
    """
    try:
        user_text = f"【群聊】\n{digest}"[:2000]
        assistant_text = " ".join(p for p in spoken_parts if p).strip()
        if not assistant_text:
            assistant_text = "（看了眼群消息，没说话）"

        history = list(history)
        if not any("chat_metadata" in m for m in history):
            history.insert(
                0, chat_utils.make_chat_header(_AUTO_USER, gs.character_name)
            )
        history.append(chat_utils.make_chat_message(_AUTO_USER, True, user_text))
        history.append(
            chat_utils.make_chat_message(gs.character_name, False, assistant_text)
        )
        await st_api.save_chat(gs.avatar_url, gs.chat_file, history)
    except Exception:
        logging.exception("ActionLoop: failed to save chat history")


async def _send_notice(conv: str, text: str) -> None:
    """Best-effort plain notice for triggers that came from a real user."""
    try:
        from nonebot import get_bot
        bot = get_bot()
        conv_type, conv_id = conv.split(":", 1)
        if conv_type == "group":
            await bot.send_group_msg(group_id=int(conv_id), message=text)
        else:
            await bot.send_private_msg(user_id=int(conv_id), message=text)
    except Exception as e:
        logging.warning(f"ActionLoop: failed to send notice to {conv}: {e}")
