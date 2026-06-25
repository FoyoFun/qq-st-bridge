"""
SillyTavern Bridge Plugin for NoneBot2
========================================
Bridges QQ group chat to SillyTavern AI character chat.
Users @mention the bot to interact with ST characters.

Commands:
  /chars    - List available characters
  /presets  - List available presets
  /char     - Select a character
  /preset   - Select a preset
  /status   - Show current binding
  /newchat  - Start a new chat
  /clear    - Clear conversation history
  /help     - Show help
"""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import httpx
from nonebot import get_driver, on_message
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment
from nonebot.exception import FinishedException
from nonebot.params import EventPlainText
from nonebot.rule import is_type, to_me

# ---------------------------------------------------------------------------
# Configuration (from .env via NoneBot2 driver config)
# ---------------------------------------------------------------------------
ST_BASE_URL: str = "http://127.0.0.1:8000"
ST_CHAT_SOURCE: str = "deepseek"
ST_MODEL: str = ""
ST_TIMEOUT: int = 120
ST_MAX_RESPONSE_LENGTH: int = 800
ST_DEFAULT_PRESET: str = ""
ST_DEFAULT_CHARACTER: str = ""

# ---------------------------------------------------------------------------
# Per-group state
# ---------------------------------------------------------------------------

@dataclass
class GroupState:
    """Per-group bridge state: which character and preset are selected."""
    character_name: Optional[str] = None
    preset_name: Optional[str] = None
    avatar_url: Optional[str] = None   # character avatar filename
    chat_file: Optional[str] = None     # ST chat filename (without .jsonl)
    chat_metadata: dict = field(default_factory=dict)

# key = group_id (int)
_group_states: dict[int, GroupState] = {}

def _get_state(group_id: int) -> GroupState:
    if group_id not in _group_states:
        _group_states[group_id] = GroupState()
    return _group_states[group_id]

# ---------------------------------------------------------------------------
# QQ号 → 昵称 映射（用于回复中还原QQ号为昵称）
# ---------------------------------------------------------------------------

_nickname_map: dict[str, str] = {}

def _remember_user(user_id: int, user_name: str) -> None:
    """记住用户的QQ号→昵称映射。"""
    _nickname_map[str(user_id)] = user_name

def _replace_qq_with_nickname(text: str) -> str:
    """将文本中已知的QQ号替换回昵称。"""
    result = text
    for qq_id, nickname in _nickname_map.items():
        result = result.replace(qq_id, nickname)
    return result

# ---------------------------------------------------------------------------
# HTTP Client for SillyTavern API
# ---------------------------------------------------------------------------

_client: Optional[httpx.AsyncClient] = None

async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(ST_TIMEOUT),
            follow_redirects=True,
        )
    return _client

async def _close_client():
    global _client
    if _client:
        await _client.aclose()
        _client = None

_b64 = _url = None  # base url, cached
def _base() -> str:
    global _b64
    if _b64 is None:
        _b64 = ST_BASE_URL.rstrip("/")
    return _b64

async def _csrf_token(client: httpx.AsyncClient) -> str:
    """Fetch a fresh CSRF token from SillyTavern."""
    resp = await client.get(f"{_base()}/csrf-token")
    resp.raise_for_status()
    return str(resp.json()["token"])

async def _post(path: str, body: dict) -> dict:
    """Make an authenticated POST to SillyTavern API."""
    client = await _get_client()
    token = await _csrf_token(client)
    resp = await client.post(
        f"{_base()}{path}",
        json=body,
        headers={"X-CSRF-Token": token},
    )
    resp.raise_for_status()
    return resp.json()

# ---------------------------------------------------------------------------
# SillyTavern API helpers
# ---------------------------------------------------------------------------

# Cache for characters and presets (refresh with /refresh or on restart)
_chars_cache: Optional[list[dict]] = None
_presets_cache: Optional[dict[str, dict]] = None  # name -> preset data

async def st_get_characters() -> list[dict]:
    """Get all characters from ST. Cached after first call."""
    global _chars_cache
    if _chars_cache is None:
        _chars_cache = await _post("/api/characters/all", {})
        if not isinstance(_chars_cache, list):
            _chars_cache = []
    return _chars_cache

async def st_get_character(avatar_url: str) -> dict | None:
    """Get a single character by avatar_url."""
    try:
        return await _post("/api/characters/get", {"avatar_url": avatar_url})
    except Exception:
        return None

async def st_get_presets() -> dict[str, dict]:
    """Get presets from ST. Returns {name: preset_data}. Cached."""
    global _presets_cache
    if _presets_cache is None:
        data = await _post("/api/settings/get", {})
        names = data.get("openai_setting_names", [])
        raw = data.get("openai_settings", [])
        _presets_cache = {}
        for name, preset_str in zip(names, raw):
            try:
                _presets_cache[name] = json.loads(preset_str) if isinstance(preset_str, str) else preset_str
            except json.JSONDecodeError:
                pass
    return _presets_cache

async def st_get_chats(avatar_url: str) -> list[dict]:
    """Get list of chat files for a character."""
    try:
        return await _post("/api/characters/chats", {"avatar_url": avatar_url})
    except Exception:
        return []

async def st_load_chat(avatar_url: str, file_name: str) -> list[dict] | None:
    """Load a chat history from ST. Returns list of messages."""
    try:
        return await _post("/api/chats/get", {
            "avatar_url": avatar_url,
            "file_name": file_name,
        })
    except Exception:
        return None

async def st_save_chat(avatar_url: str, file_name: str, chat: list[dict]) -> bool:
    """Save chat history to ST. Returns True on success."""
    try:
        await _post("/api/chats/save", {
            "avatar_url": avatar_url,
            "file_name": file_name,
            "chat": chat,
        })
        return True
    except Exception:
        return False

async def st_generate(messages: list[dict], preset: dict) -> str:
    """Send messages to ST for AI completion. Returns response text."""
    client = await _get_client()
    token = await _csrf_token(client)

    payload: dict = {
        "messages": messages,
        "chat_completion_source": preset.get("chat_completion_source", ST_CHAT_SOURCE),
        "stream": False,
    }

    # Apply preset parameters — model selection
    # ST uses different model fields per source (openai_model, claude_model, etc.)
    source = preset.get("chat_completion_source", ST_CHAT_SOURCE)
    model = ST_MODEL or preset.get("openai_model", "") or preset.get("model", "")
    # For DeepSeek source, if the model is not a DeepSeek model, fall back
    if source == "deepseek" and model and "deepseek" not in model.lower():
        model = ""  # let ST use its default
    if not model:
        # Default fallbacks per source
        defaults = {"deepseek": "deepseek-chat", "openai": "gpt-4o-mini"}
        model = defaults.get(source, "")
    if model:
        payload["model"] = model

    for key in ("temperature", "frequency_penalty", "presence_penalty",
                "top_p", "top_k", "top_a", "min_p", "repetition_penalty"):
        if key in preset:
            payload[key] = preset[key]

    tokens = preset.get("openai_max_tokens") or preset.get("max_tokens") or 500
    payload["max_tokens"] = tokens

    resp = await client.post(
        f"{_base()}/api/backends/chat-completions/generate",
        json=payload,
        headers={"X-CSRF-Token": token},
    )
    resp.raise_for_status()
    data = resp.json()

    # Extract response content (OpenAI / DeepSeek format)
    if "choices" in data and data["choices"]:
        msg = data["choices"][0].get("message", {})
        return str(msg.get("content", "") or "")
    return ""

# ---------------------------------------------------------------------------
# Prompt Builder
# ---------------------------------------------------------------------------

def build_messages(character: dict, history: list[dict], user_text: str) -> list[dict]:
    """Build the messages array for the AI backend from character card + history."""

    char_data = character.get("data", character)
    system_parts: list[str] = []

    # 1. Character system_prompt (if set, overrides everything)
    sys_prompt = char_data.get("system_prompt", "")
    if sys_prompt:
        system_parts.append(sys_prompt)
    else:
        # 2. Default system prompt from character fields
        desc = char_data.get("description", "") or character.get("description", "")
        if desc:
            system_parts.append(f"[Character: {character.get('name', '')}]\n{desc}")

        personality = char_data.get("personality", "") or character.get("personality", "")
        if personality:
            system_parts.append(f"[Personality]\n{personality}")

        scenario = char_data.get("scenario", "") or character.get("scenario", "")
        if scenario:
            system_parts.append(f"[Scenario]\n{scenario}")

    # 3. Post-history instructions (added after history, as a system message)
    post_history = char_data.get("post_history_instructions", "")

    # 4. Dialogue examples (mes_example)
    mes_example = char_data.get("mes_example", "") or character.get("mes_example", "")
    if mes_example:
        # mes_example uses <START> as delimiter between examples, {{user}}/{{char}} placeholders
        system_parts.append(
            "[Example dialogue — use this tone/style:\n"
            f"{mes_example}\n"
            "]"
        )

    # Build system message
    system_content = "\n\n".join(system_parts) if system_parts else (
        f"You are {character.get('name', 'an AI assistant')}. "
        "Be helpful, engaging, and stay in character."
    )

    messages: list[dict] = [{"role": "system", "content": system_content}]

    # Add chat history (user/assistant pairs)
    for msg in history:
        role = "assistant" if not msg.get("is_user") else "user"
        content = msg.get("mes", "")
        if content:
            messages.append({"role": role, "content": content})

    # Add post-history instructions as a system message before the current user message
    if post_history:
        messages.append({"role": "system", "content": post_history})

    # Add current user message
    messages.append({"role": "user", "content": user_text})

    return messages

# ---------------------------------------------------------------------------
# Chat History Helpers
# ---------------------------------------------------------------------------

def _new_chat_filename(character_name: str) -> str:
    """Generate a new chat filename for a character."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d@%Hh%Mm%Ss%f")[:-3] + "ms"
    safe_name = re.sub(r'[\\/:*?"<>|]', '_', character_name)
    return f"{safe_name} - {ts}"

def _make_chat_header(user_name: str = "QQ用户",
                      character_name: str = "Character") -> dict:
    """Create the first line (header) of a ST JSONL chat file."""
    return {
        "chat_metadata": {
            "tainted": False,
            "note_prompt": "",
            "note_interval": 1,
            "note_position": 1,
            "note_depth": 4,
            "note_role": 0,
            "timedWorldInfo": {"sticky": {}, "cooldown": {}},
            "lastInContextMessageId": 0,
        },
        "user_name": user_name,
        "character_name": character_name,
    }

def _make_chat_message(name: str, is_user: bool, text: str) -> dict:
    """Create a chat message object for ST JSONL format."""
    return {
        "name": name,
        "is_user": is_user,
        "is_system": False,
        "send_date": int(datetime.now(timezone.utc).timestamp() * 1000),
        "mes": text,
        "extra": {},
    }

def _extract_history(chat_data: list[dict]) -> list[dict]:
    """Extract message list from ST chat data (skip header line)."""
    history = []
    for msg in chat_data:
        if "chat_metadata" in msg:
            continue  # skip header
        if "mes" in msg and msg["mes"]:
            history.append(msg)
    return history

# ---------------------------------------------------------------------------
# Command Handlers
# ---------------------------------------------------------------------------

def _truncate(text: str, max_len: int = ST_MAX_RESPONSE_LENGTH) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + "\n...(内容过长已截断)"

async def cmd_chars() -> str:
    """List all available characters."""
    try:
        chars = await st_get_characters()
        if not chars:
            return "SillyTavern 中没有找到角色。"

        lines = ["=== 可用角色 ==="]
        for i, c in enumerate(chars, 1):
            name = c.get("name", "未知")
            avatar = c.get("avatar", "N/A")
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
        presets = await st_get_presets()
        if not presets:
            return "SillyTavern 中没有找到预设。"

        lines = ["=== 可用预设 ==="]
        for i, (name, pdata) in enumerate(presets.items(), 1):
            source = pdata.get("chat_completion_source", "?")
            temp = pdata.get("temperature", "?")
            max_tok = pdata.get("openai_max_tokens", "?")
            lines.append(f"{i}. {name}  (source={source}, temp={temp}, max_tokens={max_tok})")
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

    state = _get_state(group_id)

    # Reset chat file when switching characters
    if state.character_name and state.character_name != name:
        state.chat_file = None

    chars = await st_get_characters()
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

    state.character_name = match.get("name", name)
    state.avatar_url = match.get("avatar", "")

    # Get first_mes for greeting
    first_mes = match.get("first_mes", "")
    greeting = f"\n\n开场白:\n{first_mes}" if first_mes else ""

    return (
        f"已选择角色: {state.character_name}"
        f"{greeting}\n\n"
        f"使用 /preset <名称> 选择预设，然后直接 @我 开始对话。"
    )

async def cmd_preset_select(group_id: int, args: str) -> str:
    """Select a preset for this group."""
    name = args.strip()
    if not name:
        return "用法: /preset <预设名称>\n先用 /presets 查看可用预设"

    state = _get_state(group_id)
    presets = await st_get_presets()
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

    state.preset_name = match
    pdata = presets[match]
    return (
        f"已选择预设: {match}\n"
        f"  source: {pdata.get('chat_completion_source', 'N/A')}\n"
        f"  temperature: {pdata.get('temperature', 'N/A')}\n"
        f"  max_tokens: {pdata.get('openai_max_tokens', 'N/A')}"
    )

def cmd_status(group_id: int) -> str:
    """Show current group binding status."""
    state = _get_state(group_id)
    lines = ["=== 当前状态 ==="]
    lines.append(f"角色: {state.character_name or '未选择 (使用 /char)'}")
    lines.append(f"预设: {state.preset_name or '未选择 (使用 /preset)'}")
    lines.append(f"聊天文件: {state.chat_file or '未开始'}")
    return "\n".join(lines)

async def cmd_newchat(group_id: int, user_name: str = "QQ用户") -> str:
    """Start a new chat with the current character."""
    state = _get_state(group_id)
    if not state.character_name:
        return "请先使用 /char 选择一个角色。"

    if not state.avatar_url:
        return "角色信息不完整，请重新使用 /char 选择角色。"

    # Create a new chat
    state.chat_file = _new_chat_filename(state.character_name)

    # Save the initial empty chat with header
    header = _make_chat_header(user_name, state.character_name)
    ok = await st_save_chat(state.avatar_url, state.chat_file, [header])
    if not ok:
        return "创建新对话失败，请检查 SillyTavern 连接。"

    # Get first_mes
    char = await st_get_character(state.avatar_url)
    first_mes = ""
    if char:
        first_mes = char.get("first_mes", "") or char.get("data", {}).get("first_mes", "")
        # If there are alternate greetings, pick the first one randomly for variety?
        # For now just use first_mes

    if first_mes:
        # Save the greeting message to chat
        greeting_msg = _make_chat_message(state.character_name, False, first_mes)
        header = _make_chat_header(user_name, state.character_name)
        await st_save_chat(state.avatar_url, state.chat_file, [header, greeting_msg])
        return f"新对话已开始！\n\n{state.character_name}:\n{first_mes}"
    else:
        return f"新对话已开始！直接 @我 发送消息吧。"

async def cmd_clear(group_id: int) -> str:
    """Clear conversation history for this group."""
    state = _get_state(group_id)
    state.chat_file = None
    return "对话历史已清除。下次 @我 时将开始新对话。"

# ---------------------------------------------------------------------------
# Message Handler
# ---------------------------------------------------------------------------

at_me = on_message(rule=to_me() & is_type(GroupMessageEvent), priority=10, block=True)

@at_me.handle()
async def handle_at_me(event: GroupMessageEvent, text: str = EventPlainText()):
    """Main handler for @mention messages in QQ groups."""
    text = text.strip()
    group_id = event.group_id
    user_id = event.user_id
    state = _get_state(group_id)

    # Get sender info for chat
    try:
        user_name = event.sender.card or event.sender.nickname or str(user_id)
    except Exception:
        user_name = str(user_id)

    # Remember the QQ→nickname mapping for reverse lookup
    _remember_user(user_id, user_name)

    # --- Empty message ---
    if not text:
        if state.character_name:
            await at_me.finish(
                f"当前角色: {state.character_name}\n"
                f"当前预设: {state.preset_name or '未选择'}\n"
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
                await at_me.finish(await cmd_chars())

            elif cmd == "/presets":
                await at_me.finish(await cmd_presets())

            elif cmd == "/char":
                await at_me.finish(await cmd_char_select(group_id, args))

            elif cmd == "/preset":
                await at_me.finish(await cmd_preset_select(group_id, args))

            elif cmd == "/status":
                await at_me.finish(cmd_status(group_id))

            elif cmd == "/newchat":
                await at_me.finish(await cmd_newchat(group_id, user_name))

            elif cmd == "/clear":
                await at_me.finish(await cmd_clear(group_id))

            else:
                await at_me.finish(f"未知命令: {cmd}\n发送 /help 查看可用命令。")
        except FinishedException:
            raise
        except Exception as e:
            logging.exception(f"Command {cmd} error")
            await at_me.finish(f"命令执行出错: {e}")

        return

    # --- Normal chat: need character and preset ---
    if not state.character_name:
        await at_me.finish("请先使用 /chars 查看角色，然后用 /char <名称> 选择角色。")
        return

    if not state.preset_name:
        await at_me.finish("请先使用 /presets 查看预设，然后用 /preset <名称> 选择预设。")
        return

    # --- Load chat history ---
    chat_data: list[dict] = []
    if state.chat_file:
        loaded = await st_load_chat(state.avatar_url, state.chat_file)
        if loaded:
            chat_data = loaded
    else:
        # Start a new chat
        state.chat_file = _new_chat_filename(state.character_name)
        header = _make_chat_header(user_name, state.character_name)
        chat_data = [header]

    # --- Build messages for AI ---
    try:
        preset_data = (await st_get_presets()).get(state.preset_name, {})
        char_data = await st_get_character(state.avatar_url)
    except httpx.ConnectError:
        await at_me.finish("无法连接 SillyTavern，请确认 ST 已启动。")
        return
    except Exception as e:
        await at_me.finish(f"获取角色/预设数据失败: {e}")
        return

    if not char_data:
        await at_me.finish("无法获取角色数据，请检查 SillyTavern 连接。")
        return

    history = _extract_history(chat_data)
    # Format user message: "QQ号对char说，msg"
    formatted_text = f"{user_id}对{state.character_name}说，{text}"
    messages = build_messages(char_data, history, formatted_text)

    # --- Generate response ---
    try:
        response_text = await st_generate(messages, preset_data)
    except httpx.TimeoutException:
        await at_me.finish("AI 响应超时，请稍后重试。")
        return
    except httpx.ConnectError:
        await at_me.finish("无法连接 SillyTavern，请确认 ST 已启动。")
        return
    except httpx.HTTPStatusError as e:
        # Try to get error detail from SillyTavern
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
    except Exception as e:
        logging.exception("Generate error")
        await at_me.finish(f"生成回复时出错: {type(e).__name__}")
        return

    if not response_text or not response_text.strip():
        await at_me.finish("(AI 返回了空响应，请尝试重新发送。)")
        return

    # --- Save chat to ST (原始AI回复，含QQ号) ---
    user_msg = _make_chat_message(user_name, True, formatted_text)
    ai_msg = _make_chat_message(state.character_name, False, response_text)

    # Ensure header exists
    if not any("chat_metadata" in m for m in chat_data):
        chat_data.insert(0, _make_chat_header(user_name, state.character_name))

    chat_data.append(user_msg)
    chat_data.append(ai_msg)
    await st_save_chat(state.avatar_url, state.chat_file, chat_data)

    # --- Reply to QQ (QQ号→昵称) ---
    display_text = _replace_qq_with_nickname(response_text)
    display_text = _truncate(display_text)
    await at_me.finish(Message(display_text))

# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

driver = get_driver()

@driver.on_startup
async def _on_startup():
    """Load config and pre-fetch caches."""
    global ST_BASE_URL, ST_CHAT_SOURCE, ST_MODEL, ST_TIMEOUT
    global ST_MAX_RESPONSE_LENGTH, ST_DEFAULT_PRESET, ST_DEFAULT_CHARACTER

    config = driver.config

    ST_BASE_URL = getattr(config, "st_base_url", "http://127.0.0.1:8000")
    ST_CHAT_SOURCE = getattr(config, "st_chat_source", "deepseek")
    ST_MODEL = getattr(config, "st_model", "")
    ST_TIMEOUT = int(getattr(config, "st_timeout", 120))
    ST_MAX_RESPONSE_LENGTH = int(getattr(config, "st_max_response_length", 800))
    ST_DEFAULT_PRESET = getattr(config, "st_default_preset", "")
    ST_DEFAULT_CHARACTER = getattr(config, "st_default_character", "")

    # Reset URL cache
    global _b64
    _b64 = ST_BASE_URL.rstrip("/")

    logging.info(
        f"ST Bridge loaded: base={ST_BASE_URL}, source={ST_CHAT_SOURCE}, "
        f"model={ST_MODEL or 'from preset'}, timeout={ST_TIMEOUT}s"
    )

    # Pre-fetch caches
    try:
        await st_get_characters()
        logging.info(f"ST Bridge: {len(_chars_cache or [])} characters loaded")
    except Exception as e:
        logging.warning(f"ST Bridge: failed to preload characters: {e}")

    try:
        await st_get_presets()
        logging.info(f"ST Bridge: {len(_presets_cache or {})} presets loaded")
    except Exception as e:
        logging.warning(f"ST Bridge: failed to preload presets: {e}")


@driver.on_shutdown
async def _on_shutdown():
    """Clean up HTTP client."""
    await _close_client()
    logging.info("ST Bridge: client closed")
