# nonebot-plugin-shiro-personification

基于 NoneBot2 和 OneBot V11 的拟人化聊天插件，围绕群聊与私聊上下文构建人设回复，支持 Agent 工具调用、风格学习、主动消息、贴图语义与运行时开关。

## 特性

- 群聊回复、插话、戳一戳响应、私聊上下文记忆
- Agent 化工具调用：联网搜索、天气、时间、新闻、群信息、贴图分析、定时任务
- 群聊风格学习、话题摘要、上下文压缩
- 主动私聊、群空闲主动发话、周记/说说生成
- 用户画像、自定义 skills、贴图库自动标注与语义选图
- 白名单、永久黑名单、群配置、运行时开关

## 安装

```bash
nb plugin install nonebot-plugin-shiro-personification
```

或：

```bash
pip install nonebot-plugin-shiro-personification
```

可选好感度联动：

```bash
pip install "nonebot-plugin-shiro-personification[signin]"
```

## 环境要求

- Python `>=3.10`
- NoneBot2
- OneBot V11 适配器

## 主要配置

以下是最常用的配置项，完整字段见 [config.py](./nonebot_plugin_personification/config.py)。

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `personification_api_type` | `str` | `openai` | 主模型类型，支持 `openai`、`gemini`、`anthropic`、`openai_codex` |
| `personification_api_url` | `str` | `https://api.openai.com/v1` | 主模型接口地址 |
| `personification_api_key` | `str` | `""` | 主模型密钥 |
| `personification_model` | `str` | `gpt-4o-mini` | 主模型名称 |
| `personification_api_pools` | `str/list` | `None` | 多 provider 池配置 |
| `personification_system_prompt` | `str` | 内置默认值 | 基础人设提示词 |
| `personification_prompt_path` | `str` | `None` | 人设文件路径，支持文本/YAML |
| `personification_whitelist` | `list[str]` | `[]` | 启用群白名单 |
| `personification_probability` | `float` | `0.5` | 群聊随机回复概率 |
| `personification_history_len` | `int` | `200` | 上下文长度 |
| `personification_sticker_path` | `str` | `data/stickers` | 贴图库路径 |
| `personification_labeler_enabled` | `bool` | `True` | 是否自动标注新贴图 |
| `personification_skills_path` | `str` | `None` | 自定义 skills 根目录 |
| `personification_web_search` | `bool` | `True` | 保留的联网总开关 |
| `personification_proactive_enabled` | `bool` | `True` | 主动私聊开关 |
| `personification_group_idle_minutes` | `int` | `60` | 群空闲多久后允许主动发话 |
| `personification_persona_enabled` | `bool` | `True` | 用户画像开关 |
| `personification_data_dir` | `str` | `""` | 自定义数据目录 |

## 命令概览

常用命令：

- `申请白名单`
- `群好感` / `群好感度`
- `查看配置`
- `拟人开` / `拟人关`
- `贴图开` / `贴图关`
- `联网开关 [开/关]`
- `主动私聊开关 [开/关]`
- `拟人作息 [开启/关闭/全局开启/全局关闭]`
- `学习群聊风格`
- `查看群聊风格`
- `清除上下文 [全局/@用户/用户ID]`
- `永久拉黑 [用户ID/@用户]`
- `取消永久拉黑 [用户ID/@用户]`
- `查看永久黑名单`
- `手动发说说`

不同部署里还会根据可用能力启用用户画像、好友申请、贴图/工具调用相关命令。

## 说明

- `nonebot-plugin-shiro-signin` 为可选联动依赖，未安装时好感度相关能力会自动降级。
- `nonebot-plugin-htmlrender` 已作为默认依赖声明；若运行环境缺失或导入失败，相关渲染能力会自动降级。
- 数据默认存放在 `nonebot-plugin-localstore` 对应目录；未可用时会回退到 `data/personification`。

## 更新

### 0.4.0

- 完整迁移本地 `personification` 开发版架构到在线版包。
- 新增 Agent 工具调用、用户画像、自定义 skills、群摘要与上下文压缩。
- 新增群空闲主动发话、好友申请判定、贴图库自动标注与语义选图。
- 补齐 Anthropic、watchdog、filelock 等新版依赖。
- 更新项目元数据、README 与发布配置，使其与当前源码一致。

### 0.3.1

- 性能优化与稳定性提升。
- 修复了一些已知问题。

## License

[MIT](./LICENSE)
