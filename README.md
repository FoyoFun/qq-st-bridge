# nb_qq_bot

基于 NoneBot2 的 QQ 机器人，通过 SillyTavern Bridge 插件将 QQ 群聊与 SillyTavern AI 角色连接。

## 架构

```
QQ群 → NapCat(QQ) → OneBot V11 WebSocket → NoneBot2 → SillyTavern API → AI Backend
                                                                        ↓
QQ群 ← NapCat(QQ) ← OneBot V11 WebSocket ← NoneBot2 ← SillyTavern API ←─┘
```

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
│       ├── README.md       # 插件目录说明
│       └── st_bridge.py    # SillyTavern Bridge 插件（核心）
```

## 依赖服务

| 服务 | 路径 | 端口 | 说明 |
|------|------|------|------|
| **SillyTavern** | `D:\TempFiles\SillyTavern` | `8000` | AI 角色聊天前端，提供 API 代理 |
| **NapCat** | `D:\TempFiles\NapCatShellOneKey` | `6099` (WebUI) | QQ 机器人框架，OneBot V11 协议 |

## 快速启动

### 1. 启动 SillyTavern
```bash
cd D:\TempFiles\SillyTavern
node server.js
# → http://127.0.0.1:8000
```

### 2. 启动 NapCat（QQ 机器人）
```bash
cd D:\TempFiles\NapCatShellOneKey\bootmain
.\NapCatWinBootMain.exe
# 扫码登录 QQ，自动连接 ws://127.0.0.1:8080/onebot/v11/ws
```

### 3. 启动 nb_qq_bot
```bash
cd D:\Projects\python\nb_qq_bot
python bot.py
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

> **消息格式**：发给 AI 时会用 QQ 号代替昵称（`2254425209对Seraphina说，你好`），
> 避免奇怪群名污染 AI 理解。AI 回复中的 QQ 号会自动换回昵称再发到群聊。

## 配置 (.env)

```env
HOST=127.0.0.1
PORT=8080

# SillyTavern Bridge
ST_BASE_URL=http://127.0.0.1:8000
ST_CHAT_SOURCE=deepseek
ST_MODEL=deepseek-chat
ST_TIMEOUT=120
ST_MAX_RESPONSE_LENGTH=800
```

## 依赖

- Python >= 3.9
- nonebot2 >= 2.5.0
- nonebot-adapter-onebot >= 2.4.0
- httpx
- fastapi + uvicorn
