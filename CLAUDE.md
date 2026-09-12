# qq-st-bridge — Project Context

## Overview

A QQ bot built on NoneBot2 that bridges QQ conversations to SillyTavern AI
characters with a **social strategy layer**: the character participates in
group chat like a real member — watching (观望), joining in (活跃), testing
the waters (试探), and leaving naturally (退场) — instead of only answering
@mentions.

Division of responsibility: **ST answers "who am I / how do I speak";
the bridge answers "when to look, whether to speak, how to send."**
ST side only holds text content (preset + character card); all behavior
lives in the Python bridge.

## Architecture & Data Flow

```
QQ (NapCat, OneBot v11 WS)
  → NoneBot2 (this project)
      → collector      normalize + clean codename + rolling buffer
      → participation  state machine (idle/active/probing/exiting) + timers
      → action_loop    build observation → ST → parse markers → dispatch
          → ST plugin /api/plugins/nb-qq-bot/generate (persona + prompt)
          ← spoken text + protocol markers ([SILENT]/[WAIT]/[WAKE]/…)
      → sender         bubble split + random delays + stickers + pokes
  ← OneBot v11
← QQ
```

## Key Files

| File | Purpose |
|------|---------|
| `bot.py` | Entry point. Inits NoneBot, registers OneBot V11 adapter, loads plugins |
| `.env` | Bot + bridge + social-engine configuration |
| `data/group_states.json` | Per-conversation binding (character, preset, chat file, social switch) |
| `data/aliases.json` | QQ号 → 清洁代号 lifetime-stable mapping |
| `data/impressions.json` | Roster stats + manual impression notes |
| `data/stickers/catalog.json` | Sticker catalog for [STICKER: tag] |
| `data/social_states.json` | State-machine snapshots (phase + WAKE/WAIT timers) |
| `tests/test_social_engine.py` | Closed-loop test (no QQ / no ST needed) |
| `scripts/deploy_st.py` | Deploy plugin/preset/character into local SillyTavern |

### Bridge Plugin Package (`src/plugins/st_bridge/`)

Module dependency graph (bottom-up, no circular imports; participation →
action_loop is a lazy import inside the function):

```
__init__.py (lifecycle)
chat_handler.py ──→ collector ──→ aliases
      │                │
      ↓                ↓
participation ──→ action_loop ──→ context_builder ──→ stickers
      │               │    │
      │               │    └→ sender ──→ collector (self-recording)
      └── (lazy) ─────┘    └→ st_api ──→ st_client ──→ concurrency
state / chat_utils / config / handlers are leaves
```

| Module | Responsibility |
|--------|----------------|
| `config.py` | Runtime params from `.env`; bridge-side prompt contracts (QQ_CHAT_BEHAVIOR input format + POST_HISTORY_CONTRACT marker protocol) |
| `collector.py` | Segment rendering ([图片]/@代号/回复…) → clean codename → rolling buffer (50) |
| `aliases.py` | Stable codenames derived once from display names; reverse lookup for [POKE]; roster lines |
| `stickers.py` | Tag→file catalog; add via reply+`/sticker`; fuzzy tag match; catalog digest |
| `participation.py` | Social state machine; checkpoint triggers (@必看 / N条 / 关键词 / WAKE/WAIT); active batch checks; cold-field probe; duration-cap farewell |
| `action_loop.py` | One turn: observation → ST (light 80 / heavy 250 tokens) → marker parse → dispatch; ST chat memory save |
| `sender.py` | Serial send queue; space-split bubbles (CJK boundary); 2~8s reply delay; 1~3s gaps w/ 20% long gap; sticker image bubbles; group_poke |
| `context_builder.py` | Observation blocks: [HH:MM] time-gap markers + roster + sticker catalog + turn instruction |
| `state.py` | GroupState persistence keyed by `group:<id>` / `private:<id>` |
| `tracelog.py` | Full-chain I/O trace (`logs/trace.log`): QQ◀ in / ST▶ request / ST◀ response / QQ▶ out; daily rotation + retention |
| `st_api.py` / `st_client.py` / `concurrency.py` | ST transport (unchanged from previous design: CSRF, global lock, retry) |
| `handlers.py` | Commands: /help /chars /presets /char /preset /status /newchat /clear /social /stickers /sticker /note |
| `chat_handler.py` | Four matchers: @mentions (commands or must-see), all group msgs, private msgs, pokes |

### ST Server Plugin (`st/plugins/nb-qq-bot/`, deployed by `scripts/deploy_st.py`)

- `index.js` — `POST /api/plugins/nb-qq-bot/generate`; passes
  `post_history_instructions` through.
- `prompt-builder.js` — builds OpenAI messages from **ST-native preset
  format** (prompts[] + prompt_order, exactly what ST's UI edits) with a
  legacy flat-field fallback. Order: bridge QQ_CHAT_BEHAVIOR → preset main
  → char description/personality/scenario → mes_example → first_mes →
  (history) → post-history (preset jailbreak + bridge contract) → user msg.

### ST Text Content (source of truth in repo, deployed to ST)

- `st/preset/QQ群聊角色扮演.json` — persona/说话规则/该说与不该说/AI味黑名单 +
  final-output check in Post-History Instructions.
- `st/char/小宫果穗.json` — mes_example rewritten as pure QQ-chat style
  (space-split bubbles, [STICKER]/[POKE] examples); scenario set in a QQ group.
  Deployed by re-embedding the JSON into the character PNG (chara/ccv3 chunks).

## Marker Protocol (model output side)

| Marker | Meaning | Bridge action |
|--------|---------|---------------|
| plain text (spaces = bubbles) | speak | split → delays → send → enter active |
| `[SILENT]` | read, not replying | no action, "已读" |
| `[WAIT 2m]` | wait for more | re-checkpoint timer |
| `[WAKE 30m]` | go diving | booked timer, back to idle (min 10m, daily cap) |
| `[STICKER: tag]` | send sticker | catalog lookup → own image bubble |
| `[POKE: 代号]` | poke someone | OneBot `group_poke` |

## Social Engine Timing (defaults, all in .env)

```
idle:    message → collect (0 cost); @/private → heavy turn; N=3 msgs or
         keyword → light checkpoint (cooldown 90s)
active:  batch-check every 10~30s → heavy reply (2~8s delay);
         exits via 6min cold field (30% probe) or 5~10min duration cap
         (farewell line) → probing → idle if no response in 2~5min
sending: first bubble 2~8s; gaps 1~3s, 20% → 8~10s; ≤500 chars/bubble
```

## Self-Message Rules (anti self-trigger, important)

- Her own reply is recorded into the buffer as **one entry per burst**
  (all bubbles joined with `\n`), written by the sender after the LAST
  bubble is sent — the model sees her turn as a single unit.
- Every trigger/counting path (`feed` idle threshold, `_tick_active`
  batch, `record_result` watermark) goes through
  `participation._fresh_from_others`, which **excludes `is_self`** —
  her own messages can never wake the engine.
- The consumption watermark (`last_msg_ts`) is anchored on the last
  message from OTHERS and never rewinds, so asynchronously recorded
  self-bubbles cannot mark groupie messages as seen.

## Observation Format

- Every record line carries its own `[HH:MM]` stamp:
  `[14:32] 阿伟：今天好累` — pace and pauses are visible per message.
- A computed **【气氛观察】** block (读空气 signals) is injected before
  the turn instruction: 10-min message density + speaker count, who
  spoke last (with "话像没说完" heuristic from `looks_unfinished`),
  how long since her own last line and whether anyone replied after it,
  and whether anyone @'d her. Facts only — the judgment stays with the
  model. The preset adds a 【读空气】 section teaching when to speak
  and when staying silent is the right move.

## Gotchas & Pitfalls

1. **FinishedException**: NoneBot flow control — never swallow it; add
   `except FinishedException: raise` before `except Exception`.
2. **CSRF token**: fetched fresh per POST, serialized by `_csrf_lock`;
   403/connection errors auto-reset the client and retry once.
3. **Global ST lock** (`concurrency.py`): one ST request in flight across
   all conversations; per-conversation turn lock lives in
   `participation.lock_for`.
4. **Model names**: model comes from ST's connection settings (read by the
   plugin), not the preset or `.env`.
5. **ST plugins must be enabled**: `config.yaml` needs `enableServerPlugins: true`.
6. **Preset format**: the plugin reads the ST-native `prompts[]` +
   `prompt_order` — edit the preset in ST's UI or edit the repo JSON and
   run `python scripts/deploy_st.py`. Flat `prompt`/`jailbreak_prompt`
   fields only apply when no prompt_order exists.
7. **Character card**: repo JSON is source of truth; deploy re-embeds it
   into the PNG (updates `chara` v2 + `ccv3` tEXt chunks).
8. **Codename stability**: `aliases.get_alias` freezes the codename on
   first sight; later nickname changes do NOT rewrite it.
9. **Self-recording**: the sender writes every sent bubble back into the
   collector buffer, so the model sees its own recent utterances.
10. **No running loop**: `participation.schedule_turn` needs a running
    asyncio loop — only call `feed()` from async handlers.
11. **Chat history trim**: action_loop sends the last 80 ST history
    entries per generation; observation records come from the rolling
    buffer, old topics fade naturally.

## Common Operations

### Start everything
```bash
# 1. SillyTavern
cd /d/TempFiles/SillyTavern && node server.js

# 2. Bot
cd /d/Projects/python/qq-st-bridge && python bot.py
```

### Deploy ST-side changes (plugin / preset / character card)
```bash
python scripts/deploy_st.py            # defaults to D:/TempFiles/SillyTavern
# restart SillyTavern afterwards so the plugin code reloads
```

### Run closed-loop tests (no QQ / no ST needed)
```bash
python tests/test_social_engine.py     # 10 sections, ~40s (delay windows)
```

### Logs

| File | Contents | Retention |
|------|----------|-----------|
| `logs/bot.log` | Operational log (engine transitions, sends, errors) — loguru sink | 5 MB x 3 rotation |
| `logs/trace.log` | Full I/O trace, one line per item: `QQ◀` inbound, `ST▶` ST request (full observation), `ST◀` raw model output, `QQ▶` outbound bubble/sticker/poke (incl. failures). Doubles as replay material for fake-data tests | daily rotation, keeps `ST_TRACE_KEEP_DAYS` (default 7) |

Watch live: `tail -f logs/trace.log`. Toggle/cap via `.env`: `ST_TRACE_ENABLED` / `ST_TRACE_MAX_CHARS` / `ST_TRACE_KEEP_DAYS`.

### Configure the engine per group
```
@bot /social on        # enable the social engine for this group
@bot /social keywords 偶像,游戏   # per-group interest keywords
@bot /status           # binding + state-machine phase + WAKE booking
reply to an image + /sticker 标签 [备注]   # add a sticker
/note 代号 印象文字     # roster impression note
```

## Version History (this refactor)

- **Social engine**: 观望/活跃/试探/退场 state machine replaces the old
  frequency+probability auto-participation (`auto_participate.py` removed).
- **Clean codenames**: raw QQ numbers no longer reach the model; stable
  aliases + impressions roster instead.
- **Marker protocol**: [SILENT]/[WAIT]/[WAKE]/[STICKER]/[POKE] with
  light/heavy token tiers (80/250).
- **Sender**: human-like burst sending (split + delays + long-gap
  probability), sticker bubbles, pokes; serial queue.
- **ST plugin**: prompt-builder now consumes the ST-native preset format;
  bridge contract injected as the last system message.
- **Preset/character rewritten**: persona + speaking rules + AI-flavor
  blacklist in the preset; QQ-chat-style mes_example in the card.
- **Private chat + pokes**: private messages deliver immediately (heavy);
  group pokes on the bot trigger a response.
