# 拟人插件回放集

## 用途

记录脱敏后的真实对话片段，用于：
- 对照新旧 TurnPlan / 语义帧的决策差异
- 验证 LLM 失败时元数据 fallback 是否合理
- PR 灰度前的回归参考材料

## 文件结构

每个 `.jsonl` 文件代表一个 group/private 的若干回合，每行一个 JSON 对象：

```jsonl
{"scene": "group|private|qzone", "messages": [...], "expected_reply": "...", "expected_frame": {...}, "metadata": {...}}
```

字段说明：
- `scene`：场景类型
- `messages`：完整 messages 数组（系统提示 + 历史 + 当前消息），按拟人插件 runner 的入参格式
- `expected_reply`：当时实际产生的最终回复（脱敏过昵称/QQ号），仅用于人工对照，不做硬断言
- `expected_frame`：当时记录的 TurnSemanticFrame 关键字段（chat_intent / ambiguity_level / recommend_silence / output_mode 等）
- `metadata`：is_private / is_random_chat / is_direct_mention / has_images / message_target 等元数据

## 脱敏要求

- QQ 号 → `user_xxxx` 占位（同一段内保持映射一致）
- 群号 → `group_xxx` 占位
- 昵称 → `friend_a` / `friend_b` ...
- 真实姓名、地址、电话 → `[REDACTED]`
- 图片 URL → 保留协议 + host，参数置空
- 时间戳 → 保留相对时间，绝对时间归零到 `2024-01-01 00:00:00`

## 跑回放

```powershell
python -m tools.personification_replay --input plugin/personification/tests/replay_corpus/*.jsonl --output replay_report.md
```

输出 markdown 报表，包含每段的 plan diff。

## 当前样本

- `sample_group_banter.jsonl` 群聊接梗场景示例（10 段）
- `sample_private.jsonl` 私聊场景示例（4 段）
- `sample_qzone.jsonl` QZone 评论链场景示例（3 段）

目标：补到 ≥100 段（群聊 70 + 私聊 20 + QZone 10）。当前 17 段为冷启动样本。
