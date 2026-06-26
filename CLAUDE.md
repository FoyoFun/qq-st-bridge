# nb_qq_bot — Project Context

## Overview

A QQ bot built on NoneBot2 that bridges QQ group chat to SillyTavern AI characters.
When a user @mentions the bot, the message is forwarded to a SillyTavern server plugin,
which builds the prompt, calls the AI backend (DeepSeek), and returns the response.

## Architecture & Data Flow

```
QQ Group @bot "hello"
  → QQ Server → NapCat (QQ bot client)
    → OneBot V11 WebSocket → NoneBot2 (Python, this project)
      → ST Plugin /api/plugins/nb-qq-bot/generate
        → ST internally: load char + preset → build prompt → call AI
        ← AI response
      ← ST Plugin
    ← OneBot V11 WebSocket
  ← QQ Group: AI reply
```

## Key Files

| File | Purpose |
|------|---------|
| `bot.py` | Entry point. Inits NoneBot, registers OneBot V11 adapter, loads plugins |
| `src/plugins/st_bridge.py` | **Core plugin (~650 lines)** — the SillyTavern bridge logic |
| `.env` | Bot + ST bridge configuration (host, port, timeout, etc.) |
| `data/group_states.json` | Persisted group state — character, preset, chat per group (survives restarts) |
| `pyproject.toml` | Python project metadata, NoneBot adapter config |

### ST Server Plugin

| File | Purpose |
|------|---------|
| `..\SillyTavern\plugins\nb-qq-bot\index.js` | Plugin entry — registers `POST /api/plugins/nb-qq-bot/generate` |
| `..\SillyTavern\plugins\nb-qq-bot\prompt-builder.js` | Prompt construction — builds OpenAI-format messages from character + preset + history |

## st_bridge.py — Plugin Structure

The plugin is organized into these sections:

1. **Config** — Global variables loaded from `.env` via `driver.config` at startup
2. **GroupState** — Per-group dataclass: `character_name`, `preset_name`, `avatar_url`, `chat_file`. Persisted to `data/group_states.json` on every change; auto-restored on startup via `_load_states()` / `_save_states()`.
3. **NicknameMap** — `_nickname_map` (QQ号→昵称) and `_replace_qq_with_nickname()` for reply conversion
4. **StClient** — `httpx.AsyncClient` wrapper for SillyTavern API:
   - CSRF token management (fetch fresh token before each POST)
   - `st_get_characters()`, `st_get_presets()`, `st_get_character(avatar_url)`
   - `st_load_chat()`, `st_save_chat()`, `st_plugin_generate()` ← calls ST plugin for prompt building + AI generation
5. **Chat History** — Read/write SillyTavern's JSONL chat files via ST API
6. **Command handlers** — `/chars`, `/presets`, `/char`, `/preset`, `/status`, `/newchat`, `/clear`, `/help`
7. **Message handler** — `on_message(rule=to_me() & is_type(GroupMessageEvent))`

### Message Flow (detailed)

1. QQ group message with @mention arrives via OneBot WebSocket
2. NoneBot2 auto-strips the @mention, sets `event.to_me = True`
3. Handler checks: if empty → help; if `/cmd` → route to command; else → chat
4. Chat flow: load ST chat history → `st_plugin_generate()` → ST plugin loads character + preset, builds full prompt, calls AI → save back to ST → reply to QQ

### User message format

Messages are formatted before sending to AI using QQ numbers as stable identifiers:
```
{QQ号}：{original_text}
```
Example: `2254425209：你好`

### QQ号 → 昵称 双向映射

- **发出**: 用 `event.user_id`（QQ号）代替昵称作为发言者标识
- **返回**: AI 回复中的 QQ 号会自动替换回对应的 QQ 昵称，再发送到群聊
- 映射存储在全局 `_nickname_map: dict[str, str]` 中，每次收到消息时更新
- 仅替换已知的 QQ 号，消息原文中的其他名字不受影响

### SillyTavern API endpoints used

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/csrf-token` | GET | CSRF token + session cookie |
| `/api/characters/all` | POST | List all characters |
| `/api/characters/get` | POST | Get character card data |
| `/api/settings/get` | POST | List presets (openai_setting_names + contents) |
| `/api/chats/get` | POST | Load chat history (JSONL) |
| `/api/chats/save` | POST | Save chat history |
| `/api/plugins/nb-qq-bot/generate` | POST | **Main call** — build prompt + generate AI response in one request |

### SillyTavern CSRF flow

ST uses `csrf-sync` which ties CSRF tokens to session cookies. The `httpx.AsyncClient` handles cookies automatically:
1. `GET /csrf-token` → ST sets session cookie in response, returns token
2. `POST /api/...` with `X-CSRF-Token` header + cookie from step 1
3. Always fetch fresh token before each POST

## External Services

### SillyTavern
- **Path**: `D:\TempFiles\SillyTavern`
- **Start**: `node server.js` (port 8000)
- **Stop**: Ctrl+C or kill the node process
- **Config**: `D:\TempFiles\SillyTavern\config.yaml` (whitelist includes 127.0.0.1)

## Common Operations

### Start everything
```bash
# 1. SillyTavern
cd D:\TempFiles\SillyTavern && node server.js &

# 2. Bot
cd D:\Projects\python\nb_qq_bot && python bot.py &
```

### Restart bot after code changes
```bash
# Stop the background task, then restart
cd D:\Projects\python\nb_qq_bot && python bot.py &
```

### Run tests
```bash
# Test ST API connectivity
python -c "
import httpx, asyncio
async def test():
    client = httpx.AsyncClient()
    r = await client.get('http://127.0.0.1:8000/csrf-token')
    print('CSRF:', r.json()['token'][:20])
    await client.aclose()
asyncio.run(test())
"
```

## Gotchas & Pitfalls

1. **FinishedException**: `at_me.finish()` raises `FinishedException` (NoneBot2's normal flow control). NEVER catch it in try/except blocks — always add `except FinishedException: raise` before `except Exception`.

2. **CSRF token**: Must be fresh for each POST. The session cookie is handled by httpx's cookie jar automatically.

3. **Model names**: Model is configured in the ST preset (not in `.env`). The `ST_MODEL` field is deprecated and should be left empty. Valid DeepSeek models: `deepseek-v4-flash`, `deepseek-v4-pro`.

4. **ST plugins must be enabled**: `config.yaml` needs `enableServerPlugins: true` for the nb-qq-bot plugin to load.

4. **NapCat is a GUI app**: Cannot run from Git Bash directly. Must use `cmd.exe /c start` to launch. The QQ login window appears on the Windows desktop.

5. **ST whitelist**: SillyTavern's default config only allows `127.0.0.1` and `::1`. If moving to remote, update `config.yaml` whitelist.

6. **Chat history format**: ST uses JSONL (one JSON per line). First line = header with `chat_metadata`. Subsequent lines = messages with `is_user`, `mes`, `send_date`, `name`.

7. **Character avatar_url**: The `avatar` field from `/api/characters/all` is used as the `avatar_url` parameter for all other character/chat API calls.

## SillyTavern Plugin (nb-qq-bot)

Located at `D:\TempFiles\SillyTavern\plugins\nb-qq-bot\`. This is a server-side ST plugin that exposes:

- `POST /api/plugins/nb-qq-bot/generate` — one-shot prompt building + AI generation

The plugin is **independent of ST source code** (no imports from `src/`). It uses its own
prompt builder that constructs system prompts from character cards, preset templates, and chat history.
The `plugins/` directory is in ST's `.gitignore`, so this plugin is unaffected by upstream ST updates.

**Plugin internals**:
1. Receives `avatar_url`, `preset_name`, `chat_history`, `user_message`
2. Fetches character card via ST's own `/api/characters/get`
3. Fetches preset/settings via ST's own `/api/settings/get`
4. Resolves model from ST's connection settings (`data/default-user/OpenAI Settings/*.json` → `{source}_model`)
5. Builds messages array (system prompt + history + user message)
6. Calls ST's own `/api/backends/chat-completions/generate`
7. Returns the AI response

**Model resolution**: The plugin no longer reads model from the preset (`preset.openai_model`).
Instead, it reads ST's connection profile files to find `{source}_model` (e.g. `deepseek_model`),
so the model follows whatever is configured in SillyTavern's connection settings.

## Version History

- `31050cc` — Initial commit: bot + st_bridge plugin
- `aa3d4c2` — Message format: `user对char说，msg`
- `3ee270e` — docs: README.md and CLAUDE.md
- `e5e2de3` — Experiment with literal `{{user}}`/`{{char}}` macros (reverted)
- `b4d5eeb` — Save formatted message to ST chat history
- `24337fe` — Use actual QQ name + char name (not template macros)
- `761ef01` — QQ号 as user ID + bidirectional nickname mapping
- current — Moved prompt building to ST server plugin, removed `build_messages()`/`st_generate()`
- current — Added group state persistence (`data/group_states.json`), survives bot restarts
- current — Simplified user message format: `{QQ号}：{msg}` (was `{QQ号}对{char}说，{msg}`)
- current — Model resolution: read from ST connection settings instead of preset
