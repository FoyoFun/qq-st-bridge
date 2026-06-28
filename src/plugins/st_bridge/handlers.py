"""
Command handler functions — one per /command.

Each function is self-contained: takes inputs, calls st_api / state /
chat_utils as needed, and always returns a response string. Exception
handling is internal so the caller never sees raw tracebacks.

All functions are async except cmd_status (purely local state read).
"""

import logging

import httpx

from . import chat_utils
from . import st_api
from . import state


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
            max_tok = pdata.get("openai_max_tokens", "?")
            lines.append(
                f"{i}. {name}  (source={source}, temp={temp}, max_tokens={max_tok})"
            )
        lines.append("\n使用 /preset <名称> 选择预设")
        return "\n".join(lines)
    except httpx.ConnectError:
        return "无法连接 SillyTavern，请确认 ST 已启动。"
    except Exception as e:
        return f"获取预设列表失败: {e}"


async def cmd_char_select(group_id: int, args: str) -> str:
    """Select a character for this group."""
    name = args.strip()
    if not name:
        return "用法: /char <角色名称>\n先用 /chars 查看可用角色"

    gs = state.get_group_state(group_id)

    # Reset chat file when switching characters
    if gs.character_name and gs.character_name != name:
        gs.chat_file = None

    chars = await st_api.get_characters()
    match = None
    for c in chars:
        if c.get("name", "").lower() == name.lower():
            match = c
            break

    if not match:
        # Fuzzy match: name contains the search term
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
    state.save_group_states()

    # Get first_mes for greeting
    first_mes = match.get("first_mes", "")
    greeting = f"\n\n开场白:\n{first_mes}" if first_mes else ""

    return (
        f"已选择角色: {gs.character_name}"
        f"{greeting}\n\n"
        f"使用 /preset <名称> 选择预设，然后直接 @我 开始对话。"
    )


async def cmd_preset_select(group_id: int, args: str) -> str:
    """Select a preset for this group."""
    name = args.strip()
    if not name:
        return "用法: /preset <预设名称>\n先用 /presets 查看可用预设"

    gs = state.get_group_state(group_id)
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
            return (
                f"找到多个匹配: {', '.join(matches[:5])}\n"
                f"请更精确地指定预设名称。"
            )

    if not match:
        return f"未找到预设「{name}」。使用 /presets 查看可用预设列表。"

    gs.preset_name = match
    state.save_group_states()
    pdata = presets[match]
    return (
        f"已选择预设: {match}\n"
        f"  source: {pdata.get('chat_completion_source', 'N/A')}\n"
        f"  temperature: {pdata.get('temperature', 'N/A')}\n"
        f"  max_tokens: {pdata.get('openai_max_tokens', 'N/A')}"
    )


def cmd_status(group_id: int) -> str:
    """Show current group binding status."""
    gs = state.get_group_state(group_id)
    lines = ["=== 当前状态 ==="]
    lines.append(f"角色: {gs.character_name or '未选择 (使用 /char)'}")
    lines.append(f"预设: {gs.preset_name or '未选择 (使用 /preset)'}")
    lines.append(f"聊天文件: {gs.chat_file or '未开始'}")
    return "\n".join(lines)


async def cmd_newchat(group_id: int, user_name: str = "QQ用户") -> str:
    """Start a new chat with the current character."""
    gs = state.get_group_state(group_id)
    if not gs.character_name:
        return "请先使用 /char 选择一个角色。"

    if not gs.avatar_url:
        return "角色信息不完整，请重新使用 /char 选择角色。"

    # Create a new chat
    gs.chat_file = chat_utils.new_chat_filename(gs.character_name)
    state.save_group_states()

    # Save the initial empty chat with header
    header = chat_utils.make_chat_header(user_name, gs.character_name)
    ok = await st_api.save_chat(gs.avatar_url, gs.chat_file, [header])
    if not ok:
        return "创建新对话失败，请检查 SillyTavern 连接。"

    # Get first_mes
    char = await st_api.get_character(gs.avatar_url)
    first_mes = ""
    if char:
        first_mes = char.get("first_mes", "") or char.get("data", {}).get(
            "first_mes", ""
        )

    if first_mes:
        # Save the greeting message to chat
        greeting_msg = chat_utils.make_chat_message(
            gs.character_name, False, first_mes
        )
        header = chat_utils.make_chat_header(user_name, gs.character_name)
        await st_api.save_chat(gs.avatar_url, gs.chat_file, [header, greeting_msg])
        return f"新对话已开始！\n\n{gs.character_name}:\n{first_mes}"
    else:
        return f"新对话已开始！直接 @我 发送消息吧。"


async def cmd_clear(group_id: int) -> str:
    """Clear conversation history for this group."""
    gs = state.get_group_state(group_id)
    gs.chat_file = None
    state.save_group_states()
    return "对话历史已清除。下次 @我 时将开始新对话。"
