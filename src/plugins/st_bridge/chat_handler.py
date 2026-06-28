"""
Main @mention message handler.

Orchestrates the full chat flow: parse → validate → acquire lock →
load history → generate → save → reply. This is the glue layer that
ties together all the other modules.

Importing this module triggers on_message matcher registration as a
side effect (NoneBot2 decorator semantics).
"""

import logging

import httpx
from nonebot import on_message
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message
from nonebot.exception import FinishedException
from nonebot.params import EventPlainText
from nonebot.rule import is_type, to_me

from . import chat_utils
from . import config
from . import handlers
from . import st_api
from . import state

# ---------------------------------------------------------------------------
# Matcher registration (executes at import time)
# ---------------------------------------------------------------------------

at_me = on_message(
    rule=to_me() & is_type(GroupMessageEvent), priority=10, block=True
)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

@at_me.handle()
async def handle_at_me(
    event: GroupMessageEvent, text: str = EventPlainText()
):
    """Main handler for @mention messages in QQ groups."""
    text = text.strip()
    group_id = event.group_id
    user_id = event.user_id
    gs = state.get_group_state(group_id)

    # Get sender info for chat
    try:
        user_name = event.sender.card or event.sender.nickname or str(user_id)
    except Exception:
        user_name = str(user_id)

    # Remember the QQ→nickname mapping for reverse lookup
    state.remember_user(user_id, user_name)

    # --- Empty message ---
    if not text:
        if gs.character_name:
            await at_me.finish(
                f"当前角色: {gs.character_name}\n"
                f"当前预设: {gs.preset_name or '未选择'}\n"
                f"发送 /help 查看所有命令。"
            )
        else:
            await at_me.finish(
                "你好！请先设置角色和预设:\n"
                "/chars - 查看可用角色\n"
                "/presets - 查看可用预设\n"
                "/char <名称> - 选择角色\n"
                "/preset <名称> - 选择预设\n"
                "/help - 查看所有命令"
            )

    # --- Commands (text starting with /) ---
    if text.startswith("/"):
        await _dispatch_command(text, group_id, user_name)
        return

    # --- Normal chat: need character and preset ---
    if not gs.character_name:
        await at_me.finish(
            "请先使用 /chars 查看角色，然后用 /char <名称> 选择角色。"
        )
        return

    if not gs.preset_name:
        await at_me.finish(
            "请先使用 /presets 查看预设，然后用 /preset <名称> 选择预设。"
        )
        return

    # Note: concurrency is handled globally by st_client.auth_post() —
    # all ST operations across all groups are serialized there.
    await _chat_flow(gs, group_id, user_id, user_name, text)


# ---------------------------------------------------------------------------
# Command dispatch
# ---------------------------------------------------------------------------

async def _dispatch_command(text: str, group_id: int, user_name: str) -> None:
    """Parse and dispatch a /command."""
    parts = text.split(maxsplit=1)
    cmd = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""

    try:
        if cmd == "/help":
            await at_me.finish(
                "=== SillyTavern Bridge 命令 ===\n"
                "/chars     - 列出所有角色\n"
                "/presets   - 列出所有预设\n"
                "/char <名称>  - 选择角色\n"
                "/preset <名称> - 选择预设\n"
                "/status    - 查看当前绑定\n"
                "/newchat   - 开始新对话\n"
                "/clear     - 清除对话历史\n"
                "/help      - 显示此帮助\n\n"
                "选择角色和预设后，直接 @我 发送消息即可与 AI 对话。"
            )

        elif cmd == "/chars":
            await at_me.finish(await handlers.cmd_chars())

        elif cmd == "/presets":
            await at_me.finish(await handlers.cmd_presets())

        elif cmd == "/char":
            await at_me.finish(
                await handlers.cmd_char_select(group_id, args)
            )

        elif cmd == "/preset":
            await at_me.finish(
                await handlers.cmd_preset_select(group_id, args)
            )

        elif cmd == "/status":
            await at_me.finish(handlers.cmd_status(group_id))

        elif cmd == "/newchat":
            await at_me.finish(
                await handlers.cmd_newchat(group_id, user_name)
            )

        elif cmd == "/clear":
            await at_me.finish(await handlers.cmd_clear(group_id))

        else:
            await at_me.finish(
                f"未知命令: {cmd}\n发送 /help 查看可用命令。"
            )
    except FinishedException:
        raise
    except Exception as e:
        logging.exception(f"Command {cmd} error")
        await at_me.finish(f"命令执行出错: {e}")


# ---------------------------------------------------------------------------
# Chat flow (called inside the per-group lock)
# ---------------------------------------------------------------------------

async def _chat_flow(
    gs: state.GroupState,
    group_id: int,
    user_id: int,
    user_name: str,
    text: str,
) -> None:
    """Execute the full chat-turn pipeline (load → generate → save → reply)."""

    # --- Load chat history ---
    chat_data: list[dict] = []
    if gs.chat_file:
        loaded = await st_api.load_chat(gs.avatar_url, gs.chat_file)
        if loaded:
            chat_data = loaded
    if not chat_data:
        # Start a new chat
        gs.chat_file = chat_utils.new_chat_filename(gs.character_name)
        state.save_group_states()
        header = chat_utils.make_chat_header(user_name, gs.character_name)
        chat_data = [header]

    # --- Build prompt + Generate response via ST plugin ---
    history = chat_utils.extract_history(chat_data)
    formatted_text = f"{user_id}：{text}"

    try:
        result = await st_api.plugin_generate(
            avatar_url=gs.avatar_url,
            preset_name=gs.preset_name,
            chat_history=history,
            user_message=formatted_text,
            user_name=user_name,
            character_name=gs.character_name or "",
        )
    except httpx.ConnectError:
        await at_me.finish("无法连接 SillyTavern，请确认 ST 已启动。")
        return
    except httpx.TimeoutException:
        await at_me.finish("AI 响应超时，请稍后重试。")
        return
    except httpx.HTTPStatusError as e:
        detail = ""
        try:
            err_body = e.response.json()
            if isinstance(err_body, dict):
                detail = err_body.get("error", err_body.get("message", ""))
                if isinstance(detail, dict):
                    detail = detail.get("message", str(detail))
        except Exception:
            pass
        msg = f"AI 服务返回错误 (HTTP {e.response.status_code})"
        if detail:
            msg += f": {detail}"
        if len(msg) > 400:
            msg = msg[:400] + "..."
        await at_me.finish(msg)
        return
    except RuntimeError:
        # Retry exhaustion — both attempts failed (see plugin_generate)
        await at_me.finish("SillyTavern 连接已断开，请检查 ST 是否在运行。")
        return
    except Exception as e:
        logging.exception("Plugin generate error")
        await at_me.finish(f"生成回复时出错: {type(e).__name__}")
        return

    if not result.get("success"):
        error_msg = result.get("error", "未知错误")
        await at_me.finish(f"AI 服务返回错误: {error_msg}")
        return

    response_text = result.get("response_text", "")
    if not response_text or not response_text.strip():
        await at_me.finish("(AI 返回了空响应，请尝试重新发送。)")
        return

    # --- Save chat to ST (原始AI回复，含QQ号) ---
    user_msg = chat_utils.make_chat_message(user_name, True, formatted_text)
    ai_msg = chat_utils.make_chat_message(
        gs.character_name, False, response_text
    )

    # Ensure header exists
    if not any("chat_metadata" in m for m in chat_data):
        chat_data.insert(
            0, chat_utils.make_chat_header(user_name, gs.character_name)
        )

    chat_data.append(user_msg)
    chat_data.append(ai_msg)
    await st_api.save_chat(gs.avatar_url, gs.chat_file, chat_data)

    # --- Reply to QQ (QQ号→昵称) ---
    display_text = state.replace_qq_with_nickname(response_text)
    display_text = config.truncate(display_text)
    await at_me.finish(Message(display_text))
