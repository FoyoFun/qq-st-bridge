"""
Event entry points — the bridge's ears and command surface.

Four matchers (registered at import time):
- at_me      (priority 10, block)  @mentions: commands or must-see triggers
- all_msgs   (priority 20, pass)   every group message -> collector + engine
- private    private messages      -> collector + engine (immediate delivery)
- poke       group pokes on the bot -> engine trigger

When the social engine is disabled for a conversation, @/private chat falls
back to the legacy synchronous flow (generate + reply directly), so plain
@-chat keeps working without the simulation layer.
"""

import logging
import os
import tempfile

import httpx
from nonebot import on_message, on_notice
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
    PokeNotifyEvent,
    PrivateMessageEvent,
)
from nonebot.exception import FinishedException
from nonebot.params import EventPlainText
from nonebot.rule import is_type, to_me

from . import action_loop
from . import aliases
from . import chat_utils
from . import collector
from . import config
from . import handlers
from . import participation
from . import st_api
from . import state

# ---------------------------------------------------------------------------
# Matcher registration (executes at import time)
# ---------------------------------------------------------------------------

at_me = on_message(
    rule=to_me() & is_type(GroupMessageEvent), priority=10, block=True
)

_all_msgs = on_message(
    rule=is_type(GroupMessageEvent), priority=20, block=False
)

_private_msg = on_message(
    rule=is_type(PrivateMessageEvent), priority=10, block=False
)

_poke = on_notice(rule=is_type(PokeNotifyEvent), priority=10, block=False)


# ---------------------------------------------------------------------------
# @mention handler (commands + must-see chat trigger)
# ---------------------------------------------------------------------------

@at_me.handle()
async def handle_at_me(
    event: GroupMessageEvent, text: str = EventPlainText()
):
    """@mention messages: /commands, status help, or a must-see trigger."""
    text = (text or "").strip()
    group_id = event.group_id
    conv = state.group_conv(group_id)
    gs = state.get_state(conv)

    try:
        user_name = event.sender.card or event.sender.nickname or str(event.user_id)
    except Exception:
        user_name = str(event.user_id)

    # --- Empty @: show status/help ---
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

    # --- Commands ---
    if text.startswith("/"):
        await _dispatch_command(at_me, text, conv, user_name, event)
        return

    # --- Normal chat ---
    if not gs.character_name:
        await at_me.finish("请先使用 /chars 查看角色，然后用 /char <名称> 选择角色。")
    if not gs.preset_name:
        await at_me.finish("请先使用 /presets 查看预设，然后用 /preset <名称> 选择预设。")

    msg = collector.collect_group(event, text_override=text, is_at=True)
    if msg is None:
        return

    if gs.social_enabled:
        # Social engine: async must-see turn (reply arrives via sender)
        participation.feed(msg, is_at_bot=True)
    else:
        # Legacy direct flow: generate now and reply synchronously
        await _legacy_chat_flow(at_me, gs, event.user_id, msg.alias, text)


# ---------------------------------------------------------------------------
# All group messages -> collector + participation engine
# ---------------------------------------------------------------------------

@_all_msgs.handle()
async def handle_all_messages(
    event: GroupMessageEvent, text: str = EventPlainText()
):
    """Feed every group message into the collector and the engine."""
    if str(event.user_id) == str(event.self_id):
        return
    if event.to_me:
        return  # handled by at_me (priority 10, block)
    if (text or "").strip().startswith("/"):
        return

    gs = state.get_state(state.group_conv(event.group_id))
    if not gs.social_enabled:
        return

    msg = collector.collect_group(event)
    if msg is not None:
        participation.feed(msg)


# ---------------------------------------------------------------------------
# Private messages -> collector + engine (immediate delivery)
# ---------------------------------------------------------------------------

@_private_msg.handle()
async def handle_private(event: PrivateMessageEvent, text: str = EventPlainText()):
    """Private chat: always immediate (heavy tier) when the engine is on."""
    text = (text or "").strip()
    conv = state.private_conv(event.user_id)
    gs = state.get_state(conv)

    if text.startswith("/"):
        await _dispatch_command(_private_msg, text, conv, "", event)
        return

    if not gs.character_name or not gs.preset_name:
        return  # nothing configured for this conversation yet

    msg = collector.collect_private(event)
    if msg is None:
        return

    if gs.social_enabled:
        participation.feed(msg, is_private=True)
    else:
        await _legacy_chat_flow(_private_msg, gs, event.user_id, msg.alias, msg.text)


# ---------------------------------------------------------------------------
# Pokes on the bot
# ---------------------------------------------------------------------------

@_poke.handle()
async def handle_poke(event: PokeNotifyEvent):
    """Someone poked the bot in a group — must-see trigger."""
    if event.target_id != event.self_id or not event.group_id:
        return
    gs = state.get_state(state.group_conv(event.group_id))
    if not gs.social_enabled:
        return
    poker = aliases.get_member(event.user_id)
    poker_alias = poker.alias if poker else str(event.user_id)
    participation.on_poke(state.group_conv(event.group_id), poker_alias)


# ---------------------------------------------------------------------------
# Command dispatch
# ---------------------------------------------------------------------------

async def _dispatch_command(matcher, text: str, conv: str,
                            user_name: str, event) -> None:
    """Parse and dispatch a /command (group @ or private)."""
    parts = text.split(maxsplit=1)
    cmd = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""

    try:
        if cmd == "/help":
            await matcher.finish(handlers.cmd_help())

        elif cmd in ("/social", "/auto"):
            await matcher.finish(await handlers.cmd_social(conv, args))

        elif cmd == "/stickers":
            await matcher.finish(handlers.cmd_stickers())

        elif cmd == "/sticker":
            image_path = await _extract_replied_image(event)
            await matcher.finish(
                await handlers.cmd_sticker_manage(conv, args, image_path)
            )

        elif cmd == "/note":
            await matcher.finish(handlers.cmd_note(args))

        elif cmd == "/chars":
            await matcher.finish(await handlers.cmd_chars())

        elif cmd == "/presets":
            await matcher.finish(await handlers.cmd_presets())

        elif cmd == "/char":
            await matcher.finish(await handlers.cmd_char_select(conv, args))

        elif cmd == "/preset":
            await matcher.finish(await handlers.cmd_preset_select(conv, args))

        elif cmd == "/status":
            await matcher.finish(handlers.cmd_status(conv))

        elif cmd == "/newchat":
            await matcher.finish(await handlers.cmd_newchat(conv, user_name))

        elif cmd == "/clear":
            await matcher.finish(await handlers.cmd_clear(conv))

        else:
            await matcher.finish(f"未知命令: {cmd}\n发送 /help 查看可用命令。")
    except FinishedException:
        raise
    except Exception as e:
        logging.exception(f"Command {cmd} error")
        await matcher.finish(f"命令执行出错: {e}")


async def _extract_replied_image(event) -> str | None:
    """Download the image of the replied message to a temp file.

    Used by /sticker <tag> when replying to an image. Returns a local
    path, or None if no image is present.
    """
    try:
        if not isinstance(event, GroupMessageEvent) or not event.reply:
            return None
        for seg in event.reply.message:
            if seg.type == "image":
                url = seg.data.get("url") or seg.data.get("file")
                if not url:
                    return None
                async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                    resp = await client.get(str(url))
                    resp.raise_for_status()
                fd, path = tempfile.mkstemp(
                    suffix=".img", dir=tempfile.gettempdir()
                )
                with os.fdopen(fd, "wb") as f:
                    f.write(resp.content)
                return path
    except Exception as e:
        logging.warning(f"Sticker: failed to fetch replied image: {e}")
    return None


# ---------------------------------------------------------------------------
# Legacy direct chat flow (social engine disabled)
# ---------------------------------------------------------------------------

async def _legacy_chat_flow(matcher, gs, user_id: int,
                            alias: str, text: str) -> None:
    """Direct generate + reply without the social engine (no delays).

    `matcher` is the caller's matcher so finish() targets the right event.
    """
    history = await action_loop._load_history(gs)
    observation = f"{alias}：{text}"

    try:
        result = await st_api.plugin_generate(
            avatar_url=gs.avatar_url,
            preset_name=gs.preset_name,
            chat_history=history,
            user_message=observation,
            user_name="QQ用户",
            character_name=gs.character_name or "",
            post_history=config.POST_HISTORY_CONTRACT,
            conv=conv,
            tier="heavy",
            reason="direct",
        )
    except httpx.ConnectError:
        await matcher.finish("无法连接 SillyTavern，请确认 ST 已启动。")
    except httpx.TimeoutException:
        await matcher.finish("AI 响应超时，请稍后重试。")
    except RuntimeError:
        await matcher.finish("SillyTavern 连接已断开，请检查 ST 是否在运行。")
    except Exception as e:
        logging.exception("Legacy chat flow error")
        await matcher.finish(f"生成回复时出错: {type(e).__name__}")

    if not result.get("success"):
        await matcher.finish(f"AI 服务返回错误: {result.get('error', '未知错误')}")

    response_text = (result.get("response_text", "") or "").strip()
    if not response_text:
        await matcher.finish("(AI 返回了空响应，请尝试重新发送。)")
        return

    actions = action_loop.parse_actions(response_text)
    reply = actions.text or "（她看了看，没说话）"
    reply = config.truncate(reply)

    await action_loop._save_exchange(
        gs, f"{alias}：{text}", [reply], actions, history
    )
    await matcher.finish(Message(reply))
