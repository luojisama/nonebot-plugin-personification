from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel
import warnings

from .core.memory_defaults import (
    DEFAULT_COMPRESS_KEEP_RECENT,
    DEFAULT_COMPRESS_THRESHOLD,
    DEFAULT_GROUP_CONTEXT_EXPIRE_HOURS,
    DEFAULT_GROUP_SUMMARY_EXPIRE_HOURS,
    DEFAULT_HISTORY_LEN,
    DEFAULT_MEMORY_RECALL_TOP_K,
    DEFAULT_MESSAGE_EXPIRE_HOURS,
    DEFAULT_PERSONA_HISTORY_MAX,
    DEFAULT_PRIVATE_HISTORY_TURNS,
)
from .core.favorability import DEFAULT_FAVORABILITY_EVENT_DELTAS, DEFAULT_FAVORABILITY_LEVELS


DEFAULT_FAVORABILITY_ATTITUDES: Dict[str, str] = {
    "初见": "保持基本礼貌，态度温和但不过于亲热。",
    "面熟": "表现得比较客气，愿意倾听并给出简单回应。",
    "初识": "态度随和，偶尔会分享一些有趣的小事，语气活泼。",
    "普通": "像普通朋友一样轻松交流，会主动接话。",
    "熟悉": "言谈举止比较随意，经常互相调侃，表现得很开心。",
    "信赖": "非常信任对方，说话很贴心，会表达关心。",
    "知心": "默契十足，有很多共同话题，语气变得亲近。",
    "深厚": "关系非常深厚，会主动分享心情，给对方支持。",
    "挚友": "无话不谈，对对方充满热情和信任。",
    "亲密": "非常亲昵，语气温柔，充满宠溺和爱护。",
}


class Config(BaseModel):
    personification_whitelist: List[str] = []
    personification_probability: float = 0.30

    # 其他机器人 / Q 群管家的 user_id（用于 peer_awareness 检测），
    # 命中后本轮静默，避免 bot 与管家互相对话。
    personification_peer_bot_ids: List[str] = []

    personification_global_enabled: bool = True
    personification_tts_global_enabled: bool = True

    personification_agent_enabled: bool = True
    personification_agent_max_steps: int = 10
    personification_agent_budget_mode: str = "shadow"
    personification_response_timeout: int = 180
    personification_image_input_mode: str = "auto"
    personification_image_detail: str = "auto"
    personification_sticker_vision_max: int = 3
    personification_gif_understanding_enabled: bool = False
    personification_gif_understanding_timeout: float = 12.0
    personification_gif_max_bytes: int = 8 * 1024 * 1024
    personification_gif_max_decode_frames: int = 180
    personification_gif_sample_frames: int = 8
    personification_gif_contact_sheet_long_edge: int = 1600
    personification_gif_max_per_turn: int = 1
    personification_gif_summary_cache_enabled: bool = True
    personification_builtin_search: bool = True
    # 默认启用：Gemini/Anthropic/OpenAICodex 等支持的 caller 会直接用 provider 原生
    # 联网搜索（google_search / web_search_20250305 / web_search_options），无需任何 key。
    # 不支持的 provider 自动回落到外部 web_search 工具。可在 WebUI 关回 False。
    personification_model_builtin_search_enabled: bool = True
    personification_tool_web_search_enabled: bool = True
    personification_tool_web_search_mode: str = "enabled"
    personification_tool_web_fetch_enabled: bool = True
    personification_tool_web_fetch_timeout: int = 60
    # 免配置联网搜索引擎链（按顺序并行调用，合并去重）。可选项：wikipedia / searxng / duckduckgo。
    personification_free_search_engines: List[str] = ["wikipedia", "searxng", "duckduckgo"]
    # SearXNG 公共实例池，留空则用 core/free_search.py:DEFAULT_SEARXNG_INSTANCES。
    personification_searxng_instances: List[str] = []
    # web_search 返回给 LLM 的结果上限（top-N 渲染）与单条 snippet 字符上限。
    personification_web_search_max_results: int = 6
    personification_web_search_snippet_chars: int = 400
    # web_fetch / web_search 走哪个 HTTP 代理（http://host:port）。
    # 国内服务器抓取被 DNS 污染/墙的站点（如 Cloudflare 前置站点、海外 API）时，
    # 设置后请求走代理、由代理侧解析 DNS 并连接，绕开本地污染。
    # 非空时 web_fetch 会跳过"本地 DNS 解析到内网就拒绝"的判断（仍拦截字面内网 IP）。
    # 留空 = 直连 + 本地 DNS SSRF 校验。
    personification_web_proxy: str = ""
    # Antigravity CLI 调用走哪个 HTTP 代理（http://host:port）。
    # 非空时所有 antigravity v1internal / OAuth refresh 请求都强制走它，
    # 不依赖 HTTPS_PROXY / HTTP_PROXY 环境变量（bot 进程未必继承终端 env）。
    # 留空 = 沿用 httpx 的环境变量解析（trust_env 默认 True）。
    personification_antigravity_cli_proxy: str = ""
    # /拟人更新 与 WebUI 插件更新走的 git 镜像反代列表（GitHub 在国内不稳时按顺序探测）。
    # 有配置时：并行 HEAD 探测每一项的联通性，按列表顺序选第一个能通的，
    # 用 -c url.X.insteadOf 临时改写重试 fetch/pull，不污染全局 git config。
    # 留空 = 关闭镜像优先，只走直连。
    personification_git_mirror_prefixes: List[str] = [
        "https://ghproxy.com",
        "https://gh-proxy.com",
        "https://mirror.ghproxy.com",
        "https://hub.gitmirror.com",
    ]
    # 单个镜像配置（向后兼容；非空时会自动并入 prefixes 末尾）
    personification_git_mirror_prefix: str = ""
    # Provider 动态优先级（基于真实请求 latency / success_rate 自动调整排序）
    personification_provider_dynamic_priority_enabled: bool = True
    # 样本数 < min_samples 时仍用配置的 base priority，避免冷启动 fluke
    personification_provider_health_min_samples: int = 3
    # ──────────── Social Intelligence（主动社交框架）────────────
    # 总开关：默认关闭，配置好场景后再打开避免上线就乱发
    personification_social_intelligence_enabled: bool = False
    # LLM 闸门：开启则每次发送前用 lite_model 二次决策"现在合不合适"
    personification_social_gate_enabled: bool = True
    # 每用户每日最多收到的主动社交消息数（跨场景共享）
    personification_social_daily_quota_per_user: int = 2
    # 早安问候 cron 时点
    personification_social_morning_hour: int = 8
    personification_social_morning_greeting_enabled: bool = True
    # 晚安问候 cron 时点
    personification_social_evening_hour: int = 22
    personification_social_evening_greeting_enabled: bool = True
    # 单场景冷却（默认 18 小时，避免一天给同一人发两次早安）
    personification_social_greeting_cooldown_seconds: int = 64800
    # 早晚问候每次最多发给多少人（按 persona updated_at 取最近活跃的）
    personification_social_greeting_max_recipients: int = 8
    # 定时新闻推送
    personification_social_news_enabled: bool = False
    personification_social_news_hour: int = 9
    personification_social_news_users: List[str] = []
    personification_social_news_groups: List[str] = []
    # 新闻来源：daily / ai / history
    personification_social_news_source: str = "daily"
    personification_social_news_cooldown_seconds: int = 72000
    # 话题延续：扫描间隔（分钟）+ 跟进窗口（承诺时间 ± N 小时内才跟进）
    personification_social_topic_followup_enabled: bool = True
    personification_social_topic_scan_interval_minutes: int = 60
    personification_social_topic_followup_window_hours: int = 24
    personification_social_topic_followup_cooldown_seconds: int = 43200
    # 节日祝福：公历节日 + 生日（从 persona 抽取）
    personification_social_festival_enabled: bool = True
    personification_social_festival_hour: int = 9
    personification_social_festival_max_recipients: int = 20
    personification_social_festival_cooldown_seconds: int = 82800
    personification_thinking_mode: str = "none"
    personification_state_thinking_mode: str = "adaptive"
    personification_model_overrides: Dict[str, str] = {}
    personification_response_review_enabled: bool = False
    personification_response_review_model_role: str = "review"
    personification_turn_planner_enabled: bool = False
    personification_turn_planner_shadow_enabled: bool = False
    personification_semantic_frame_timeout: float = 8.0
    personification_evidence_synthesizer_enabled: bool = False
    personification_cross_verify_enabled: bool = False
    personification_lorebook_enabled: bool = False
    personification_group_knowledge_enabled: bool = False
    personification_group_knowledge_autobuild_enabled: bool = True
    personification_group_knowledge_interval_hours: int = 4
    personification_group_knowledge_daily_limit: int = 6
    personification_group_knowledge_min_messages: int = 50
    personification_qzone_quiet_hour_start: int = 0
    personification_qzone_quiet_hour_end: int = 7
    # Provider 月度额度（本地记账，3 家 provider 无官方 quota API；0=不限额仅显示用量）
    personification_quota_anthropic_monthly_tokens: int = 0
    personification_quota_openai_monthly_tokens: int = 0
    personification_quota_gemini_cli_monthly_tokens: int = 0
    personification_quota_codex_monthly_tokens: int = 0
    personification_group_style_autobuild_enabled: bool = True
    personification_group_style_interval_hours: int = 12
    personification_group_style_daily_limit: int = 2
    personification_group_style_min_messages: int = 100
    personification_image_host_allowlist: List[str] = []
    personification_active_learning_enabled: bool = False
    personification_active_learning_daily_quota: int = 5
    personification_relation_evolution_enabled: bool = False
    personification_relation_evolution_daily_quota: int = 10
    personification_persona_responder_json_enabled: bool = False
    personification_data_dir: str = ""
    personification_persona_enabled: bool = True
    personification_persona_history_max: int = DEFAULT_PERSONA_HISTORY_MAX
    personification_persona_data_path: Optional[str] = None
    personification_persona_snippet_max_chars: int = 150
    personification_persona_prompt_max_chars: int = 120
    personification_memory_enabled: bool = True
    personification_memory_palace_enabled: bool = True
    personification_real_embedding_enabled: bool = False
    personification_embedding_provider: str = "hash_bow"
    personification_embedding_model: str = ""
    personification_embedding_api_url: str = ""
    personification_embedding_api_key: str = ""
    personification_embedding_batch_size: int = 16
    personification_memory_vector_backend: str = "sqlite_exact"
    personification_memory_rag_enabled: bool = True
    personification_memory_rag_candidate_limit: int = 80
    personification_memory_decay_enabled: bool = True
    personification_memory_consolidation_enabled: bool = True
    personification_memory_recall_top_k: int = DEFAULT_MEMORY_RECALL_TOP_K
    personification_memory_search_scan_limit: int = 800
    personification_memory_capture_policy: str = "balanced"
    personification_agent_memory_write_enabled: bool = True
    personification_background_intelligence_enabled: bool = True
    personification_background_evolves_enabled: bool = True
    personification_background_crystals_enabled: bool = True
    personification_background_max_llm_tasks_per_hour: int = 6
    personification_background_max_llm_tasks_per_day: int = 24
    personification_background_debounce_seconds: int = 90
    personification_max_output_chars: int = 0
    personification_max_segment_chars: int = 0
    personification_skills_path: Optional[str] = None
    personification_skill_sources: Optional[Union[str, List[Any]]] = None
    personification_skill_remote_enabled: bool = False
    personification_skill_cache_dir: str = ""
    personification_skill_update_interval: int = 3600
    personification_skill_default_timeout: int = 15
    personification_skill_mcp_timeout: int = 20
    personification_skill_allow_unsafe_external: bool = False
    personification_skill_require_admin_review: bool = True
    personification_use_skillpacks: bool = False
    personification_timezone: str = "Asia/Shanghai"
    personification_sticker_semantic: bool = True
    personification_sticker_collect_meme_policy: str = "reject"
    personification_weather_api: str = "wttr"
    personification_labeler_enabled: bool = True
    personification_labeler_api_type: str = "openai"
    personification_labeler_api_url: str = ""
    personification_labeler_api_key: str = ""
    personification_labeler_model: str = ""
    personification_labeler_concurrency: int = 3
    personification_sticker_labeler_research_enabled: bool = True
    personification_sticker_labeler_research_max_queries: int = 2
    personification_sticker_labeler_research_timeout: float = 12.0
    personification_fallback_enabled: bool = True
    personification_fallback_api_type: str = ""
    personification_fallback_api_url: str = ""
    personification_fallback_api_key: str = ""
    personification_fallback_model: str = ""
    personification_fallback_auth_path: str = ""
    personification_vision_fallback_enabled: bool = True
    personification_vision_fallback_provider: str = ""
    personification_vision_fallback_model: str = ""
    personification_video_understanding_enabled: bool = False
    personification_video_fallback_enabled: bool = True
    personification_video_fallback_provider: str = ""
    personification_video_fallback_api_url: str = ""
    personification_video_fallback_api_key: str = ""
    personification_video_fallback_model: str = ""
    personification_video_fallback_auth_path: str = ""
    personification_plugin_knowledge_build_enabled: bool = False
    # plugin_invoker：让 bot 代为执行其它已安装插件的命令并转述结果（默认关闭，安全起见）
    personification_plugin_invoker_enabled: bool = False
    personification_plugin_invoker_allowlist: list[str] = []  # 非空则仅允许这些插件
    personification_plugin_invoker_blocklist: list[str] = []  # 插件名 或 插件名:命令
    personification_plugin_invoker_max_calls_per_turn: int = 2
    personification_plugin_invoker_capture_timeout: float = 15.0
    personification_plugin_invoker_max_output_chars: int = 1500
    personification_plugin_invoker_extra_danger_keywords: list[str] = []
    personification_image_gen_enabled: bool = True
    personification_image_gen_api_type: str = "auto"
    personification_image_gen_api_url: str = ""
    personification_image_gen_api_key: str = ""
    personification_image_gen_model: str = "gpt-image-2"
    personification_image_gen_nanobanan_model: str = "gemini-3-pro-image-preview"
    personification_image_gen_background_enabled: bool = True
    personification_image_gen_timeout: int = 180
    personification_parallel_research_enabled: bool = True
    personification_deep_research_v2_enabled: bool = False
    personification_parallel_research_lookup_enabled: bool = True
    personification_parallel_research_max_workers: int = 6
    personification_parallel_research_worker_timeout: int = 35
    personification_parallel_research_total_timeout: int = 90
    personification_parallel_research_max_tool_rounds: int = 2
    personification_parallel_research_pages_per_worker: int = 20
    personification_qzone_enabled: bool = False
    personification_qzone_cookie: str = ""
    # DEPRECATED: use personification_qzone_cookie.
    qzone_cookie: str = ""
    personification_qzone_proactive_enabled: bool = True
    personification_qzone_check_interval: int = 60
    personification_qzone_monthly_limit: int = 30
    personification_qzone_agent_max_steps: int = 4
    personification_qzone_probability: float = 0.20
    personification_qzone_min_interval_hours: float = 12.0
    personification_qzone_social_enabled: bool = True
    personification_qzone_social_check_interval: int = 30
    personification_qzone_social_scope: str = "recent_interactions"
    personification_qzone_social_like_limit: int = 0
    personification_qzone_social_comment_limit: int = 0
    personification_qzone_social_per_friend_limit: int = 0
    personification_qzone_social_max_feeds_per_scan: int = 5
    personification_qzone_forward_enabled: bool = True
    personification_qzone_forward_limit: int = 1
    personification_qzone_forward_max_per_scan: int = 1
    personification_qzone_third_party_chime_in_enabled: bool = True
    personification_qzone_inbound_enabled: bool = True
    personification_qzone_inbound_check_interval: int = 3
    personification_qzone_inbound_max_feeds_per_scan: int = 20
    personification_qzone_inbound_max_comments_per_feed: int = 20
    personification_qzone_outbound_reply_enabled: bool = True
    personification_qzone_outbound_reply_check_interval: int = 3
    personification_qzone_outbound_reply_max_feeds: int = 30
    personification_qzone_outbound_reply_lookback_hours: float = 72.0
    personification_image_search_api_key: str = ""
    personification_github_token: str = ""
    personification_web_search_always: bool = False
    personification_state_model: str = ""
    personification_wiki_enabled: bool = True
    personification_wiki_fandom_enabled: bool = True
    personification_fandom_wikis: Optional[Union[str, Dict[str, str]]] = None
    # 游戏信息工具（game_info）：聚合更新公告/攻略/剧情/技巧，数据源含 Steam 官方与社区。
    personification_game_info_enabled: bool = True
    personification_game_info_timeout: float = 15.0
    # 攻略/技巧定向搜索时附加的社区站点白名单（覆盖默认）；留空用内置默认列表。
    personification_game_info_community_sites: Optional[Union[str, List[str]]] = None

    personification_api_pools: Optional[Union[str, List[Dict[str, Any]]]] = None
    personification_api_type: str = "openai"
    personification_api_url: str = ""
    personification_api_key: str = ""
    personification_model: str = "gpt-4o-mini"
    # 轻量任务（intent 分类、回复 review、随机插话判定、图片分类）使用的模型名。
    # 留空时 fallback 到主模型，无需额外配置。
    # 建议值：与主模型同 provider 的 mini 版本（如 gpt-4.1-mini / gpt-5.4-mini）。
    personification_lite_model: str = ""
    # 严格主模型模式：开启后忽略 lite_model 配置，所有 intent / review / 闸门 /
    # 表情决策都走主模型，避免 cooldown / 网关失败时降级到弱模型导致 bot 突然变傻。
    # 默认关闭，让已配置的 lite_model 真正承担轻量任务，降低普通闲聊回复延迟。
    personification_strict_main_model: bool = False
    personification_persona_api_type: str = ""
    personification_persona_api_url: str = ""
    personification_persona_api_key: str = ""
    personification_persona_model: str = ""
    personification_style_api_type: str = ""
    personification_style_api_url: str = ""
    personification_style_api_key: str = ""
    personification_style_api_model: str = ""
    personification_tts_enabled: bool = False
    personification_tts_auto_enabled: bool = False
    personification_tts_auto_probability: float = 0.2
    personification_tts_llm_decision_enabled: bool = False
    personification_tts_llm_decision_model_role: str = "agent"
    personification_tts_decision_timeout: int = 8
    personification_tts_builtin_safety_enabled: bool = True
    personification_tts_forbidden_policy: str = ""
    personification_tts_api_key: str = ""
    personification_tts_api_url: str = "https://api.xiaomimimo.com/v1"
    personification_tts_model: str = "mimo-v2.5-tts"
    personification_tts_mode: str = "preset"
    personification_tts_default_voice: str = "mimo_default"
    personification_tts_voice_design_prompt: str = ""
    personification_tts_voice_clone: str = ""
    personification_tts_voice_clone_path: str = ""
    personification_tts_default_format: str = "wav"
    personification_tts_max_chars_per_segment: int = 120
    personification_tts_timeout: int = 60
    personification_tts_style_planner_enabled: bool = False
    personification_tts_command_prefixes: List[str] = ["说", "朗读", "配音"]
    personification_tts_private_force_auto: bool = False
    personification_tts_group_default_enabled: bool = True

    personification_thinking_budget: int = 0
    personification_include_thoughts: bool = True

    personification_system_prompt: str = (
        "你是一个群聊成员，性格活泼，说话幽默。"
        "你可以根据当前语境决定是否回复，如果不回复请只输出 [NO_REPLY]。"
    )
    personification_core_values_enabled: bool = True
    personification_core_values_prompt: str = (
        "你有稳定的基础三观和判断底线：尊重生命、公共安全、法律责任与人的尊严；"
        "不要把违法、危险、伤害他人、逃避责任或损害公共秩序的行为说成值得同情、羡慕或鼓励的事。"
        "群聊玩笑可以接，但底线不能歪：如果话题涉及安全、违法、伤害或责任，"
        "先自然承认行为本身不对或风险很大，再用简短口语把话接住；不要长篇说教，也不要装成官方普法。"
        "对受害者、弱者、被伤害的人保持基本共情；不嘲笑苦难，不美化欺凌、歧视、暴力或剥削。"
    )
    personification_prompt_path: Optional[str] = None
    personification_system_path: Optional[str] = None

    personification_favorability_enabled: bool = True
    personification_favorability_default_score: float = 0.0
    personification_favorability_group_default_score: float = 100.0
    personification_favorability_levels: Dict[str, float] = DEFAULT_FAVORABILITY_LEVELS.copy()
    personification_favorability_attitudes: Dict[str, str] = DEFAULT_FAVORABILITY_ATTITUDES.copy()
    personification_favorability_event_deltas: Dict[str, float] = DEFAULT_FAVORABILITY_EVENT_DELTAS.copy()
    personification_favorability_daily_positive_cap: float = 5.0
    personification_favorability_group_daily_positive_cap: float = 10.0
    personification_favorability_daily_negative_cap: float = 30.0
    personification_favorability_event_log_limit: int = 50
    personification_favorability_decay_enabled: bool = False
    personification_favorability_decay_idle_days: int = 14
    personification_favorability_decay_delta: float = -0.20

    personification_history_len: int = DEFAULT_HISTORY_LEN
    # 滚动窗口：触发压缩的条数阈值（达到此数量时压缩）
    personification_compress_threshold: int = DEFAULT_COMPRESS_THRESHOLD
    # 压缩后保留的最近原始消息条数
    personification_compress_keep_recent: int = DEFAULT_COMPRESS_KEEP_RECENT
    # 私聊送入主模型的最近消息条数上限，越大越容易延续长对话，但也更耗 token
    personification_private_history_turns: int = DEFAULT_PRIVATE_HISTORY_TURNS
    # 消息过期时间（小时），超过此时间的消息不再作为上下文，设为 0 禁用
    personification_message_expire_hours: float = DEFAULT_MESSAGE_EXPIRE_HOURS
    # 群聊上下文默认衰减更快，减少机器人长期围着旧话题打转
    personification_group_context_expire_hours: float = DEFAULT_GROUP_CONTEXT_EXPIRE_HOURS
    # 群聊话题摘要过期时间（小时），过期后不再注入旧摘要
    personification_group_summary_expire_hours: float = DEFAULT_GROUP_SUMMARY_EXPIRE_HOURS
    # DEPRECATED: 仅在未配置 personification_fallback_* 时作为兜底别名读取
    personification_compress_api_type: str = ""
    # DEPRECATED: 仅在未配置 personification_fallback_* 时作为兜底别名读取
    personification_compress_api_url: str = ""
    # DEPRECATED: 仅在未配置 personification_fallback_* 时作为兜底别名读取
    personification_compress_api_key: str = ""
    # DEPRECATED: 仅在未配置 personification_fallback_* 时作为兜底别名读取
    personification_compress_model: str = ""

    personification_sticker_path: Optional[str] = "data/stickers"
    personification_sticker_probability: float = 0.24
    personification_sticker_library_soft_limit: int = 800
    personification_sticker_library_hard_limit: int = 1200
    personification_sticker_per_mood_limit: int = 50
    personification_sticker_collect_cooldown_seconds: int = 60
    personification_sticker_collect_sample_rate: float = 0.5
    personification_sticker_collect_min_confidence: float = 0.7
    personification_sticker_second_judge_enabled: bool = False
    personification_sticker_curator_enabled: bool = False
    personification_sticker_curator_interval_days: int = 3
    personification_qq_expression_enabled: bool = True
    personification_qq_expression_probability: float = 0.08
    personification_qq_expression_cooldown_seconds: int = 180
    personification_qq_expression_daily_limit: int = 30
    personification_qq_expression_super_probability: float = 0.02
    personification_qq_expression_triple_probability: float = 0.02
    personification_qq_favorite_expression_probability: float = 0.02

    personification_poke_probability: float = 0.35
    # DEPRECATED: replaced by the agent web_search skill configuration.
    personification_web_search: bool = True
    personification_schedule_global: bool = False

    personification_proactive_enabled: bool = True
    personification_proactive_threshold: float = 60.0
    personification_proactive_require_user_profile: bool = True
    personification_proactive_daily_limit: int = 3
    personification_proactive_interval: int = 30
    personification_proactive_probability: float = 0.18
    personification_proactive_idle_hours: float = 24.0
    personification_proactive_unsuitable_prob: float = 0.18
    personification_proactive_without_signin: bool = True
    # 群聊空闲主动发话配置
    # 群聊多少分钟无消息后触发主动发话（默认 90 分钟）
    personification_group_idle_minutes: int = 90
    personification_group_idle_enabled: bool = False
    # 主动发话的检测间隔（定时任务频率，分钟，默认 15）
    personification_group_idle_check_interval: int = 15
    # 每个群每天最多主动发话次数（默认 1）
    personification_group_idle_daily_limit: int = 1
    # J4: 主动水群两阶段——多大概率额外跑一次"决定模式"LLM call，0=永远纯文本（旧行为）
    personification_group_idle_mode_decision_prob: float = 0.4
    # Bot 刚接过话后，保留一段“活跃窗口”，更容易继续顺着当前话题聊
    personification_group_chat_active_minutes: int = 12
    personification_group_chat_follow_probability: float = 0.96
    # 群风格自动分析阈值；首次达到后改用冷却+新增消息策略控制重触发。
    personification_group_style_auto_analyze_threshold: int = 200
    # 距离上次自动分析至少新增多少条消息才允许再次触发。
    personification_group_style_auto_analyze_min_new_messages: int = 50
    # 自动风格分析冷却时间（小时）。
    personification_group_style_auto_analyze_cooldown_hours: float = 12.0
    # 深夜禁发的起始小时（含，默认 0 点）
    personification_group_quiet_hour_start: int = 0
    # 深夜禁发的结束小时（不含，默认 7 点）
    personification_group_quiet_hour_end: int = 8
    personification_group_summary_enabled: bool = True
    personification_friend_request_enabled: bool = False
    personification_friend_request_min_fav: float = 85.0
    personification_friend_request_daily_limit: int = 2

    # KY 保护：热聊时 bot 随机发言的最低通过概率（0.0 完全拦截，0.3 保留30%机会）
    personification_hot_chat_min_pass_rate: float = 0.40

    # ──────────── 拟人化发送层（humanize）────────────
    # 协议扩展档位：auto=按 get_version_info 自动识别；none=禁用全部扩展 API；
    # 也可强制指定 napcat / lagrange / llonebot / gocq
    personification_protocol_extensions: str = "auto"
    # 打字延迟：首条回复按 阅读时间+len/cps-LLM已耗时 模拟，消除"秒回长文"
    personification_humanize_typing_enabled: bool = True
    personification_humanize_typing_cps: float = 7.0
    personification_humanize_typing_max_delay: float = 5.0
    # 碎片化输出：off=不干预；prompt=提示词引导拆成 1-3 条群聊短消息
    personification_humanize_fragment_style: str = "prompt"
    # 跨楼回复时带引用（OneBot v11 标准 reply 段，全端可用）
    personification_humanize_quote_reply_enabled: bool = True
    personification_humanize_quote_reply_min_gap: int = 4
    # NO_REPLY 时按概率贴表情代替沉默（NapCat/LLOneBot/Lagrange 扩展 API）
    personification_humanize_reaction_enabled: bool = True
    personification_humanize_reaction_probability: float = 0.25
    personification_humanize_reaction_daily_limit: int = 20
    # 闲聊短句低概率错别字+跟发修正；0=关闭，建议 0.03
    personification_humanize_typo_probability: float = 0.0
    # 被拍后拍回去的概率；主动拍一拍默认关闭
    personification_humanize_poke_back_probability: float = 0.3
    personification_humanize_proactive_poke_enabled: bool = False
    # 多人混战时 @ 回复对象（与引用互斥）
    personification_humanize_at_enabled: bool = True
    # 私聊回复前显示"正在输入"（仅 NapCat 系支持）
    personification_humanize_input_status_enabled: bool = True

    # WebUI 新设备登录需已批准设备确认（首个设备自动批准，防锁死）
    personification_webui_require_device_approval: bool = True
    # 登录页是否公开展示可登录管理员 QQ；公网暴露 WebUI 时建议保持关闭，改为手动输入 QQ
    personification_webui_expose_admin_list: bool = False
    personification_webui_log_retention_days: int = 7
    personification_webui_log_max_entries: int = 10000
    personification_webui_log_capture_level: str = "INFO"
    personification_turn_trace_enabled: bool = True
    # 功能体检"实际交互测试"的目标：测试群号 / 测试私聊用户 QQ（任填其一即可）
    personification_webui_test_group_id: str = ""
    personification_webui_test_user_id: str = ""

    personification_blacklist_duration: int = 300

    # 60s API 配置
    personification_60s_api_base: str = "https://60s.viki.moe"
    personification_60s_local_api_base: str = "http://127.0.0.1:4399"
    personification_60s_enabled: bool = True

    # git 自动更新配置
    personification_git_auto_update: bool = False
    personification_git_auto_update_interval: int = 60

    # OpenAI Codex OAuth 配置
    # personification_api_type = "openai_codex" 时生效
    # 留空则自动按优先级查找 ~/.codex/auth.json
    personification_codex_auth_path: str = ""

    # gemini-cli OAuth 配置
    # personification_api_type = "gemini_cli" 时生效
    # 留空则按 ~/.gemini/oauth_creds.json、$GEMINI_HOME 等顺序查找
    personification_gemini_cli_auth_path: str = ""
    # cloudaicompanionProject；留空时通过 v1internal:loadCodeAssist 自动解析并缓存
    personification_gemini_cli_project: str = ""

    # Antigravity CLI OAuth 配置
    # personification_api_type = "antigravity_cli" 时生效
    # 留空则按 ~/.gemini/antigravity-cli、$ANTIGRAVITY_CLI_HOME 等顺序查找；
    # 若仍未找到，会兼容回退到 gemini-cli OAuth 凭证。
    personification_antigravity_cli_auth_path: str = ""
    # Antigravity/Gemini companion project；留空时先自动解析，再兼容 gemini-cli/gcloud 配置。
    personification_antigravity_cli_project: str = ""

    # claude-code OAuth 配置
    # personification_api_type = "claude_code" 时生效
    # 留空则按 ~/.claude/.credentials.json、$CLAUDE_CONFIG_DIR 等顺序查找
    personification_claude_code_auth_path: str = ""

    def model_post_init(self, __context: Any) -> None:
        fields_set = getattr(self, "__pydantic_fields_set__", set())
        if (
            "personification_model_builtin_search_enabled" not in fields_set
            and "personification_builtin_search" in fields_set
        ):
            self.personification_model_builtin_search_enabled = bool(self.personification_builtin_search)
        elif "personification_model_builtin_search_enabled" in fields_set and "personification_builtin_search" not in fields_set:
            self.personification_builtin_search = bool(self.personification_model_builtin_search_enabled)
        if (
            "personification_tool_web_search_enabled" not in fields_set
            and "personification_web_search" in fields_set
        ):
            self.personification_tool_web_search_enabled = bool(self.personification_web_search)
        elif "personification_tool_web_search_enabled" in fields_set and "personification_web_search" not in fields_set:
            self.personification_web_search = bool(self.personification_tool_web_search_enabled)
        if "qzone_cookie" in fields_set:
            if "personification_qzone_cookie" not in fields_set:
                self.personification_qzone_cookie = str(self.qzone_cookie or "")
            warnings.warn(
                "qzone_cookie 已废弃，请改用 personification_qzone_cookie",
                DeprecationWarning,
                stacklevel=2,
            )
        if "personification_web_search" in fields_set:
            warnings.warn(
                "personification_web_search 已废弃，请改用 skill 配置控制联网搜索",
                DeprecationWarning,
                stacklevel=2,
            )
