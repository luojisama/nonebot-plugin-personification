# nonebot-plugin-shiro-personification

基于 NoneBot2 和 OneBot V11 的拟人化聊天插件，围绕群聊与私聊上下文构建人设回复，支持作息模拟、联网检索、风格学习、主动私聊、贴图、画像、长期记忆与 Agent 工具调用。

## 特性

- 群聊回复、随机插话、戳一戳响应、私聊上下文记忆
- Agent 工具调用：联网搜索、天气、时间、新闻、群信息、好友申请、定时任务
- 用户画像、长期记忆、记忆宫殿、群聊风格学习、话题摘要与上下文压缩
- 主动私聊、群空闲主动发话、Qzone 说说、远程 skill 审批与插件知识库
- 贴图库自动标注、语义选图、视觉分析与 TTS 语音回复

## 安装

```bash
nb plugin install nonebot-plugin-shiro-personification
```

或：

```bash
pip install nonebot-plugin-shiro-personification
```

## 环境要求

- Python `>=3.10`
- NoneBot2
- OneBot V11 适配器

## 配置

- 完整配置表见 [CONFIG.md](./CONFIG.md)，其中包含每一个配置项的示例写法、默认值与备注。
- 插件数据目录固定使用 `nonebot-plugin-localstore` 的 `get_plugin_data_dir()` 结果，不再提供自定义数据目录配置。

## 常用命令

- `拟人帮助`
- `查看配置`
- `拟人开关 [开启/关闭]`
- `拟人语音 [开启/关闭]`
- `拟人联网 [开启/关闭]`
- `拟人主动消息 [开启/关闭]`
- `开启拟人` / `关闭拟人`
- `开启表情包` / `关闭表情包`
- `拟人作息 [开启/关闭/全局开启/全局关闭]`
- `学习群聊风格`
- `查看群聊风格 [群号]`
- `查看画像` / `刷新画像`
- `群好感` / `设置群好感 [群号] [数值]`
- `清除记忆 [全局/@用户/用户ID]`
- `完全清除记忆`
- `永久拉黑 [用户ID/@用户]`
- `取消永久拉黑 [用户ID/@用户]`
- `发个说说`
- `/persona help`

## 联动与兼容

- `nonebot-plugin-htmlrender` 作为默认依赖声明；不可用时相关渲染能力会自动降级，不影响主插件加载。
- `nonebot-plugin-shiro-signin` 暂未发布，因此当前不会作为安装文档中的可选 extra 提供。
- 未安装签到联动插件时，好感度、称号、黑名单等联动能力会自动降级，不影响主插件加载。
- 依赖其他插件时统一使用 `require(...)` 声明，避免因普通 `import` 提前导入导致插件加载失败。

## 更新

### 0.5.0

- 完整迁移本地 `personification` 功能到发布包，补齐长期记忆、记忆宫殿、TTS、远程 skill 审批、插件知识库等能力。
- 修复插件商店加载问题，避免 `nonebot_plugin_htmlrender` 因提前导入导致后续 `require()` 失败。
- 统一改为使用 `nonebot-plugin-localstore` 的 `get_plugin_data_dir()` 管理插件数据目录。
- 放宽 `pydantic` 依赖限制，并修正配置模型以兼容 `pydantic v1/v2`。
- 增补完整配置文档，覆盖全部配置项、示例写法、默认值与备注。
- 文档中明确说明签到联动插件暂未发布，相关能力仅保留兼容降级逻辑。

### 0.4.0

- 完整迁移本地 `personification` 开发版架构到在线版包。
- 新增 Agent 工具调用、用户画像、自定义 skills、群摘要与上下文压缩。
- 新增群空闲主动发话、好友申请判定、贴图库自动标注与语义选图。

## License

[MIT](./LICENSE)
