#!/usr/bin/env python3
"""NoneBot2 QQ 机器人入口文件"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# ---------------------------------------------------------------------------
# 文件日志：所有日志（NoneBot 内部 + 插件）写入 logs/bot.log，轮转 5MB x 3
# nonebot.init() 会自动把标准 logging 桥接到 loguru（LoguruHandler），
# 这里只需给 loguru 加一个文件 sink，并把 root logger 级别提到 INFO
# （在 nonebot.init 之后设置，避免重复桥接造成日志双份）。
# ---------------------------------------------------------------------------

from nonebot.log import logger as _nb_logger

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "bot.log"

_nb_logger.add(
    str(LOG_FILE),
    rotation="5 MB",
    retention=3,
    encoding="utf-8",
    level="INFO",
    backtrace=False,
    diagnose=False,
)

import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

# 初始化 NoneBot
nonebot.init()

# 插件模块使用标准 logging —— 提高 root 级别让 INFO 也走 loguru 落盘
logging.getLogger().setLevel(logging.INFO)

# 注册 OneBot V11 适配器
driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)

# 加载 src/plugins 下的所有插件
nonebot.load_plugins("src/plugins")

if __name__ == "__main__":
    nonebot.run()
