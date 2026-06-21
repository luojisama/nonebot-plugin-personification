# plugin_invoker

让 bot 能够**代为执行**同一 NoneBot2 实例上安装的【其它插件】的命令，并把这些插件本来要发出的
消息捕获下来，交给 LLM 用人设语气转述给用户——而不是让用户自己去敲命令。

与 `plugin_knowledge`（只能“描述”插件）互补：`plugin_knowledge` 负责定位插件与命令，
`plugin_invoker` 负责真正“使用”它们。

## 工具

- `invoke_plugin(plugin_name, command_text)`：在内部以当前用户的身份把 `command_text`（如
  `/天气 北京`）分发给目标插件，捕获其输出并返回。返回文本会作为 tool 结果进入 Agent 上下文，
  由 Agent 用人设语气转述。

## 机制

1. 校验 `command_text` 的命令名（head）确实属于 `plugin_name` 的某个已索引触发方式（`triggers`，
   command/regex/keyword），过滤掉明显不属于该插件的输入。
2. 危险/管理员命令过滤（封禁、踢人、删除、关机、全局开关、发奖、充值等关键词）+ 可配置
   allow/blocklist + 排除 personification 自身命令（防递归）。
3. 克隆当前消息事件、替换为目标命令、打 `_personification_synthetic` 标记，用一个子类化的代理 Bot
   拦截所有发送类 `call_api`（缓冲而非真正发出），再经 `nonebot.message.handle_event` 重新分发。
4. personification 自身的各 matcher（含规则结果缓存层）顶部会检测合成标记并短路，避免递归。

## 安全模型与边界（请如实理解）

这是一个**默认关闭**的高权限工具，开启前请知悉其真实边界，不要高估其隔离性：

- **以用户身份真实执行**：被调用的命令会在内部真正运行，等同于用户亲自发送，可能产生写库、积分、
  签到等副作用。这是“代为使用”的固有语义。
- **分发是全局的**：底层用 `handle_event` 重新分发，会经过**所有**已加载插件的 matcher，而不仅是
  `plugin_name` 指定的那一个。命令名校验只能保证“这是某个已索引插件的合法命令”，不能保证只有目标
  插件响应——若多个插件命中同一命令，都会执行（输出被代理缓冲）。
- **危险过滤是关键词**denylist**，并不完备**：无法覆盖所有破坏性命令（同义词、其它语言、自定义命名）。
  真正的安全边界是【总开关默认关闭 + 强烈建议配置 `allowlist` 白名单】，而不是黑名单。
- **可能被绕过的发送**：极少数插件若直接 `nonebot.get_bot()` 取真实 Bot 或依赖“被@即回”的兜底
  逻辑，其输出可能不被代理拦截。请在受控环境验证后再开启。

**强烈建议**：生产环境用 `personification_plugin_invoker_allowlist` 显式限定可调用的插件，而非依赖
默认的全量 + 关键词过滤。

相关配置见 `config.py`：

- `personification_plugin_invoker_enabled`
- `personification_plugin_invoker_allowlist` / `personification_plugin_invoker_blocklist`
- `personification_plugin_invoker_max_calls_per_turn`
- `personification_plugin_invoker_capture_timeout`
- `personification_plugin_invoker_max_output_chars`
- `personification_plugin_invoker_extra_danger_keywords`

> 这是一个 per-request 工具：它需要当前的 `bot`/`event`，因此由
> `handlers/reply_pipeline/pipeline_context.py` 在每次回复时通过
> `build_invoke_plugin_tool_for_runtime(...)` 注册，而**不是**作为静态 skillpack 的 `build_tools`。
