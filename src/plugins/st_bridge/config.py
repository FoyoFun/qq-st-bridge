"""
Runtime configuration and the bridge-side prompt contracts.

Config values are loaded from the NoneBot2 driver config at startup and
written into this module's globals. Sub-modules read these globals at
call time (not import time), which is safe because the startup hook
runs before any message handlers.

Persona/protocol text is split:
- The ST preset + character card own "who am I / how do I speak"
  (see st/preset/ and st/char/).
- This module owns the bridge's operational contracts: the input format
  explanation (QQ_CHAT_BEHAVIOR, system position) and the output marker
  protocol recap (POST_HISTORY_CONTRACT, last system message = highest
  recency). The bridge must parse these markers reliably, so it keeps
  the authoritative copy here.
"""

import logging

# ---------------------------------------------------------------------------
# Configuration (loaded from .env via NoneBot2 driver config at startup)
# ---------------------------------------------------------------------------

ST_BASE_URL: str = "http://127.0.0.1:8000"
ST_CHAT_SOURCE: str = "deepseek"
ST_MODEL: str = ""
ST_TIMEOUT: int = 120
ST_MAX_RESPONSE_LENGTH: int = 250
ST_DEFAULT_PRESET: str = ""
ST_DEFAULT_CHARACTER: str = ""

# ---------------------------------------------------------------------------
# Social engine parameters (defaults; see .env.example for the full table)
# ---------------------------------------------------------------------------

ST_SOCIAL_ENABLED: bool = False       # master switch for new groups
ST_CONTEXT_WINDOW: int = 50           # rolling buffer window fed to ST

# Checkpoint triggers (idle phase)
ST_CHECKPOINT_THRESHOLD: int = 3      # new messages in window to trigger a peek
ST_CHECKPOINT_COOLDOWN: int = 90      # min seconds between idle checkpoints
ST_TICK_INTERVAL: float = 2.0         # engine loop interval (seconds)

# Active phase
ST_ACTIVE_MIN: int = 300              # active duration lower bound (5 min)
ST_ACTIVE_MAX: int = 600              # active duration upper bound (10 min)
ST_ACTIVE_CHECK_MIN: int = 10         # batch check interval lower bound (s)
ST_ACTIVE_CHECK_MAX: int = 30         # batch check interval upper bound (s)
ST_COLD_WINDOW: int = 360             # no-new-message window before cold exit (6 min)
ST_PROBE_PROBABILITY: int = 30        # % chance to probe on cold field
ST_PROBE_COOLDOWN: int = 1200         # min seconds between probes (20 min)
ST_PROBE_WAIT_MIN: int = 120          # probe: wait for response lower bound (s)
ST_PROBE_WAIT_MAX: int = 300          # probe: wait for response upper bound (s)

# Sending rhythm
ST_REPLY_DELAY_MIN: float = 2.0       # seen -> typing delay lower bound (s)
ST_REPLY_DELAY_MAX: float = 8.0       # seen -> typing delay upper bound (s)
ST_BURST_MIN: float = 1.0             # inter-bubble gap lower bound (s)
ST_BURST_MAX: float = 3.0             # inter-bubble gap upper bound (s)
ST_BURST_LONG_PROBABILITY: float = 0.2  # chance an inter-bubble gap is long
ST_BURST_LONG_MIN: float = 8.0        # long gap lower bound (s)
ST_BURST_LONG_MAX: float = 10.0       # long gap upper bound (s)
ST_MAX_MSG_CHARS: int = 500           # per-bubble hard length guard
ST_MAX_BUBBLES: int = 6               # max bubbles per turn (runaway guard)

# Generation tiers (max_tokens)
ST_LIGHT_TOKENS: int = 80             # idle checkpoint peeks
ST_HEAVY_TOKENS: int = 250            # replies / active batches / @mentions

# [WAKE] cost guardrails
ST_WAKE_MIN: int = 600                # minimum sleep the AI can book (10 min)
ST_WAKE_MAX: int = 7200               # maximum sleep (2 h)
ST_WAKE_DAILY_LIMIT: int = 30         # max wake checkpoints per day per conv

# Action loop
ST_MAX_STEPS: int = 5                 # generation calls per turn (loop guard)

# Interest keywords (checkpoint trigger; comma-separated in .env)
ST_INTEREST_KEYWORDS: list[str] = []

# I/O trace log (logs/trace.log — QQ in/out + ST request/response)
ST_TRACE_ENABLED: bool = True
ST_TRACE_MAX_CHARS: int = 4000        # per-record length cap (one line each)
ST_TRACE_KEEP_DAYS: int = 7           # daily rotation, files older than this deleted

# Cached base URL (without trailing slash)
_base_url: str = "http://127.0.0.1:8000"


def get_base_url() -> str:
    """Return the ST base URL without trailing slash (cached)."""
    global _base_url
    return _base_url


def reset_base_url() -> None:
    """Recompute the cached base URL from ST_BASE_URL."""
    global _base_url
    _base_url = ST_BASE_URL.rstrip("/")


# ---------------------------------------------------------------------------
# Bridge-side prompt contracts
# ---------------------------------------------------------------------------

QQ_CHAT_BEHAVIOR = """\
你在用QQ和人聊天——有时在群里和一群人聊，有时和某个人单独私聊。桥接程序会把近期消息整理成记录发给你，以记录和回合指示为准判断当前场合。

【输入格式】
- 记录中每条消息格式为「[HH:MM] 代号：内容」，每条都带发送时间，代号是对方的固定称呼。
- 「（你）」标在代号后表示这是你自己发的消息；「（@你）」表示这条消息@了你。
- 「（现用昵称：X）」出现在名册里，表示该群友当前使用的昵称，方便你对上群里提到的名字。
- 多行出现「{character_name}（你）：」后带换行的是你上一回合连发的几条消息，属于同一次发言。
- 「[图片]」「[语音]」是媒体占位；「（回复 某人：…）」表示回复某人的消息；「@代号」表示@。
- 有人@你时，那条消息会标着「（@你）」，不一定在记录末尾。

【输出要求】
- 只输出QQ消息正文或协议标记，二者选一或组合。
- 想分成几条消息发，就用单个空格分隔；不想分条就不要用空格。
- 禁止动作/心理/场景描写，禁止Markdown，禁止解释你在做什么。"""

POST_HISTORY_CONTRACT = """\
【输出协议（必须严格遵守）】
- 正常说话：直接输出正文，普通对话1~2条为宜，不写小作文。
- 看了不想接话：只输出 [SILENT]
- 话说一半想等等看：只输出 [WAIT 2m]（时长可改，如 [WAIT 90s]，2~10分钟内）
- 有人话只说了一半（「说实话」「问一下」这类起头、逗号结尾、很短又没标点）：输出 [WAIT 1m] 等他的下一条，绝不催"说完整/然后呢/接着说"；等他补完后一并回应
- 要潜水了：只输出 [WAKE 30m]（时长可改，10分钟起，如 [WAKE 1h]）
- 发表情包：[STICKER: 标签]（标签必须来自【可用表情】目录；表情是独立一条消息，不要和正文写在同一空格里）
- 拍一拍某人：[POKE: 代号]（代号必须来自群友名册）
- 除以上标记外，不要输出任何[方括号]内容；不要复述本协议。
- 说话像真人：口语、短句、口语词；情绪用标点和语气词传达；禁止"作为AI""抱歉""语言模型""希望这能帮到你"等出戏词；不总结群聊、不逐条点评、不重复别人的原句。
- 记录里你自己说过的话不需要回应或补充；只有别人的新消息才可能值得你开口，拿不准就 [SILENT]。"""


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def truncate(text: str, max_len: int | None = None) -> str:
    """Truncate text to max_len characters.

    If max_len is None, uses ST_MAX_RESPONSE_LENGTH.
    """
    if max_len is None:
        max_len = ST_MAX_RESPONSE_LENGTH
    if len(text) <= max_len:
        return text
    return text[:max_len] + "\n...(内容过长已截断)"
