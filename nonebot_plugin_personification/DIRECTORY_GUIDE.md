# personification 目录使用说明与示例

## 目录总览

- `agent`：Agent 循环、工具注册、工具执行
- `core`：运行时组装、provider 路由、提示词加载
- `handlers`：消息接收与回复流程
- `flows`：YAML 风格流程、状态流、主动消息流
- `jobs`：定时任务和后台任务构建
- `skills`：可调用能力实现

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

- 作用：具体能力实现目录，模型通过 tool call 间接调用这些能力。
- 示例目录：
  - `news.py`：今日新闻、热搜、段子、历史今天、Epic 免费游戏
  - `weather.py`：天气查询
  - `datetime_tool.py`：日期时间工具
  - `web_search.py`：联网搜索
  - `sticker_tool.py`：语义选表情包
  - `user_tasks.py`：任务创建与取消
  - `user_persona.py`：用户画像相关能力
  - `sticker_labeler.py` / `vision_caller.py`：视觉打标与视觉调用
  - `tool_caller.py`：LLM provider 与工具调用协议封装
  - `custom_loader.py`：加载自定义技能

## 调用策略建议

- 实时信息优先调用 `skills` 而非凭空回答。
- 先调用工具取数，再筛选重点，最后用角色口吻输出。
- 输出中不要暴露“我调用了工具/根据 API 返回”。
