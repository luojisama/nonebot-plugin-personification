# 拟人化升级方案：让 bot 更像群友

> 状态：方案设计（待实施）。协议层能力已对照 OneBot v11 标准与 NapCat / Lagrange /
> LLOneBot / go-cqhttp 各端文档核实，见「协议能力矩阵」。

## 1. 背景与目标

当前插件的拟人机制（回复缓冲抢占、KY 保护、作息、好感度、表情包、社交主动）已经
覆盖"说什么"的层面，但在"怎么说、何时说、除了说还能做什么"上仍有明显的 bot 特征：

1. 首条回复秒回，且回复长度与等待时间无关（最大破绽）；
2. 从不贴表情回应（真群友最高频的轻量互动）；
3. 从不引用回复、不 @ 人，跨楼回复显得没头没尾；
4. 输出多为完整段落，缺少群聊式碎片化短消息；
5. 从不犯错、从不拍人、私聊没有"正在输入"状态。

目标：在**不增加主链路 LLM 调用**的前提下，用发送层行为 + 协议端扩展 API 消除
这些特征。所有扩展 API 必须探测降级，任何失败不得影响主回复。

## 2. 协议能力矩阵（已核实）

| 能力 | OneBot v11 标准 | NapCat | Lagrange.OneBot | LLOneBot | go-cqhttp |
|---|---|---|---|---|---|
| reply 消息段（引用） | ✅ 收发均支持，参数 `id` | ✅ | ✅ | ✅ | ✅ |
| at 消息段 | ✅ 参数 `qq`（QQ 号或 all） | ✅ | ✅ | ✅ | ✅ |
| 贴表情 | ❌ 非标准 | ✅ `set_msg_emoji_like(message_id, emoji_id, set?)` | ✅ `set_group_reaction(group_id, message_id, code, is_add)` | ✅ `set_msg_emoji_like`（与 NapCat 同源，实施时真机确认） | ❌ |
| 戳一戳 API | ❌ 仅 poke 消息段（`type`+`id` 可发，NTQQ 端多不支持） | ✅ `group_poke(group_id, user_id)` / `friend_poke(user_id)` / `send_poke`（统一版） | ✅ `group_poke` / `friend_poke` | ✅ 同 NapCat | ⚠️ 仅消息段 |
| 输入状态（私聊"正在输入"） | ❌ 非标准 | ✅ `set_input_status(user_id, event_type)`，event_type=1 正在输入 / 0 正在说话（实施时真机确认取值） | ❌ | ⚠️ 待确认 | ❌ |
| 撤回消息 | ✅ `delete_msg(message_id)` | ✅ | ✅ | ✅ | ✅ |
| 协议端识别 | ✅ `get_version_info` 返回 `app_name` | "NapCat.Onebot" | "Lagrange.OneBot" | "LLOneBot" | "go-cqhttp" |

来源：botuniverse/onebot-11 segment.md、napneko.github.io/develop/api（兼容表）、
napcat.apifox.cn（参数）、Lagrange 拓展 API 文档与 koishi adapter-onebot issue #56。

表情 ID：贴表情用 QQ face id（如 76=赞、66=爱心、182=笑哭、124=OK、4=得意），
完整对照以 koishi QFace 表 / coolq face id wiki 为准，实施时内置一张小映射表。

## 3. 总体架构：协议能力探测层

新增 `core/protocol_capabilities.py`：

```
class ProtocolCapabilities:
    # 启动后首次使用时调 get_version_info（标准 API、无副作用）识别 app_name，
    # 得到 napcat / lagrange / llonebot / gocq / unknown 档位。
    # 扩展 API 采用"惰性失败降级"：第一次真实调用时捕获 ActionFailed，
    # 按 (self_id, api_name) 缓存 unsupported，之后直接跳过，绝不重复试错。
    async def emoji_react(bot, *, group_id, message_id, face_id) -> bool
    async def poke(bot, *, group_id, user_id) -> bool      # group_poke→send_poke→poke段 兜底链
    async def set_typing(bot, *, user_id) -> bool          # 仅 napcat 档位尝试
```

- 配置 `personification_protocol_extensions`：`auto`（默认，按 app_name + 失败缓存）/
  `none`（禁用全部扩展 API）/ 强制指定档位。
- 所有方法 never-raise，返回 bool；失败仅 debug 日志。

## 4. 特性分期

### Phase A（纯发送层，无新增 LLM 调用，优先实施）

**A1 打字延迟（消灭秒回长文）**
- 锚点：`handlers/reply_pipeline/processor.py` 发送循环（现 ~1612 行起）。
- 公式：`delay = base_read(0.5-1.2s) + len(segment)/cps - already_elapsed`，
  `cps` 默认 7 字/秒，clamp 到 [0, max_delay(默认 5s)]。
  **关键**：扣除 LLM 生成已耗时（函数内已有 `started_at`），生成本身慢就不再叠加；
  作息深夜时段（复用 `is_rest_time`）乘 1.5。
- 段间延迟从固定 `uniform(0.8,1.6)` 改为按下一段长度计算。
- sleep 之后保留现有 `_stale_reply_abort_reason` 检查（延迟期间新消息可抢占）。

**A2 碎片化输出（群聊式短消息）**
- 锚点：核心人设模板（`core/prompts/`）追加输出风格约束：
  "像 QQ 群友一样把回复拆成 1-3 条短消息（空行分隔），单条尽量 ≤40 字，
  口语化，不用完整段落"。受已有 group_style 分析结果调制（话痨群多拆、安静群少发）。
- 下游 `split_text_into_segments`（event_rules.py:460）已按空行切分，无需改动。
- 与回复审阅（response_review）兼容：审阅提示词同步加"保持分条格式"。

**A3 引用回复**
- 锚点：processor.py 发送首段处；`reply_to_msg_id` 已在 relation metadata
  （processor.py:1711）。
- 条件：被回复消息之后群里已有 ≥N 条新消息（默认 4，可配）、或 buffer 抢占后回复
  非最后一条消息时，首段构造 `MessageSegment.reply(msg_id) + 文本`（v11 标准段，全端可用）。
- 约束：私聊不引用；对同一用户连续两轮不重复引用。

**A4 贴表情回应**
- 新文件 `handlers/reaction.py`，两个触发点：
  1. intent 判定 NO_REPLY 但情绪信号明确（pipeline_emotion 已产出情绪标签）→
     概率 p（默认 0.25）对触发消息贴表情，代替沉默；
  2. 正常回复前小概率（默认 0.1）先给对方消息贴个表情再发文字（"已读+回应"双动作）。
- 表情选择：内置 情绪→face_id 候选表（不新增 LLM 调用），同情绪随机；
  后续迭代可让主回复 LLM 输出 `[REACT:赞]` 标记（解析后移除）。
- 风控：同一消息只贴一次；同群冷却 ≥60s；每群每日上限（默认 20）。
- 走 ProtocolCapabilities.emoji_react，go-cqhttp 等不支持端静默跳过。

### Phase B（概率行为，第二批）

**B1 错别字 + 修正**
- 锚点：`handlers/reply_pipeline/postprocess.py`。
- 仅 `message_intent == "banter"` 的闲聊短句（6-40 字纯中文）注入同音/形近错字
  （内置小字典：的地得、在再、做作、那哪、他她它…，不引第三方拼音库），
  概率默认 0（关闭，文档建议 0.03）；70% 概率隔 1-2s 跟一条"打错了，是 X"。
- 绝不作用于事实解释类输出（复用已有 `looks_like_explanatory_output`）。

**B2 主动拍一拍**
- 被拍响应升级：现有 poke 回复流程中加"拍回去"分支（概率默认 0.3），
  走 `ProtocolCapabilities.poke`。
- 主动拍：好感 ≥阈值用户冒泡且本轮不回文本时小概率拍（默认关闭，日限 3/群）。

**B3 @ 人**
- 触发：buffer 合并了多个发言人且回复对象不是最后发言者时，首段前加
  `MessageSegment.at(user_id) + " "`（标准段）。与 A3 互斥：已引用则不再 @。
- social_topic_followup 群内跟进消息也可带 @。

**B4 私聊"正在输入"**
- 私聊回复且 A1 延迟 >1.5s 时，先 `set_typing(user_id)` 再 sleep。
  仅 napcat 档位尝试；event_type 取值实施时真机确认（预期 1=正在输入）。

### 不做的：撤回重发
风控敏感（频繁撤回易触发风控）且 LLM 难以自然决策"发错了什么"，收益/风险比最差，
本期不做。

## 5. 新增配置（全部注册到 config_registry_extra.py，分组「拟人行为」）

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| personification_protocol_extensions | str | auto | 协议扩展档位 auto/none/napcat/lagrange |
| personification_humanize_typing_enabled | bool | true | 打字延迟 |
| personification_humanize_typing_cps | float | 7.0 | 模拟打字速度（字/秒） |
| personification_humanize_typing_max_delay | float | 5.0 | 单段延迟上限（秒） |
| personification_humanize_fragment_style | str | prompt | 碎片化输出 off/prompt |
| personification_humanize_quote_reply_enabled | bool | true | 引用回复 |
| personification_humanize_quote_reply_min_gap | int | 4 | 触发引用的最小间隔消息数 |
| personification_humanize_reaction_enabled | bool | true | 贴表情 |
| personification_humanize_reaction_probability | float | 0.25 | NO_REPLY 时贴表情概率 |
| personification_humanize_reaction_daily_limit | int | 20 | 每群每日贴表情上限 |
| personification_humanize_typo_probability | float | 0.0 | 错别字概率（0=关） |
| personification_humanize_poke_back_probability | float | 0.3 | 被拍后拍回去概率 |
| personification_humanize_proactive_poke_enabled | bool | false | 主动拍一拍 |
| personification_humanize_at_enabled | bool | true | 多人场景 @ 对方 |
| personification_humanize_input_status_enabled | bool | true | 私聊输入状态 |

注意：`tests/test_config_registry_coverage.py` 会强制 config.py 新字段必须同步注册，
两边一起提交。

## 6. 测试与验证

单元测试（新增 `tests/test_humanize_*.py`）：
- 延迟公式：长度/已耗时/上限/深夜系数的边界值；
- typo 注入器：固定 seed 下的确定性输出、explanatory 文本绝不注入；
- 引用触发条件：gap 计数、私聊豁免、连续去重；
- ProtocolCapabilities：mock `bot.call_api` 抛 ActionFailed → unsupported 缓存生效、
  二次调用不再发请求；get_version_info 各 app_name 档位判定。

真机验收清单（NapCat 为主、Lagrange 抽查）：
- [ ] 长回复有可感知的"打字时间"，且 LLM 慢响应时不叠加延迟
- [ ] 延迟期间新消息进来能正常抢占
- [ ] NO_REPLY 场景观察到贴表情，群日限生效
- [ ] 跨楼回复带引用、混战场景带 @，私聊无引用
- [ ] 被拍后偶尔被拍回
- [ ] go-cqhttp / 关闭扩展档位下所有功能静默降级、主回复无异常
- [ ] Lagrange 下贴表情走 set_group_reaction（参数名以其 apifox 文档为准）

## 7. 实施顺序与风险

顺序：A1 → A2 → 能力探测层 → A4 → A3 → B3 → B2 → B4 → B1（每步可独立上线）。

风险：
- 扩展 API 触发风控：所有主动行为均有概率 + 冷却 + 日限三重闸，默认值保守；
- 协议端版本差异：惰性降级保证最坏情况 = 行为不出现，而非报错；
- A2 改提示词影响回复质量：humanize_fragment_style=off 一键回退；
- 延迟引入的时序问题：复用现有 stale-abort 与 generation 抢占机制，不另起状态。
