# nonebot-plugin-personification

✨ 拟人化群聊回复插件，支持多 API 池、好感度系统、作息模拟、YAML 人设、主动私聊、联网搜索 ✨

## 📖 介绍

`nonebot-plugin-personification` 是一个基于 NoneBot2 的拟人化群聊插件。插件支持多 API 池故障切换、好感度联动、群聊作息模拟、YAML/TXT 人设加载、主动私聊与联网搜索，能够在群聊中提供更自然、更贴近角色设定的回复体验。

## 💿 安装

```bash
nb plugin install nonebot-plugin-shiro-personification
# 或
pip install nonebot-plugin-shiro-personification
```

## 📦 依赖项

- `nonebot-plugin-apscheduler`（必须）
- `nonebot-plugin-localstore`（必须）
- `nonebot-plugin-htmlrender`（可选，Markdown 转图片）
- `nonebot-plugin-shiro-signin`（可选，好感度系统）

## ⚙️ 配置项

在 `.env` 文件中添加以下配置：

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `personification_whitelist` | `list` | `[]` | 启用插件的群号列表 |
| `personification_probability` | `float` | `0.5` | 随机插话概率 |
| `personification_api_pools` | `str/list` | `None` | 多 API 池配置（JSON 数组） |
| `personification_api_type` | `str` | `openai` | API 类型：openai/gemini/gemini_official/anthropic |
| `personification_api_url` | `str` | `https://api.openai.com/v1` | API 基础地址 |
| `personification_api_key` | `str` | `""` | API 密钥 |
| `personification_model` | `str` | `gpt-4o-mini` | 模型名称 |
| `personification_thinking_budget` | `int` | `0` | 思考预算（0 为关闭） |
| `personification_include_thoughts` | `bool` | `True` | 是否包含思考过程 |
| `personification_system_prompt` | `str` | （内置） | 默认系统提示词 |
| `personification_prompt_path` | `str` | `None` | 人设文件路径（.txt/.yaml） |
| `personification_system_path` | `str` | `None` | 同上（别名） |
| `personification_favorability_attitudes` | `dict` | （内置十级） | 各好感等级的态度描述 |
| `personification_history_len` | `int` | `200` | 上下文保留条数 |
| `personification_sticker_path` | `str` | `data/stickers` | 表情包目录 |
| `personification_sticker_probability` | `float` | `0.2` | 表情包发送概率 |
| `personification_poke_probability` | `float` | `0.3` | 戳一戳响应概率 |
| `personification_web_search` | `bool` | `True` | 是否开启联网搜索 |
| `personification_schedule_global` | `bool` | `False` | 全局开启作息模拟 |
| `personification_proactive_enabled` | `bool` | `True` | 是否开启主动私聊 |
| `personification_proactive_threshold` | `float` | `60.0` | 触发主动私聊的最低好感度 |
| `personification_proactive_daily_limit` | `int` | `3` | 每日主动私聊上限 |
| `personification_proactive_interval` | `int` | `30` | 主动私聊检查间隔（分钟） |
| `personification_proactive_probability` | `float` | `0.5` | 单次触发概率 |
| `personification_proactive_idle_hours` | `float` | `24.0` | 超时保底发送时长（小时） |
| `personification_blacklist_duration` | `int` | `300` | 临时拉黑时长（秒） |

## 📝 命令列表

**普通用户**

- `申请白名单`：申请将当前群加入白名单
- `群好感` / `群好感度`：查看当前群整体好感度

**管理员命令（SUPERUSER）**

白名单管理：
- `同意白名单 [群号]` / `拒绝白名单 [群号]`
- `添加白名单 [群号]` / `移除白名单 [群号]`

功能开关：
- `开启拟人` / `关闭拟人`（别名：`拟人开启` / `拟人关闭`）
- `开启表情包` / `关闭表情包`
- `拟人作息 [开启/关闭]`
- `拟人联网 [开启/关闭]`
- `拟人主动消息 [开启/关闭]`

人设管理：
- `查看人设`：查看当前群生效的人设
- `设置人设 [群号] <提示词>`：设置群人设
- `重置人设`：重置为默认人设
- `拟人配置`：查看当前配置信息

好感度管理：
- `设置群好感 [群号] [分值]`

黑名单管理：
- `永久拉黑 [用户ID/@用户]`
- `取消永久拉黑 [用户ID/@用户]`
- `永久黑名单列表`

上下文管理：
- `清除记忆` / `清除上下文` / `重置记忆`

群聊风格：
- `学习群聊风格`：分析并学习当前群聊语言风格
- `查看群聊风格`

日记：
- `发个说说`：手动触发 AI 生成并发布周记

## Changelog

### v0.3.0
- 重构：将单文件架构重构为 core/flows/handlers/jobs 四层模块
- 新增：多 API 池配置（personification_api_pools），支持故障自动切换
- 新增：Anthropic 原生联网搜索支持（web_search_20250305）
- 新增：主动私聊功能及开关命令（拟人主动消息）
- 新增：sticker 目录扫描 TTL 缓存，减少 IO 开销
- 新增：HTTP 连接池复用，提升图片处理性能
- 新增：JSON 文件写入原子锁，防止并发数据损坏
- 新增：bot_nickname 从人设配置动态读取，不再硬编码
- 修复：消除 handlers 层的动态 import，统一依赖注入架构
- 修复：shutdown hook 正确关闭共享 HTTP 客户端
- 变更：指令 `拟人开启/关闭` 改为 `开启/关闭拟人`（保留旧指令作别名）
- 变更：personification_model 默认值从 gpt-3.5-turbo 改为 gpt-4o-mini
- 变更：personification_web_search 默认值从 False 改为 True
- 变更：personification_history_len 默认值从 50 改为 200

### v0.2.1
- Migrate all latest features from local `personification` source.
- Added commands: `拒绝白名单`, `学习群聊风格`, `查看群聊风格`, `测试主动消息`.
- Added persistent group style and group chat history support.
- Added command set for whitelist rejection, style learning/viewing, and proactive-message testing.
- Runtime data paths now use `nonebot_plugin_localstore` with legacy path fallback.
- `nonebot-plugin-shiro-signin` changed to optional dependency, related features auto-degrade when missing.
