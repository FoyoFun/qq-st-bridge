"""
Participation engine — the social strategy layer.

Per-conversation state machine (idle/active/probing/exiting) deciding WHEN
the character looks at the group and WHETHER she speaks:

- idle (观望): only checkpoints trigger generation — @mention (must, heavy),
  N new messages (light), interest keyword (light), [WAKE]/[WAIT] timers.
- active (活跃): she is talking — attention has inertia. Batch-checks new
  messages every 10~30s and replies (heavy). Exits via cold field
  (6 min no messages -> 30% probe) or duration cap (5~10 min -> farewell).
- probing (试探): she said something into the quiet; if nobody responds
  within 2~5 min, back to idle.
- exiting (退场): farewell line queued; returns to idle after the turn.

All generation is delegated to action_loop.run_turn via scheduled tasks;
this module never blocks the message handler.
"""

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import date

from . import collector
from . import config
from . import state

# ---------------------------------------------------------------------------
# Per-conversation runtime state
# ---------------------------------------------------------------------------

@dataclass
class ConvSocialState:
    """Runtime social state for one conversation (group or private)."""
    conv: str
    phase: str = "idle"            # idle | active | probing | exiting
    last_msg_ts: float = 0.0       # ts of last message consumed by a turn
    last_turn_at: float = 0.0      # last checkpoint/turn (idle cooldown anchor)
    # active phase
    active_until: float = 0.0      # duration cap deadline
    next_check_at: float = 0.0     # next batch check
    last_active_msg_at: float = 0.0  # last new-message sighting during active
    # probing
    probe_deadline: float = 0.0
    last_probe_at: float = 0.0
    # [WAKE] / [WAIT] bookings
    wake_at: float = 0.0
    wait_at: float = 0.0
    wake_count_today: int = 0
    wake_day: str = ""
    # re-trigger deferred because a turn was already running
    pending_reason: str = ""


_states: dict[str, ConvSocialState] = {}
_locks: dict[str, asyncio.Lock] = {}
_engine_task: asyncio.Task | None = None


def _get(conv: str) -> ConvSocialState:
    if conv not in _states:
        _states[conv] = ConvSocialState(conv=conv)
    return _states[conv]


def lock_for(conv: str) -> asyncio.Lock:
    """One turn at a time per conversation."""
    if conv not in _locks:
        _locks[conv] = asyncio.Lock()
    return _locks[conv]


def _enabled(conv: str) -> bool:
    gs = state.get_state(conv)
    return bool(gs.social_enabled and gs.character_name and gs.preset_name)


def _interest_hit(text: str, gs) -> bool:
    keywords = list(config.ST_INTEREST_KEYWORDS) + list(gs.interest_keywords)
    return any(kw and kw in text for kw in keywords)


def _today() -> str:
    return date.today().isoformat()


def _fresh_from_others(conv: str, since_ts: float) -> list[collector.ConvMsg]:
    """New messages that came from OTHER people (self never triggers).

    The sender writes her own bubbles back into the buffer (with delays),
    so every trigger/counting path must exclude is_self entries or she
    would respond to herself.
    """
    return [m for m in collector.messages_since(conv, since_ts) if not m.is_self]


# ---------------------------------------------------------------------------
# Turn scheduling
# ---------------------------------------------------------------------------

def schedule_turn(conv: str, tier: str, reason: str, delay: float) -> None:
    """Schedule one generation turn after a human-like delay.

    If a turn is already running for this conversation, defer: record the
    reason and let the engine loop re-trigger when the lock frees up.
    """
    st = _get(conv)
    if lock_for(conv).locked():
        if not st.pending_reason or reason in ("at", "private"):
            st.pending_reason = reason
        logging.info(f"Engine: turn deferred on {conv} ({reason}), will re-trigger")
        return
    # Cooldown anchor starts at schedule time, so a burst of messages cannot
    # schedule several checkpoints back-to-back before the first one runs.
    st.last_turn_at = time.time()
    try:
        asyncio.create_task(_delayed_turn(conv, tier, reason, delay))
    except RuntimeError as e:
        # No running loop — feed() is only ever called from async handlers,
        # so this should not happen; drop the turn rather than crash.
        logging.warning(f"Engine: cannot schedule turn on {conv} ({reason}): {e}")


async def _delayed_turn(conv: str, tier: str, reason: str, delay: float) -> None:
    try:
        await asyncio.sleep(max(0.0, delay))
        if not _enabled(conv):
            return
        from . import action_loop
        await action_loop.run_turn(conv, tier, reason)
    except asyncio.CancelledError:
        raise
    except Exception:
        logging.exception(f"Engine: turn failed on {conv} ({reason})")


# ---------------------------------------------------------------------------
# Inbound hooks (called from chat_handler)
# ---------------------------------------------------------------------------

def feed(msg: collector.ConvMsg, *, is_at_bot: bool = False,
         is_private: bool = False) -> None:
    """Feed one collected message into the engine.

    Cheap by design: idle-phase triggers are evaluated inline; active-phase
    batching is left to the engine tick.
    """
    conv = msg.conv
    st = _get(conv)
    now = msg.ts

    if msg.is_self:
        return  # our own sends never trigger us
    if not _enabled(conv):
        return

    gs = state.get_state(conv)

    # Direct human attention (@/private) supersedes any booked timers:
    # a stale [WAKE]/[WAIT] checkpoint firing right after she answered the
    # @ would only waste a generation and produce an odd "时间到了" line.
    if is_private or is_at_bot:
        st.wake_at = 0.0
        st.wait_at = 0.0

    # Private messages are always delivered immediately (heavy tier)
    if is_private:
        st.last_msg_ts = now
        st.last_active_msg_at = now
        if st.phase == "probing":
            st.phase = "active"
        schedule_turn(conv, "heavy", "private", random.uniform(
            config.ST_REPLY_DELAY_MIN, config.ST_REPLY_DELAY_MAX))
        return

    # @mentions are must-see (heavy tier)
    if is_at_bot:
        st.last_msg_ts = now
        st.last_active_msg_at = now
        if st.phase == "probing":
            st.phase = "active"
        elif st.phase == "idle":
            enter_active(conv)
        schedule_turn(conv, "heavy", "at", random.uniform(
            config.ST_REPLY_DELAY_MIN, config.ST_REPLY_DELAY_MAX))
        return

    # Someone responded during probing -> she is back in the conversation
    if st.phase == "probing":
        st.phase = "active"
        st.active_until = now + random.uniform(config.ST_ACTIVE_MIN, config.ST_ACTIVE_MAX)
        st.next_check_at = now + random.uniform(config.ST_ACTIVE_CHECK_MIN, config.ST_ACTIVE_CHECK_MAX)
        st.last_active_msg_at = now
        return

    st.last_active_msg_at = now

    if st.phase != "idle":
        return  # active/exiting: the tick picks up new messages in batches

    # --- idle-phase checkpoints ---
    fresh = _fresh_from_others(conv, st.last_msg_ts)
    cooldown_ok = (now - st.last_turn_at) >= config.ST_CHECKPOINT_COOLDOWN
    if not cooldown_ok:
        return

    if len(fresh) >= config.ST_CHECKPOINT_THRESHOLD:
        schedule_turn(conv, "light", "batch", random.uniform(1.0, 4.0))
    elif _interest_hit(msg.text, gs):
        schedule_turn(conv, "light", "keyword", random.uniform(2.0, 6.0))


def on_poke(conv: str, poker_alias: str) -> None:
    """Someone poked the bot in a group (must-see, heavy tier)."""
    if not _enabled(conv):
        return
    st = _get(conv)
    now = time.time()
    collector.add_event(conv, poker_alias, "戳了戳你")
    st.last_msg_ts = now
    st.last_active_msg_at = now
    if st.phase == "probing":
        st.phase = "active"
    schedule_turn(conv, "heavy", "poke", random.uniform(1.5, 5.0))


# ---------------------------------------------------------------------------
# State transitions (called by action_loop via record_result)
# ---------------------------------------------------------------------------

def enter_active(conv: str) -> None:
    """She spoke — attention has inertia; enter the active phase."""
    st = _get(conv)
    now = time.time()
    st.phase = "active"
    st.active_until = now + random.uniform(config.ST_ACTIVE_MIN, config.ST_ACTIVE_MAX)
    st.next_check_at = now + random.uniform(config.ST_ACTIVE_CHECK_MIN, config.ST_ACTIVE_CHECK_MAX)
    st.last_active_msg_at = now
    logging.info(f"Engine: {conv} -> active (until +{int(st.active_until - now)}s)")


def record_result(
    conv: str, *, spoke: bool = False, silent: bool = False,
    wait_s: float = 0.0, wake_s: float = 0.0,
) -> None:
    """Update the state machine after a turn resolved."""
    st = _get(conv)
    now = time.time()
    st.last_turn_at = now

    # consume everything buffered up to now — anchored on the last message
    # from OTHERS so her own (asynchronously recorded) bubbles never mark
    # groupie messages as seen; never rewinds
    others = _fresh_from_others(conv, 0.0)
    if others:
        st.last_msg_ts = max(st.last_msg_ts, others[-1].ts)

    if wake_s > 0:
        st.wake_at = now + wake_s
        st.phase = "idle"
        st.probe_deadline = 0.0
        logging.info(f"Engine: {conv} booked [WAKE] in {int(wake_s)}s")
    elif wait_s > 0:
        st.wait_at = now + wait_s
        logging.info(f"Engine: {conv} booked [WAIT] in {int(wait_s)}s")
    elif spoke:
        if st.phase in ("idle", "probing", "exiting"):
            enter_active(conv)
        elif st.phase == "active":
            # refresh inertia window while the conversation keeps flowing
            st.last_active_msg_at = now
    elif silent:
        if st.phase in ("probing", "exiting"):
            st.phase = "idle"
            st.probe_deadline = 0.0
    save_snapshots()


# ---------------------------------------------------------------------------
# Engine tick
# ---------------------------------------------------------------------------

def run_tick() -> None:
    """Scan all conversations: active batching, cold field, probes, timers."""
    now = time.time()
    for st in list(_states.values()):
        conv = st.conv
        if st.wake_day != _today():
            st.wake_day = _today()
            st.wake_count_today = 0
        if not _enabled(conv):
            continue

        # pending re-trigger from a deferred turn
        if st.pending_reason and not lock_for(conv).locked():
            reason = st.pending_reason
            st.pending_reason = ""
            schedule_turn(conv, "heavy", reason, 1.0)
            continue

        if st.phase == "active":
            _tick_active(st, conv, now)
        elif st.phase == "probing":
            if now >= st.probe_deadline:
                st.phase = "idle"
                st.probe_deadline = 0.0
                logging.info(f"Engine: {conv} probe unanswered -> idle")

        # timers fire in any phase
        if st.wait_at and now >= st.wait_at:
            st.wait_at = 0.0
            schedule_turn(conv, "light", "wait", 0.5)
        if st.wake_at and now >= st.wake_at:
            st.wake_at = 0.0
            if st.wake_count_today >= config.ST_WAKE_DAILY_LIMIT:
                logging.info(f"Engine: {conv} wake skipped (daily limit)")
            else:
                st.wake_count_today += 1
                schedule_turn(conv, "light", "wake", 0.5)


def _tick_active(st: ConvSocialState, conv: str, now: float) -> None:
    gs = state.get_state(conv)

    # Duration cap: she has been talking too long — natural farewell
    # (groups only; private chats quietly go idle instead)
    if st.active_until and now >= st.active_until:
        if conv.startswith("group:"):
            st.phase = "exiting"
            schedule_turn(conv, "heavy", "exit", random.uniform(
                config.ST_REPLY_DELAY_MIN, config.ST_REPLY_DELAY_MAX))
            logging.info(f"Engine: {conv} active duration cap -> exiting")
        else:
            st.phase = "idle"
            logging.info(f"Engine: {conv} active duration cap -> idle (private)")
        return

    if now < st.next_check_at:
        return
    st.next_check_at = now + random.uniform(
        config.ST_ACTIVE_CHECK_MIN, config.ST_ACTIVE_CHECK_MAX)

    fresh = _fresh_from_others(conv, st.last_msg_ts)
    if fresh:
        st.last_active_msg_at = now
        # advance the watermark past everything currently buffered
        latest = collector.last(conv)
        if latest is not None:
            st.last_msg_ts = max(st.last_msg_ts, latest.ts)
        schedule_turn(conv, "heavy", "batch", random.uniform(
            config.ST_REPLY_DELAY_MIN, config.ST_REPLY_DELAY_MAX))
        return

    # Cold field: nobody said anything for a while
    if now - st.last_active_msg_at >= config.ST_COLD_WINDOW:
        can_probe = (now - st.last_probe_at) >= config.ST_PROBE_COOLDOWN
        if can_probe and random.randint(1, 100) <= config.ST_PROBE_PROBABILITY:
            st.phase = "probing"
            st.probe_deadline = now + random.uniform(
                config.ST_PROBE_WAIT_MIN, config.ST_PROBE_WAIT_MAX)
            st.last_probe_at = now
            schedule_turn(conv, "light", "probe", random.uniform(
                config.ST_REPLY_DELAY_MIN, config.ST_REPLY_DELAY_MAX))
            logging.info(f"Engine: {conv} cold field -> probing")
        else:
            st.phase = "idle"
            logging.info(f"Engine: {conv} cold field -> idle")


async def engine_loop() -> None:
    """Background loop driving the state machine."""
    while True:
        try:
            run_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Engine: tick error")
        await asyncio.sleep(config.ST_TICK_INTERVAL)


def start_engine() -> None:
    global _engine_task
    if _engine_task is None or _engine_task.done():
        _engine_task = asyncio.create_task(engine_loop())
        logging.info("Engine: participation engine started")


async def stop_engine() -> None:
    global _engine_task
    if _engine_task is not None:
        _engine_task.cancel()
        try:
            await _engine_task
        except asyncio.CancelledError:
            pass
        _engine_task = None


# ---------------------------------------------------------------------------
# Snapshot persistence (phase + timers survive restarts)
# ---------------------------------------------------------------------------

_SNAPSHOT_FILE = None


def _snapshot_path() -> str:
    import os
    global _SNAPSHOT_FILE
    if _SNAPSHOT_FILE is None:
        _SNAPSHOT_FILE = os.path.join(
            os.path.dirname(__file__), "..", "..", "..", "data", "social_states.json"
        )
    return _SNAPSHOT_FILE


def save_snapshots() -> None:
    """Persist minimal state-machine snapshots to disk."""
    import json
    try:
        data = {}
        for st in _states.values():
            if st.phase == "idle" and not st.wake_at and not st.wait_at:
                continue
            data[st.conv] = {
                "phase": st.phase,
                "wake_at": st.wake_at,
                "wait_at": st.wait_at,
                "wake_count_today": st.wake_count_today,
                "wake_day": st.wake_day,
            }
        path = _snapshot_path()
        import os
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.warning(f"Engine: failed to save snapshots: {e}")


def load_snapshots() -> None:
    """Restore state-machine snapshots at startup."""
    import json
    try:
        path = _snapshot_path()
        if not path or not __import__("os").path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for conv, fields in data.items():
            st = _get(conv)
            st.phase = str(fields.get("phase", "idle"))
            st.wake_at = float(fields.get("wake_at", 0.0))
            st.wait_at = float(fields.get("wait_at", 0.0))
            st.wake_count_today = int(fields.get("wake_count_today", 0))
            st.wake_day = str(fields.get("wake_day", ""))
        logging.info(f"Engine: restored {len(data)} conversation snapshot(s)")
    except Exception as e:
        logging.warning(f"Engine: failed to load snapshots: {e}")
