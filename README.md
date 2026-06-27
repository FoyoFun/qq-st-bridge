# nb_qq_bot

将 QQ 群聊与 **SillyTavern AI 角色** 连接的机器人。  
在群里 @机器人，即可与 SillyTavern 中的 AI 角色实时对话。

## 架构

```
QQ 群成员 @机器人 "你好"
  │
  ▼
NapCat (QQ 客户端) ──WebSocket (OneBot V11)──▶ NoneBot2 (Python)
                                                   │
                                                   │ POST /api/plugins/nb-qq-bot/generate
                                                   ▼
                                         SillyTavern 服务端
                                         ┌──────────────────────┐
                                         │ nb-qq-bot 插件       │
                                         │  → 加载角色卡         │
                                         │  → 加载预设参数       │
                                         │  → 构建完整 Prompt    │
                                         │  → 调用 AI 后端       │
                                         └──────┬───────────────┘
                                                │ AI 回复
                                                ▼
                                          QQ 群收到 AI 消息
```

**关键设计**：Prompt 构建由 SillyTavern 服务端插件（`st/plugins/nb-qq-bot/`）完成，复用 ST 本身的角色卡、预设模板和 AI 后端配置，保证回复质量与 ST 网页版一致。Python 端只做消息转发和群聊管理。

## 项目结构

```
nb_qq_bot/
├── bot.py                       # NoneBot2 入口
├── pyproject.toml               # 项目元数据 & 依赖
├── .env.example                 # 配置模板（复制为 .env 后填写）
├── README.md                    # 本文件
│
├── src/plugins/
│   └── st_bridge.py             # QQ ↔ ST 桥接核心逻辑
│
└── st/plugins/nb-qq-bot/        # SillyTavern 服务端插件
    ├── index.js                 #   插件入口 & HTTP 编排
    ├── prompt-builder.js        #   Prompt 构建器
    └── README.md                #   插件文档
```

## 前置依赖

| 组件 | 说明 |
|------|------|
| **SillyTavern** | AI 角色聊天前端，需已部署并运行 |
| **NapCat** | QQ 机器人客户端（OneBot V11 协议实现） |
| **Python ≥ 3.9** | 运行 nb_qq_bot |
| **Node.js ≥ 18** | 运行 SillyTavern（内置 fetch） |

## 安装与配置

### 1. 部署 SillyTavern 插件

将本项目中的 `st/plugins/nb-qq-bot/` 目录**复制或链接**到你的 SillyTavern 插件目录：

```bash
# 方式一：直接复制
cp -r st/plugins/nb-qq-bot /path/to/SillyTavern/plugins/

# 方式二：符号链接（Windows 需要管理员终端）
mklink /D D:\TempFiles\SillyTavern\plugins\nb-qq-bot D:\Projects\nb_qq_bot\st\plugins\nb-qq-bot
```

确保 SillyTavern 的 `config.yaml` 中启用了服务端插件：

```yaml
enableServerPlugins: true
```

### 2. 配置机器人

```bash
# 复制配置模板
cp .env.example .env
```

编辑 `.env` 文件，各配置项说明：

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `HOST` | 机器人监听地址 | `127.0.0.1` |
| `PORT` | 机器人监听端口（需与 NapCat 配置一致） | `8080` |
| `SUPERUSERS` | 管理员 QQ 号列表 | `[]` |
| `NICKNAME` | 机器人昵称，群内 @ 时也可用此名 | `["bot"]` |
| `ST_BASE_URL` | SillyTavern 服务地址 | `http://127.0.0.1:8000` |
| `ST_CHAT_SOURCE` | AI 后端名称（对应 ST 连接配置） | `deepseek` |
| `ST_TIMEOUT` | 请求超时时间（秒） | `120` |
| `ST_MAX_RESPONSE_LENGTH` | AI 回复最大长度 | `800` |
| `ST_DEFAULT_CHARACTER` | 默认角色（留空则首次使用时选择） | — |
| `ST_DEFAULT_PRESET` | 默认预设（留空则使用 ST 默认预设） | — |

### 3. 安装 Python 依赖

```bash
pip install nonebot2 nonebot-adapter-onebot httpx
```

### 4. 启动 NapCat

NapCat 是 QQ 机器人客户端，提供 OneBot V11 WebSocket 服务。

启动 NapCat 后，确保其 WebSocket 配置的端口与 `.env` 中的 `PORT` 一致。

### 5. 启动

```bash
# 终端 1：先启动 SillyTavern
cd /path/to/SillyTavern
node server.js

# 终端 2：再启动 nb_qq_bot
cd /path/to/nb_qq_bot
python bot.py
```

## 使用方法

在 QQ 群中 @机器人 或使用机器人昵称触发：

```
@bot /help      查看所有命令
@bot /chars     列出 SillyTavern 中所有角色
@bot /presets   列出所有预设
@bot /char XX   选择角色（如：/char 小宫果穗）
@bot /preset XX 选择预设（如：/preset deepseek-rp）
@bot /newchat   清除对话历史，开始新对话
@bot /clear     清空当前对话上下文
@bot /status    查看当前绑定的角色和预设
@bot <消息>     与 AI 角色对话
```

> **提示**：`/char` 和 `/preset` 每个群独立设置，不同群可以使用不同的角色和预设。

## 工作流程

1. **QQ 消息** → 群成员 @机器人发送消息
2. **NoneBot2** → 收到 OneBot 事件，提取消息文本
3. **消息格式转换** → 将 QQ 号作为稳定发言者标识：`{QQ号}：{消息内容}`
4. **请求 ST 插件** → `POST /api/plugins/nb-qq-bot/generate`，携带角色、预设、聊天历史
5. **ST 插件处理** →
   - 加载角色卡（system prompt、性格、示例对话等）
   - 加载预设模板（main prompt、jailbreak、生成参数）
   - 从 ST 连接配置解析当前模型
   - 构建完整 OpenAI-format messages 数组
   - 调用 AI 后端生成回复
6. **响应转换** → AI 回复中的 QQ 号自动替换回群昵称
7. **发送到群** → 机器人将 AI 回复发送到 QQ 群

## 消息格式

发送给 AI 的消息格式为：

```
{QQ号}：{消息内容}
```

例如：`123456789：你好`

这样设计的原因：
- QQ 号是稳定唯一标识，不受群昵称频繁变化的影响
- 避免奇怪的特殊字符或群名干扰 AI 对发言者的理解
- AI 回复中如出现 QQ 号，会自动替换回该用户在群中的昵称

## 关于 ST 插件

`st/plugins/nb-qq-bot/` 是本项目配套的 SillyTavern 服务端插件，它：

- 不依赖 ST 前端代码（独立于 `public/scripts/`）
- 不 import ST 的 `src/` 内部模块
- 完整读取角色卡和预设数据，构建与 ST 网页版一致的 prompt
- 使用 CommonJS 格式，兼容 ST 的插件加载器

详见 `st/plugins/nb-qq-bot/README.md`。

## 依赖清单

**Python**
- nonebot2 ≥ 2.5.0
- nonebot-adapter-onebot ≥ 2.4.0
- httpx
- fastapi + uvicorn（NoneBot2 内置依赖）

**Node.js**
- Node.js ≥ 18（全局 `fetch`）
- SillyTavern（含插件系统）
