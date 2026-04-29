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

    personification_global_enabled: bool = True
    personification_tts_global_enabled: bool = True

    personification_agent_enabled: bool = True
    personification_agent_max_steps: int = 10
    personification_response_timeout: int = 180
    personification_image_input_mode: str = "auto"
    personification_image_detail: str = "auto"
    personification_builtin_search: bool = True
    personification_model_builtin_search_enabled: bool = False
    personification_tool_web_search_enabled: bool = True
    personification_tool_web_search_mode: str = "enabled"
    personification_thinking_mode: str = "none"
    personification_state_thinking_mode: str = "adaptive"
    personification_model_overrides: Dict[str, str] = {}
    personification_data_dir: str = ""
    personification_persona_enabled: bool = True
    personification_persona_history_max: int = DEFAULT_PERSONA_HISTORY_MAX
    personification_persona_data_path: Optional[str] = None
    personification_persona_snippet_max_chars: int = 150
    personification_persona_prompt_max_chars: int = 120
    personification_memory_enabled: bool = True
    personification_memory_palace_enabled: bool = False
    personification_memory_decay_enabled: bool = True
    personification_memory_consolidation_enabled: bool = True
    personification_memory_recall_top_k: int = DEFAULT_MEMORY_RECALL_TOP_K
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
    personification_weather_api: str = "wttr"
    personification_labeler_enabled: bool = True
    personification_labeler_api_type: str = "openai"
    personification_labeler_api_url: str = ""
    personification_labeler_api_key: str = ""
    personification_labeler_model: str = "gemini-2.0-flash"
    personification_labeler_concurrency: int = 3
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
    personification_image_gen_enabled: bool = True
    personification_image_gen_model: str = "gpt-image-2"
    personification_image_gen_background_enabled: bool = True
    personification_image_gen_timeout: int = 180
    personification_parallel_research_enabled: bool = True
    personification_parallel_research_lookup_enabled: bool = True
    personification_parallel_research_max_workers: int = 6
    personification_parallel_research_worker_timeout: int = 35
    personification_parallel_research_total_timeout: int = 90
    personification_parallel_research_max_tool_rounds: int = 2
    personification_qzone_enabled: bool = False
    personification_qzone_cookie: str = ""
    # DEPRECATED: use personification_qzone_cookie.
    qzone_cookie: str = ""
    personification_qzone_proactive_enabled: bool = False
    personification_qzone_check_interval: int = 180
    personification_qzone_daily_limit: int = 2
    personification_qzone_probability: float = 0.35
    personification_qzone_min_interval_hours: float = 8.0
    personification_image_search_api_key: str = ""
    personification_github_token: str = ""
    personification_web_search_always: bool = False
    personification_state_model: str = ""
    personification_wiki_enabled: bool = True
    personification_wiki_fandom_enabled: bool = True
    personification_fandom_wikis: Optional[Union[str, Dict[str, str]]] = None

    personification_api_pools: Optional[Union[str, List[Dict[str, Any]]]] = None
    personification_api_type: str = "openai"
    personification_api_url: str = ""
    personification_api_key: str = ""
    personification_model: str = "gpt-4o-mini"
    # 轻量任务（intent 分类、回复 review、随机插话判定、图片分类）使用的模型名。
    # 留空时 fallback 到主模型，无需额外配置。
    # 建议值：与主模型同 provider 的 mini 版本（如 gpt-4.1-mini / gpt-5.4-mini）。
    personification_lite_model: str = ""
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
    personification_tts_llm_decision_enabled: bool = True
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
    personification_prompt_path: Optional[str] = None
    personification_system_path: Optional[str] = None

    personification_favorability_attitudes: Dict[str, str] = DEFAULT_FAVORABILITY_ATTITUDES.copy()

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

    personification_poke_probability: float = 0.35
    # DEPRECATED: replaced by the agent web_search skill configuration.
    personification_web_search: bool = True
    personification_schedule_global: bool = False

    personification_proactive_enabled: bool = False
    personification_proactive_threshold: float = 60.0
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

    personification_blacklist_duration: int = 300

    # 60s API 配置
    personification_60s_api_base: str = "https://60s.viki.moe"
    personification_60s_local_api_base: str = "http://127.0.0.1:4399"
    personification_60s_enabled: bool = True

    # OpenAI Codex OAuth 配置
    # personification_api_type = "openai_codex" 时生效
    # 留空则自动按优先级查找 ~/.codex/auth.json
    personification_codex_auth_path: str = ""

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
