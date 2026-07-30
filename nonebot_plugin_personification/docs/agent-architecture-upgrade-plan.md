# Agent 架构升级计划

更新时间：2026-07-01

## 目标

在不破坏“正常聊天语义 LLM-led”的前提下，把当前 Agent 从“能完整走工具循环并生成回复”继续推进到“能稳定决定怎么参与聊天、怎么执行外发动作、怎么被回放评估和灰度优化”的结构。

本计划不引入关键词/正则语义路由。代码继续负责上下文、工具、契约、预算、trace、持久化和兜底；模型继续负责是否回复、当前轮怎么理解、是否调用工具、证据是否足够、最终怎么说。

## 当前评分

当前架构约 8.4/10。

- 优点：完整 Agent 覆盖面已经很大；TurnPlan、语义帧、工具筛选、skillpack、trace、WebUI 观测都有基础；扩展能力主要靠工具而不是核心分支。
- 进展：`speech_act` 说话动作层、发送型工具 metadata 契约、`tool_contracts`、`final_synthesis`、`loop_utils`、`prompting`、`tool_loop` 与 `stop_flow` 分层、随机插话结构静默门控、坏例回放报表、Agent 预算模式 shadow/adaptive、短期话题状态注入与 WebUI trace 信号已经落地，回复基调、外发工具静默收尾和“该沉默就沉默”的入口兜底不再只靠事后补救。
- 短板：runner 仍承担主循环迭代、query rewrite 前后文准备和证据调度闭包；真实坏例回放和量化评估还需要继续扩样；延迟预算虽可显式 adaptive 接管，但默认仍是 shadow，生产收益要在 trace 和灰度里验证；短期话题状态还处在结构化注入与观测阶段，还要用真实坏例验证模型侧收益。

## 阶段一：说话动作层

状态：本轮已落地第一批，并补充随机插话结构静默门控。

新增 TurnPlan `speech_act` 字段，让模型在回合规划阶段明确最终可见回复承担的聊天动作：

- `participate`：参与讨论或闲聊推进半步。
- `answer`：回答问题。
- `ask_followup`：追问一个具体点。
- `clarify`：信息不足时短澄清。
- `tease`：轻吐槽或接梗。
- `execute_action`：主要执行会外发内容的工具。
- `source_summary`：基于证据做简明总结。
- `silence`：保持沉默。

落点：

- `agent/runtime/planner.py` 负责生成、解析、fallback 和 semantic frame 互转。
- `core/reply_style_policy.py` 负责把 `speech_act` 转成模型侧输出纪律。
- `core/response_review.py::arbitrate_reply_mode()` 在随机插话、非直呼、非连续独聊、语义帧建议沉默且目标不是 bot 时直接静默，避免低置信 fallback 继续放行到 Agent 生成观望式废话。
- 普通回复、YAML 回复、Agent runner 都注入同一说话动作提示。
- `reply_turn_trace` 记录 `speech_act`，方便后续观测和灰度。

验收：

- 普通回复和 YAML 回复在同一 TurnPlan 下得到一致的说话动作提示。
- “讨论、闲聊为主，而不是附和、感叹、转述”不再只靠事后 review，而是在生成前就进入结构化计划。
- `execute_action/silence` 场景优先静默，不用“先看看情况/等会再说”这种旁白代替沉默。

## 阶段二：工具动作契约

状态：本轮已落地第一批。

把发送型工具的副作用和最终回复行为写入工具 metadata，而不是在 `runner.py` 里维护工具名白名单。

新增 metadata 字段：

- `side_effect`：默认 `none`，发送消息类工具用 `send_message`。
- `final_behavior`：默认 `continue`，外发成功后静默类工具用 `silence_on_success`。
- `retryable`：默认 `false`，普通查证工具可标记为可重试，外发副作用工具默认不可重试，避免重复发送。

落点：

- `agent/runtime/tool_catalog.py` 提供默认契约和 `tool_runtime_metadata()`。
- `agent/runtime/tool_contracts.py` 读取契约决定工具结果是否直接收尾为 `[SILENCE]`，并承接直出媒体、图片生成失败格式化和意图推荐工具逻辑。
- `agent/runtime/runner.py` 只调用契约层，不再直接维护副作用工具收尾细节。
- QQ 表情、本地表情包、联网搜图直发等工具统一走 `side_effect=send_message` + `final_behavior=silence_on_success`。

验收：

- 新增发送型工具只要声明 metadata，就能在成功排队后静默收尾。
- runner 不再需要维护发送型工具名集合。
- 副作用工具不可重试，避免重复发图/发表情。

## 阶段三：runner 分层拆解

状态：已落地工具结果契约层、最终合成层、提示组装层、工具循环 I/O 层和 stop/fallback 收尾层。

目标：降低 `agent/runtime/runner.py` 的维护压力。

建议拆分：

- `planning_context`：意图、TurnPlan、query rewrite、候选工具提示。
- `tool_loop`：模型 step I/O、usage 记账、工具结果回灌。
- `stop_flow`：模型 stop 后的 banter 查证裁判、语义 fallback、视觉 fallback 和最终 `[NO_REPLY]` / 正文收口。
- `tool_contracts`：副作用工具、直出媒体、失败格式化、静默收尾、意图推荐工具。
- `final_synthesis`：工具结果拟人化、证据综合提示、最终回复收口。

当前落点：

- `agent/runtime/final_synthesis.py` 承接 `AgentResult`、发送型工具直接收尾转 `AgentResult`、最大步数/时间预算耗尽时基于最后工具结果的人设化收口。
- `agent/runtime/loop_utils.py` 承接工具循环通用小件：工具签名、工具结果 trace 状态、空 stop 原始响应摘要、builtin search caller 判断、ack 安全发送和 reply trace 记录。
- `agent/runtime/prompting.py` 承接 Agent 系统提示组装：工具纪律、聊天基调、`speech_act`、多话题边界、意图专属提示、媒体输出纪律、检索计划和图片上下文提示。
- `agent/runtime/tool_loop.py` 承接模型 step I/O、usage 记账、`tool_calls` / tool result 消息回填，以及工具调用/结果 trace。
- `agent/runtime/stop_flow.py` 承接模型 stop 后的 banter 查证裁判、语义 fallback、视觉 fallback、空 stop 静默和正文收尾；`StopFlowState` 保存工具循环中的最后结果、空结果去重和证据跟进状态。
- `agent/runtime/runner.py` 保持兼容导出，只负责在对应阶段调用 `direct_tool_result_agent_result()`、`synthesize_max_steps_result()`、`append_agent_system_prompts()`、`observe_model_step()`、`handle_model_stop()` 或 tool loop / loop utils。
- `tests/test_final_synthesis.py` 直接覆盖外发工具静默、空结果 `[NO_REPLY]`、最后工具结果拟人化包装三类收口行为。
- `tests/test_tool_loop.py` 直接覆盖 assistant tool-call 消息回填、fallback 单工具交换回填、模型空 stop 观测与 trace。
- `tests/test_stop_flow.py` 直接覆盖结构性 banter 查证触发、banter stop 收口、工具后正文 bypass 长度限制和空 stop `[NO_REPLY]`。

验收：

- runner 主函数逐步保持编排视角，不再塞入所有判断细节。
- 新增工具契约或最终收口行为时优先改对应分层模块，而不是改核心循环主体。
- 相关单测能直接测契约层/最终合成层，而不是整条 Agent 循环。

## 阶段四：真实坏例回放与量化评估

状态：本轮已落地冷启动坏回复质量样例与报表汇总。

目标：把回复自然度从“调 prompt 凭感觉”推进到“可回放、可比较、可灰度”。

回放集至少覆盖：

- 空泛附和、感叹。
- 转述群友原话或总结聊天内容。
- 旁白式“我先看看/等会再说/先围观”。
- 该沉默时插话。
- 该接话时冷掉。
- 发送图片/表情后又补废话。
- 高歧义梗或专名未查证就硬接。
- 图片/表情包场景主动讲解画面。

验收：

- 每个坏例有输入上下文、期望 `speech_act`、期望回复/沉默边界和失败原因标签。
- `scripts/replay_corpus.py` 能输出 Markdown 对比报告，汇总 plan diff、质量标签、回复边界和坏回复样例。
- 新 prompt 或结构改动先看回放集差异，再灰度上线。

## 阶段五：延迟预算分层

状态：已落地 shadow 观测和可选 adaptive 接管；默认 `personification_agent_budget_mode=shadow`，不改变生产超时或工具步数，显式设置为 `adaptive` 后才按预算画像调整本轮 Agent 步数和剩余秒数。

目标：短闲聊不要每轮都付完整查证/工具循环成本，复杂查询也不要被短预算截断。

建议策略：

- `speech_act in {participate, tease}` 且 `tool_intent=["none"]`：短预算、轻工具、少模型步。
- `speech_act in {source_summary, answer}` 且 `research_need>=medium`：完整工具预算、证据综合可用。
- `speech_act=execute_action`：优先动作工具预算，成功后静默。
- `speech_act=silence`：尽快结束，不进入可见生成。

验收：

- trace 中能看到本轮预算模式、建议步数/秒数和实际步数/秒数。
- adaptive 模式下 trace `source=adaptive`，且 `agent_start.max_steps` 与实际 deadline 使用预算画像后的值。
- WebUI 慢阶段能区分语义帧、TurnPlan、工具循环、证据综合、发送层拟人延迟。
- 短闲聊 P95 明显下降，同时复杂查询不退化。

## 阶段六：短期话题状态

状态：已落地第一批结构化注入与 trace 观测。

目标：减少群聊接错话题、跨人拼接、问已知信息。

已维护轻量 topic state：

- 当前消息、当前线程与主要参与者。
- bot 是否在当前线程最近发过言。
- 当前消息是在回复谁、是否回复 bot。
- 当前消息提及了谁。
- 同时存在多少其它线程。

落点：

- `core/group_context.py` 生成 `ShortTermTopicState`，并渲染为群聊上下文的第一块提示。
- `core/builtin_hooks.py` 的近期群聊 hook 改为注入完整 `GroupConversationContext`，使普通 Agent prompt 能拿到 topic state。
- 普通回复和 YAML 回复分别记录 `topic_state` / `yaml_topic_state` trace 阶段。
- WebUI trace 信号标签展示 `topic_thread`、`reply_to_bot`、`bot_in_thread`、`parallel_threads` 等结构信号。

这仍是结构化上下文，不是关键词语义路由。模型拿 topic state 做判断，代码只负责维护事实线索；后续收益要继续通过真实坏例回放和生产 trace 验证。

验收：

- 低信息跟帖优先沿用最近同一话题。
- 多人并行话题不跨人拼接。
- 被明确 cue 时不被相邻图片/表情覆盖。

## 阶段七：WebUI 诊断面

状态：已在 trace 过程视图中展示 `speech_act`、工具、预算模式、短期 topic state 等可抽取信号；仍需继续扩充 review 和工具契约命中阶段。

目标：让调参和排错能看见 Agent 的结构决定，而不是只看最终回复。

建议展示：

- `reply_action`
- `speech_act`
- `message_target`
- `output_mode`
- `tool_intent`
- 工具契约命中情况
- 预算模式和慢阶段
- review 是否改写/沉默

验收：

- 一条不自然回复能从 WebUI 看出是 TurnPlan 错、工具选择错、证据不足、final synthesis 跑偏，还是 review 没兜住。

## 阶段八：灰度与回滚

目标：每个高风险行为层都有开关、trace 和回退路径。

建议：

- `speech_act` 先默认启用提示注入，但 TurnPlan 接管仍尊重现有开关。
- runner 拆分保持函数签名兼容。
- 新预算策略默认 shadow 记录；只有显式设置 `personification_agent_budget_mode=adaptive` 才接管生产步数/剩余秒数。
- replay 通过后再考虑默认打开更激进的策略。

验收：

- 任一阶段改坏时能通过配置回退到上一层行为。
- 单测、语义扫描和回放报告能证明没有引入关键词语义路由。
