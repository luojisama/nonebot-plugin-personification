from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel


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
    personification_probability: float = 0.5

    personification_agent_enabled: bool = True
    personification_agent_max_steps: int = 5
    personification_builtin_search: bool = True
    personification_thinking_mode: str = "none"
    personification_state_thinking_mode: str = "adaptive"
    personification_data_dir: str = ""
    personification_persona_enabled: bool = True
    personification_persona_history_max: int = 30
    personification_persona_data_path: Optional[str] = None
    personification_persona_snippet_max_chars: int = 150
    personification_max_output_chars: int = 0
    personification_max_segment_chars: int = 0
    personification_skills_path: Optional[str] = None
    personification_timezone: str = "Asia/Shanghai"
    personification_sticker_semantic: bool = True
    personification_weather_api: str = "wttr"
    personification_labeler_enabled: bool = True
    personification_labeler_api_type: str = "openai"
    personification_labeler_api_url: str = ""
    personification_labeler_api_key: str = ""
    personification_labeler_model: str = "gemini-2.0-flash"
    personification_labeler_concurrency: int = 3
    personification_qzone_cookie: str = ""
    qzone_cookie: str = ""
    personification_image_search_api_key: str = ""
    personification_state_model: str = ""

    personification_api_pools: Optional[Union[str, List[Dict[str, Any]]]] = None
    personification_api_type: str = "openai"
    personification_api_url: str = "https://api.openai.com/v1"
    personification_api_key: str = ""
    personification_model: str = "gpt-4o-mini"
    personification_persona_api_type: str = ""
    personification_persona_api_url: str = ""
    personification_persona_api_key: str = ""
    personification_persona_model: str = ""
    personification_style_api_type: str = ""
    personification_style_api_url: str = ""
    personification_style_api_key: str = ""
    personification_style_api_model: str = ""

    personification_thinking_budget: int = 0
    personification_include_thoughts: bool = True

    personification_system_prompt: str = (
        "你是一个群聊成员，性格活泼，说话幽默。"
        "你可以根据当前语境决定是否回复，如果不回复请只输出 [NO_REPLY]。"
    )
    personification_prompt_path: Optional[str] = None
    personification_system_path: Optional[str] = None

    personification_favorability_attitudes: Dict[str, str] = DEFAULT_FAVORABILITY_ATTITUDES.copy()

    personification_history_len: int = 200
    personification_compress_threshold: int = 100
    personification_compress_keep_recent: int = 20
    personification_compress_api_type: str = ""
    personification_compress_api_url: str = ""
    personification_compress_api_key: str = ""
    personification_compress_model: str = ""

    personification_sticker_path: Optional[str] = "data/stickers"
    personification_sticker_probability: float = 0.2

    personification_poke_probability: float = 0.3
    personification_web_search: bool = True
    personification_schedule_global: bool = False

    personification_proactive_enabled: bool = True
    personification_proactive_threshold: float = 60.0
    personification_proactive_daily_limit: int = 3
    personification_proactive_interval: int = 30
    personification_proactive_probability: float = 0.5
    personification_proactive_idle_hours: float = 24.0
    personification_group_idle_minutes: int = 60
    personification_group_idle_check_interval: int = 15
    personification_group_idle_daily_limit: int = 3
    personification_group_quiet_hour_start: int = 0
    personification_group_quiet_hour_end: int = 7
    personification_group_summary_enabled: bool = True
    personification_friend_request_enabled: bool = False
    personification_friend_request_min_fav: float = 85.0
    personification_friend_request_daily_limit: int = 2

    personification_hot_chat_min_pass_rate: float = 0.2

    personification_blacklist_duration: int = 300

    personification_60s_api_base: str = "https://60s.viki.moe"
    personification_60s_local_api_base: str = "http://127.0.0.1:4399"
    personification_60s_enabled: bool = True

    personification_codex_auth_path: str = ""
