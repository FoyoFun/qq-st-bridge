"""
Full-chain I/O trace — every QQ input/output and ST request/response.

A dedicated trace file (logs/trace.log, one line per item, rotated daily
at midnight, auto-deleted after N days) recording the four stages of the
pipeline:

    QQ◀  inbound QQ message / social event   (collector)
    ST▶  request sent to SillyTavern         (st_api.plugin_generate)
    ST◀  response returned by SillyTavern    (st_api.plugin_generate)
    QQ▶  outbound QQ bubble/sticker/poke     (sender)

Purpose: locating problems, tuning prompts, and replaying conversations
with fake data. The trace logger does not propagate — nothing extra is
printed to the console or the main bot.log; tail the file instead:

    tail -f logs/trace.log
"""

import logging
import os
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

# Dedicated logger: DEBUG level, no propagation (keeps the console and
# logs/bot.log clean), its own daily-rotating handler with retention.
_trace_logger = logging.getLogger("st_bridge.trace")
_trace_logger.propagate = False
_trace_logger.setLevel(logging.DEBUG)

_handler = None
_default_logs_dir = Path(__file__).resolve().parent.parent.parent.parent / "logs"

# Set from config at startup
enabled: bool = True
max_chars: int = 4000


def setup(
    logs_dir: str | os.PathLike | None = None,
    trace_enabled: bool = True,
    trace_max_chars: int = 4000,
    keep_days: int = 7,
) -> None:
    """Attach the rotating trace file handler (called at startup)."""
    global _handler, enabled, max_chars
    enabled = trace_enabled
    max_chars = max(200, trace_max_chars)

    # Re-entrant: drop the previous handler if startup runs twice
    if _handler is not None:
        _trace_logger.removeHandler(_handler)
        _handler.close()
        _handler = None

    if not enabled:
        _trace_logger.disabled = True
        return
    _trace_logger.disabled = False

    directory = Path(logs_dir) if logs_dir else _default_logs_dir
    directory.mkdir(parents=True, exist_ok=True)
    _handler = TimedRotatingFileHandler(
        directory / "trace.log",
        when="midnight",        # 轮转：每天 0 点切新文件
        backupCount=max(1, keep_days),  # 清理：只保留最近 N 天，过期自动删除
        encoding="utf-8",
    )
    _handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
    _trace_logger.addHandler(_handler)
    _trace_logger.info("=== trace started (max_chars=%d, keep=%dd) ===", max_chars, keep_days)


def _one_line(text: str, extra_head: str = "") -> str:
    """Escape newlines and cap the length so every record stays one line."""
    raw = str(text if text is not None else "")
    line = raw.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "")
    limit = max_chars - len(extra_head)
    if len(line) > limit:
        line = line[: max(0, limit)] + f"...(截断，全长{len(raw)}字符)"
    return line


def qq_in(conv: str, alias: str, text: str) -> None:
    """One inbound QQ message (or social event) as seen by the collector."""
    _trace_logger.info("QQ◀ [%s] %s: %s", conv, alias, _one_line(text, extra_head=conv))


def st_request(
    conv: str, tier: str, reason: str, history_count: int,
    user_message: str, max_tokens: int,
) -> None:
    """One request dispatched to the ST plugin (observation + context)."""
    head = f"[{conv}] tier={tier} reason={reason} tokens={max_tokens} history={history_count}"
    _trace_logger.info("ST▶ %s | %s", head, _one_line(user_message, extra_head=head))


def st_response(conv: str, ok: bool, response_text: str, error: str = "") -> None:
    """The raw model output returned by SillyTavern."""
    if ok:
        _trace_logger.info("ST◀ [%s] ok=True | %s", conv, _one_line(response_text, extra_head=conv))
    else:
        _trace_logger.warning(
            "ST◀ [%s] ok=False | %s", conv, _one_line(error or response_text, extra_head=conv)
        )


def qq_out(conv: str, kind: str, content: str, ok: bool = True, detail: str = "") -> None:
    """One outbound QQ bubble / sticker / poke (after the send attempt)."""
    head = f"[{conv}] {kind}"
    body = _one_line(content, extra_head=head)
    suffix = "" if ok else f" | FAILED: {detail}"
    ( _trace_logger.info if ok else _trace_logger.warning )(
        "QQ▶ %s | %s%s", head, body, suffix
    )
