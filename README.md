# qq-st-bridge

将 QQ 群聊与 **SillyTavern AI 角色**连接的机器人。

不只是 @机器人 问答——内置**社交引擎**，让 AI 角色像一个真实的群友一样参与群聊：
平时潜水观望，群里热闹时冒泡接话，聊 high 了连续回复，冷场了自然退场，还可以发表情包、拍人。

## 架构

```
┌────────────────────────── QQ 侧 ───────────────────────────┐
│   NapCat (OneBot v11) ◄──WebSocket──► NoneBot2 (bot.py)     │
└───────────────┬─────────────────────────────────────────────┘
                │ 全量群消息 / 私聊 / @事件 / 戳一拍
                ▼
┌────────────────── 桥接层（身体 · 行为）──────────────────────┐
│ ① 消息收集器   归一化 → 清洁代号 → 写入滚动缓冲               │
│ ② 参与引擎     观望/活跃/试探/退场状态机 + 检查点触发          │
│ ③ 上下文构建器 逐条时间戳的记录 + 气氛观察 + 群友名册 + 表情库 │
│ ④ 动作循环     调 ST → 解析协议标记 → 执行（轻量/重量两档）   │
│ ⑤ 发送器       空格分条 → 随机延迟 → 表情 / 拍一拍            │
│ ⑥ 状态存储     代号名册 / 表情目录 / 状态机快照（本地 JSON）   │
└───────────────┬─────────────────────────────────────────────┘
                │ HTTP POST /generate（唯一入口，ST 无感知）
                ▼
┌────────────────── 人格层（大脑 · ST 不动架构）────────────────┐
│   nb-qq-bot 插件：角色卡 + 预设 → messages → 模型             │
│   输出：正文（空格 = 分条）/ 协议标记                          │
└──────────────────────────────────────────────────────────────┘
```

职责一句话：**ST 回答"我是谁、怎么说"，桥接回答"何时看、说不说、怎么发"**。
ST 侧只放文本内容（预设 + 角色卡），所有行为逻辑都在 Python 桥接层。

## 社交引擎

| 阶段 | 行为 |
|------|------|
| **观望** | 每条消息零成本收集；被 @ / 攒够 N 条消息 / 命中兴趣关键词时才"看一眼"（轻量生成） |
| **活跃** | 她开口说话后注意力有惯性：每 10~30 秒批量看新消息并回复；5~10 分钟后自然收尾退场 |
| **试探** | 冷场时有概率主动说一句试探，2~5 分钟无人回应就安静回观望 |
| **退场** | 说一句"我先潜了"，回观望潜水 |

模型通过输出协议标记控制行为，桥接解析执行：

| 标记 | 含义 | 桥接动作 |
|------|------|---------|
| 正文（空格分条） | 说话 | 拆条 → 2~8s 打字延迟 → 条间 1~3s → 发送 |
| `[SILENT]` | 看了，不接话 | 无动作 |
| `[WAIT 2m]` | 话说一半，等等看 | 定时后重新触发检查点 |
| `[WAKE 30m]` | 潜水，晚点叫我 | 预约定时器（最小 10 分钟，每日上限） |
| `[STICKER: 标签]` | 发表情包 | 查目录 → 独立图片气泡 |
| `[POKE: 代号]` | 拍一拍 | OneBot `group_poke` |

防自言自语：她自己的发言合并为单条记录回写缓冲，且引擎全链路过滤自己的消息——她永远不会被自己触发。

## 项目结构

```
qq-st-bridge/
├── bot.py                        # NoneBot2 入口
├── .env.example                  # 配置模板（社交引擎全参数带注释）
├── src/plugins/st_bridge/        # 桥接插件（Python）
│   ├── collector.py              #   消息收集器（分段解析 + 滚动缓冲）
│   ├── participation.py          #   参与引擎（状态机 + 定时器）
│   ├── context_builder.py        #   上下文构建器（时间戳 + 气氛观察）
│   ├── action_loop.py            #   动作循环（调 ST + 标记解析）
│   ├── sender.py                 #   发送器（分条 + 延迟 + 表情/拍一拍）
│   ├── aliases.py                #   清洁代号 + 印象名册
│   ├── stickers.py               #   表情包目录
│   ├── tracelog.py               #   全链路 trace 日志（logs/trace.log）
│   └── ...                       #   config/state/st_api/handlers 等
├── st/plugins/nb-qq-bot/         # SillyTavern 服务端插件（JS）
├── st/char/ st/preset/           # 示例角色卡与预设
├── tests/test_social_engine.py   # 闭环测试（无需 QQ / ST，假数据跑通全链）
├── scripts/deploy_st.py          # 一键部署插件/预设/角色卡到本地 ST
└── CLAUDE.md                     # 开发者深入文档（架构细节/坑/参数）
```

## 前置依赖

| 组件 | 说明 |
|------|------|
| **SillyTavern** | AI 角色聊天前端，需已部署并运行 |
| **NapCat**（或其他 OneBot V11 客户端） | QQ 机器人客户端 |
| **Python ≥ 3.10** | 运行 qq-st-bridge |
| **Node.js ≥ 18** | 运行 SillyTavern |

## 快速开始

### 1. 部署 SillyTavern 插件

```bash
# 方式一：复制
cp -r st/plugins/nb-qq-bot /path/to/SillyTavern/plugins/

# 方式二：本仓库脚本（同时部署预设和角色卡，Windows 本机 ST 用）
python scripts/deploy_st.py <你的SillyTavern路径>
```

确保 SillyTavern 的 `config.yaml` 中启用了服务端插件：

```yaml
enableServerPlugins: true
```

### 2. 配置机器人

```bash
cp .env.example .env   # 按注释填写；社交引擎参数均有默认值
```

关键配置：`ST_BASE_URL`（ST 地址）、`ST_CHAT_SOURCE`（AI 后端，对应 ST 连接配置）、
`ST_SOCIAL_ENABLED`（社交引擎总开关）。完整参数表见 `.env.example`。

### 3. 启动

```bash
# 终端 1：先启动 SillyTavern
cd /path/to/SillyTavern && node server.js

# 终端 2：再启动 qq-st-bridge（NapCat 需已启动并指向 PORT）
python bot.py
```

## 使用方法

```
@bot /char 小宫果穗        选择角色
@bot /preset QQ群聊角色扮演 选择预设
@bot /social on            开启社交引擎（像群友一样自发参与）
@bot /status               查看绑定与状态机阶段
@bot /help                 全部命令
回复一张图片 + /sticker 开心 高兴时用     添加表情包
/note 阿伟 群里最懂游戏的   给群友的代号写印象备注
```

开启社交引擎后，她会自己观察群里聊什么、决定要不要接话；@她 则必定回应。
每个群独立绑定角色/预设，也可单独开关社交引擎。

## 自检测试

不依赖 QQ 和 ST，用假数据跑通「收集 → 触发 → 生成解析 → 发送」全链路：

```bash
python tests/test_social_engine.py
```

## 致谢（Acknowledgements）

- [Derpyu520/qq-bridge](https://github.com/Derpyu520/qq-bridge) —— 本项目的社交引擎
  （观望/活跃/试探/退场状态机、检查点触发、空格分条发送、读空气思路）在设计上
  参考了该项目，受益匪浅。qq-st-bridge 为全新实现（Python + SillyTavern 插件架构），
  未使用其源代码。

## 许可

[MIT](LICENSE)
