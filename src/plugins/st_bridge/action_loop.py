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
from . import collector
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

# Defense: the model sometimes renders "read but not speaking" as prose
# (e.g. "（看了眼群消息，没说话）") instead of the [SILENT] marker. Such
# text must never reach QQ as a spoken bubble.
_ACTION_ONLY_RE = re.compile(r"(?:[（(][^（）()]{0,50}[)）][\s，,]*)+")
_ACTION_AST_RE = re.compile(r"\*[^*\n]{0,50}\*")
# Bare-prose variant without brackets: "看了眼群消息，没说话"
_SILENCE_PROSE_RE = re.compile(
    r"^(?:看了[一眼]|瞄[了]?一?眼|扫[了]?一?眼|望[了]?一?眼)?[^。，,]{0,20}[，,]?"
    r"\s*(?:没有?说话|没吭声|保持沉默)[。~～]*$"
)
# Action-ish keywords for stripping a leading "（……）" prefix from real speech
_ACTION_HINT_RE = re.compile(
    r"看|瞄|扫|望|笑|叹|点头|摇头|挠|沉默|没说话|不说话|没吭声|走神"
)

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


def _sanitize_action_prose(actions: Actions) -> None:
    """Keep action-description prose from being sent as a spoken bubble.

    Whole-output action text ("（看了眼群消息，没说话）", "*叹气*") is the
    model's way of saying "read, not replying" -> downgrade to [SILENT].
    A leading action prefix before real speech ("（看了眼群消息）大家好啊")
    is stripped instead, keeping the speech.
    """
    text = actions.text
    if not text:
        return
    if (_ACTION_ONLY_RE.fullmatch(text) or _ACTION_AST_RE.fullmatch(text)
            or _SILENCE_PROSE_RE.fullmatch(text.strip())):
        logging.info(f"ActionLoop: action prose downgraded to silence: {text!r}")
        actions.silent = True
        actions.text = ""
        return
    stripped = text
    while True:
        m = re.match(r"^[（(]([^（）()]{1,24})[)）]\s*", stripped)
        if not m or not _ACTION_HINT_RE.search(m.group(1)):
            break
        stripped = stripped[m.end():]
    if stripped != text:
        logging.info(f"ActionLoop: stripped action prefix -> {stripped.strip()!r}")
        actions.text = stripped.strip()


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
    _sanitize_action_prose(actions)
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


def continuation_wait_seconds(conv: str, max_age: float = 60.0) -> float:
    """Seconds to hold before generating, when the last OTHER-person
    message looks like half of an utterance ("说实话"…).

    QQ users often split one sentence across messages; generating right
    away produced "说实话什么/说完整" interruptions. Waiting a few
    seconds lets the continuation land in the buffer first. Returns 0
    when the last message is hers, too old, or looks complete.
    """
    import time as _time

    last = collector.last(conv)
    if last is None or last.is_self:
        return 0.0
    if _time.time() - last.ts > max_age:
        return 0.0
    if context_builder.looks_unfinished(last.text):
        import random as _random
        return _random.uniform(6.0, 12.0)
    return 0.0


async def _run_turn_locked(conv: str, tier: str, reason: str) -> None:
    gs = state.get_state(conv)
    if not (gs.character_name and gs.preset_name):
        return

    # Stale re-trigger guard: two quick private/@ messages each schedule a
    # turn; the second one would run after the first with nothing new to
    # see and answer the same content all over again.
    if reason in ("private", "at") and not participation.has_unseen_messages(conv):
        logging.info(
            f"ActionLoop: skipped stale {reason} turn on {conv} "
            f"(nothing new since last observed message)"
        )
        return

    # Hold the door for a likely continuation before snapshotting context
    wait = continuation_wait_seconds(conv)
    if wait > 0:
        logging.info(f"ActionLoop: {conv} last message looks unfinished, "
                     f"holding {wait:.1f}s for the follow-up")
        await asyncio.sleep(wait)

    max_tokens = config.ST_LIGHT_TOKENS if tier == "light" else config.ST_HEAVY_TOKENS
    instruction = context_builder.build_instruction(tier, reason)
    observation, digest = context_builder.build_observation(
        conv, gs.character_name, instruction
    )
    snapshot_newest = participation.newest_other_ts(conv)
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

    # The turn answered everything up to the snapshot — stamp the watermark
    # so a queued duplicate turn for the same messages gets skipped.
    participation.mark_seen(conv, snapshot_newest)

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
        if sender.is_repeat(conv, actions.text):
            logging.info(f"ActionLoop: dropped repeat of her recent reply on {conv}")
            actions.text = ""
        else:
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
            # Store the marker, never prose: a prose placeholder ("（看了眼
            # 群消息，没说话）") sat in memory as few-shot and taught the
            # model to emit that text instead of [SILENT].
            assistant_text = "[SILENT]"

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
