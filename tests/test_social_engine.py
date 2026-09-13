"""
Closed-loop test for the social engine — no QQ, no ST, no network.

Run from the project root:
    python tests/test_social_engine.py

Pipes fake data through the full pipeline:
collector buffer -> context observation -> participation triggers ->
action parsing -> send-bubble splitting -> state-machine transitions.
External effects (ST calls, QQ sends) are stubbed out and recorded.
"""

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import nonebot  # noqa: E402

nonebot.init()  # the plugin package __init__ calls get_driver()

from plugins.st_bridge import (  # noqa: E402
    action_loop,
    aliases,
    collector,
    config,
    context_builder,
    participation,
    sender,
    state,
    stickers,
    tracelog,
)
from plugins.st_bridge.state import GroupState  # noqa: E402


class FakeSender:
    """event.sender stand-in"""

    def __init__(self, card="", nickname=""):
        self.card = card
        self.nickname = nickname


class FakeEvent:
    """GroupMessageEvent stand-in with the attrs the collector touches."""

    def __init__(self, group_id, user_id, message, card="", nickname=""):
        self.group_id = group_id
        self.user_id = user_id
        self.message = message
        self.sender = FakeSender(card, nickname)
        self.to_me = False
        self.self_id = 999


TURN_LOG: list[tuple] = []


async def fake_run_turn(conv, tier, reason):
    TURN_LOG.append((conv, tier, reason, time.time()))
    # simulate having spoken -> engine enters active
    participation.record_result(conv, spoke=True)


def push(conv, qq, alias, text):
    """Append a message to the collector buffer, then feed the engine."""
    msg = collector.ConvMsg(conv=conv, qq=qq, alias=alias, text=text, ts=time.time())
    collector._buffer(conv).append(msg)
    return msg


def make_msg(text):
    from nonebot.adapters.onebot.v11 import Message, MessageSegment
    return Message([MessageSegment.text(text)])


async def main():  # noqa: C901
    # Snapshot data/ BEFORE touching anything: pre-existing files (real
    # runtime state written by the live bot) are restored byte-for-byte
    # afterwards; files the test created are deleted.
    data_dir = os.path.join(os.path.dirname(__file__), "..", "data")
    snapshot: dict[str, bytes | None] = {}
    for root, _dirs, files in os.walk(data_dir):
        for name in files:
            path = os.path.abspath(os.path.join(root, name))
            with open(path, "rb") as f:
                snapshot[path] = f.read()

    conv = "group:123"
    ok = 0

    # ---- 1. aliases: stable codenames ----
    a1 = aliases.get_alias(111, "阿伟12345")
    a2 = aliases.get_alias(111, "阿伟改名了")
    assert a1 == a2, f"alias not stable: {a1} vs {a2}"
    a3 = aliases.get_alias(222, a1)  # collision -> must differ
    assert a3 != a1, "alias collision not resolved"
    assert aliases.resolve_qq(a3) == "222", "reverse lookup failed"
    ok += 1
    print(f"[1] aliases OK: {a1} (stable), {a3} (dedup)")

    # ---- 2. stickers: catalog add/find ----
    os.makedirs("data/stickers", exist_ok=True)
    img = os.path.abspath("data/stickers/_test.gif")
    with open(img, "wb") as f:
        f.write(b"GIF89a-test")
    tag = stickers.add("开心", img, "高兴的时候用")
    assert stickers.find("开心") is not None
    assert stickers.find("开") is not None          # substring match
    assert stickers.find("不存在") is None
    summary = stickers.catalog_summary()
    assert "开心" in summary
    ok += 1
    print(f"[2] stickers OK: tag={tag}, summary has tag")

    # ---- 3. collector: segments -> buffer ----
    ev = FakeEvent(123, 111, make_msg("今天好累啊"), card=a1)
    msg = collector.collect_group(ev)
    assert msg and msg.alias == a1 and msg.text == "今天好累啊"
    collector.record_self(conv, "辛苦啦~", "静流")
    assert collector.last(conv).is_self
    assert len(collector.recent(conv)) == 2
    ok += 1
    print("[3] collector OK: buffer + self-recording")

    # ---- 4. context_builder: records + time markers + observation ----
    future = time.time() - 700  # >10 min gap -> [HH:MM] marker expected
    old = collector.ConvMsg(conv=conv, qq="333", alias="小红",
                            text=" earlier msg ", ts=future)
    collector._buffer(conv).insert(0, old)
    records = context_builder.render_records(collector.recent(conv))
    assert "[HH" in records or "[" in records, "time marker missing"
    obs, digest = context_builder.build_observation(
        conv, "静流", "看一眼群聊。")
    assert "【群聊记录】" in obs and "静流（你）：" in obs
    assert "【当前时间】" in obs
    instr = context_builder.build_instruction("light", "probe")
    assert "[SILENT]" in instr
    ok += 1
    print("[4] context_builder OK: markers + observation + roster/sticker blocks")

    # ---- 5. sender: bubble splitting ----
    b = sender.plan_bubbles("今天天气超好 我们去公园吧 玩一整天")
    assert b == ["今天天气超好", "我们去公园吧", "玩一整天"], b
    latin = sender.plan_bubbles("use some english words here")
    assert latin == ["use some english words here"], latin
    long_guard = sender.plan_bubbles("哈" * 1200)
    assert len(long_guard) == 3 and all(
        len(x) <= config.ST_MAX_MSG_CHARS for x in long_guard)
    ok += 1
    print(f"[5] sender OK: CJK split={len(b)}, latin kept, hard-split={len(long_guard)}")

    # ---- 5b. sender: repeat guard (enqueue-time registry) ----
    conv_r = "group:repeat-test"
    sender._recent_replies.pop(conv_r, None)
    reply = "呵呵，天使哪有我这么爱走神的"
    assert not sender.is_repeat(conv_r, reply)
    assert sender.enqueue_text(conv_r, reply, "静流") == 1
    assert sender.is_repeat(conv_r, reply)
    assert sender.is_repeat(conv_r, "呵呵，天使哪有我这么爱走神的 ")  # whitespace-insensitive
    assert not sender.is_repeat(conv_r, "今天天气不错")
    while not sender._queue.empty():
        sender._queue.get_nowait()
        sender._queue.task_done()
    sender._recent_replies.pop(conv_r, None)
    ok += 1
    print("[5b] sender OK: repeat guard detects enqueue-time duplicates")

    # ---- 6. action_loop: marker parsing ----
    p = action_loop.parse_actions
    r = p("[SILENT]")
    assert r.silent and not r.text
    r = p("好耶 太棒了 [STICKER: 开心]")
    assert r.text == "好耶 太棒了" and r.stickers == ["开心"], r
    r = p("[WAIT 90s]")
    assert r.wait_s == 90
    r = p("[WAKE 30m]")
    assert r.wake_s == 1800
    r = p("拍你一下 [POKE: 阿伟] [POKE: 小红]")
    assert r.pokes == ["阿伟", "小红"] and r.text == "拍你一下", r
    r = p("正常聊天 不分条")
    assert r.text == "正常聊天 不分条" and not r.spoke or r.spoke
    # action-prose defense: silence rendered as prose must never reach QQ
    r = p("（看了眼群消息，没说话）")
    assert r.silent and not r.text and not r.spoke, r
    r = p("（看了看大家，没说话）")
    assert r.silent and not r.text, r
    r = p("[SILENT]（看了眼群消息，没说话）")
    assert r.silent and not r.text, r
    r = p("*看了看群消息，没有说话*")
    assert r.silent and not r.text, r
    r = p("看了眼群消息，没说话。")
    assert r.silent and not r.text, r
    # leading action prefix is stripped, speech kept
    r = p("（看了眼群消息）大家好啊")
    assert not r.silent and r.text == "大家好啊", r
    # parenthetical without action hints (or not leading) stays untouched
    r = p("笑死（不是）")
    assert r.text == "笑死（不是）", r
    r = p("（划掉）其实我想说的是这个")
    assert r.text == "（划掉）其实我想说的是这个", r
    ok += 1
    print("[6] parse_actions OK: SILENT/WAIT/WAKE/STICKER/POKE + text strip + action-prose defense")

    # ---- 7. participation: triggers + state machine ----
    action_loop.run_turn = fake_run_turn  # stub external effects
    state._states[conv] = GroupState(
        social_enabled=True, character_name="静流",
        preset_name="QQ群聊角色扮演",
    )
    state._states["private:555"] = GroupState(
        social_enabled=True, character_name="静流",
        preset_name="QQ群聊角色扮演",
    )
    # 7a. threshold trigger: 3 fresh messages -> light checkpoint
    for i in range(3):
        participation.feed(push(conv, "1", "甲", f"第{i}条测试消息"))
    # 7b. @ trigger -> heavy 'at'
    participation.feed(push(conv, "2", "乙", "@静流 在吗"), is_at_bot=True)
    # 7c. private -> heavy
    participation.feed(push("private:555", "555", "丙", "在吗"), is_private=True)

    async def drive():
        # let scheduled tasks run (delays up to ~8s)
        await asyncio.sleep(9)

    await drive()

    reasons = [t[2] for t in TURN_LOG]
    assert "at" in reasons, f"@ trigger missing: {TURN_LOG}"
    assert "private" in reasons, f"private trigger missing: {TURN_LOG}"
    assert "batch" in reasons, f"threshold trigger missing: {TURN_LOG}"
    tiers = {t[2]: t[1] for t in TURN_LOG}
    assert tiers["at"] == "heavy" and tiers["private"] == "heavy"
    assert tiers["batch"] == "light"
    # record_result(spoke=True) was called -> all these convs are active
    assert participation._get(conv).phase == "active"
    ok += 1
    print(f"[7] participation OK: triggers={reasons}, phase=active")

    # ---- 8. active batch + cold field + wake timer ----
    st = participation._get(conv)
    st.next_check_at = time.time() - 1
    participation.feed(push(conv, "1", "甲", "回个话呗朋友"))
    participation.run_tick()
    await asyncio.sleep(10)  # batch turn delay is 2~8s
    assert any(t[2] == "batch" and t[1] == "heavy" for t in TURN_LOG), TURN_LOG

    st.last_active_msg_at = time.time() - config.ST_COLD_WINDOW - 10
    st.next_check_at = time.time() - 1
    st.last_probe_at = 0
    participation.run_tick()  # cold -> probe(30%) or idle
    assert st.phase in ("probing", "idle"), st.phase
    await asyncio.sleep(9)  # probe turn (if scheduled) delay is 2~8s

    st.phase = "idle"
    st.wake_at = time.time() - 1
    st.last_turn_at = 0
    n_before = len(TURN_LOG)
    participation.run_tick()
    assert st.wake_at == 0
    await asyncio.sleep(2)  # wake turn delay is 0.5s
    assert any(t[2] == "wake" for t in TURN_LOG[n_before:])
    ok += 1
    print("[8] tick OK: active batch / cold field / wake timer")

    # ---- 9. snapshots round-trip ----
    st.wake_at = time.time() + 600
    st.phase = "idle"
    participation.save_snapshots()
    participation._states.pop(conv)
    participation.load_snapshots()
    assert participation._get(conv).wake_at > time.time()
    ok += 1
    print("[9] snapshots OK: persist + restore")

    # ---- 10. tracelog: four stages land in one line each ----
    import tempfile
    trace_dir = os.path.join(tempfile.mkdtemp(prefix="trace_test_"))
    tracelog.setup(trace_dir, trace_enabled=True, trace_max_chars=500, keep_days=7)
    tracelog.qq_in(conv, "阿伟", "第一行\n第二行")
    tracelog.st_request(conv, "heavy", "at", 42, "【群聊记录】\n阿伟：hi", 250)
    tracelog.st_response(conv, True, "好耶 [STICKER: 开心]")
    tracelog.st_response(conv, False, "", error="HTTP 500")
    tracelog.qq_out(conv, "text", "好耶", ok=True)
    tracelog.qq_out(conv, "poke", "111", ok=False, detail="poke failed")
    for handler in tracelog._trace_logger.handlers:
        handler.flush()
    with open(os.path.join(trace_dir, "trace.log"), encoding="utf-8") as f:
        lines = [line.rstrip("\n") for line in f if line.strip()]
    body = "\n".join(lines)
    assert any("QQ◀" in l and "第一行\\n第二行" in l for l in lines), body
    assert any("ST▶" in l and "tier=heavy" in l and "history=42" in l for l in lines), body
    assert any("ST◀" in l and "ok=True" in l and "STICKER" in l for l in lines), body
    assert any("ST◀" in l and "ok=False" in l and "HTTP 500" in l for l in lines), body
    assert any("QQ▶" in l and "text" in l and "好耶" in l for l in lines), body
    assert any("QQ▶" in l and "poke" in l and "FAILED" in l for l in lines), body
    # truncation: a huge message must not blow up the line cap
    tracelog.qq_in(conv, "阿伟", "长" * 5000)
    for handler in tracelog._trace_logger.handlers:
        handler.flush()
    with open(os.path.join(trace_dir, "trace.log"), encoding="utf-8") as f:
        last_line = [l for l in f if l.strip()][-1]
    assert len(last_line) < 600 and "截断" in last_line
    ok += 1
    print(f"[10] tracelog OK: {len(lines)} records, one-line + truncation")

    # ---- 11. self messages NEVER trigger a turn (regression) ----
    conv2 = "group:777"
    state._states[conv2] = GroupState(
        social_enabled=True, character_name="静流", preset_name="QQ群聊角色扮演",
    )
    st2 = participation._get(conv2)
    st2.phase = "active"
    st2.last_msg_ts = time.time() - 60
    st2.last_active_msg_at = time.time() - 60
    n_self = len(TURN_LOG)
    # her own bubbles land in the buffer (async, like the real sender)
    collector.record_self(conv2, "我自己说的话\n第二条", "静流")
    st2.next_check_at = time.time() - 1
    participation.run_tick()
    await asyncio.sleep(10)
    assert len(TURN_LOG) == n_self, f"self message triggered a turn: {TURN_LOG[n_self:]}"
    # a real groupie message still triggers the active batch
    participation.feed(push(conv2, "9", "丁", "有人说话了吗"))
    st2.next_check_at = time.time() - 1
    participation.run_tick()
    await asyncio.sleep(10)
    assert any(t[0] == conv2 for t in TURN_LOG[n_self:]), "groupie message lost"
    ok += 1
    print("[11] self-trigger regression OK: own bubbles never fire, others do")

    # ---- 12. per-line timestamps + atmosphere + burst single record ----
    rec = context_builder.render_records(collector.recent(conv2))
    # two buffer entries (her burst + 丁), each entry starts with [HH:MM];
    # her burst stays ONE entry even though it spans two physical lines
    entry_lines = [l for l in rec.splitlines() if l.startswith("[")]
    assert len(entry_lines) == 2, rec
    assert "我自己说的话\n第二条" in rec

    assert context_builder.looks_unfinished("我跟你讲") is True
    assert context_builder.looks_unfinished("那个，") is True
    assert context_builder.looks_unfinished("好的。") is False

    conv3 = "group:888"
    state._states[conv3] = GroupState(
        social_enabled=True, character_name="静流", preset_name="QQ群聊角色扮演",
    )
    push(conv3, "1", "甲", "今天好累")
    push(conv3, "2", "乙", "怎么了")
    atmo = context_builder.build_atmosphere(conv3, "静流")
    assert "【气氛观察】" in atmo and "最后一条" in atmo and "你最近没说过话" in atmo, atmo
    obs3, _ = context_builder.build_observation(conv3, "静流", "看一眼。")
    assert "【气氛观察】" in obs3 and "【群友名册】" in obs3

    # enqueue a 3-bubble burst -> 3 queue items, ONE record on the last
    n_bubbles = sender.enqueue_text(conv3, "第一句 第二句 第三句", "静流")
    assert n_bubbles == 3
    items = [sender._queue.get_nowait() for _ in range(3)]
    assert [i.record_text for i in items] == ["", "", "第一句\n第二句\n第三句"]
    ok += 1
    print("[12] timestamps/atmosphere/burst-record OK")

    # ---- 13. @/self markers + nickname tracking + timer supersede ----
    ev_at = FakeEvent(123, 111, make_msg("今天天气如何"), card="阿伟新昵称")
    msg_at = collector.collect_group(ev_at, text_override="今天天气如何", is_at=True)
    assert msg_at.is_at
    rec_at = context_builder.render_records([msg_at])
    assert "（@你）" in rec_at and rec_at.startswith("["), rec_at
    rec_self = context_builder.render_records([
        collector.ConvMsg(conv=conv, qq="", alias="静流", text="哈哈",
                          ts=time.time(), is_self=True)
    ])
    assert "静流（你）：哈哈" in rec_self, rec_self

    # nickname change is tracked, codename stays frozen
    member = aliases.get_member(111)
    assert member.alias == a1, "codename must not follow nickname changes"
    assert member.last_display == "阿伟新昵称"
    assert any("现用昵称：阿伟新昵称" in l for l in aliases.roster_lines())

    # @ supersedes booked WAKE/WAIT timers
    conv4 = "group:999"
    state._states[conv4] = GroupState(
        social_enabled=True, character_name="静流", preset_name="QQ群聊角色扮演",
    )
    st4 = participation._get(conv4)
    st4.wake_at = time.time() + 600
    st4.wait_at = time.time() + 300
    participation.feed(push(conv4, "5", "戊", "@静流 看我看我"), is_at_bot=True)
    assert st4.wake_at == 0.0 and st4.wait_at == 0.0
    await asyncio.sleep(9)  # let the scheduled @ turn (stubbed) run out
    ok += 1
    print("[13] @/self markers + nickname tracking + timer supersede OK")

    # ---- 14. unfinished utterances: detect + hold, never interrupt ----
    lu = context_builder.looks_unfinished
    assert lu("说实话") is True            # lead-in opener (the live bug)
    assert lu("问一下") is True
    assert lu("我跟你们说") is True
    assert lu("那个，") is True            # open punctuation
    assert lu("我还真想整个比较温柔的人格") is False   # the completion itself
    assert lu("感觉不对了") is False       # complete statement, not flagged
    assert lu("哈哈哈哈") is False         # pure reaction
    assert lu("确实") is False
    assert lu("好的。") is False           # terminal punctuation

    # hold-before-generate: unfinished tail -> 6~12s, complete/stale -> 0
    push(conv2, "9", "丁", "说实话")
    w = action_loop.continuation_wait_seconds(conv2)
    assert 6.0 <= w <= 12.0, w
    push(conv2, "9", "丁", "就这样吧 你说呢。")
    assert action_loop.continuation_wait_seconds(conv2) == 0.0
    stale = collector.ConvMsg(conv=conv2, qq="9", alias="丁", text="说实话",
                              ts=time.time() - 120)
    collector._buffer(conv2).append(stale)
    assert action_loop.continuation_wait_seconds(conv2) == 0.0
    # checkpoint delay for unfinished triggers is the longer window
    d = participation._checkpoint_delay(
        collector.ConvMsg(conv=conv2, qq="9", alias="丁", text="说实话", ts=time.time()))
    assert 8.0 <= d <= 15.0, d
    ok += 1
    print("[14] unfinished-utterance OK: detect/hold/no-interrupt")

    print(f"\nALL {ok}/{ok} CLOSED-LOOP TESTS PASSED")

    # Restore data/ to its pre-test state: delete files the test created,
    # restore pre-existing files byte-for-byte (the live bot's runtime
    # stores must survive test runs untouched).
    import shutil
    removed = 0
    restored = 0
    for root, _dirs, files in os.walk(data_dir, topdown=False):
        for name in files:
            path = os.path.abspath(os.path.join(root, name))
            if path in snapshot:
                if snapshot[path] is not None:
                    with open(path, "wb") as f:
                        f.write(snapshot[path])  # restore original content
                    restored += 1
                else:
                    os.remove(path)
                    removed += 1
            else:
                os.remove(path)  # created by the test
                removed += 1
        if not os.listdir(root) and os.path.abspath(root) != os.path.abspath(data_dir):
            shutil.rmtree(root, ignore_errors=True)
    print(f"cleanup: removed {removed}, restored {restored} pre-existing file(s)")


if __name__ == "__main__":
    asyncio.run(main())
