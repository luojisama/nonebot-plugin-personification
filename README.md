# nonebot-plugin-personification

✨ 实现拟人化的群聊回复，支持好感度系统和自主回复决策 ✨

## 📖 介绍

这是一个基于 OpenAI API 的 NoneBot2 插件，旨在让机器人在群聊中表现得更像一个真正的成员。它能够根据上下文决定是否回复，并支持基于好感度系统的动态态度调整。

## 📦 依赖项

在使用本项目之前，请确保已安装以下插件：

- [nonebot-adapter-onebot](https://github.com/nonebot/adapter-onebot): OneBot V11 适配器
- [nonebot-plugin-htmlrender](https://github.com/kexue-go/nonebot-plugin-htmlrender): (可选) Markdown 转图片支持
- [nonebot-plugin-sign-in](https://github.com/LanMiao-Labs/nonebot-plugin-sign-in): (可选) 好感度系统关联插件

## 💿 安装

使用 nb-cli 安装：
```bash
nb plugin install nonebot-plugin-personification
```

或者使用 pip 安装：
```bash
pip install nonebot-plugin-personification
```

## ⚙️ 配置

在 `.env` 文件中添加以下配置项：

| 配置项 | 类型 | 默认值 | 说明 |
|:-----|:----:|:----:|:----|
| `personification_api_key` | `str` | `""` | OpenAI 或兼容服务的 API Key |
| `personification_api_url` | `str` | `"https://api.openai.com/v1"` | API 基础路径 |
| `personification_model` | `str` | `"gpt-3.5-turbo"` | 使用的模型名称 |
| `personification_whitelist` | `list` | `[]` | 启用插件的群号列表 |
| `personification_probability` | `float` | `0.5` | 随机回复概率 (0-1) |
| `personification_system_prompt` | `str` | (见代码) | 默认系统提示词 |
| `personification_prompt_path` | `str` | `None` | 自定义人格设定文件路径 |
| `personification_history_len` | `int` | `50` | 上下文参考长度 |
| `personification_sticker_path` | `str` | `"data/stickers"` | 表情包文件夹路径 |
| `personification_poke_probability` | `float` | `0.3` | 戳一戳响应概率 |

## 🎉 使用

1. 将机器人加入白名单群组。
2. 机器人在群聊中会根据配置的概率随机回复消息。
3. 当被 @ 时，机器人必定回复。
4. 支持戳一戳响应。
5. 支持随机发送表情包（需配置表情包路径）。

### 🌟 好感度系统

本插件支持与签到插件（如 `nonebot-plugin-sign-in`）关联。如果检测到签到插件，将根据用户的好感度等级调整回复态度。

### 🖼️ 图片渲染 (可选)

安装 `nonebot-plugin-htmlrender` 后，插件支持将 Markdown 格式的回复渲染为图片。

## 📄 开源许可

本项目采用 [MIT](LICENSE) 许可协议。
