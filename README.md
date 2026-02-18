# nonebot-plugin-shiro-personification

✨ 实现拟人化的群聊回复，支持好感度系统、自主回复决策及 YAML 剧本模式 ✨

## 📖 介绍

这是一个基于 OpenAI API / Gemini API 的 NoneBot2 插件，旨在让机器人在群聊中表现得更像一个真正的成员。它能够根据上下文决定是否回复，支持基于好感度系统的动态态度调整，并引入了作息模拟和 YAML 剧本模式，让机器人拥有更丰富的生活感。

## 📦 依赖项

在使用本项目之前，请确保已安装以下插件：

- [nonebot-adapter-onebot](https://github.com/nonebot/adapter-onebot): OneBot V11 适配器
- [nonebot-plugin-apscheduler](https://github.com/nonebot/plugin-apscheduler): 任务调度支持
- [nonebot-plugin-localstore](https://github.com/nonebot/plugin-localstore): 本地数据存储
- [nonebot-plugin-htmlrender](https://github.com/kexue-go/nonebot-plugin-htmlrender): (可选) Markdown 转图片支持
- [nonebot-plugin-sign-in](https://github.com/luojisama/nonebot-plugin-sign-in): (可选) 好感度系统关联插件

## 💿 安装

使用 nb-cli 安装：
```bash
nb plugin install nonebot-plugin-shiro-personification
```

或者使用 pip 安装：
```bash
pip install nonebot-plugin-shiro-personification
```

## ⚙️ 配置

在 `.env` 文件中添加以下配置项：

| 配置项 | 类型 | 默认值 | 说明 |
|:-----|:----:|:----:|:----|
| `personification_api_key` | `str` | `""` | OpenAI / Gemini API Key |
| `personification_api_url` | `str` | `"https://api.openai.com/v1"` | API 基础路径 |
| `personification_api_type` | `str` | `"openai"` | API 类型: `openai` / `gemini` / `gemini_official` |
| `personification_model` | `str` | `"gpt-3.5-turbo"` | 使用的模型名称 (如 `gemini-1.5-flash`) |
| `personification_whitelist` | `list` | `[]` | 启用插件的群号列表 |
| `personification_probability` | `float` | `0.5` | 随机回复概率 (0-1) |
| `personification_system_prompt` | `str` | (见代码) | 默认系统提示词 |
| `personification_prompt_path` | `str` | `None` | 自定义人格设定文件路径 (支持 .txt 或 .yaml) |
| `personification_history_len` | `int` | `50` | 上下文参考长度 |
| `personification_sticker_path` | `str` | `"data/stickers"` | 表情包文件夹路径 |
| `personification_poke_probability` | `float` | `0.3` | 戳一戳响应概率 |
| `personification_schedule_global` | `bool` | `False` | 是否全局开启作息模拟 |
| `personification_web_search` | `bool` | `False` | 是否开启联网搜索 (需模型支持) |

## 🎉 功能特性

### 1. 拟人化回复
- **自主决策**：机器人会根据对话内容判断是否需要回复，甚至主动结束话题 ([SILENCE])。
- **作息模拟**：模拟日本中学生的作息时间（上课、社团、睡觉等），在不同时间段有不同的响应状态。
- **水群模式**：随机发送表情包、文本或混合内容，模拟真实群友。

### 2. 好感度系统
- **动态态度**：结合 `nonebot-plugin-sign-in` 插件，根据用户的好感度等级（陌生 -> 挚友）调整回复语气。
- **群氛围感知**：根据群聊整体的好感度水平，调整机器人的心情（压抑 vs 开心）。

### 3. YAML 剧本模式
- 支持通过 YAML 文件定义复杂的状态机逻辑。
- 可根据关键词触发特定回复，并改变机器人内部状态。

### 4. 视觉感知与表情包
- **表情包发送**：支持配置本地表情包文件夹，随机发送表情包。
- **视觉识别**：支持识别用户发送的表情包/图片（需模型支持 Vision）。

## 📝 命令列表

**基础功能**
- `申请白名单`：申请将当前群聊加入白名单
- `群好感` / `群好感度`：查看当前群聊的整体好感

**管理员命令 (SUPERUSER)**
- `拟人配置`：查看当前拟人插件的全局及群组配置
- `拟人开启/关闭`：开启或关闭当前群的拟人功能
- `拟人作息 [开启/关闭]`：开启或关闭当前群的作息模拟
- `开启/关闭表情包`：开启或关闭当前群的表情包功能
- `拟人联网 [开启/关闭]`：切换 AI 联网搜索功能
- `查看人设`：查看当前群生效的人设提示词
- `设置人设 [群号] <提示词>`：设置指定群或当前群的人设
- `重置人设`：重置当前群的人设为默认配置
- `设置群好感 [群号] [分值]`：手动调整群好感
- `永久拉黑 [用户ID/@用户]`：禁止用户与 AI 交互
- `取消永久拉黑 [用户ID/@用户]`：移除永久黑名单
- `永久黑名单列表`：查看所有被封禁的用户
- `同意白名单 [群号]`：批准群聊加入白名单
- `拒绝白名单 [群号]`：拒绝群聊加入白名单
- `添加白名单 [群号]`：将指定群聊添加到白名单
- `移除白名单 [群号]`：将群聊移出白名单
- `清除记忆` / `清除上下文 [群号]`：清除当前群或指定群的短期对话上下文
- `发个说说`：手动触发一次 AI 周记说说发布

## 📄 开源许可

本项目采用 [MIT](LICENSE) 许可协议。

## 💡 鸣谢

- [nonebot_plugin_random_reply](https://github.com/Alpaca4610/nonebot_plugin_random_reply): 灵感来源
