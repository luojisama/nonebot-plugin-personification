# personification 目录使用说明与示例

## 目录总览

- `agent`：Agent 循环、工具注册、工具执行
- `core`：运行时组装、provider 路由、提示词加载
- `handlers`：消息接收与回复流程
- `flows`：YAML 风格流程、状态流、主动消息流
- `jobs`：定时任务和后台任务构建
- `skills`：标准 skillpack 内容目录
- `skill_runtime`：skillpack 运行时基础设施

## agent

- 作用：让模型在多轮中决定“是否调用工具、调用哪个工具、如何利用结果继续回答”。
- 示例：用户问“今天有什么新闻”，Agent 可先发起 `get_daily_news`，收到工具输出后再选出重点回复。

## core

- 作用：把配置、ToolCaller、ToolRegistry、会话与处理器组装成运行时。
- 示例：`service_factory.py` 在启动时注册 `news/weather/web_search` 等工具并注入到 Agent。

## handlers

- 作用：承接 NoneBot 事件，拼接上下文，进入 Agent 或 YAML 流程，最终发送消息。
- 示例：群消息进入 `reply_processor.py` 后生成 `messages`，交由 Agent 进行工具决策。

## flows

- 作用：处理 YAML 模板化回复、主动私聊、日志/日记等流程。
- 示例：YAML 模式下仍可通过 Agent 调用工具，再将结果转为角色化表达。

## jobs

- 作用：管理定时任务相关逻辑，支持创建/取消用户任务。
- 示例：用户说“每天8点提醒我喝水”，模型应调用任务工具写入持久化任务。

## skills

- 作用：具体 skillpack 内容目录，模型通过 tool call 间接调用这些能力。
- skillpack 标准结构（自动加载）：
  - `your-skill/SKILL.md`
  - 或 `your-skill/skill.yaml`
  - `your-skill/agents/openai.yaml`
  - `your-skill/scripts/*.py`
  - `your-skill/references/*`
  - `your-skill/assets/*`
- 标准入口约定：
  - 优先 `scripts/main.py`
  - 可在 `skill.yaml` / `SKILL.md` frontmatter 里声明 `entrypoint`
  - 入口可实现 `run(...)`、`build_tools(runtime)` 或 `register(runtime, registry)`
- 标准元数据建议：
  - `name`
  - `description`
  - `parameters`
  - `entrypoint`
  - `python_paths`：额外加入 `sys.path` 的相对目录，便于公开 skill 包携带自己的辅助模块
  - `isolation`：skill 执行隔离配置
  - `mcp`：MCP 兼容配置（当前支持 stdio）
- `isolation` 建议字段：
  - `mode`: `inprocess` 或 `process`
  - `timeout`: 执行超时秒数
  - `python` / `python_executable`: 指定隔离进程 Python
  - `cwd`: 隔离进程工作目录，相对 skill 根目录
  - `inherit_env`: 是否继承宿主环境变量
  - `env`: 额外注入的环境变量
- `mcp` 建议字段：
  - `transport`: 当前支持 `stdio`
  - `command`: MCP server 启动命令
  - `args`: MCP server 启动参数
  - `cwd`: server 工作目录，相对 skill 根目录
  - `env`: server 环境变量
  - `inherit_env`: 是否继承宿主环境变量
  - `tool` / `tools`: 限定暴露哪些 MCP tool
  - `name_prefix`: 给导入的 MCP tool 加统一前缀，避免重名
  - `timeout`: MCP 初始化和调用超时
- 内置 skillpack 目录：`skills/skillpacks`
- 用户自定义 skillpack 目录：由 `personification_skills_path` 指定
- 远程公开 skill 源：由 `personification_skill_sources` 指定，支持：
  - 本地目录
  - 本地 `.zip`
  - 直接 `.zip` URL
  - GitHub 仓库 URL（自动转 zip 下载，可带 `/tree/<ref>/<subdir>`）
- 外部 source 的安全策略：
  - 远程公开 skill 默认按不可信来源处理
  - 不可信来源默认使用 `run(...)` 子进程隔离执行
  - 如需直接在宿主进程内执行 `build_tools(runtime)` / `register(runtime, registry)`，需显式开启 `personification_skill_allow_unsafe_external`
  - 不可信来源更推荐通过 `mcp` 暴露工具，而不是直接依赖宿主运行时
- 远程 skill 会缓存到 `personification_skill_cache_dir`，未配置时默认落到插件 `localstore` 数据目录下的 `skill_cache`
- 远程仓库目录发现规则：
  - 如果根目录自身包含 `skill.yaml` 或 `SKILL.md`，按单 skill 加载
  - 否则递归发现仓库内所有符合标准结构的子目录并逐个加载
- 示例目录：
  - `skillpacks/news`：今日新闻、热搜、段子、历史今天、Epic 免费游戏
  - `skillpacks/weather`：天气查询
  - `skillpacks/datetime_tool`：日期时间工具
  - `skillpacks/web_search`：联网搜索
  - `skillpacks/sticker_tool`：语义选表情包与图像分析
  - `skillpacks/user_tasks`：任务创建与取消
  - `skillpacks/user_persona`：用户画像相关能力
  - `skillpacks/sticker_labeler` / `skillpacks/vision_caller`：视觉打标与视觉调用
  - `skillpacks/tool_caller`：LLM provider 与工具调用协议封装
  - `skill_runtime/custom_loader.py`：加载内置、本地、远程与 MCP 兼容 skillpack（兼容旧 custom/skill.yaml）

### 外部 sources 示例

```yaml
personification_skill_sources:
  - name: public-weather-pack
    source: https://github.com/example/skill-weather
    ref: main
  - name: public-tools
    source: https://example.com/open-skills.zip
    subdir: packs
```

### skill.yaml 示例

```yaml
name: public_weather
description: 公开天气查询 skill
entrypoint: scripts/main.py
parameters:
  type: object
  properties:
    city:
      type: string
  required: [city]
isolation:
  mode: process
  timeout: 12
```

### MCP 示例

```yaml
name: ext_mcp_tools
description: 通过 MCP 暴露外部工具
mcp:
  transport: stdio
  command: python
  args: ["server.py"]
  cwd: .
  tool: search_docs
  name_prefix: ext_
  timeout: 20
```

## 调用策略建议

- 实时信息优先调用 `skills` 而非凭空回答。
- 先调用工具取数，再筛选重点，最后用角色口吻输出。
- 输出中不要暴露“我调用了工具/根据 API 返回”。

## 全局联网模式

- 配置项：`personification_web_search_always`
- 设为 `true` 时，每轮回复前都会强制先走一次联网检索，再组织最终答复。
- 适合高时效群聊，例如游戏更新、新闻、热搜、价格、比赛结果。
- 代价是每轮延迟和 token 消耗都会上升，默认值保持 `false`。
