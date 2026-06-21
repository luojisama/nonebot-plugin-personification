# 剩余功能实施计划（item 5 / 8 / 10）

> 状态：计划。按 5 → 8 → 10 顺序实现，每项独立提交并合并 main。
> 已完成项见前序提交（画像加权、防拒绝、体检缓存/子模型、配色、群成员画像、MCP 核查等）。

## item 5：配置中心少用 JSON —— 字符串列表友好编辑器

**现状**：registry 已按类型推断 kind（bool→开关、choices→下拉、数字→数字框、
secret→密码、其余 list/dict→json）。标量字段已是表单；剩下用 JSON 的是结构化
数组/对象。痛点是「简单字符串数组」（白名单、peer_bot_ids、社交推送用户/群、
命令前缀等）也要手写 JSON。

**做法**：
- registry 新增 kind `strlist`：当 value_type=="list" 且非"对象数组"字段时推断为
  strlist（api_pools / model_overrides / 其它对象数组保持 json）。用字段白名单
  或"默认值元素是 str/标量"来判定。
- 前端新增 strlist 编辑器：逐项 input + 删除按钮 + 「添加一项」；保存时序列化为数组，
  复用现有 `saveField`（后端 normalize_value 已能吃数组）。
- 不改后端存储语义，仅改输入控件。

**验证**：白名单等字段在配置页显示为可增删的输入行；保存后值为字符串数组；
test_config_registry 校验 strlist 推断；webui smoke 保存往返。

## item 8：WebUI 接入 QQ 账号管理（敏感操作）

**能力**（基于 OneBot v11 + NapCat 扩展，均探测降级）：
- 账号资料：`set_qq_avatar`(头像)、`set_qq_profile`/`set_self_longnick`(昵称/签名)；
  当前信息 `get_login_info`。
- 群管理：`get_group_list` 列群、`set_group_leave` 退群（OneBot 无"主动按号加群"，
  仅能处理群邀请；加群以「处理群邀请」`set_group_add_request` 形式提供）。
- 好友管理：`get_friend_list` 列好友、`delete_friend`(删好友, NapCat)、
  处理好友请求 `set_friend_add_request`（OneBot 无"主动按号加好友"）。
- 待处理请求：监听不在范围；改为按需主动拉取/由请求事件落库后展示（先做删/退/改）。

**实现**：
- 新增 `webui/routes/qq_routes.py`：上述端点，全部 `require_admin`，调用
  `bot.call_api(...)`；不支持的 API 捕获 ActionFailed 返回友好错误（参考
  protocol_capabilities 的降级思路）。
- 头像上传：接收图片(base64/url)→ set_qq_avatar。
- 前端新增「QQ 管理」导航页：当前账号信息 + 改昵称/签名/头像 + 群列表(退群) +
  好友列表(删好友)；**所有写操作二次确认**。
- 安全：仅管理员；危险操作（退群/删好友/改资料）confirm + 审计日志。

**验证**：mock bot 的 call_api，测试各端点鉴权、参数透传、不支持 API 的降级；
前端按钮带确认。

## item 10：画像/群风格并入记忆 + 结构化持久化 + 用户更正回路

**目标**：性别/职业/年龄段等做结构化持久保存（不随历史清空丢失）；用户查看自己
画像后可提更改 → LLM 修正画像与记忆 → 群内表现迭代。

**分步**：
1. **结构化抽取与持久化**：画像生成除文本外，让 LLM 同时产出结构化 JSON
   （gender/age_group/occupation/interests/communication_style/relationship 等，
   每项含 value+confidence+evidence）。`_save_persona_sync` 把 JSON 存入
   `profile_service.upsert_core_profile(profile_json=...)`（已支持 json 列）。
   半永久字段（性别/职业）即使 history 清空也保留。
2. **用户更正回路**：
   - 入口 A：私聊/群命令「我的画像」查看 + 「修正画像 <内容>」提交更正。
   - 入口 B：WebUI 用户画像页可编辑某字段并标记"用户确认"。
   - 更正写入 core profile 的 `corrections` 段（高优先级、带"用户更正"标记）。
   - 下次再生成时，UPDATE 提示词已优先保留「用户确认/用户更正」内容（前序已埋点），
     并要求与新证据冲突时以用户更正为准。
3. **并入记忆**：把结构化半永久事实（性别/职业/重要锚点）写入 memory_store 为
   semi-permanent memory item，使其参与召回、不被衰减清理（memory_decay 已有
   semi_permanent 类型保护）。
4. **自我迭代**：群风格快照同理保留 + 用户更正；回复链路读取结构化画像增强一致性。

**风险控制**：改动触及画像生成/存储与记忆，分小步提交；每步保持向后兼容
（旧文本画像仍可读）；结构化抽取失败时回退纯文本，不阻断。

**验证**：画像生成产出并持久化 JSON；用户更正后字段被保留且标记；记忆中出现
半永久画像事实；全量回归。
