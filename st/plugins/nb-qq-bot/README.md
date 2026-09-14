# nb-qq-bot — SillyTavern Plugin

ST 服务端插件，为 qq-st-bridge（NoneBot2 QQ 机器人）提供统一的 prompt 构建 + AI 生成 API。

## 端点

```
POST /api/plugins/nb-qq-bot/generate
```

### 请求

```json
{
  "avatar_url": "CharacterName.png",
  "preset_name": "my-preset",
  "chat_history": [
    {"is_user": true, "mes": "...", "name": "QQ用户"},
    {"is_user": false, "mes": "...", "name": "Character"}
  ],
  "user_message": "你好",
  "user_name": "QQ用户",
  "qq_chat_behavior": "【QQ群聊行为指令...】",
  "max_response_length": 800,
  "chat_completion_source": "deepseek",
  "stream": false
}
```

| 字段 | 必填 | 说明 |
|------|------|------|
| `avatar_url` | ✅ | ST 角色文件名（如 `水无月静流.png`） |
| `preset_name` | ✅ | ST 预设名称（需在 `openai_setting_names` 中） |
| `user_message` | ✅ | 当前用户消息文本 |
| `chat_history` | — | 对话历史（ST JSONL 格式，可选） |
| `user_name` | — | 用户名称（默认 `QQ用户`） |
| `qq_chat_behavior` | — | QQ 群聊行为指令，注入到 system prompt 最前面 |
| `max_response_length` | — | 回复最大 token 数（默认 800） |
| `chat_completion_source` | — | AI 后端类型（默认 `deepseek`） |
| `model` | — | 强制指定模型；一般留空，见下方「模型选择」 |
| `stream` | — | 是否流式（默认 false） |

## 模型选择

插件每次请求时按以下顺序确定模型，**无需在项目里配置任何模型名**：

1. 请求里的显式 `model` 字段（手动覆盖，一般不用）
2. **ST 界面当前的连接设置** — `/api/settings/get` 返回的
   `oai_settings.<source>_model`（即 ST 网页 UI 里选中的模型）
3. 连接预设文件兜底（`data/<user>/OpenAI Settings/*.json` 里的
   `<source>_model`，兼容旧数据）

选定后会对照该后端**当前实际提供的模型列表**
（`/api/backends/chat-completions/status`，结果缓存 10 分钟）校验：
若模型已被上游改名/下架，自动退到最接近的档位（如 `*-flash` →
`*-flash`），并在 ST 控制台输出警告，而不是直接报 400。

新环境部署：只需在 ST 网页 UI 里连好 API（选后端、填 key、选模型），
机器人即自动跟随；`scripts/deploy_st.py` 部署结束时也会打印当前
连接检查结果。

### 响应

成功：
```json
{
  "success": true,
  "response_text": "AI 的回复文本..."
}
```

失败：
```json
{
  "success": false,
  "error": "描述性错误信息"
}
```

## 内部流程

1. 获取 CSRF token + session cookie
2. `POST /api/characters/get` — 加载角色卡数据
3. `POST /api/settings/get` — 加载预设参数 & 提示词模板
4. `prompt-builder.js` — 构建 OpenAI-format messages 数组
5. `POST /api/backends/chat-completions/generate` — 调用 AI 生成
6. 返回 AI 回复

## Prompt 构建顺序

系统消息（单条 `system` role）按以下顺序拼接：

1. **QQ 群聊行为指令** — 从 `qq_chat_behavior` 参数（通常告诉 AI 输出纯文字、不写动作描写）
2. **角色 system_prompt** — 角色卡自定义的完整系统提示（若为空则从 description/personality/scenario 字段构建）
3. **Preset main_prompt** — 预设的主提示模板（替换 `{{char}}` `{{user}}` 宏）
4. **Preset jailbreak_prompt** — 预设的越狱提示（若存在）
5. **mes_example** — 角色卡的示例对话（替换宏，格式化为 `[Example dialogue]` 块）
6. **first_mes** — 角色的第一条消息（作为语气参考）
7. **Preset enhance_definitions_prompt** — 预设的增强定义提示（若存在）
8. **Preset NSFW prompt** — 若存在

消息数组：
```
[system] ← 上述拼接
[history] ← user/assistant 交替
[system] ← post_history_instructions（若存在）
[user] ← 当前用户消息
```

## 独立性

此插件：
- 不 import ST 的 `src/` 代码
- 不使用 ST 前端（`public/scripts/`）逻辑
- 有自己的 prompt 构建器，完整读取角色卡和预设数据
- 位于 ST 的 `plugins/` 目录（已在 `.gitignore` 中），不受 ST 上游更新影响
- CommonJS 格式（匹配 ST 插件加载器的要求）

## 相关文件

| 文件 | 说明 |
|------|------|
| `index.js` | 插件入口，路由注册，HTTP 编排 |
| `prompt-builder.js` | Prompt 构建纯函数 |

## 依赖

- Node.js ≥ 18（使用全局 `fetch`）
- ST 插件系统（`config.yaml` 中 `enableServerPlugins: true`）
- ST 的 `plugins/package.json` 为 CommonJS 模式
