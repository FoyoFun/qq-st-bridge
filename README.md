# nb_qq_bot

基于 NoneBot2 的 QQ 机器人，通过 ST 插件将 QQ 群聊与 SillyTavern AI 角色连接。

## 架构

```
QQ群 → NapCat(QQ) → OneBot V11 WS → NoneBot2 → ST 插件(本地API) → ST 内部 → AI Backend
                                                                              ↓
QQ群 ← NapCat(QQ) ← OneBot V11 WS ← NoneBot2 ←        ST 插件       ←──────┘
```

Prompt 构建由 ST 服务端插件（`plugins/nb-qq-bot/`）负责，复用 ST 的角色卡数据、预设参数，
保证回复质量与 ST 网页版一致。nb_qq_bot 不再手工拼贴 prompt。

## 项目结构

```
nb_qq_bot/
├── bot.py                  # NoneBot2 入口，注册 OneBot V11 适配器
├── pyproject.toml          # 项目元数据 & NoneBot2 配置
├── .env                    # 机器人和 ST Bridge 配置
├── README.md               # 项目说明
├── CLAUDE.md               # Claude Code 项目上下文
├── src/
│   └── plugins/
│       └── st_bridge.py    # SillyTavern Bridge 插件（核心）

SillyTavern/                # ST 根目录（独立仓库）
└── plugins/
    └── nb-qq-bot/          # ST 服务端插件（本项目配套）
        ├── index.js         #   插件入口，路由注册
        └── prompt-builder.js #   Prompt 构建器
```

## 依赖服务

| 服务 | 路径 | 端口 | 说明 |
|------|------|------|------|
| **SillyTavern** | `path/to/SillyTavern` | `8000` | AI 角色聊天前端，含 nb-qq-bot 插件 |
| **NapCat** | `path/to/NapCat` | `6099` (WebUI) | QQ 机器人框架，OneBot V11 协议 |

## 快速启动

### 1. 启动 SillyTavern
```bash
cd path/to/SillyTavern
node server.js
# → http://127.0.0.1:8000
# ST 启动时自动加载 plugins/nb-qq-bot/ 插件
# 前提：config.yaml 中 enableServerPlugins: true
```

### 2. 启动 NapCat（QQ 机器人）
```bash
cd path/to/NapCat/bootmain
.\NapCatWinBootMain.exe
# 扫码登录 QQ，自动连接 ws://127.0.0.1:8080/onebot/v11/ws
```

### 3. 启动 nb_qq_bot
```bash
cd path/to/nb_qq_bot
python3 bot.py
# → http://127.0.0.1:8080
```

### 4. 在 QQ 群中使用
```
@bot /help      # 查看所有命令
@bot /chars     # 列出 SillyTavern 角色
@bot /char XX   # 选择角色
@bot /presets   # 列出预设
@bot /preset XX # 选择预设
@bot <消息>     # 与 AI 对话
```

> **消息格式**：发给 AI 时会用 QQ 号代替昵称（`123456789对Seraphina说，你好`），
> 避免奇怪群名污染 AI 理解。AI 回复中的 QQ 号会自动换回昵称再发到群聊。

## 配置 (.env)

```env
HOST=127.0.0.1
PORT=8080

# SillyTavern Bridge
ST_BASE_URL=http://127.0.0.1:8000
ST_CHAT_SOURCE=deepseek
ST_TIMEOUT=120
ST_MAX_RESPONSE_LENGTH=800
ST_DEFAULT_CHARACTER=
ST_DEFAULT_PRESET=

# ST_MODEL 已废弃 — 模型由 ST 预设配置，不要在此设置
# 请勿在等号后面写注释，会被当值读取
ST_MODEL=
```

## 关键变更

与旧版相比，当前版本的 Prompt 构建已从 Python 端（`build_messages()`）迁移至
SillyTavern 服务端插件。Python 端只需调用一次 `POST /api/plugins/nb-qq-bot/generate`，
插件负责加载角色卡、预设、构建 prompt、调用 AI、返回回复。

详见 `CLAUDE.md` 中的 "SillyTavern Plugin" 章节。

## 依赖

- Python >= 3.9
- nonebot2 >= 2.5.0
- nonebot-adapter-onebot >= 2.4.0
- httpx
- fastapi + uvicorn
