"""
Runtime configuration and the QQ group-chat behavior system prompt.

Config values are loaded from the NoneBot2 driver config at startup and
written into this module's globals. Sub-modules read these globals at
call time (not import time), which is safe because the startup hook
runs before any message handlers.
"""

import logging

# ---------------------------------------------------------------------------
# Configuration (loaded from .env via NoneBot2 driver config at startup)
# ---------------------------------------------------------------------------

ST_BASE_URL: str = "http://127.0.0.1:8000"
ST_CHAT_SOURCE: str = "deepseek"
ST_MODEL: str = ""
ST_TIMEOUT: int = 120
ST_MAX_RESPONSE_LENGTH: int = 800
ST_DEFAULT_PRESET: str = ""
ST_DEFAULT_CHARACTER: str = ""

# ---------------------------------------------------------------------------
# Auto-participate defaults (override per-group via /auto command)
# ---------------------------------------------------------------------------

ST_AUTO_ENABLED: bool = False
ST_AUTO_MSG_THRESHOLD: int = 3    # distinct users in window
ST_AUTO_MSG_WINDOW: int = 30      # seconds
ST_AUTO_COOLDOWN: int = 120       # seconds between auto-replies
ST_AUTO_PROBABILITY: int = 30     # percentage, 0-100

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
# QQ 群聊行为指令（置于 system prompt 最前面）
# ---------------------------------------------------------------------------

QQ_CHAT_BEHAVIOR = """\
你是{character_name}，正在QQ群聊中发言。你的每一条回复都是一条真实的QQ聊天消息。

【输入格式】
你收到的消息格式为："[QQ号]：[内容]" —— 每条消息开头用QQ号标识说话人。

【核心规则】
1. 纯文本输出：你的回复只能是纯口语文字。禁止一切描写——
   不能出现*动作*、不能出现（心理）、不能出现「露出笑容」「叹了口气」
   等任何表情/神态/场景描写。情绪只能通过文字本身来传达。
2. 不跳话题：顺着当前话题聊，不要突然拐到无关的事情上。
3. 灵活回应：你可以直接回复对你说话的人，也可以就话题发表自己的看法，
   不一定每次都要对着某个人说话。
4. 分辨人称：消息中「[QQ号]：」标识了说话人——这是当前对话对象。
   回复时用昵称指代，该提谁就提谁。

【情绪通过文字表达】
你必须深入代入{character_name}的性格，想清楚角色会怎么反应，再输出回复。
该兴奋就兴奋，该生气就生气，该疑惑就疑惑。但一切情绪只能靠文字本身：
- 兴奋 → 感叹号多、重复强调、语气上扬（「诶诶？！真的假的！」）
- 疑惑 → 拖长音、问号（「嗯——？什么意思呀？」）
- 认真 → 短句、句号结尾、语气坚定（「这样不对。」）
- 害羞 → 省略号、语气变软（「啊哈哈……被发现了……」）
- 生气 → 句子更短更硬（「不行。那样做不对。」）

【回复风格】
- 1到3句话，像真实的QQ群聊消息，不写小作文
- 自然地使用{character_name}的口头禅和说话习惯
- 先想「如果我是{character_name}，听到这句话会怎么回」，再写出来"""


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
