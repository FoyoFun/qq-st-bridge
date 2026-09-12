"""
Command handler functions — one per /command.

Each function is self-contained: takes inputs, calls st_api / state /
aliases / stickers as needed, and always returns a response string.
Exception handling is internal so the caller never sees raw tracebacks.
"""

import logging
import time

import httpx

from . import aliases
from . import chat_utils
from . import config
from . import participation
from . import st_api
from . import state
from . import stickers


async def cmd_chars() -> str:
    """List all available characters."""
    try:
        chars = await st_api.get_characters()
        if not chars:
            return "SillyTavern 中没有找到角色。"

        lines = ["=== 可用角色 ==="]
        for i, c in enumerate(chars, 1):
            name = c.get("name", "未知")
            lines.append(f"{i}. {name}")
        lines.append("\n使用 /char <名称> 选择角色")
        return "\n".join(lines)
    except httpx.ConnectError:
        return "无法连接 SillyTavern，请确认 ST 已启动。"
    except Exception as e:
        return f"获取角色列表失败: {e}"


async def cmd_presets() -> str:
    """List all available presets."""
    try:
        presets = await st_api.get_presets()
        if not presets:
            return "SillyTavern 中没有找到预设。"

        lines = ["=== 可用预设 ==="]
        for i, (name, pdata) in enumerate(presets.items(), 1):
            source = pdata.get("chat_completion_source", "?")
            temp = pdata.get("temperature", "?")
            lines.append(f"{i}. {name}  (source={source}, temp={temp})")
        lines.append("\n使用 /preset <名称> 选择预设")
        return "\n".join(lines)
    except httpx.ConnectError:
        return "无法连接 SillyTavern，请确认 ST 已启动。"
    except Exception as e:
        return f"获取预设列表失败: {e}"


async def cmd_char_select(conv: str, args: str) -> str:
    """Select a character for this conversation."""
    name = args.strip()
    if not name:
        return "用法: /char <角色名称>\n先用 /chars 查看可用角色"

    gs = state.get_state(conv)
    if gs.character_name and gs.character_name != name:
        gs.chat_file = None

    chars = await st_api.get_characters()
    match = None
    for c in chars:
        if c.get("name", "").lower() == name.lower():
            match = c
            break

    if not match:
        matches = [c for c in chars if name.lower() in c.get("name", "").lower()]
        if len(matches) == 1:
            match = matches[0]
        elif len(matches) > 1:
            names = ", ".join(c.get("name", "") for c in matches[:5])
            return f"找到多个匹配: {names}\n请更精确地指定角色名称。"

    if not match:
        return f"未找到角色「{name}」。使用 /chars 查看可用角色列表。"

    gs.character_name = match.get("name", name)
    gs.avatar_url = match.get("avatar", "")
    gs.chat_file = None  # reset chat when switching character
    state.save_states()

    first_mes = match.get("first_mes", "")
    greeting = f"\n\n开场白:\n{first_mes}" if first_mes else ""

    return (
        f"已选择角色: {gs.character_name}"
        f"{greeting}\n\n"
        f"使用 /preset <名称> 选择预设，然后直接 @我 开始对话。"
    )


async def cmd_preset_select(conv: str, args: str) -> str:
    """Select a preset for this conversation."""
    name = args.strip()
    if not name:
        return "用法: /preset <预设名称>\n先用 /presets 查看可用预设"

    gs = state.get_state(conv)
    presets = await st_api.get_presets()
    match = None

    for pname in presets:
        if pname.lower() == name.lower():
            match = pname
            break

    if not match:
        matches = [p for p in presets if name.lower() in p.lower()]
        if len(matches) == 1:
            match = matches[0]
        elif len(matches) > 1:
            return f"找到多个匹配: {', '.join(matches[:5])}\n请更精确地指定预设名称。"

    if not match:
        return f"未找到预设「{name}」。使用 /presets 查看可用预设列表。"

    gs.preset_name = match
    state.save_states()
    pdata = presets[match]
    return (
        f"已选择预设: {match}\n"
        f"  source: {pdata.get('chat_completion_source', 'N/A')}\n"
        f"  temperature: {pdata.get('temperature', 'N/A')}\n"
        f"  max_tokens: {pdata.get('openai_max_tokens', 'N/A')}"
    )


def cmd_status(conv: str) -> str:
    """Show current conversation binding + engine status."""
    gs = state.get_state(conv)
    st = participation._get(conv)
    lines = ["=== 当前状态 ==="]
    lines.append(f"会话: {conv}")
    lines.append(f"角色: {gs.character_name or '未选择 (使用 /char)'}")
    lines.append(f"预设: {gs.preset_name or '未选择 (使用 /preset)'}")
    lines.append(f"聊天文件: {gs.chat_file or '未开始'}")
    status_text = "已开启" if gs.social_enabled else "已关闭 (/social on)"
    lines.append(f"社交引擎: {status_text}  阶段: {st.phase}")
    if st.wake_at:
        remain = int(st.wake_at - time.time())
        if remain > 0:
            lines.append(f"[WAKE] 预约: {remain}s 后")
    return "\n".join(lines)


async def cmd_newchat(conv: str, user_name: str = "QQ用户") -> str:
    """Start a new chat with the current character."""
    gs = state.get_state(conv)
    if not gs.character_name:
        return "请先使用 /char 选择一个角色。"
    if not gs.avatar_url:
        return "角色信息不完整，请重新使用 /char 选择角色。"

    gs.chat_file = chat_utils.new_chat_filename(gs.character_name)
    state.save_states()

    header = chat_utils.make_chat_header(user_name, gs.character_name)
    ok = await st_api.save_chat(gs.avatar_url, gs.chat_file, [header])
    if not ok:
        return "创建新对话失败，请检查 SillyTavern 连接。"

    char = await st_api.get_character(gs.avatar_url)
    first_mes = ""
    if char:
        first_mes = char.get("first_mes", "") or char.get("data", {}).get("first_mes", "")

    if first_mes:
        greeting_msg = chat_utils.make_chat_message(gs.character_name, False, first_mes)
        await st_api.save_chat(
            gs.avatar_url, gs.chat_file, [header, greeting_msg]
        )
        return f"新对话已开始！\n\n{gs.character_name}:\n{first_mes}"
    return "新对话已开始！直接 @我 发送消息吧。"


async def cmd_clear(conv: str) -> str:
    """Clear conversation history for this conversation."""
    gs = state.get_state(conv)
    gs.chat_file = None
    state.save_states()
    return "对话历史已清除。下次对话时将开始新对话。"


def cmd_help() -> str:
    """Help text for all commands."""
    return (
        "=== SillyTavern Bridge 命令 ===\n"
        "/chars     - 列出所有角色\n"
        "/presets   - 列出所有预设\n"
        "/char <名称>  - 选择角色\n"
        "/preset <名称> - 选择预设\n"
        "/status    - 查看当前绑定与引擎状态\n"
        "/newchat   - 开始新对话\n"
        "/clear     - 清除对话历史\n"
        "/social    - 社交引擎开关（观望/活跃/试探/退场）\n"
        "/stickers  - 查看表情包目录\n"
        "/sticker   - 管理/添加表情包（回复图片使用）\n"
        "/note      - 给群友的代号写印象备注\n"
        "/help      - 显示此帮助\n\n"
        "选择角色和预设后，@我 即可对话；开启 /social 后我会像普通群友一样参与群聊。"
    )


# ---------------------------------------------------------------------------
# /social — 社交引擎开关
# ---------------------------------------------------------------------------


async def cmd_social(conv: str, args: str) -> str:
    """Toggle the social engine for this conversation.

    Sub-commands:
      /social on|off|status
      /social keywords <词1,词2,...>   — per-conversation interest keywords
    (checkpoint thresholds / delays etc. are global, configured in .env)
    """
    gs = state.get_state(conv)
    parts = args.strip().split(maxsplit=1)
    sub = parts[0].lower() if parts else ""

    if sub in ("", "help"):
        return (
            "=== /social 社交引擎 ===\n"
            "/social on      — 开启（像群友一样自发参与）\n"
            "/social off     — 关闭（仅 @我 时回复）\n"
            "/social status  — 查看状态机阶段\n"
            "/social keywords <词1,词2,...> — 设置兴趣关键词（命中即看一眼）\n"
            "检查点阈值/活跃期/延迟等参数在 .env 中全局配置。"
        )

    if sub == "on":
        if not (gs.character_name and gs.preset_name):
            return "请先用 /char 和 /preset 选择角色和预设。"
        gs.social_enabled = True
        state.save_states()
        return (
            "社交引擎已 开启\n"
            "她会以观望/活跃/试探/退场的节奏参与群聊；@她 必定回应。\n"
            f"兴趣关键词: {', '.join(config.ST_INTEREST_KEYWORDS + gs.interest_keywords) or '无'}"
        )

    if sub == "off":
        gs.social_enabled = False
        state.save_states()
        return "社交引擎已 关闭"

    if sub == "status":
        return cmd_status(conv)

    if sub == "keywords":
        if len(parts) < 2:
            kws = ", ".join(config.ST_INTEREST_KEYWORDS + gs.interest_keywords) or "无"
            return f"当前兴趣关键词: {kws}\n用法: /social keywords <词1,词2,...>"
        gs.interest_keywords = [
            k.strip() for k in parts[1].replace("，", ",").split(",") if k.strip()
        ]
        state.save_states()
        return f"兴趣关键词已更新: {', '.join(gs.interest_keywords) or '（空）'}"

    return f"未知子命令: {sub}\n发送 /social help 查看用法"


# ---------------------------------------------------------------------------
# /stickers /sticker — 表情包目录管理
# ---------------------------------------------------------------------------


def cmd_stickers() -> str:
    """List the sticker catalog."""
    entries = stickers.all_tags()
    if not entries:
        return (
            "表情包目录为空。\n"
            "回复一条图片消息发送 /sticker <标签> [备注] 即可添加。"
        )
    lines = ["=== 表情包目录 ==="]
    for s in entries:
        note = f"（{s.note}）" if s.note else ""
        used = f" 用过{s.use_count}次" if s.use_count else ""
        lines.append(f"- {s.tag}{note}{used}")
    lines.append("\nAI 可用 [STICKER: 标签] 发送；/sticker del <标签> 删除")
    return "\n".join(lines)


async def cmd_sticker_manage(conv: str, args: str, image_path: str | None) -> str:
    """Add or remove a sticker. Add requires a replied image."""
    gs = state.get_state(conv)
    if not gs.character_name:
        return "请先选择角色（表情目录按角色共用，但仍需先配置完成）。"

    parts = args.strip().split(maxsplit=1)
    if not parts:
        return (
            "用法:\n"
            "回复图片 + /sticker <标签> [备注] — 添加\n"
            "/sticker del <标签> — 删除"
        )

    if parts[0].lower() == "del":
        if len(parts) < 2:
            return "用法: /sticker del <标签>"
        return "已删除。" if stickers.remove(parts[1]) else f"未找到标签「{parts[1]}」。"

    tag = parts[0]
    note = parts[1] if len(parts) > 1 else ""
    if image_path is None:
        return "请先回复一条图片消息，再发送 /sticker <标签> [备注]。"
    try:
        normalized = stickers.add(tag, image_path, note)
    except ValueError as e:
        return str(e)
    finally:
        try:
            import os
            if image_path and os.path.exists(image_path):
                os.remove(image_path)
        except OSError:
            pass
    return f"表情已添加: {normalized}{('（' + note + '）') if note else ''}"


# ---------------------------------------------------------------------------
# /note — 群友印象备注
# ---------------------------------------------------------------------------


def cmd_note(args: str) -> str:
    """Set an impression note for a member codename."""
    parts = args.strip().split(maxsplit=1)
    if len(parts) < 2:
        return "用法: /note <代号> <印象文字>"
    alias, note = parts[0], parts[1].strip()
    qq = aliases.resolve_qq(alias)
    if qq is None:
        return f"未找到代号「{alias}」。/status 查看名册请看群聊记录。"
    aliases.set_note(qq, note)
    return f"已记录印象: {alias} —— {note}"
