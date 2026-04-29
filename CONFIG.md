# 配置参考

以下示例均采用 `.env.prod` / 环境变量风格书写；列表与字典请使用 JSON 字符串。

```env
personification_api_type="openai"
personification_api_url="https://api.openai.com/v1"
personification_api_key="sk-xxxx"
personification_model="gpt-4o-mini"
personification_whitelist=["123456789","987654321"]
```

默认使用 `nonebot-plugin-localstore` 的 `get_plugin_data_dir()` 结果作为插件数据目录；如需兼容旧部署或显式指定路径，可通过 `personification_data_dir` 覆盖。

## 数据目录与路径

| 配置项 | 示例写法 | 默认值 | 备注 |
| --- | --- | --- | --- |
| `personification_data_dir` | `"data/personification"` | `""` | 插件数据目录；留空时自动使用 `nonebot-plugin-localstore`。 |

## 基础行为

| 配置项 | 示例写法 | 默认值 | 备注 |
| --- | --- | --- | --- |
| `personification_whitelist` | `["123456789","987654321"]` | `[]` | 启用插件的群白名单；留空表示仅依赖命令动态申请/审批。 |
| `personification_probability` | `0.35` | `0.24` | 群聊普通随机回复概率。 |
| `personification_poke_probability` | `0.5` | `0.35` | 戳一戳事件触发回复概率。 |
| `personification_global_enabled` | `true` | `true` | 全局拟人主开关。 |
| `personification_tts_global_enabled` | `false` | `true` | 全局语音回复总开关。 |
| `personification_agent_enabled` | `true` | `true` | 是否启用 Agent 工具调用链。 |
| `personification_agent_max_steps` | `6` | `5` | 单次 Agent 允许的最大工具调用步数。 |
| `personification_image_input_mode` | `"auto"` | `"auto"` | 图片输入策略；常用值为 `auto` / `url` / `base64`，具体取决于接入模型。 |
| `personification_image_detail` | `"high"` | `"auto"` | 视觉输入细节等级；是否生效取决于 provider。 |
| `personification_schedule_global` | `true` | `false` | 全局作息模拟开关。 |
| `personification_thinking_mode` | `"low"` | `"none"` | 主回复思考模式，取值依赖接入模型。 |
| `personification_state_thinking_mode` | `"adaptive"` | `"adaptive"` | 内心状态更新时的思考模式。 |
| `personification_thinking_budget` | `512` | `0` | 推理预算；`0` 表示使用模型默认行为。 |
| `personification_include_thoughts` | `false` | `true` | 是否在部分内部流程中保留思考输出。 |
| `personification_timezone` | `"Asia/Shanghai"` | `"Asia/Shanghai"` | 作息、定时任务、时间感知使用的时区。 |
| `personification_system_prompt` | `"你现在扮演群友白露。"` | 内置默认提示词 | 主人格系统提示词。 |
| `personification_prompt_path` | `"configs/persona.yaml"` | `None` | 外部人格文件路径，支持文本或 YAML。 |
| `personification_system_path` | `"configs/system.txt"` | `None` | 外部系统提示词文件路径。 |
| `personification_max_output_chars` | `600` | `0` | 单次最终输出最大字符数；`0` 表示不额外截断。 |
| `personification_max_segment_chars` | `180` | `0` | 长消息拆段阈值；`0` 表示不额外拆段。 |

## 联网、技能与远程来源

| 配置项 | 示例写法 | 默认值 | 备注 |
| --- | --- | --- | --- |
| `personification_builtin_search` | `true` | `true` | 是否允许主模型使用内建联网能力。 |
| `personification_model_builtin_search_enabled` | `true` | `false` | 细粒度控制模型原生联网。 |
| `personification_tool_web_search_enabled` | `true` | `true` | 是否允许工具层执行联网搜索。 |
| `personification_tool_web_search_mode` | `"enabled"` | `"enabled"` | 工具联网模式；通常保持默认。 |
| `personification_web_search` | `true` | `true` | 兼容旧配置的联网总开关，已废弃。 |
| `personification_web_search_always` | `false` | `false` | 是否更激进地触发联网。 |
| `personification_skills_path` | `"data/skills"` | `None` | 本地自定义 skills 根目录。 |
| `personification_skill_sources` | `["https://github.com/org/repo"]` | `None` | 远程 skill 源列表。 |
| `personification_skill_remote_enabled` | `true` | `false` | 是否允许加载远程 skill。 |
| `personification_skill_cache_dir` | `"data/skill_cache"` | `""` | 远程 skill 缓存目录；留空由插件自行管理。 |
| `personification_skill_update_interval` | `1800` | `3600` | 远程 skill 更新检查间隔，单位秒。 |
| `personification_skill_default_timeout` | `20` | `15` | 普通 skill 执行超时，单位秒。 |
| `personification_skill_mcp_timeout` | `30` | `20` | MCP skill 调用超时，单位秒。 |
| `personification_skill_allow_unsafe_external` | `false` | `false` | 是否允许风险较高的外部来源。 |
| `personification_skill_require_admin_review` | `true` | `true` | 是否要求超管审批远程 skill。 |
| `personification_use_skillpacks` | `true` | `false` | 是否强制走 skillpack 体系。 |
| `personification_github_token` | `"ghp_xxx"` | `""` | 远程 skill / GitHub 访问令牌。 |
| `personification_plugin_knowledge_build_enabled` | `true` | `false` | 是否启用插件知识库构建能力。 |
| `personification_parallel_research_enabled` | `true` | `true` | 是否启用并行研究工具。 |
| `personification_parallel_research_lookup_enabled` | `true` | `true` | 是否允许并行研究用于复杂查询；关闭后主要用于生图准备。 |
| `personification_parallel_research_max_workers` | `4` | `6` | 单次并行研究最多子 Agent 数，上限为 6。 |
| `personification_parallel_research_worker_timeout` | `40` | `35` | 单个子 Agent 超时时间，单位秒。 |
| `personification_parallel_research_total_timeout` | `120` | `90` | 单次并行研究总超时时间，单位秒。 |
| `personification_parallel_research_max_tool_rounds` | `2` | `2` | 每个子 Agent 最多工具调用轮次。 |

## 主模型与专用模型

| 配置项 | 示例写法 | 默认值 | 备注 |
| --- | --- | --- | --- |
| `personification_api_pools` | `[{"name":"main","api_type":"openai","api_url":"https://api.openai.com/v1","api_key":"sk-xxx","model":"gpt-4o-mini"}]` | `None` | 多 provider 池配置。 |
| `personification_api_type` | `"openai"` | `"openai"` | 主模型 provider 类型。 |
| `personification_api_url` | `"https://api.openai.com/v1"` | `"https://api.openai.com/v1"` | 主模型 API 地址。 |
| `personification_api_key` | `"sk-xxxx"` | `""` | 主模型 API Key。 |
| `personification_model` | `"gpt-4o-mini"` | `"gpt-4o-mini"` | 主回复模型。 |
| `personification_lite_model` | `"gpt-5.4-mini"` | `""` | 轻量任务专用模型，用于意图分类、回复 review、图片分类等流程；留空回退到主模型。 |
| `personification_persona_api_type` | `"openai"` | `""` | 画像模型 provider；留空沿用主模型。 |
| `personification_persona_api_url` | `"https://api.openai.com/v1"` | `""` | 画像模型 API 地址。 |
| `personification_persona_api_key` | `"sk-xxxx"` | `""` | 画像模型 API Key。 |
| `personification_persona_model` | `"gpt-4o-mini"` | `""` | 用户画像分析模型。 |
| `personification_style_api_type` | `"openai"` | `""` | 风格学习模型 provider；留空沿用主模型。 |
| `personification_style_api_url` | `"https://api.openai.com/v1"` | `""` | 风格学习模型 API 地址。 |
| `personification_style_api_key` | `"sk-xxxx"` | `""` | 风格学习模型 API Key。 |
| `personification_style_api_model` | `"gpt-4o-mini"` | `""` | 风格学习/群风格分析模型。 |
| `personification_state_model` | `"gpt-4o-mini"` | `""` | 内心状态专用模型；留空沿用主模型。 |
| `personification_compress_api_type` | `"openai"` | `""` | 上下文压缩模型 provider。 |
| `personification_compress_api_url` | `"https://api.openai.com/v1"` | `""` | 上下文压缩模型 API 地址。 |
| `personification_compress_api_key` | `"sk-xxxx"` | `""` | 上下文压缩模型 API Key。 |
| `personification_compress_model` | `"gpt-4o-mini"` | `""` | 上下文压缩模型。 |
| `personification_codex_auth_path` | `"C:/Users/you/.codex/auth.json"` | `""` | `openai_codex` 模式下的 OAuth 凭证路径。 |

## 搜索、Wiki 与补充联网

| 配置项 | 示例写法 | 默认值 | 备注 |
| --- | --- | --- | --- |
| `personification_weather_api` | `"wttr"` | `"wttr"` | 天气 skill 默认使用的服务。 |
| `personification_image_search_api_key` | `"serp-xxxx"` | `""` | 图片搜索能力所需 API Key。 |
| `personification_wiki_enabled` | `true` | `true` | 是否启用 Wiki 检索。 |
| `personification_wiki_fandom_enabled` | `true` | `true` | 是否启用 Fandom Wiki 检索。 |
| `personification_fandom_wikis` | `{"原神":"genshin-impact","星穹铁道":"honkai-star-rail"}` | `None` | Fandom 站点映射。 |

## 贴图、视觉与标注

| 配置项 | 示例写法 | 默认值 | 备注 |
| --- | --- | --- | --- |
| `personification_sticker_path` | `"data/stickers"` | `"data/stickers"` | 贴图库目录。 |
| `personification_sticker_probability` | `0.35` | `0.24` | 发送贴图的概率。 |
| `personification_sticker_semantic` | `true` | `true` | 是否启用语义选图。 |
| `personification_labeler_enabled` | `true` | `true` | 是否在启动/监控时自动标注贴图。 |
| `personification_labeler_api_type` | `"openai"` | `"openai"` | 贴图标注模型 provider。 |
| `personification_labeler_api_url` | `"https://api.openai.com/v1"` | `""` | 贴图标注模型 API 地址。 |
| `personification_labeler_api_key` | `"sk-xxxx"` | `""` | 贴图标注模型 API Key。 |
| `personification_labeler_model` | `"gemini-2.0-flash"` | `"gemini-2.0-flash"` | 贴图标注模型。 |
| `personification_labeler_concurrency` | `4` | `3` | 贴图扫描并发数。 |
| `personification_fallback_enabled` | `true` | `true` | 主流程不可用时是否允许回退到补充模型。 |
| `personification_fallback_api_type` | `"openai"` | `""` | 主流程回退模型 provider。 |
| `personification_fallback_api_url` | `"https://api.openai.com/v1"` | `""` | 主流程回退模型 API 地址。 |
| `personification_fallback_api_key` | `"sk-xxxx"` | `""` | 主流程回退模型 API Key。 |
| `personification_fallback_model` | `"gpt-4o-mini"` | `""` | 主流程回退模型。 |
| `personification_fallback_auth_path` | `"C:/Users/you/.codex/auth.json"` | `""` | `openai_codex` 等模式下回退模型的凭证路径。 |
| `personification_vision_fallback_enabled` | `true` | `true` | 视觉能力不可用时是否启用回退模型。 |
| `personification_vision_fallback_provider` | `"openai"` | `""` | 回退视觉 provider；留空按默认路由。 |
| `personification_vision_fallback_model` | `"gpt-4o-mini"` | `"gpt-5.4"` | 回退视觉模型。 |
| `personification_video_understanding_enabled` | `true` | `false` | 是否启用视频理解。 |
| `personification_video_fallback_enabled` | `true` | `true` | 视频理解不可用时是否允许回退到补充模型。 |
| `personification_video_fallback_provider` | `"openai"` | `""` | 视频理解回退 provider。 |
| `personification_video_fallback_api_url` | `"https://api.openai.com/v1"` | `""` | 视频理解回退 API 地址。 |
| `personification_video_fallback_api_key` | `"sk-xxxx"` | `""` | 视频理解回退 API Key。 |
| `personification_video_fallback_model` | `"gpt-4o-mini"` | `""` | 视频理解回退模型。 |
| `personification_video_fallback_auth_path` | `"C:/Users/you/.codex/auth.json"` | `""` | 视频理解回退凭证路径。 |
| `personification_image_gen_enabled` | `true` | `true` | 是否启用图片生成 skill。 |
| `personification_image_gen_model` | `"gpt-image-2"` | `"gpt-image-2"` | 图片生成模型名。 |
| `personification_image_gen_background_enabled` | `true` | `true` | 是否允许后台图片生成流程。 |
| `personification_image_gen_timeout` | `240` | `180` | 图片生成超时时间，单位秒。 |

## 用户画像与长期记忆

| 配置项 | 示例写法 | 默认值 | 备注 |
| --- | --- | --- | --- |
| `personification_persona_enabled` | `true` | `true` | 是否启用用户画像。 |
| `personification_persona_history_max` | `50` | `30` | 画像保留的历史片段数。 |
| `personification_persona_data_path` | `"data/user_personas.json"` | `None` | 画像文件路径；留空使用插件数据目录。 |
| `personification_persona_snippet_max_chars` | `200` | `150` | 画像摘要注入上限。 |
| `personification_persona_prompt_max_chars` | `180` | `120` | 注入模型前的人像提示词截断上限。 |
| `personification_favorability_attitudes` | `{"普通":"像普通朋友一样轻松交流，会主动接话。"}` | 内置映射 | 好感阶段到口吻描述的映射。 |
| `personification_memory_enabled` | `true` | `true` | 是否启用长期记忆总开关。 |
| `personification_memory_palace_enabled` | `true` | `false` | 是否启用记忆宫殿。 |
| `personification_memory_decay_enabled` | `true` | `true` | 是否启用记忆衰减。 |
| `personification_memory_consolidation_enabled` | `true` | `true` | 是否启用记忆整理/固化。 |
| `personification_memory_recall_top_k` | `8` | `6` | 长期记忆召回数量上限。 |
| `personification_background_intelligence_enabled` | `true` | `true` | 是否启用后台智能处理。 |
| `personification_background_evolves_enabled` | `true` | `true` | 是否启用后台关系演化。 |
| `personification_background_crystals_enabled` | `true` | `true` | 是否启用后台晶化/整理。 |
| `personification_background_max_llm_tasks_per_hour` | `10` | `6` | 后台每小时最多 LLM 任务数。 |
| `personification_background_max_llm_tasks_per_day` | `40` | `24` | 后台每天最多 LLM 任务数。 |
| `personification_background_debounce_seconds` | `120` | `90` | 后台智能处理去抖时间，单位秒。 |

## 上下文、压缩与历史

| 配置项 | 示例写法 | 默认值 | 备注 |
| --- | --- | --- | --- |
| `personification_history_len` | `300` | `200` | 主对话上下文长度。 |
| `personification_compress_threshold` | `120` | `100` | 达到该条数后触发压缩。 |
| `personification_compress_keep_recent` | `24` | `20` | 压缩后保留的最近原始消息数。 |
| `personification_private_history_turns` | `40` | `30` | 私聊送入主模型的最近消息轮数上限。 |
| `personification_message_expire_hours` | `12.0` | `24.0` | 消息上下文过期时间，`0` 为禁用。 |
| `personification_group_context_expire_hours` | `4.0` | `6.0` | 群聊上下文衰减时间。 |
| `personification_group_summary_expire_hours` | `6.0` | `4.0` | 群话题摘要过期时间。 |

## 语音 / TTS

| 配置项 | 示例写法 | 默认值 | 备注 |
| --- | --- | --- | --- |
| `personification_tts_enabled` | `true` | `false` | 是否启用 TTS 功能。 |
| `personification_tts_auto_enabled` | `true` | `false` | 是否允许自动语音回复。 |
| `personification_tts_auto_probability` | `0.3` | `0.2` | 自动语音回复概率。 |
| `personification_tts_llm_decision_enabled` | `true` | `true` | 是否在合成前由 LLM 决定 `voice/text/block`。 |
| `personification_tts_decision_timeout` | `8` | `8` | TTS LLM 决策超时时间，单位秒。 |
| `personification_tts_builtin_safety_enabled` | `true` | `true` | 是否启用内置高风险内容禁读策略。 |
| `personification_tts_forbidden_policy` | `"不要朗读测试禁区内容"` | `""` | 自定义禁读策略文本；由 LLM 语义判断，不做本地关键词匹配。 |
| `personification_tts_api_key` | `"tts-xxxx"` | `""` | TTS 服务 API Key。 |
| `personification_tts_api_url` | `"https://api.xiaomimimo.com/v1"` | `"https://api.xiaomimimo.com/v1"` | TTS 服务地址。 |
| `personification_tts_model` | `"mimo-v2.5-tts"` | `"mimo-v2.5-tts"` | TTS 模型名。 |
| `personification_tts_mode` | `"preset"` | `"preset"` | TTS 模式：`preset` / `design` / `clone`。 |
| `personification_tts_default_voice` | `"mimo_default"` | `"mimo_default"` | 默认音色。 |
| `personification_tts_voice_design_prompt` | `"少女声线，自然活泼"` | `""` | design 模式下的音色描述。 |
| `personification_tts_voice_clone` | `"data:audio/wav;base64,..."` | `""` | clone 模式下的音频样本 data URL。 |
| `personification_tts_voice_clone_path` | `"data/voice/sample.wav"` | `""` | clone 模式下的音频样本文件路径。 |
| `personification_tts_default_format` | `"mp3"` | `"wav"` | 默认音频格式。 |
| `personification_tts_max_chars_per_segment` | `100` | `120` | 单段 TTS 最大字符数。 |
| `personification_tts_timeout` | `90` | `60` | TTS 请求超时，单位秒。 |
| `personification_tts_style_planner_enabled` | `true` | `false` | 是否启用风格规划器。 |
| `personification_tts_command_prefixes` | `["说","朗读","配音"]` | `["说","朗读","配音"]` | TTS 命令前缀列表。 |
| `personification_tts_private_force_auto` | `true` | `false` | 私聊下是否强制自动转语音。 |
| `personification_tts_group_default_enabled` | `false` | `true` | 群聊默认是否允许语音。 |

## 主动消息、群聊节奏与风格

| 配置项 | 示例写法 | 默认值 | 备注 |
| --- | --- | --- | --- |
| `personification_proactive_enabled` | `true` | `false` | 是否启用主动私聊。 |
| `personification_proactive_threshold` | `70.0` | `60.0` | 主动私聊触发阈值。 |
| `personification_proactive_daily_limit` | `5` | `3` | 每日主动私聊上限。 |
| `personification_proactive_interval` | `20` | `30` | 主动私聊检查间隔，单位分钟。 |
| `personification_proactive_probability` | `0.25` | `0.18` | 满足条件后的发送概率。 |
| `personification_proactive_idle_hours` | `12.0` | `24.0` | 用户空闲多久后才考虑主动触发。 |
| `personification_proactive_unsuitable_prob` | `0.1` | `0.18` | 不完全适合时仍尝试发送的概率。 |
| `personification_proactive_without_signin` | `true` | `true` | 未接入签到联动时是否仍允许主动私聊。 |
| `personification_group_idle_minutes` | `120` | `90` | 群聊空闲多久后才尝试主动发话。 |
| `personification_group_idle_enabled` | `true` | `false` | 是否启用群空闲主动发话。 |
| `personification_group_idle_check_interval` | `10` | `15` | 群空闲检查间隔，单位分钟。 |
| `personification_group_idle_daily_limit` | `2` | `1` | 每个群每天最大主动发话次数。 |
| `personification_group_chat_active_minutes` | `10` | `8` | bot 接话后的活跃窗口长度。 |
| `personification_group_chat_follow_probability` | `0.85` | `0.92` | 活跃窗口内继续接话概率。 |
| `personification_group_style_auto_analyze_threshold` | `300` | `200` | 首次自动风格分析阈值。 |
| `personification_group_style_auto_analyze_min_new_messages` | `80` | `50` | 再次自动分析前至少新增消息数。 |
| `personification_group_style_auto_analyze_cooldown_hours` | `24.0` | `12.0` | 自动分析冷却时间。 |
| `personification_group_quiet_hour_start` | `0` | `0` | 深夜禁发起始小时。 |
| `personification_group_quiet_hour_end` | `7` | `8` | 深夜禁发结束小时。 |
| `personification_group_summary_enabled` | `true` | `true` | 是否启用群聊摘要。 |

## 好友申请、黑名单与热聊保护

| 配置项 | 示例写法 | 默认值 | 备注 |
| --- | --- | --- | --- |
| `personification_friend_request_enabled` | `true` | `false` | 是否允许自动发送好友申请。 |
| `personification_friend_request_min_fav` | `90.0` | `85.0` | 触发好友申请所需最低好感。 |
| `personification_friend_request_daily_limit` | `1` | `2` | 每日好友申请上限。 |
| `personification_hot_chat_min_pass_rate` | `0.2` | `0.28` | 热聊时 bot 最低放行概率。 |
| `personification_blacklist_duration` | `600` | `300` | 临时黑名单时长，单位秒。 |

## 说说 / Qzone

| 配置项 | 示例写法 | 默认值 | 备注 |
| --- | --- | --- | --- |
| `personification_qzone_enabled` | `true` | `false` | 是否启用手动/定时发说说能力。 |
| `personification_qzone_cookie` | `"uin=o123; skey=xxx;"` | `""` | Qzone Cookie 主配置。 |
| `qzone_cookie` | `"uin=o123; skey=xxx;"` | `""` | 兼容旧配置名。 |
| `personification_qzone_proactive_enabled` | `true` | `false` | 是否启用主动检查并发说说。 |
| `personification_qzone_check_interval` | `120` | `180` | Qzone 检查间隔，单位分钟。 |
| `personification_qzone_daily_limit` | `3` | `2` | 每日最多自动发说说次数。 |
| `personification_qzone_probability` | `0.5` | `0.35` | 命中条件后发说说的概率。 |
| `personification_qzone_min_interval_hours` | `12.0` | `8.0` | 两次自动发说说最小间隔。 |

## 其他外部能力

| 配置项 | 示例写法 | 默认值 | 备注 |
| --- | --- | --- | --- |
| `personification_60s_api_base` | `"https://60s.viki.moe"` | `"https://60s.viki.moe"` | 60s 世界新闻远程接口。 |
| `personification_60s_local_api_base` | `"http://127.0.0.1:4399"` | `"http://127.0.0.1:4399"` | 本地 60s 服务接口。 |
| `personification_60s_enabled` | `true` | `true` | 是否启用 60s 新闻能力。 |

## 签到联动说明

- `nonebot-plugin-shiro-signin` 目前暂未发布，因此本插件不会将其作为必装依赖。
- 未安装签到联动插件时，相关好感度、黑名单、称号联动功能会自动降级，不影响主插件加载。
- 文档中仍保留相关配置与命令说明，是为了兼容后续联动发布以及已有本地集成环境。
