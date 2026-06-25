# nb_qq_bot — Project Context

## Overview

A QQ bot built on NoneBot2 that bridges QQ group chat to SillyTavern AI characters.
When a user @mentions the bot, the message is forwarded to SillyTavern's API, which proxies
to the configured AI backend (DeepSeek). The AI's response is returned to the QQ group.

## Architecture & Data Flow

```
QQ Group @bot "hello"
  → QQ Server → NapCat (QQ bot client)
    → OneBot V11 WebSocket → NoneBot2 (Python, this project)
      → SillyTavern HTTP API (Node.js, D:\TempFiles\SillyTavern)
        → AI Backend (DeepSeek API)
      ← AI response
    ← OneBot V11 WebSocket
  ← QQ Group: AI reply
```

## Key Files

| File | Purpose |
|------|---------|
| `bot.py` | Entry point. Inits NoneBot, registers OneBot V11 adapter, loads plugins |
| `src/plugins/st_bridge.py` | **Core plugin (~700 lines)** — the entire SillyTavern bridge logic |
| `.env` | Bot + ST bridge configuration (host, port, model, timeout, etc.) |
| `pyproject.toml` | Python project metadata, NoneBot adapter config |

## st_bridge.py — Plugin Structure

The plugin is organized into these sections:

1. **Config** — Global variables loaded from `.env` via `driver.config` at startup
2. **GroupState** — Per-group dataclass: `character_name`, `preset_name`, `avatar_url`, `chat_file`
3. **StClient** — `httpx.AsyncClient` wrapper for SillyTavern API:
   - CSRF token management (fetch fresh token before each POST)
   - `st_get_characters()`, `st_get_presets()`, `st_get_character(avatar_url)`
   - `st_load_chat()`, `st_save_chat()`, `st_generate()`
4. **PromptBuilder** — `build_messages()` constructs the OpenAI-format messages array
5. **Chat History** — Read/write SillyTavern's JSONL chat files via ST API
6. **Command handlers** — `/chars`, `/presets`, `/char`, `/preset`, `/status`, `/newchat`, `/clear`, `/help`
7. **Message handler** — `on_message(rule=to_me() & is_type(GroupMessageEvent))`

### Message Flow (detailed)

1. QQ group message with @mention arrives via OneBot WebSocket
2. NoneBot2 auto-strips the @mention, sets `event.to_me = True`
3. Handler checks: if empty → help; if `/cmd` → route to command; else → chat
4. Chat flow: load ST chat history → build messages array → call `/api/backends/chat-completions/generate` → save back to ST → reply to QQ

### User message format

Messages are formatted before sending to AI:
```
{user_name}对{character_name}说，{original_text}
```
Example: `张三对Seraphina说，你好`

### SillyTavern API endpoints used

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/csrf-token` | GET | CSRF token + session cookie |
| `/api/characters/all` | POST | List all characters |
| `/api/characters/get` | POST | Get character card data |
| `/api/settings/get` | POST | List presets (openai_setting_names + contents) |
| `/api/chats/get` | POST | Load chat history (JSONL) |
| `/api/chats/save` | POST | Save chat history |
| `/api/backends/chat-completions/generate` | POST | Generate AI response (OpenAI-format proxy) |

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

### NapCat (QQ Bot)
- **Path**: `D:\TempFiles\NapCatShellOneKey\bootmain`
- **Start**: `NapCatWinBootMain.exe` (GUI app, launches QQ login window)
- **WebUI**: `http://127.0.0.1:6099/webui` (token: `665fc923821e`)
- **Config**: `.../napcat/config/onebot11_3524611244.json` (WS client → `ws://127.0.0.1:8080/onebot/v11/ws`)

## Common Operations

### Start everything
```bash
# 1. SillyTavern
cd D:\TempFiles\SillyTavern && node server.js &

# 2. NapCat (Windows GUI — needs desktop access for QR scan)
cmd.exe /c "start D:\TempFiles\NapCatShellOneKey\bootmain\NapCatWinBootMain.exe"

# 3. Bot
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

3. **Model names**: When `chat_completion_source` is `deepseek`, the model must be a valid DeepSeek model (`deepseek-chat`, `deepseek-v4-flash`, `deepseek-v4-pro`). Using `gpt-4-turbo` with `deepseek` source causes 400 errors.

4. **NapCat is a GUI app**: Cannot run from Git Bash directly. Must use `cmd.exe /c start` to launch. The QQ login window appears on the Windows desktop.

5. **ST whitelist**: SillyTavern's default config only allows `127.0.0.1` and `::1`. If moving to remote, update `config.yaml` whitelist.

6. **Chat history format**: ST uses JSONL (one JSON per line). First line = header with `chat_metadata`. Subsequent lines = messages with `is_user`, `mes`, `send_date`, `name`.

7. **Character avatar_url**: The `avatar` field from `/api/characters/all` is used as the `avatar_url` parameter for all other character/chat API calls.

## Version History

- `31050cc` — Initial commit: bot + st_bridge plugin
- `aa3d4c2` — Message format: `user对char说，msg`
