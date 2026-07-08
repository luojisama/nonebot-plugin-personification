from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable

from .model_router import MODEL_OVERRIDE_ROLES, normalize_model_overrides, resolve_model_role

from .memory_defaults import (
    DEFAULT_COMPRESS_KEEP_RECENT,
    DEFAULT_COMPRESS_THRESHOLD,
    DEFAULT_GROUP_CONTEXT_EXPIRE_HOURS,
    DEFAULT_GROUP_SUMMARY_EXPIRE_HOURS,
    DEFAULT_HISTORY_LEN,
    DEFAULT_MEMORY_RECALL_TOP_K,
    DEFAULT_MESSAGE_EXPIRE_HOURS,
    DEFAULT_PERSONA_HISTORY_MAX,
    DEFAULT_PRIVATE_HISTORY_TURNS,
    MAX_MEMORY_RECALL_TOP_K,
    MAX_PRIVATE_HISTORY_TURNS,
)


GLOBAL_SCOPE = "global"
GROUP_SCOPE = "group"


def _bool_parser(raw: str) -> bool:
    text = str(raw or "").strip().lower()
    mapping = {
        "on": True,
        "off": False,
        "true": True,
        "false": False,
        "1": True,
        "0": False,
        "yes": True,
        "no": False,
        "开": True,
        "关": False,
        "开启": True,
        "关闭": False,
        "启用": True,
        "禁用": False,
    }
    if text not in mapping:
        raise ValueError("布尔值仅支持 on/off、开/关、true/false、1/0")
    return mapping[text]


def _int_parser(raw: str) -> int:
    try:
        return int(str(raw or "").strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("需要整数值") from exc


def _float_parser(raw: str) -> float:
    try:
        return float(str(raw or "").strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("需要数字值") from exc


def _str_parser(raw: str) -> str:
    return str(raw or "").strip()


def _json_object_parser(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except Exception as exc:
        raise ValueError("需要 JSON 对象") from exc
    if not isinstance(parsed, dict):
        raise ValueError("需要 JSON 对象")
    return parsed


def _json_array_parser(raw: str) -> list[Any]:
    text = str(raw or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except Exception as exc:
        raise ValueError("需要 JSON 数组") from exc
    if not isinstance(parsed, list):
        raise ValueError("需要 JSON 数组")
    return parsed


def _web_search_mode_parser(raw: str) -> str:
    text = str(raw or "").strip().lower()
    mapping = {
        "enabled": "enabled",
        "开启": "enabled",
        "启用": "enabled",
        "默认": "enabled",
        "live": "live",
        "实时": "live",
        "即时": "live",
        "cached": "cached",
        "缓存": "cached",
        "disabled": "disabled",
        "关闭": "disabled",
        "禁用": "disabled",
    }
    return mapping.get(text, text)


def _tts_mode_parser(raw: str) -> str:
    text = str(raw or "").strip().lower()
    mapping = {
        "preset": "preset",
        "builtin": "preset",
        "built_in": "preset",
        "预置": "preset",
        "内置": "preset",
        "design": "design",
        "voice_design": "design",
        "voicedesign": "design",
        "描述": "design",
        "定制": "design",
        "设计": "design",
        "clone": "clone",
        "voice_clone": "clone",
        "voiceclone": "clone",
        "克隆": "clone",
        "复刻": "clone",
    }
    return mapping.get(text, text)


def _qzone_social_scope_parser(raw: str) -> str:
    text = str(raw or "").strip().lower()
    mapping = {
        "recent": "recent_interactions",
        "recent_interactions": "recent_interactions",
        "最近互动": "recent_interactions",
        "近期互动": "recent_interactions",
    }
    return mapping.get(text, text)


def _model_role_parser(raw: str) -> str:
    role = resolve_model_role(raw)
    if not role or role not in MODEL_OVERRIDE_ROLES:
        raise ValueError("可选值: intent, review, agent, sticker")
    return role


def _embedding_provider_parser(raw: str) -> str:
    text = str(raw or "").strip().lower().replace("-", "_")
    mapping = {
        "hash": "hash_bow",
        "hashbow": "hash_bow",
        "hash_bow": "hash_bow",
        "gemini": "gemini",
        "openai": "openai",
    }
    return mapping.get(text, text)


def _memory_vector_backend_parser(raw: str) -> str:
    text = str(raw or "").strip().lower().replace("-", "_")
    mapping = {
        "sqlite": "sqlite_exact",
        "local": "sqlite_exact",
        "sqlite_exact": "sqlite_exact",
        "hash": "sqlite_exact",
        "off": "disabled",
        "none": "disabled",
        "disabled": "disabled",
    }
    return mapping.get(text, text)


@dataclass(frozen=True)
class ConfigEntry:
    key: str
    field_name: str
    display_name: str
    value_type: str
    default: Any
    scope: str
    description: str
    category: str
    admin_only: bool = True
    hot_reloadable: bool = True
    choices: tuple[str, ...] = ()
    min_value: float | None = None
    max_value: float | None = None
    help_aliases: tuple[str, ...] = ()
    risk_note: str = ""
    parser: Callable[[str], Any] | None = None
    required: bool = False
    kind: str = ""
    group: str = "其他"
    secret: bool = False
    advanced: bool = False
    example: str = ""

    def normalize_value(self, raw: Any) -> Any:
        if isinstance(raw, bool) and self.value_type == "bool":
            value = raw
        elif isinstance(raw, (list, dict)):
            # 前端可能直接传来已解析的 JSON 数组/对象（如 API Provider 池编辑器
            # 提交的 provider 列表）。此时 str(raw) 会得到 Python repr（单引号、
            # True/False），交给 json.loads 必然失败，导致保存被拒、配置"回退"。
            # 用 json.dumps 转回合法 JSON 再交给 parser 校验归一。
            parser = self.parser or _str_parser
            value = parser(json.dumps(raw, ensure_ascii=False))
        else:
            parser = self.parser or _str_parser
            value = parser(str(raw or ""))
        if self.choices:
            normalized = str(value).strip().lower()
            allowed = {choice.lower(): choice for choice in self.choices}
            if normalized not in allowed:
                raise ValueError(f"可选值: {', '.join(self.choices)}")
            value = allowed[normalized]
        if self.value_type == "int":
            number = int(value)
            if self.min_value is not None and number < self.min_value:
                raise ValueError(f"不能小于 {self.min_value}")
            if self.max_value is not None and number > self.max_value:
                raise ValueError(f"不能大于 {self.max_value}")
            return number
        if self.value_type == "float":
            number = float(value)
            if self.min_value is not None and number < self.min_value:
                raise ValueError(f"不能小于 {self.min_value}")
            if self.max_value is not None and number > self.max_value:
                raise ValueError(f"不能大于 {self.max_value}")
            return number
        return value


_GROUP_RULES: tuple[tuple[Callable[[str], bool], str], ...] = (
    (lambda k: k in {"global_enabled", "image_host_allowlist", "probability", "poke_probability"}, "核心开关"),
    (lambda k: k.startswith("response_review_"), "回复审阅"),
    (lambda k: k.startswith("turn_planner_") or k == "evidence_synthesizer_enabled", "意图规划"),
    (
        lambda k: k.startswith("parallel_research_")
        or k.startswith("web_search_")
        or k in {
            "deep_research_v2_enabled",
            "tool_web_search_enabled",
            "tool_web_search_mode",
            "model_builtin_search_enabled",
            "free_search_engines",
            "searxng_instances",
            "web_proxy",
        },
        "联网搜索",
    ),
    (lambda k: k.startswith("fallback_"), "模型回退"),
    (lambda k: k.startswith("video_"), "视频理解"),
    (lambda k: k.startswith("image_gen_"), "图像生成"),
    (lambda k: k.startswith("tts_"), "TTS 语音"),
    (lambda k: k.startswith("qzone_"), "QQ 空间"),
    (
        lambda k: k.startswith("memory_")
        or k.startswith("embedding_")
        or k in {"real_embedding_enabled", "embedding_provider", "agent_memory_write_enabled"},
        "记忆",
    ),
    (
        lambda k: k in {"persona_history_max", "private_history_turns", "persona_enabled"},
        "画像",
    ),
    (
        lambda k: k.startswith("sticker_") or k in {"labeler_enabled", "labeler_concurrency"},
        "表情包",
    ),
    (
        lambda k: k in {"agent_enabled", "thinking_mode", "state_thinking_mode", "builtin_search"},
        "Agent",
    ),
    (
        lambda k: k in {
            "history_len",
            "compress_threshold",
            "compress_keep_recent",
            "message_expire_hours",
            "group_context_expire_hours",
            "group_summary_expire_hours",
        },
        "上下文压缩",
    ),
    (lambda k: k.startswith("background_"), "后台任务"),
    (lambda k: k.startswith("social_intelligence_") or k.startswith("social_"), "后台任务"),
    (lambda k: k.startswith("wiki"), "百科工具"),
    (lambda k: k.startswith("tool_"), "工具"),
    (lambda k: k == "plugin_knowledge_build_enabled" or k.startswith("group_knowledge") or k.startswith("group_style"), "知识库"),
    (lambda k: k.startswith("proactive_"), "主动私聊"),
    (lambda k: k in {"schedule_global", "group_schedule_enabled"}, "作息"),
    (lambda k: k.startswith("group_"), "群作用域"),
    (
        lambda k: k in {"api_pools", "model_overrides", "lite_model", "antigravity_cli_proxy"}
        or k.startswith("quota_")
        or k.startswith("provider_"),
        "模型路由",
    ),
    (lambda k: k in {"agent_max_steps", "response_timeout"}, "Agent"),
)

_REQUIRED_KEYS: frozenset[str] = frozenset({"global_enabled", "api_pools"})

_SECRET_FIELD_HINTS: tuple[str, ...] = ("_api_key", "_token", "_secret", "auth_path", "cookie")

# value_type=="list" 但元素是对象 / 结构化项的字段，保留 JSON 编辑器
_OBJECT_LIST_KEYS: frozenset[str] = frozenset({
    "api_pools", "skill_sources",
})


def _infer_group(key: str) -> str:
    for predicate, name in _GROUP_RULES:
        if predicate(key):
            return name
    return "其他"


def _infer_secret(field_name: str) -> bool:
    name = str(field_name or "").lower()
    return any(hint in name for hint in _SECRET_FIELD_HINTS)


def _infer_kind(entry: ConfigEntry, *, secret: bool) -> str:
    if entry.value_type == "bool":
        return "toggle"
    if secret:
        return "secret"
    name = str(entry.field_name or "").lower()
    if "_path" in name or name.endswith("_dir") or name.endswith("_root"):
        return "path"
    if entry.choices:
        return "select"
    if entry.value_type == "list":
        # 对象数组（provider 池 / 远程 skill 源 / 社交场景配置）保留 JSON 编辑；
        # 简单字符串数组用更友好的「逐项增删」编辑器。
        if entry.key in _OBJECT_LIST_KEYS:
            return "json"
        return "strlist"
    if entry.value_type == "dict":
        return "json"
    if entry.value_type == "int":
        return "int"
    if entry.value_type == "float":
        return "float"
    return "text"


# 关键字命中即视为"高级/调优"字段；前端默认折叠，需勾选「显示高级配置」才出现。
# 普通用户日常关心的开关、模型、人设、表情包、画像保留为基础配置。
# 重要配置项的"详细描述 + 示例"覆盖字典；以 ConfigEntry.key 为键。
# 用于在 WebUI 中给基础字段提供新手友好的解释；未列入这里的字段沿用 entry 原 description。
# 模板：「作用 + 取值范围 + 默认含义 + 实际影响」串成 1-2 句。
_DETAILED_DESCRIPTIONS: dict[str, tuple[str, str]] = {
    "global_enabled": (
        "拟人插件的总开关。关闭后 bot 不再自动回复群消息（管理员指令仍可用）。",
        "true",
    ),
    "api_pools": (
        "多模型 API 池配置，按 priority 顺序尝试；某池连续失败会自动冷却 10 分钟。"
        "每项包含 name/api_type/model/api_key/api_url/priority/enabled 等字段。",
        '[{"name":"main","api_type":"openai","api_url":"...","api_key":"sk-...","model":"gpt-4o-mini","priority":1,"enabled":true}]',
    ),
    "agent_enabled": (
        "Agent 工具调用模式总开关。关闭后回退到旧版「单次 LLM 调用」，无法用搜索/图像生成/记忆等工具。",
        "true",
    ),
    "agent_max_steps": (
        "Agent 一轮对话最多调用工具的步数（ReAct 循环深度）。超过则强制输出。"
        "调大允许更复杂任务但成本上升；建议 3-8。",
        "5",
    ),
    "probability": (
        "群消息进入 Agent 处理的概率（0~1）。被 @ / 提及名字 / 戳一戳 时无视此概率必触发。"
        "数值越高 bot 越主动接话，token 消耗越多。",
        "0.35",
    ),
    "poke_probability": (
        "收到 QQ 戳一戳事件时回复的概率。设 1 即每次必回，设 0 则不回。",
        "0.5",
    ),
    "thinking_mode": (
        "主对话推理档位。none=零额外开销适合群聊高频；adaptive=根据复杂度切换；low/high=固定推理预算。"
        "高级用户调；普通群聊保持 none。",
        "none",
    ),
    "state_thinking_mode": (
        "inner_state（情绪/能量/好感度）更新的独立推理档位，不影响主对话。"
        "adaptive 在多数场景下成本/质量平衡较好。",
        "adaptive",
    ),
    "response_review_enabled": (
        "在 bot 即将发送回复前用 review 模型再做一次审阅与改写。"
        "可减少明显错误但每次回复多花一次 LLM 调用。",
        "false",
    ),
    "builtin_search": (
        "启用模型原生的内置搜索能力：Gemini 的 google_search、Anthropic 的 web_search_20250305。"
        "比工具调用更省 token，但需对应 provider 支持。",
        "true",
    ),
    "proactive_enabled": (
        "主动私聊总开关。开启后 bot 会定期检查 inner_state 与好感度，自主决定是否给某用户发起私聊。",
        "true",
    ),
    "proactive_threshold": (
        "触发主动私聊所需的最低好感度（0~100）。低于此值的用户不会被主动联系。",
        "50",
    ),
    "proactive_daily_limit": (
        "每个用户每天最多被主动私聊的次数上限，防止打扰。",
        "5",
    ),
    "proactive_interval": (
        "两次主动消息之间的最短间隔（分钟）；调度器最小粒度 5 分钟。",
        "10",
    ),
    "proactive_idle_hours": (
        "用户超过多少小时未互动才纳入主动联系候选（避免打扰活跃用户）。",
        "24.0",
    ),
    "memory_enabled": (
        "长期记忆系统总开关。关闭后 bot 不再持久化对话上下文/记忆条目。",
        "true",
    ),
    "memory_palace_enabled": (
        "高级长期记忆：用向量化 + 衰减机制存储 fact/persona/事件/群知识等条目。"
        "WebUI「Agent 记忆」需要此项为 true 才能看到数据。",
        "false",
    ),
    "persona_enabled": (
        "用户画像功能总开关。开启后 bot 会按消息累计自动生成/更新画像，并注入 system prompt。",
        "true",
    ),
    "persona_history_max": (
        "累积多少条消息后触发一次画像生成/更新。调大画像更稳定但更新延迟；调小更敏感但 LLM 调用多。",
        "100",
    ),
    "sticker_path": (
        "表情包根目录（绝对路径或相对项目根）。下面平铺 jpg/png/gif，stickers.json 存元数据。",
        "/bot/shizuku/qqgif",
    ),
    "sticker_probability": (
        "Agent 模式下退回的概率发送（仅在语义选择关闭时生效）。",
        "0.1",
    ),
    "sticker_semantic": (
        "启用基于 stickers.json 标签的语义选择（推荐）。关则回退到纯概率发送。",
        "true",
    ),
    "tts_global_enabled": (
        "语音合成总开关。开启后 bot 在合适场景会用 TTS 生成语音消息。",
        "true",
    ),
    "tts_mode": (
        "TTS 工作模式：preset 用内置音色；design 让模型按文字描述生成音色；clone 用 voice_clone_path 指定的音频复刻。",
        "clone",
    ),
    "tts_model": (
        "TTS 模型名称，需与 personification_tts_api_key 的服务对齐。",
        "mimo-v2.5-tts-voiceclone",
    ),
    "qzone_social_enabled": (
        "QQ 空间互动总开关。开启后 bot 会扫好友空间动态，根据好感度自动点赞/评论。",
        "true",
    ),
    "wiki_enabled": (
        "Wikipedia 查询工具开关。开启后 agent 可调 wiki_search 工具检索。",
        "true",
    ),
    "fallback_enabled": (
        "API 池全部失败时启用单独的 fallback 模型救援；需配 fallback_api_type/key/model 等。",
        "false",
    ),
    "group_knowledge_autobuild_enabled": (
        "群知识库定时扫描开关。开启后会按 interval_hours 周期扫描各群消息，LLM 抽取常用词/绰号/内部梗写入。",
        "true",
    ),
    "image_host_allowlist": (
        "可信图片域名后缀白名单（JSON 数组）；命中时跳过 SSRF 内网 IP 检查。"
        "腾讯系 .qq.com/.qpic.cn 已内置。若你的 QQ 协议端用其他域名（如 Lagrange）需在此追加。",
        '[".lagrange.app"]',
    ),
}


_ADVANCED_FIELD_PATTERNS: tuple[str, ...] = (
    "_concurrency",
    "_timeout",
    "_debounce",
    "_cooldown",
    "_workers",
    "_max_steps",
    "_max_tokens",
    "_max_tool_rounds",
    "_max_feeds",
    "_max_comments",
    "_min_interval",
    "_min_messages",
    "_ratio",
    "_threshold",
    "_decay",
    "_keep_recent",
    "_compress_",
    "_history_len",
    "_expire_hours",
    "_check_interval",
    "_response_timeout",
    "thinking_mode",
    "_shadow_enabled",
    "_builtin_safety",
    "_fallback_auth",
    "_fallback_provider",
    "_supports_native_search",
    "_persona_history_max",
    "_recall_top_k",
    "_snippet_max_chars",
    "_prompt_max_chars",
)


def _infer_advanced(field_name: str) -> bool:
    name = str(field_name or "").lower()
    for pattern in _ADVANCED_FIELD_PATTERNS:
        if pattern in name:
            return True
    return False


def _enrich_entry(entry: ConfigEntry) -> ConfigEntry:
    secret = entry.secret or _infer_secret(entry.field_name)
    detailed = _DETAILED_DESCRIPTIONS.get(entry.key)
    description = entry.description
    example = entry.example
    if detailed:
        if detailed[0]:
            description = detailed[0]
        if detailed[1] and not example:
            example = detailed[1]
    return replace(
        entry,
        group=entry.group if entry.group and entry.group != "其他" else _infer_group(entry.key),
        secret=secret,
        kind=entry.kind or _infer_kind(entry, secret=secret),
        required=entry.required or (entry.key in _REQUIRED_KEYS),
        advanced=entry.advanced or _infer_advanced(entry.field_name),
        description=description,
        example=example,
    )


def _build_entries() -> list[ConfigEntry]:
    entries = [
        ConfigEntry(
            key="model_overrides",
            field_name="personification_model_overrides",
            display_name="模型覆盖",
            value_type="dict",
            default={},
            scope=GLOBAL_SCOPE,
            description="按调用阶段覆盖模型，支持 intent/review/agent/sticker。推荐用“拟人 模型”命令热更新。",
            category="config",
            help_aliases=("模型覆盖", "model_overrides", "模型路由"),
            parser=lambda raw: normalize_model_overrides(_json_object_parser(raw)),
        ),
        ConfigEntry(
            key="api_pools",
            field_name="personification_api_pools",
            display_name="API Provider 池",
            value_type="list",
            default=None,
            scope=GLOBAL_SCOPE,
            description="主回复模型 provider 池。推荐用“拟人 模型 路由”命令热切换；环境变量显式设置时重启后以环境变量为准。",
            category="config",
            help_aliases=("provider池", "api_pools", "api_pool", "模型路由池", "provider_route"),
            parser=_json_array_parser,
        ),
        ConfigEntry(
            key="response_review_enabled",
            field_name="personification_response_review_enabled",
            display_name="回复 LLM 审阅",
            value_type="bool",
            default=False,
            scope=GLOBAL_SCOPE,
            description="是否在最终回复发送前调用 LLM 审阅/改写；默认关闭以减少延迟和额外 token。",
            category="config",
            help_aliases=("回复审阅", "llm审查", "response_review", "回复审核"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="response_review_model_role",
            field_name="personification_response_review_model_role",
            display_name="回复审阅模型角色",
            value_type="str",
            default="review",
            scope=GLOBAL_SCOPE,
            description="回复 LLM 审阅使用的模型角色；agent=复用主模型，review/intent/sticker 使用对应四类模型覆盖。",
            category="config",
            choices=("intent", "review", "agent", "sticker"),
            help_aliases=("回复审阅模型", "审查模型", "review_model_role", "response_review_role"),
            parser=_model_role_parser,
        ),
        ConfigEntry(
            key="turn_planner_enabled",
            field_name="personification_turn_planner_enabled",
            display_name="TurnPlan 接管",
            value_type="bool",
            default=False,
            scope=GLOBAL_SCOPE,
            description="启用新的 TurnPlan 语义规划器接管主回复路径；默认关闭，建议先开启 shadow 观测。",
            category="config",
            help_aliases=("turn_planner", "TurnPlan", "回合规划", "语义规划器"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="turn_planner_shadow_enabled",
            field_name="personification_turn_planner_shadow_enabled",
            display_name="TurnPlan 影子观测",
            value_type="bool",
            default=False,
            scope=GLOBAL_SCOPE,
            description="并行运行新的 TurnPlan 语义规划器并记录差异，不影响实际回复决策。",
            category="config",
            help_aliases=("turn_planner_shadow", "TurnPlan影子", "规划器观测"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="semantic_frame_timeout",
            field_name="personification_semantic_frame_timeout",
            display_name="语义帧超时（秒）",
            value_type="float",
            default=8.0,
            scope=GLOBAL_SCOPE,
            description="前置语义帧 / TurnPlan 判断阶段的总耗时预算；lite/intent 路径慢或无效时会在预算内尝试主 Agent caller 重判，最后才 metadata fallback。",
            category="config",
            min_value=1,
            max_value=60,
            help_aliases=("语义帧超时", "semantic_frame_timeout", "慢回复", "TurnPlan超时"),
            parser=_float_parser,
            group="意图规划",
        ),
        ConfigEntry(
            key="evidence_synthesizer_enabled",
            field_name="personification_evidence_synthesizer_enabled",
            display_name="证据综合器",
            value_type="bool",
            default=False,
            scope=GLOBAL_SCOPE,
            description="启用 EvidenceSynthesizer 综合工具结果与候选记忆，默认关闭，建议在 TurnPlan 灰度稳定后开启。",
            category="config",
            help_aliases=("evidence_synthesizer", "证据综合", "证据合流", "EvidenceSynthesizer"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="model_builtin_search_enabled",
            field_name="personification_model_builtin_search_enabled",
            display_name="模型内置搜索",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description=(
                "允许主模型直接使用 provider 原生 builtin search（Gemini google_search、"
                "Anthropic web_search_20250305、OpenAI web_search_options），无需任何 API key。"
                "仅当 caller 支持时生效；不支持的 provider 自动回落到外部 web_search 工具。"
            ),
            category="config",
            help_aliases=("builtin_search", "内置搜索", "模型搜索"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="tool_web_search_enabled",
            field_name="personification_tool_web_search_enabled",
            display_name="工具联网搜索",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description="允许工具层执行联网搜索。",
            category="config",
            help_aliases=("web_search_enabled", "联网搜索", "工具联网"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="tool_web_search_mode",
            field_name="personification_tool_web_search_mode",
            display_name="联网模式",
            value_type="str",
            default="enabled",
            scope=GLOBAL_SCOPE,
            description="工具联网模式。",
            category="config",
            choices=("enabled", "live", "cached", "disabled"),
            help_aliases=("web_search_mode", "搜索模式", "联网方式"),
            parser=_web_search_mode_parser,
        ),
        ConfigEntry(
            key="free_search_engines",
            field_name="personification_free_search_engines",
            display_name="免配置搜索引擎链",
            value_type="list",
            default=["wikipedia", "searxng", "duckduckgo"],
            scope=GLOBAL_SCOPE,
            description=(
                "外部 web_search 兜底用的免 key 搜索引擎，按顺序并行调用并合并去重。"
                "可选：wikipedia / searxng / duckduckgo。"
            ),
            category="config",
            help_aliases=("免key搜索", "免配置搜索", "search_engines"),
            parser=_json_array_parser,
        ),
        ConfigEntry(
            key="searxng_instances",
            field_name="personification_searxng_instances",
            display_name="SearXNG 实例池",
            value_type="list",
            default=[],
            scope=GLOBAL_SCOPE,
            description=(
                "SearXNG 公共实例 URL 列表（留空 = 用插件内置默认池）。"
                "运行时会并行 HEAD 探测，选第一个能通的实例发请求。"
            ),
            category="config",
            help_aliases=("searxng", "searx", "实例池"),
            parser=_json_array_parser,
        ),
        ConfigEntry(
            key="web_search_max_results",
            field_name="personification_web_search_max_results",
            display_name="搜索结果上限",
            value_type="int",
            default=6,
            scope=GLOBAL_SCOPE,
            description="web_search 返回给 LLM 的最大结果条数（合并去重后）。",
            category="config",
            help_aliases=("max_results", "搜索条数"),
        ),
        ConfigEntry(
            key="web_search_snippet_chars",
            field_name="personification_web_search_snippet_chars",
            display_name="搜索摘要长度",
            value_type="int",
            default=400,
            scope=GLOBAL_SCOPE,
            description="每条搜索结果的 snippet 字符上限。",
            category="config",
            help_aliases=("snippet_chars", "摘要长度"),
        ),
        ConfigEntry(
            key="tool_web_fetch_enabled",
            field_name="personification_tool_web_fetch_enabled",
            display_name="网页抓取工具",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description="允许 agent 调用 web_fetch 抓取指定 URL 的网页正文。",
            category="config",
            help_aliases=("web_fetch_enabled", "抓取网页", "页面抓取"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="tool_web_fetch_timeout",
            field_name="personification_tool_web_fetch_timeout",
            display_name="网页抓取超时（秒）",
            value_type="int",
            default=60,
            scope=GLOBAL_SCOPE,
            description="web_fetch 工具单次请求整体超时秒数。",
            category="config",
            help_aliases=("web_fetch_timeout", "抓取超时"),
        ),
        ConfigEntry(
            key="web_proxy",
            field_name="personification_web_proxy",
            display_name="联网抓取代理",
            value_type="str",
            default="",
            scope=GLOBAL_SCOPE,
            description=(
                "web_fetch / web_search 走的 HTTP 代理（如 http://127.0.0.1:7890）。"
                "国内服务器抓被 DNS 污染/墙的站点（Cloudflare 前置站、海外 API）时填写，"
                "请求会经代理解析 DNS 并连接，绕开本地污染；非空时还会跳过"
                "“本地解析到内网就拒绝”的判断（仍拦截字面内网 IP）。留空 = 直连。"
            ),
            category="config",
            help_aliases=("web_proxy", "抓取代理", "联网代理", "搜索代理"),
        ),
        ConfigEntry(
            key="provider_dynamic_priority_enabled",
            field_name="personification_provider_dynamic_priority_enabled",
            display_name="Provider 动态优先级",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description=(
                "开启后按真实请求的 latency 与 success_rate 自动调整 provider "
                "排序：高 latency / 高失败率的会自动排到后面；关闭则只用配置的 "
                "priority。冷启动样本不足时仍用 base priority。"
            ),
            category="config",
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="provider_health_min_samples",
            field_name="personification_provider_health_min_samples",
            display_name="动态优先级最小样本数",
            value_type="int",
            default=3,
            scope=GLOBAL_SCOPE,
            description="provider 累积达到 N 次真实请求后才参与动态排序，否则用 base priority。",
            category="config",
        ),
        ConfigEntry(
            key="antigravity_cli_proxy",
            field_name="personification_antigravity_cli_proxy",
            display_name="Antigravity CLI 代理",
            value_type="str",
            default="",
            scope=GLOBAL_SCOPE,
            description=(
                "Antigravity v1internal 与 OAuth refresh 走的 HTTP 代理（如 "
                "http://127.0.0.1:17890）。非空则覆盖 HTTPS_PROXY 环境变量，"
                "确保 bot 子进程一定经过该代理。留空则沿用 httpx 的环境变量解析。"
            ),
            category="config",
            help_aliases=("antigravity_proxy", "agy_proxy", "antigravity 代理"),
        ),
        ConfigEntry(
            key="social_intelligence_enabled",
            field_name="personification_social_intelligence_enabled",
            display_name="主动社交总开关",
            value_type="bool",
            default=False,
            scope=GLOBAL_SCOPE,
            description=(
                "总开关：开启后 bot 会按场景配置主动给用户发问候、新闻、节日"
                "祝福等。默认关闭，确认场景配好后再打开。"
            ),
            category="config",
            help_aliases=("social_intelligence", "主动社交", "社交"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="social_gate_enabled",
            field_name="personification_social_gate_enabled",
            display_name="主动社交 LLM 闸门",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description=(
                "开启则每次主动发送前用 lite 模型二次决策'现在发合不合适'，"
                "避免显得机器人在群发；关闭则只走 quota 与 cooldown。"
            ),
            category="config",
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="social_daily_quota_per_user",
            field_name="personification_social_daily_quota_per_user",
            display_name="每用户每日主动消息上限",
            value_type="int",
            default=2,
            scope=GLOBAL_SCOPE,
            description="所有主动社交场景共享的每用户日额度，避免骚扰。",
            category="config",
        ),
        ConfigEntry(
            key="social_morning_hour",
            field_name="personification_social_morning_hour",
            display_name="主动早安时点（小时）",
            value_type="int",
            default=8,
            scope=GLOBAL_SCOPE,
            description="0-23，早安问候触发的小时。",
            category="config",
        ),
        ConfigEntry(
            key="social_evening_hour",
            field_name="personification_social_evening_hour",
            display_name="主动晚安时点（小时）",
            value_type="int",
            default=22,
            scope=GLOBAL_SCOPE,
            description="0-23，晚安问候触发的小时。",
            category="config",
        ),
        ConfigEntry(
            key="social_news_enabled",
            field_name="personification_social_news_enabled",
            display_name="主动新闻推送",
            value_type="bool",
            default=False,
            scope=GLOBAL_SCOPE,
            description=(
                "开启后每天指定时点向 personification_social_news_users / "
                "_news_groups 列出的目标推送一条自然语气的新闻摘要。"
            ),
            category="config",
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="social_news_hour",
            field_name="personification_social_news_hour",
            display_name="主动新闻推送时点（小时）",
            value_type="int",
            default=9,
            scope=GLOBAL_SCOPE,
            description="0-23，每天哪个时点触发新闻推送。",
            category="config",
        ),
        ConfigEntry(
            key="social_news_source",
            field_name="personification_social_news_source",
            display_name="新闻来源类型",
            value_type="str",
            default="daily",
            scope=GLOBAL_SCOPE,
            description="daily=早报、ai=AI 资讯、history=历史上的今天。",
            category="config",
            choices=("daily", "ai", "history"),
        ),
        ConfigEntry(
            key="social_topic_followup_enabled",
            field_name="personification_social_topic_followup_enabled",
            display_name="话题延续主动跟进",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description=(
                "开启后 bot 会在用户提到'明天去上海''下周考试'等未来事件时，"
                "记录到 pending_topics 表，到承诺时间附近自动发关心话术。"
            ),
            category="config",
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="social_topic_scan_interval_minutes",
            field_name="personification_social_topic_scan_interval_minutes",
            display_name="话题延续扫描间隔（分钟）",
            value_type="int",
            default=60,
            scope=GLOBAL_SCOPE,
            description="多久扫一次 pending_topics 表，最小 15 分钟。",
            category="config",
        ),
        ConfigEntry(
            key="social_festival_enabled",
            field_name="personification_social_festival_enabled",
            display_name="主动节日 / 生日祝福",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description=(
                "公历节日（元旦 / 国庆 / 圣诞等内置）+ 用户生日（从 persona "
                "文本里抽 '生日：MM-DD' 模式）；命中当天自动发祝福。"
            ),
            category="config",
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="social_festival_hour",
            field_name="personification_social_festival_hour",
            display_name="节日祝福触发时点（小时）",
            value_type="int",
            default=9,
            scope=GLOBAL_SCOPE,
            description="0-23，每天检查节日 / 生日的时点。",
            category="config",
        ),
        ConfigEntry(
            key="agent_max_steps",
            field_name="personification_agent_max_steps",
            display_name="Agent 最大步数",
            value_type="int",
            default=10,
            scope=GLOBAL_SCOPE,
            description="单轮 Agent 最多模型/工具循环次数；复杂查证可调高，但会增加耗时。",
            category="config",
            min_value=3,
            max_value=16,
            help_aliases=("agent步数", "工具循环", "agent_max_steps"),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="agent_budget_mode",
            field_name="personification_agent_budget_mode",
            display_name="Agent 预算模式",
            value_type="str",
            default="shadow",
            scope=GLOBAL_SCOPE,
            description=(
                "Agent 预算画像的生效方式。shadow 只记录建议步数/秒数，不改变生产行为；"
                "adaptive 会按 TurnPlan、意图和工具需求缩短闲聊预算、保留查证预算。"
            ),
            category="config",
            choices=("shadow", "adaptive"),
            help_aliases=("agent预算", "预算模式", "agent_budget_mode"),
            parser=_str_parser,
            group="Agent",
            advanced=True,
        ),
        ConfigEntry(
            key="real_embedding_enabled",
            field_name="personification_real_embedding_enabled",
            display_name="真实向量记忆",
            value_type="bool",
            default=False,
            scope=GLOBAL_SCOPE,
            description="允许记忆系统使用真实 embedding provider；关闭时继续使用 hash_bow 兼容路径。",
            category="config",
            help_aliases=("real_embedding", "真实embedding", "向量记忆"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="embedding_provider",
            field_name="personification_embedding_provider",
            display_name="Embedding Provider",
            value_type="str",
            default="hash_bow",
            scope=GLOBAL_SCOPE,
            description="记忆 embedding provider，支持 hash_bow/gemini/openai。",
            category="config",
            choices=("hash_bow", "gemini", "openai"),
            help_aliases=("embedding_provider", "向量模型", "embedding"),
            parser=_embedding_provider_parser,
        ),
        ConfigEntry(
            key="embedding_model",
            field_name="personification_embedding_model",
            display_name="Embedding 模型",
            value_type="str",
            default="",
            scope=GLOBAL_SCOPE,
            description="真实向量记忆使用的 embedding 模型；留空时按 provider 使用默认模型。",
            category="config",
            help_aliases=("embedding_model", "向量模型名", "嵌入模型"),
            parser=_str_parser,
        ),
        ConfigEntry(
            key="embedding_api_url",
            field_name="personification_embedding_api_url",
            display_name="Embedding API URL",
            value_type="str",
            default="",
            scope=GLOBAL_SCOPE,
            description="OpenAI 兼容 embedding 接口 Base URL；留空时使用 SDK 默认地址。",
            category="config",
            help_aliases=("embedding_api_url", "向量接口", "embedding_base_url"),
            parser=_str_parser,
        ),
        ConfigEntry(
            key="embedding_api_key",
            field_name="personification_embedding_api_key",
            display_name="Embedding API Key",
            value_type="str",
            default="",
            scope=GLOBAL_SCOPE,
            description="真实 embedding provider 的 API Key。留空时尝试复用对应 SDK 的环境变量。",
            category="config",
            help_aliases=("embedding_api_key", "向量key", "embedding_key"),
            parser=_str_parser,
        ),
        ConfigEntry(
            key="embedding_batch_size",
            field_name="personification_embedding_batch_size",
            display_name="Embedding 批量大小",
            value_type="int",
            default=16,
            scope=GLOBAL_SCOPE,
            description="后台重建向量索引时单批提交的文本数量；过大可能触发 provider 限流。",
            category="config",
            min_value=1,
            max_value=128,
            help_aliases=("embedding_batch", "向量批量", "embedding批量"),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="memory_vector_backend",
            field_name="personification_memory_vector_backend",
            display_name="记忆向量后端",
            value_type="str",
            default="sqlite_exact",
            scope=GLOBAL_SCOPE,
            description="长期记忆 RAG 的本地向量后端；sqlite_exact 不需要额外服务，disabled 关闭向量索引。",
            category="config",
            choices=("sqlite_exact", "disabled"),
            help_aliases=("向量后端", "vector_backend", "rag_backend"),
            parser=_memory_vector_backend_parser,
        ),
        ConfigEntry(
            key="memory_rag_enabled",
            field_name="personification_memory_rag_enabled",
            display_name="记忆 RAG 召回",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description="启用长期记忆的 chunk 向量索引与 RAG 召回；关闭后仍保留 FTS/实体/时间召回。",
            category="config",
            help_aliases=("rag", "记忆rag", "向量召回"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="memory_rag_candidate_limit",
            field_name="personification_memory_rag_candidate_limit",
            display_name="RAG 候选上限",
            value_type="int",
            default=80,
            scope=GLOBAL_SCOPE,
            description="单次 RAG 向量召回最多扫描的 chunk 候选数；数值越大越容易找回旧记忆但会增加 CPU 开销。",
            category="config",
            min_value=20,
            max_value=1000,
            help_aliases=("rag候选", "向量候选", "memory_rag_candidate_limit"),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="response_timeout",
            field_name="personification_response_timeout",
            display_name="单轮回复超时",
            value_type="int",
            default=180,
            scope=GLOBAL_SCOPE,
            description="单轮回复处理总超时时间，秒。Agent 会在该预算内预留收尾时间。",
            category="config",
            min_value=30,
            max_value=600,
            help_aliases=("回复超时", "单轮超时", "response_timeout"),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="deep_research_v2_enabled",
            field_name="personification_deep_research_v2_enabled",
            display_name="深度研究 v2",
            value_type="bool",
            default=False,
            scope=GLOBAL_SCOPE,
            description="启用深度研究 v2 档位与多页抓取路径；默认关闭，保留旧 parallel_research 行为。",
            category="config",
            help_aliases=("deep_research_v2", "深度研究v2", "研究扩档"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="fallback_enabled",
            field_name="personification_fallback_enabled",
            display_name="全局兜底",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description="主模型路由失败时允许统一进入全局兜底。",
            category="config",
            help_aliases=("视觉兜底", "vision_fallback", "fallback", "全局fallback"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="fallback_api_type",
            field_name="personification_fallback_api_type",
            display_name="全局兜底供应商",
            value_type="str",
            default="",
            scope=GLOBAL_SCOPE,
            description="全局兜底 provider 类型，主路由失败后才使用。",
            category="config",
            help_aliases=("全局兜底供应商", "fallback_provider", "fallback_api_type"),
            parser=_str_parser,
        ),
        ConfigEntry(
            key="fallback_api_url",
            field_name="personification_fallback_api_url",
            display_name="全局兜底 API 地址",
            value_type="str",
            default="",
            scope=GLOBAL_SCOPE,
            description="全局兜底 API 地址。",
            category="config",
            help_aliases=("全局兜底地址", "fallback_url", "fallback_api_url"),
            parser=_str_parser,
        ),
        ConfigEntry(
            key="fallback_api_key",
            field_name="personification_fallback_api_key",
            display_name="全局兜底 API Key",
            value_type="str",
            default="",
            scope=GLOBAL_SCOPE,
            description="全局兜底 API Key。",
            category="config",
            help_aliases=("全局兜底密钥", "fallback_key", "fallback_api_key"),
            parser=_str_parser,
        ),
        ConfigEntry(
            key="fallback_model",
            field_name="personification_fallback_model",
            display_name="全局兜底模型",
            value_type="str",
            default="",
            scope=GLOBAL_SCOPE,
            description="全局兜底模型名。",
            category="config",
            help_aliases=("视觉兜底模型", "vision_fallback_model", "fallback_model"),
            parser=_str_parser,
        ),
        ConfigEntry(
            key="fallback_auth_path",
            field_name="personification_fallback_auth_path",
            display_name="全局兜底认证路径",
            value_type="str",
            default="",
            scope=GLOBAL_SCOPE,
            description="全局兜底 Codex 等 provider 使用的本地认证路径。",
            category="config",
            help_aliases=("全局兜底认证", "fallback_auth_path"),
            parser=_str_parser,
        ),
        ConfigEntry(
            key="video_understanding_enabled",
            field_name="personification_video_understanding_enabled",
            display_name="视频理解",
            value_type="bool",
            default=False,
            scope=GLOBAL_SCOPE,
            description="允许在支持的视频路由上启用视频理解；Gemini 官方主路由会优先使用原生视频理解。",
            category="config",
            help_aliases=("视频理解", "video", "video_enabled"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="video_fallback_enabled",
            field_name="personification_video_fallback_enabled",
            display_name="视频兜底",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description="主模型不支持视频时允许使用独立视频兜底。",
            category="config",
            help_aliases=("视频兜底", "video_fallback"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="video_fallback_provider",
            field_name="personification_video_fallback_provider",
            display_name="视频兜底供应商",
            value_type="str",
            default="",
            scope=GLOBAL_SCOPE,
            description="视频兜底的 provider 类型，留空继承全局兜底。",
            category="config",
            help_aliases=("视频兜底供应商", "video_fallback_provider"),
            parser=_str_parser,
        ),
        ConfigEntry(
            key="video_fallback_api_url",
            field_name="personification_video_fallback_api_url",
            display_name="视频兜底 API 地址",
            value_type="str",
            default="",
            scope=GLOBAL_SCOPE,
            description="视频兜底 API 地址，留空继承全局兜底。",
            category="config",
            help_aliases=("视频兜底地址", "video_fallback_url", "video_fallback_api_url"),
            parser=_str_parser,
        ),
        ConfigEntry(
            key="video_fallback_api_key",
            field_name="personification_video_fallback_api_key",
            display_name="视频兜底 API Key",
            value_type="str",
            default="",
            scope=GLOBAL_SCOPE,
            description="视频兜底 API Key，留空继承全局兜底。",
            category="config",
            help_aliases=("视频兜底密钥", "video_fallback_key", "video_fallback_api_key"),
            parser=_str_parser,
        ),
        ConfigEntry(
            key="video_fallback_model",
            field_name="personification_video_fallback_model",
            display_name="视频兜底模型",
            value_type="str",
            default="",
            scope=GLOBAL_SCOPE,
            description="视频兜底模型名，留空继承全局兜底。",
            category="config",
            help_aliases=("视频兜底模型", "video_fallback_model"),
            parser=_str_parser,
        ),
        ConfigEntry(
            key="video_fallback_auth_path",
            field_name="personification_video_fallback_auth_path",
            display_name="视频兜底认证路径",
            value_type="str",
            default="",
            scope=GLOBAL_SCOPE,
            description="视频兜底认证路径，留空继承全局兜底。",
            category="config",
            help_aliases=("视频兜底认证", "video_fallback_auth_path"),
            parser=_str_parser,
        ),
        ConfigEntry(
            key="image_gen_enabled",
            field_name="personification_image_gen_enabled",
            display_name="图片生成工具",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description="允许 agent 在用户明确要求画图/生成图片时调用统一图片生成工具。",
            category="config",
            help_aliases=("图片生成", "image_gen", "画图工具"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="image_gen_model",
            field_name="personification_image_gen_model",
            display_name="图片生成模型",
            value_type="str",
            default="gpt-image-2",
            scope=GLOBAL_SCOPE,
            description="统一图片生成模型名；OpenAI-compatible/Codex 默认 gpt-image-2，Gemini HTTP 也可填 Gemini 图片模型。",
            category="config",
            help_aliases=("图片生成模型", "image_gen_model", "image2", "gpt-image-2"),
            parser=_str_parser,
        ),
        ConfigEntry(
            key="image_gen_nanobanan_model",
            field_name="personification_image_gen_nanobanan_model",
            display_name="Nano Banana 图片生成模型",
            value_type="str",
            default="gemini-3-pro-image-preview",
            scope=GLOBAL_SCOPE,
            description="gemini_cli / antigravity_cli 路由下的 Gemini 图片模型 ID；留空时回退到统一图片生成模型。",
            category="config",
            help_aliases=("nanobanan", "nano_banana", "gemini图片生成"),
            parser=_str_parser,
        ),
        ConfigEntry(
            key="image_gen_background_enabled",
            field_name="personification_image_gen_background_enabled",
            display_name="图片生成后台发送",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description="用户明确要求生成图片时，先快速回复并在后台继续生成图片，避免单轮回复超时。",
            category="config",
            help_aliases=("图片后台生成", "image_gen_background", "画图后台发送"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="image_gen_timeout",
            field_name="personification_image_gen_timeout",
            display_name="图片生成超时",
            value_type="int",
            default=180,
            scope=GLOBAL_SCOPE,
            description="统一图片生成工具的等待秒数。",
            category="config",
            help_aliases=("图片生成超时", "image_gen_timeout", "画图超时"),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="parallel_research_enabled",
            field_name="personification_parallel_research_enabled",
            display_name="并行研究工具",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description="允许 agent 调用并行子Agent研究工具，聚合联网、百科、图片和视觉资料。",
            category="config",
            help_aliases=("并行研究", "parallel_research", "子agent搜索"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="parallel_research_lookup_enabled",
            field_name="personification_parallel_research_lookup_enabled",
            display_name="查询场景并行研究",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description="允许 parallel_research 用于复杂查询；关闭后仅建议用于生图准备。",
            category="config",
            help_aliases=("查询并行研究", "parallel_research_lookup"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="parallel_research_max_workers",
            field_name="personification_parallel_research_max_workers",
            display_name="并行研究最大子Agent数",
            value_type="int",
            default=6,
            scope=GLOBAL_SCOPE,
            description="parallel_research 单次最多启动的研究子Agent数量。LLM 动态决定实际数量，代码按该值截断。",
            category="config",
            min_value=0,
            max_value=6,
            help_aliases=("子agent数量", "parallel_workers", "并行worker"),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="parallel_research_worker_timeout",
            field_name="personification_parallel_research_worker_timeout",
            display_name="并行研究单子Agent超时",
            value_type="int",
            default=35,
            scope=GLOBAL_SCOPE,
            description="parallel_research 单个研究子Agent的最长运行秒数。",
            category="config",
            min_value=5,
            max_value=180,
            help_aliases=("子agent超时", "parallel_worker_timeout"),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="parallel_research_total_timeout",
            field_name="personification_parallel_research_total_timeout",
            display_name="并行研究总超时",
            value_type="int",
            default=90,
            scope=GLOBAL_SCOPE,
            description="parallel_research 单次规划、并发研究和聚合的总超时秒数。",
            category="config",
            min_value=10,
            max_value=300,
            help_aliases=("并行研究超时", "parallel_total_timeout"),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="parallel_research_max_tool_rounds",
            field_name="personification_parallel_research_max_tool_rounds",
            display_name="并行研究工具轮次",
            value_type="int",
            default=2,
            scope=GLOBAL_SCOPE,
            description="parallel_research 每个子Agent最多可进行的工具调用轮次。",
            category="config",
            min_value=0,
            max_value=4,
            help_aliases=("子agent工具轮次", "parallel_tool_rounds"),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="plugin_knowledge_build_enabled",
            field_name="personification_plugin_knowledge_build_enabled",
            display_name="插件知识库构建",
            value_type="bool",
            default=False,
            scope=GLOBAL_SCOPE,
            description="允许自动或手动启动插件知识库构建任务；需先执行“拟人 知识库 构建/重建插件知识库”后才会注入插件摘要。",
            category="config",
            help_aliases=("知识库构建", "plugin_knowledge_build", "插件知识库"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="group_idle_mode_decision_prob",
            field_name="personification_group_idle_mode_decision_prob",
            display_name="水群模式决策概率",
            value_type="float",
            default=0.4,
            scope=GLOBAL_SCOPE,
            description=(
                "主动水群触发时，多大概率额外调一次 LLM 决定模式（纯文本 / 仅表情包 / 文本+表情包）。"
                "0 表示永远纯文本（与 J4 之前的行为一致）；1 表示每次都决策。"
                "决策会多花一次 LLM 调用，但能让 bot 像真人一样偶尔只丢个表情包。"
            ),
            category="config",
            min_value=0.0,
            max_value=1.0,
            help_aliases=("两阶段水群", "水群模式概率"),
            parser=_float_parser,
        ),
        ConfigEntry(
            key="group_knowledge_autobuild_enabled",
            field_name="personification_group_knowledge_autobuild_enabled",
            display_name="群知识库自动构建",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description="启用后，定时扫描各群最近未总结的对话，LLM 抽取常用词/绰号/内部梗写入群知识库，供后续回复引用。",
            category="config",
            help_aliases=("群知识库", "群知识自构建", "group_knowledge_autobuild"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="group_knowledge_interval_hours",
            field_name="personification_group_knowledge_interval_hours",
            display_name="群知识扫描间隔（小时）",
            value_type="int",
            default=4,
            scope=GLOBAL_SCOPE,
            description="每多少小时扫一次所有群最近消息。最低 1 小时；调大可降低 LLM 开销。",
            category="config",
            min_value=1,
            max_value=72,
            help_aliases=("群知识间隔",),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="group_knowledge_daily_limit",
            field_name="personification_group_knowledge_daily_limit",
            display_name="单群每日构建上限",
            value_type="int",
            default=6,
            scope=GLOBAL_SCOPE,
            description="同一个群每天最多调用 LLM 构建知识库的次数，超出后等次日重置。",
            category="config",
            min_value=1,
            max_value=48,
            help_aliases=("群知识上限",),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="group_style_autobuild_enabled",
            field_name="personification_group_style_autobuild_enabled",
            display_name="群风格自动总结",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description="启用后定时扫描各群对话，由 LLM 总结语气/节奏/口头禅/禁忌/句长 5 个维度并入库；最多保留最近 3 个快照。",
            category="config",
            help_aliases=("群风格", "style_autobuild"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="group_style_interval_hours",
            field_name="personification_group_style_interval_hours",
            display_name="群风格扫描间隔（小时）",
            value_type="int",
            default=12,
            scope=GLOBAL_SCOPE,
            description="每多少小时扫一次群风格。默认 12 小时；风格变化较慢，不建议调小过低。",
            category="config",
            min_value=1,
            max_value=72,
            help_aliases=("群风格间隔",),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="group_style_daily_limit",
            field_name="personification_group_style_daily_limit",
            display_name="单群每日风格构建上限",
            value_type="int",
            default=2,
            scope=GLOBAL_SCOPE,
            description="同一个群每天最多调用 LLM 总结风格的次数；超出后等次日重置。",
            category="config",
            min_value=1,
            max_value=24,
            help_aliases=("群风格上限",),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="group_style_min_messages",
            field_name="personification_group_style_min_messages",
            display_name="群风格构建所需最少消息",
            value_type="int",
            default=100,
            scope=GLOBAL_SCOPE,
            description="自上次扫描以来累计新消息少于该值时跳过风格构建。",
            category="config",
            min_value=20,
            max_value=500,
            help_aliases=("群风格阈值",),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="group_knowledge_min_messages",
            field_name="personification_group_knowledge_min_messages",
            display_name="构建所需最少消息条数",
            value_type="int",
            default=50,
            scope=GLOBAL_SCOPE,
            description="自上次扫描以来累计新消息少于该值时跳过构建。",
            category="config",
            min_value=10,
            max_value=500,
            help_aliases=("群知识阈值",),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="agent_enabled",
            field_name="personification_agent_enabled",
            display_name="Agent 模式",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description="（占位）",
            category="config",
            help_aliases=("agent", "工具调用模式"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="probability",
            field_name="personification_probability",
            display_name="群消息触发概率",
            value_type="float",
            default=0.30,
            scope=GLOBAL_SCOPE,
            description="（占位）",
            category="config",
            min_value=0.0,
            max_value=1.0,
            help_aliases=("回复概率", "触发概率"),
            parser=_float_parser,
        ),
        ConfigEntry(
            key="poke_probability",
            field_name="personification_poke_probability",
            display_name="戳一戳回复概率",
            value_type="float",
            default=0.35,
            scope=GLOBAL_SCOPE,
            description="（占位）",
            category="config",
            min_value=0.0,
            max_value=1.0,
            help_aliases=("戳一戳",),
            parser=_float_parser,
        ),
        ConfigEntry(
            key="thinking_mode",
            field_name="personification_thinking_mode",
            display_name="推理档位（主对话）",
            value_type="str",
            default="none",
            scope=GLOBAL_SCOPE,
            description="（占位）",
            category="config",
            choices=("none", "adaptive", "low", "high"),
            help_aliases=("thinking", "思考"),
            parser=_str_parser,
        ),
        ConfigEntry(
            key="state_thinking_mode",
            field_name="personification_state_thinking_mode",
            display_name="推理档位（情绪状态）",
            value_type="str",
            default="adaptive",
            scope=GLOBAL_SCOPE,
            description="（占位）",
            category="config",
            choices=("none", "adaptive", "low", "high"),
            help_aliases=("情绪思考",),
            parser=_str_parser,
        ),
        ConfigEntry(
            key="builtin_search",
            field_name="personification_builtin_search",
            display_name="原生联网搜索",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description="（占位）",
            category="config",
            help_aliases=("内置搜索",),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="persona_enabled",
            field_name="personification_persona_enabled",
            display_name="用户画像",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description="（占位）",
            category="config",
            help_aliases=("画像", "persona"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="sticker_path",
            field_name="personification_sticker_path",
            display_name="表情包目录",
            value_type="str",
            default="data/stickers",
            scope=GLOBAL_SCOPE,
            description="（占位）",
            category="config",
            help_aliases=("表情包路径",),
            parser=_str_parser,
        ),
        ConfigEntry(
            key="sticker_probability",
            field_name="personification_sticker_probability",
            display_name="表情包发送概率",
            value_type="float",
            default=0.24,
            scope=GLOBAL_SCOPE,
            description="（占位）",
            category="config",
            min_value=0.0,
            max_value=1.0,
            help_aliases=("表情包概率",),
            parser=_float_parser,
        ),
        ConfigEntry(
            key="sticker_semantic",
            field_name="personification_sticker_semantic",
            display_name="语义选表情包",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description="（占位）",
            category="config",
            help_aliases=("语义表情包",),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="labeler_enabled",
            field_name="personification_labeler_enabled",
            display_name="表情包自动打标",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description="启动时扫描表情包目录，用视觉模型为新增/未打标的表情包生成 description/mood_tags 等元数据，写入 stickers.json。",
            category="config",
            help_aliases=("labeler", "打标"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="qzone_proactive_enabled",
            field_name="personification_qzone_proactive_enabled",
            display_name="QQ空间主动发表",
            value_type="bool",
            default=False,
            scope=GLOBAL_SCOPE,
            description="开启后 bot 会按 inner_state / 主动发表节奏自主发空间说说，受 monthly_limit 与 min_interval_hours 约束。",
            category="config",
            help_aliases=("qzone主动发表", "空间主动"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="qzone_quiet_hour_start",
            field_name="personification_qzone_quiet_hour_start",
            display_name="QQ空间静默时段起始小时",
            value_type="int",
            default=0,
            scope=GLOBAL_SCOPE,
            description=(
                "主动发空间说说的「静默期」起始小时（0-23）。"
                "在 [start, end) 区间内（支持跨午夜，如 22→7）不触发主动发表。"
                "用来避免半夜刷屏；与 group_quiet_hour 各自独立。"
            ),
            category="config",
            min_value=0,
            max_value=23,
            help_aliases=("空间静默起始", "qzone安静起始"),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="quota_anthropic_monthly_tokens",
            field_name="personification_quota_anthropic_monthly_tokens",
            display_name="Anthropic 月度额度（tokens）",
            value_type="int",
            default=0,
            scope=GLOBAL_SCOPE,
            description=(
                "用于在 WebUI 仪表盘 / QQ「拟人 额度」命令显示进度条；本地记账非 Anthropic 官方 quota API。"
                "0 = 不设上限（只显示累计）。例：Claude Pro $200/月，按 5M tokens 估算可填 5000000。"
            ),
            category="config",
            min_value=0,
            max_value=2_000_000_000,
            help_aliases=("claude额度", "anthropic额度"),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="quota_openai_monthly_tokens",
            field_name="personification_quota_openai_monthly_tokens",
            display_name="OpenAI 月度额度（tokens）",
            value_type="int",
            default=0,
            scope=GLOBAL_SCOPE,
            description="同 Anthropic 额度——本地记账，仅供进度条参考。0 = 不限。",
            category="config",
            min_value=0,
            max_value=2_000_000_000,
            help_aliases=("openai额度", "gpt额度"),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="quota_gemini_cli_monthly_tokens",
            field_name="personification_quota_gemini_cli_monthly_tokens",
            display_name="Gemini/Antigravity CLI 月度额度（tokens）",
            value_type="int",
            default=0,
            scope=GLOBAL_SCOPE,
            description="Gemini/Antigravity CLI 走 cloudcode-pa 无官方 quota API。本地记账，0 = 不限。",
            category="config",
            min_value=0,
            max_value=2_000_000_000,
            help_aliases=("gemini额度",),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="quota_codex_monthly_tokens",
            field_name="personification_quota_codex_monthly_tokens",
            display_name="Codex 月度额度（tokens）",
            value_type="int",
            default=0,
            scope=GLOBAL_SCOPE,
            description="Codex（ChatGPT OAuth）订阅制无 API quota。本地记账，0 = 不限。",
            category="config",
            min_value=0,
            max_value=2_000_000_000,
            help_aliases=("codex额度", "chatgpt额度"),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="qzone_quiet_hour_end",
            field_name="personification_qzone_quiet_hour_end",
            display_name="QQ空间静默时段结束小时",
            value_type="int",
            default=7,
            scope=GLOBAL_SCOPE,
            description=(
                "主动发空间说说的「静默期」结束小时（不含）。"
                "默认 7 表示 0-6 点不发；起始与结束相同（如均为 0）则禁用静默期。"
            ),
            category="config",
            min_value=0,
            max_value=24,
            help_aliases=("空间静默结束", "qzone安静结束"),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="proactive_threshold",
            field_name="personification_proactive_threshold",
            display_name="主动私聊好感度阈值",
            value_type="float",
            default=60.0,
            scope=GLOBAL_SCOPE,
            description="（占位）",
            category="config",
            min_value=0.0,
            max_value=100.0,
            help_aliases=("主动阈值",),
            parser=_float_parser,
        ),
        ConfigEntry(
            key="proactive_daily_limit",
            field_name="personification_proactive_daily_limit",
            display_name="主动私聊日上限",
            value_type="int",
            default=3,
            scope=GLOBAL_SCOPE,
            description="（占位）",
            category="config",
            min_value=0,
            max_value=50,
            help_aliases=("主动日限",),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="proactive_interval",
            field_name="personification_proactive_interval",
            display_name="主动私聊间隔（分钟）",
            value_type="int",
            default=30,
            scope=GLOBAL_SCOPE,
            description="（占位）",
            category="config",
            min_value=5,
            max_value=720,
            help_aliases=("主动间隔",),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="proactive_idle_hours",
            field_name="personification_proactive_idle_hours",
            display_name="主动私聊空闲门槛（小时）",
            value_type="float",
            default=24.0,
            scope=GLOBAL_SCOPE,
            description="（占位）",
            category="config",
            min_value=0.0,
            max_value=720.0,
            help_aliases=("主动空闲",),
            parser=_float_parser,
        ),
        ConfigEntry(
            key="image_host_allowlist",
            field_name="personification_image_host_allowlist",
            display_name="图片域名信任白名单",
            value_type="list",
            default=[],
            scope=GLOBAL_SCOPE,
            description=(
                "追加可信图床域名后缀（如 .example.cn），命中时跳过 SSRF 内网 IP 检查。"
                "腾讯系（.qq.com / .qpic.cn 等）已内置免配置。"
                "NTQQ 会把图片下载路由到 198.18.0.0/15 客户端代理；只有该域名在此白名单内才能下载到图给 vision 模型分析。"
            ),
            category="config",
            help_aliases=("图片白名单", "image_host_allow"),
            parser=_json_array_parser,
        ),
        ConfigEntry(
            key="lite_model",
            field_name="personification_lite_model",
            display_name="轻量辅助模型",
            value_type="str",
            default="",
            scope=GLOBAL_SCOPE,
            description="用于语义分类、审阅、图片分类等辅助链路；留空回退主模型。",
            category="config",
            help_aliases=("lite_model", "轻量模型", "辅助模型"),
            parser=_str_parser,
        ),
        ConfigEntry(
            key="global_enabled",
            field_name="personification_global_enabled",
            display_name="全局拟人回复",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description="总开关，关闭后所有群聊和私聊拟人回复都会停用。",
            category="config",
            help_aliases=("全局拟人", "拟人开关", "总开关"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="tts_global_enabled",
            field_name="personification_tts_global_enabled",
            display_name="全局语音回复",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description="控制语音能力是否允许在所有场景下启用。",
            category="config",
            help_aliases=("全局语音", "拟人语音", "语音开关"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="tts_llm_decision_enabled",
            field_name="personification_tts_llm_decision_enabled",
            display_name="TTS LLM 决策",
            value_type="bool",
            default=False,
            scope=GLOBAL_SCOPE,
            description="自动语音和命令语音在合成前交由 LLM 判断 voice/text/block，并进行语义违禁阻断；默认关闭。",
            category="config",
            help_aliases=("语音LLM决策", "tts_llm_decision", "语音审查"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="tts_llm_decision_model_role",
            field_name="personification_tts_llm_decision_model_role",
            display_name="TTS 审查模型角色",
            value_type="str",
            default="agent",
            scope=GLOBAL_SCOPE,
            description="TTS LLM 审查使用的模型角色；agent=复用主模型，review/intent/sticker 使用对应四类模型覆盖。",
            category="config",
            choices=("intent", "review", "agent", "sticker"),
            help_aliases=("语音审查模型", "tts_decision_model_role", "tts_llm_role"),
            parser=_model_role_parser,
        ),
        ConfigEntry(
            key="tts_decision_timeout",
            field_name="personification_tts_decision_timeout",
            display_name="TTS 决策超时",
            value_type="int",
            default=8,
            scope=GLOBAL_SCOPE,
            description="TTS 合成前 LLM 决策/审查的超时秒数；失败时回退文字，不合成语音。",
            category="config",
            min_value=2,
            max_value=30,
            help_aliases=("语音审查超时", "tts_decision_timeout"),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="tts_builtin_safety_enabled",
            field_name="personification_tts_builtin_safety_enabled",
            display_name="TTS 内置安全策略",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description="启用内置高风险内容分类，交由 LLM 在语音合成前做语义判断。",
            category="config",
            help_aliases=("语音内置安全", "tts_builtin_safety"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="tts_forbidden_policy",
            field_name="personification_tts_forbidden_policy",
            display_name="TTS 自定义违禁策略",
            value_type="str",
            default="",
            scope=GLOBAL_SCOPE,
            description="追加给 LLM 的语音合成违禁策略文本；不做本地关键词匹配。",
            category="config",
            help_aliases=("语音违禁策略", "tts_forbidden_policy", "语音禁读"),
            parser=_str_parser,
        ),
        ConfigEntry(
            key="tts_mode",
            field_name="personification_tts_mode",
            display_name="TTS 模式",
            value_type="str",
            default="preset",
            scope=GLOBAL_SCOPE,
            description="MiMo-V2.5 TTS 模式：preset 预置音色、design 描述定制音色、clone 音频样本克隆。",
            category="config",
            choices=("preset", "design", "clone"),
            help_aliases=("tts_mode", "语音模式", "音色模式"),
            parser=_tts_mode_parser,
        ),
        ConfigEntry(
            key="tts_model",
            field_name="personification_tts_model",
            display_name="TTS 模型",
            value_type="str",
            default="mimo-v2.5-tts",
            scope=GLOBAL_SCOPE,
            description="MiMo TTS 模型名；通常由 TTS 模式自动选择。",
            category="config",
            help_aliases=("tts_model", "语音模型"),
            parser=_str_parser,
        ),
        ConfigEntry(
            key="tts_default_voice",
            field_name="personification_tts_default_voice",
            display_name="TTS 预置音色",
            value_type="str",
            default="mimo_default",
            scope=GLOBAL_SCOPE,
            description="预置音色 ID，例如 mimo_default、冰糖、茉莉、苏打、白桦、Mia、Chloe、Milo、Dean。",
            category="config",
            help_aliases=("tts_voice", "语音音色", "预置音色"),
            parser=_str_parser,
        ),
        ConfigEntry(
            key="tts_voice_design_prompt",
            field_name="personification_tts_voice_design_prompt",
            display_name="TTS 音色描述",
            value_type="str",
            default="",
            scope=GLOBAL_SCOPE,
            description="design 模式下放入 user message 的音色描述。",
            category="config",
            help_aliases=("音色描述", "voice_design_prompt", "voice_prompt"),
            parser=_str_parser,
        ),
        ConfigEntry(
            key="tts_voice_clone_path",
            field_name="personification_tts_voice_clone_path",
            display_name="TTS 克隆样本路径",
            value_type="str",
            default="",
            scope=GLOBAL_SCOPE,
            description="clone 模式下的 mp3/wav 样本路径，文件需小于 10 MB。",
            category="config",
            help_aliases=("克隆音色路径", "voice_clone_path", "clone_path"),
            parser=_str_parser,
        ),
        ConfigEntry(
            key="proactive_enabled",
            field_name="personification_proactive_enabled",
            display_name="主动私聊",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description="允许 Bot 在合适的时候主动发起私聊。",
            category="config",
            help_aliases=("主动消息", "主动私聊", "拟人主动消息"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="proactive_require_user_profile",
            field_name="personification_proactive_require_user_profile",
            display_name="主动私聊要求画像",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description="开启后，主动私聊候选必须已有用户画像且仍是 Bot 好友；好感度不再作为准入门槛。",
            category="config",
            help_aliases=("主动私聊画像", "主动消息画像", "proactive_profile"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="qzone_social_enabled",
            field_name="personification_qzone_social_enabled",
            display_name="好友空间互动",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description="允许定时读取最近互动好友的 QQ 空间，并由 LLM 自主决定点赞或短评。",
            category="config",
            help_aliases=("空间互动", "好友空间", "qzone_social"),
            parser=_bool_parser,
            risk_note="会对好友空间产生点赞/评论等外部可见行为，建议先用“拟人 空间 测试 <QQ号>”验证。",
        ),
        ConfigEntry(
            key="qzone_social_check_interval",
            field_name="personification_qzone_social_check_interval",
            display_name="好友空间扫描间隔",
            value_type="int",
            default=30,
            scope=GLOBAL_SCOPE,
            description="好友空间互动定时扫描间隔，单位分钟。默认 30 分钟。",
            category="config",
            min_value=30,
            max_value=1440,
            help_aliases=("空间扫描间隔", "好友空间间隔"),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="qzone_third_party_chime_in_enabled",
            field_name="personification_qzone_third_party_chime_in_enabled",
            display_name="好友空间第三方插话",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description="扫描自己空间评论区时，对好友 A 给好友 B 留言这种交叉对话，允许 LLM 判断是否插一句轻量评论。",
            category="config",
            help_aliases=("空间插话", "第三方插话", "qzone_third_party"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="qzone_social_scope",
            field_name="personification_qzone_social_scope",
            display_name="好友空间扫描范围",
            value_type="str",
            default="recent_interactions",
            scope=GLOBAL_SCOPE,
            description="好友空间扫描范围；当前实现默认并仅支持 recent_interactions（最近互动好友）。",
            category="config",
            choices=("recent_interactions",),
            help_aliases=("空间扫描范围", "好友空间范围"),
            parser=_qzone_social_scope_parser,
        ),
        ConfigEntry(
            key="qzone_social_like_limit",
            field_name="personification_qzone_social_like_limit",
            display_name="空间每日点赞上限",
            value_type="int",
            default=0,
            scope=GLOBAL_SCOPE,
            description="好友空间互动每日点赞上限；0 表示无总量上限。",
            category="config",
            min_value=0,
            max_value=1000,
            help_aliases=("空间点赞上限", "好友空间点赞上限"),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="qzone_social_comment_limit",
            field_name="personification_qzone_social_comment_limit",
            display_name="空间每日评论上限",
            value_type="int",
            default=0,
            scope=GLOBAL_SCOPE,
            description="好友空间互动每日评论上限；0 表示无总量上限。",
            category="config",
            min_value=0,
            max_value=1000,
            help_aliases=("空间评论上限", "好友空间评论上限"),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="qzone_social_per_friend_limit",
            field_name="personification_qzone_social_per_friend_limit",
            display_name="空间单好友每日上限",
            value_type="int",
            default=0,
            scope=GLOBAL_SCOPE,
            description="好友空间互动中单个好友每日最多互动次数；0 表示无上限。",
            category="config",
            min_value=0,
            max_value=1000,
            help_aliases=("空间单好友上限", "好友空间单人上限"),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="qzone_social_max_feeds_per_scan",
            field_name="personification_qzone_social_max_feeds_per_scan",
            display_name="空间单次扫描动态数",
            value_type="int",
            default=5,
            scope=GLOBAL_SCOPE,
            description="每次好友空间扫描最多交给 LLM 判断的动态数量，用于控制耗时和外部请求量。",
            category="config",
            min_value=1,
            max_value=50,
            help_aliases=("空间扫描条数", "好友空间单次条数"),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="qzone_inbound_enabled",
            field_name="personification_qzone_inbound_enabled",
            display_name="空间消息轮询",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description="允许短周期读取 Bot 自己 QQ 空间近期说说下的新留言，并由 LLM 判断是否回复。",
            category="config",
            help_aliases=("空间消息", "空间留言", "qzone_inbound"),
            parser=_bool_parser,
            risk_note="会在 Bot 自己空间评论区产生外部可见回复；仅回复 Bot 好友的留言。",
        ),
        ConfigEntry(
            key="qzone_inbound_check_interval",
            field_name="personification_qzone_inbound_check_interval",
            display_name="空间消息轮询间隔",
            value_type="int",
            default=3,
            scope=GLOBAL_SCOPE,
            description="Bot 自己空间留言轮询间隔，单位分钟。QQ 空间无 OneBot 实时事件，插件使用近实时轮询。",
            category="config",
            min_value=1,
            max_value=60,
            help_aliases=("空间消息间隔", "空间留言间隔"),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="qzone_inbound_max_feeds_per_scan",
            field_name="personification_qzone_inbound_max_feeds_per_scan",
            display_name="空间消息扫描说说数",
            value_type="int",
            default=20,
            scope=GLOBAL_SCOPE,
            description="每次空间消息轮询最多检查 Bot 自己近期多少条说说。",
            category="config",
            min_value=1,
            max_value=100,
            help_aliases=("空间留言说说数", "空间消息说说数"),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="qzone_inbound_max_comments_per_feed",
            field_name="personification_qzone_inbound_max_comments_per_feed",
            display_name="空间单说说留言数",
            value_type="int",
            default=20,
            scope=GLOBAL_SCOPE,
            description="每次空间消息轮询中，每条 Bot 说说最多检查多少条留言。",
            category="config",
            min_value=1,
            max_value=100,
            help_aliases=("空间单条留言数", "空间留言条数"),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="qzone_outbound_reply_enabled",
            field_name="personification_qzone_outbound_reply_enabled",
            default=True,
            display_name="Bot 评论被回复轮询",
            value_type="bool",
            scope=GLOBAL_SCOPE,
            description="近实时检查 Bot 自己之前在好友空间留下的评论，看是否有人在评论下回复，并由 LLM 决定是否再回一句。",
            category="config",
            help_aliases=("空间评论被回复", "qzone_outbound_reply", "评论回响"),
            parser=_bool_parser,
            risk_note="会在好友空间评论区发出回复，仅当原作者或留言者是 Bot 好友时触发。",
        ),
        ConfigEntry(
            key="qzone_outbound_reply_check_interval",
            field_name="personification_qzone_outbound_reply_check_interval",
            display_name="Bot 评论被回复轮询间隔",
            value_type="int",
            default=3,
            scope=GLOBAL_SCOPE,
            description="Bot 评论被回复轮询间隔，单位分钟。建议保持 1-5 分钟。",
            category="config",
            min_value=1,
            max_value=60,
            help_aliases=("评论回响间隔", "qzone_outbound_interval"),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="qzone_outbound_reply_max_feeds",
            field_name="personification_qzone_outbound_reply_max_feeds",
            display_name="Bot 评论被回复扫描动态数",
            value_type="int",
            default=30,
            scope=GLOBAL_SCOPE,
            description="每次轮询追溯 Bot 之前评论过的多少条好友动态。",
            category="config",
            min_value=1,
            max_value=200,
            help_aliases=("评论回响动态数",),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="schedule_global",
            field_name="personification_schedule_global",
            display_name="全局作息模拟",
            value_type="bool",
            default=False,
            scope=GLOBAL_SCOPE,
            description="让所有群统一启用作息背景，不必逐群单独开启。",
            category="config",
            help_aliases=("全局作息", "拟人作息", "作息全局"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="memory_enabled",
            field_name="personification_memory_enabled",
            display_name="记忆总开关",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description="总开关，控制记忆体系是否运行。",
            category="config",
            help_aliases=("记忆", "记忆开关"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="memory_palace_enabled",
            field_name="personification_memory_palace_enabled",
            display_name="记忆宫殿",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description="启用长期记忆宫殿存储与 recall。",
            category="config",
            help_aliases=("记忆宫殿", "长期记忆"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="memory_decay_enabled",
            field_name="personification_memory_decay_enabled",
            display_name="记忆衰减",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description="允许后台执行记忆衰减。",
            category="config",
            help_aliases=("衰减", "自动衰减"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="memory_consolidation_enabled",
            field_name="personification_memory_consolidation_enabled",
            display_name="记忆整合",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description="允许后台执行记忆聚合与 crystal 检查。",
            category="config",
            help_aliases=("整合", "记忆整合", "结晶检查"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="memory_recall_top_k",
            field_name="personification_memory_recall_top_k",
            display_name="记忆召回条数",
            value_type="int",
            default=DEFAULT_MEMORY_RECALL_TOP_K,
            scope=GLOBAL_SCOPE,
            description="单次 recall 默认返回记忆条数。",
            category="config",
            min_value=1,
            max_value=MAX_MEMORY_RECALL_TOP_K,
            help_aliases=("召回条数", "recall条数"),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="memory_search_scan_limit",
            field_name="personification_memory_search_scan_limit",
            display_name="记忆召回扫描池",
            value_type="int",
            default=800,
            scope=GLOBAL_SCOPE,
            description="单次长期记忆召回最多扫描多少条候选记忆；越大越容易找回旧记忆，但会增加 CPU/SQLite 开销。",
            category="config",
            min_value=80,
            max_value=5000,
            help_aliases=("记忆扫描池", "旧记忆召回", "memory_scan_limit"),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="memory_capture_policy",
            field_name="personification_memory_capture_policy",
            display_name="长期记忆写入策略",
            value_type="str",
            default="balanced",
            scope=GLOBAL_SCOPE,
            description="控制聊天回合进入长期记忆的积极程度：balanced 只保留较有信息量内容，conservative 更克制，all 保持尽量全量写入。",
            category="config",
            choices=("balanced", "conservative", "all"),
            help_aliases=("记忆写入策略", "记忆采集策略", "memory_capture_policy"),
            parser=_str_parser,
        ),
        ConfigEntry(
            key="agent_memory_write_enabled",
            field_name="personification_agent_memory_write_enabled",
            display_name="Agent 写长期记忆",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description="允许 Agent 通过工具主动沉淀用户或群长期记忆；关闭后仍可读取已有记忆。",
            category="config",
            help_aliases=("agent记忆写入", "主动记忆", "remember工具"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="persona_history_max",
            field_name="personification_persona_history_max",
            display_name="画像更新阈值",
            value_type="int",
            default=DEFAULT_PERSONA_HISTORY_MAX,
            scope=GLOBAL_SCOPE,
            description="单个用户累计多少条新消息后触发一次画像更新。",
            category="config",
            min_value=10,
            max_value=200,
            help_aliases=("画像阈值", "画像历史条数", "人格画像阈值"),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="history_len",
            field_name="personification_history_len",
            display_name="会话历史上限",
            value_type="int",
            default=DEFAULT_HISTORY_LEN,
            scope=GLOBAL_SCOPE,
            description="数据库中每个会话最多保留多少条原始消息，再多会滚动清理。",
            category="config",
            min_value=80,
            max_value=800,
            help_aliases=("上下文长度", "历史长度", "聊天上下文长度"),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="compress_threshold",
            field_name="personification_compress_threshold",
            display_name="压缩触发条数",
            value_type="int",
            default=DEFAULT_COMPRESS_THRESHOLD,
            scope=GLOBAL_SCOPE,
            description="会话累计到多少条后开始把旧消息压缩成摘要。",
            category="config",
            min_value=40,
            max_value=600,
            help_aliases=("压缩阈值", "摘要阈值"),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="compress_keep_recent",
            field_name="personification_compress_keep_recent",
            display_name="压缩保留条数",
            value_type="int",
            default=DEFAULT_COMPRESS_KEEP_RECENT,
            scope=GLOBAL_SCOPE,
            description="压缩后仍保留多少条最近原始消息，帮助模型续接当前话题。",
            category="config",
            min_value=8,
            max_value=120,
            help_aliases=("保留条数", "最近保留条数"),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="private_history_turns",
            field_name="personification_private_history_turns",
            display_name="私聊送模条数",
            value_type="int",
            default=DEFAULT_PRIVATE_HISTORY_TURNS,
            scope=GLOBAL_SCOPE,
            description="私聊时真正送进主模型的最近消息条数上限，越大越容易延续长对话。",
            category="config",
            min_value=12,
            max_value=MAX_PRIVATE_HISTORY_TURNS,
            help_aliases=("私聊上下文条数", "私聊历史条数", "私聊轮数"),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="message_expire_hours",
            field_name="personification_message_expire_hours",
            display_name="私聊上下文过期小时",
            value_type="float",
            default=DEFAULT_MESSAGE_EXPIRE_HOURS,
            scope=GLOBAL_SCOPE,
            description="私聊旧消息超过这个时长后不再参与上下文；0 表示不过期。",
            category="config",
            min_value=0,
            max_value=720,
            help_aliases=("消息过期小时", "私聊过期时间"),
            parser=_float_parser,
        ),
        ConfigEntry(
            key="group_context_expire_hours",
            field_name="personification_group_context_expire_hours",
            display_name="群上下文过期小时",
            value_type="float",
            default=DEFAULT_GROUP_CONTEXT_EXPIRE_HOURS,
            scope=GLOBAL_SCOPE,
            description="群聊旧消息超过这个时长后不再参与上下文；0 表示不过期。",
            category="config",
            min_value=0,
            max_value=240,
            help_aliases=("群过期时间", "群上下文时间"),
            parser=_float_parser,
        ),
        ConfigEntry(
            key="group_summary_expire_hours",
            field_name="personification_group_summary_expire_hours",
            display_name="群摘要过期小时",
            value_type="float",
            default=DEFAULT_GROUP_SUMMARY_EXPIRE_HOURS,
            scope=GLOBAL_SCOPE,
            description="群话题摘要在超过这个时长后不再注入给模型。",
            category="config",
            min_value=0,
            max_value=240,
            help_aliases=("群摘要时间", "话题摘要过期"),
            parser=_float_parser,
        ),
        ConfigEntry(
            key="background_intelligence_enabled",
            field_name="personification_background_intelligence_enabled",
            display_name="后台智能",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description="启用统一后台智能调度层。",
            category="config",
            help_aliases=("后台智能",),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="background_evolves_enabled",
            field_name="personification_background_evolves_enabled",
            display_name="后台演化关系",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description="启用后台 EVOLVES 关系检测。",
            category="config",
            help_aliases=("演化关系", "evolves"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="background_crystals_enabled",
            field_name="personification_background_crystals_enabled",
            display_name="后台结晶",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description="启用后台 crystal 候选生成。",
            category="config",
            help_aliases=("结晶", "crystal", "记忆结晶"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="background_max_llm_tasks_per_hour",
            field_name="personification_background_max_llm_tasks_per_hour",
            display_name="每小时后台任务上限",
            value_type="int",
            default=6,
            scope=GLOBAL_SCOPE,
            description="后台每小时最多 LLM 任务数。",
            category="config",
            min_value=0,
            max_value=120,
            help_aliases=("每小时任务上限", "小时预算"),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="background_max_llm_tasks_per_day",
            field_name="personification_background_max_llm_tasks_per_day",
            display_name="每日后台任务上限",
            value_type="int",
            default=24,
            scope=GLOBAL_SCOPE,
            description="后台每日最多 LLM 任务数。",
            category="config",
            min_value=0,
            max_value=500,
            help_aliases=("每日任务上限", "每日预算"),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="background_debounce_seconds",
            field_name="personification_background_debounce_seconds",
            display_name="后台防抖秒数",
            value_type="int",
            default=90,
            scope=GLOBAL_SCOPE,
            description="同类后台任务的防抖时间。",
            category="config",
            min_value=5,
            max_value=3600,
            help_aliases=("防抖秒数", "后台防抖"),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="wiki_enabled",
            field_name="personification_wiki_enabled",
            display_name="Wiki 查询",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description="允许使用 wiki 能力。",
            category="config",
            help_aliases=("wiki", "百科查询"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="wiki_fandom_enabled",
            field_name="personification_wiki_fandom_enabled",
            display_name="Fandom Wiki",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description="允许 Fandom wiki 作为补充来源。",
            category="config",
            help_aliases=("fandom", "fandom wiki", "粉丝百科"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="group_enabled",
            field_name="enabled",
            display_name="本群拟人回复",
            value_type="bool",
            default=True,
            scope=GROUP_SCOPE,
            description="当前群是否启用拟人回复。",
            category="config",
            help_aliases=("personification_enabled", "本群拟人", "群回复"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="group_sticker_enabled",
            field_name="sticker_enabled",
            display_name="本群表情包",
            value_type="bool",
            default=True,
            scope=GROUP_SCOPE,
            description="当前群是否允许发表情包。",
            category="config",
            help_aliases=("本群表情包", "群表情包"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="group_tts_enabled",
            field_name="tts_enabled",
            display_name="本群语音回复",
            value_type="bool",
            default=True,
            scope=GROUP_SCOPE,
            description="当前群是否允许自动或手动语音回复。",
            category="config",
            help_aliases=("本群语音", "群语音回复"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="group_schedule_enabled",
            field_name="schedule_enabled",
            display_name="本群作息模拟",
            value_type="bool",
            default=False,
            scope=GROUP_SCOPE,
            description="当前群是否启用作息模拟。",
            category="config",
            help_aliases=("作息模拟", "群作息"),
            parser=_bool_parser,
        ),
    ]
    entries.extend(_build_extra_entries())
    return [_enrich_entry(entry) for entry in entries]


_EXTRA_SPEC_PARSERS: dict[str, Callable[[str], Any]] = {
    "bool": _bool_parser,
    "int": _int_parser,
    "float": _float_parser,
    "str": _str_parser,
    "list": _json_array_parser,
    "dict": _json_object_parser,
}


def _build_extra_entries() -> list[ConfigEntry]:
    from .config_registry_extra import EXTRA_CONFIG_SPECS

    entries: list[ConfigEntry] = []
    for spec in EXTRA_CONFIG_SPECS:
        field_name = str(spec["field"])
        value_type = str(spec["t"])
        entries.append(
            ConfigEntry(
                key=field_name.removeprefix("personification_"),
                field_name=field_name,
                display_name=str(spec["name"]),
                value_type=value_type,
                default=spec["default"],
                scope=GLOBAL_SCOPE,
                description=str(spec["desc"]),
                category="config",
                choices=tuple(spec.get("choices", ())),
                min_value=spec.get("min"),
                max_value=spec.get("max"),
                help_aliases=tuple(spec.get("aliases", ())),
                risk_note=str(spec.get("risk", "")),
                parser=_EXTRA_SPEC_PARSERS[value_type],
                kind=str(spec.get("kind", "")),
                group=str(spec.get("group", "其他")),
                advanced=bool(spec.get("advanced", False)),
                hot_reloadable=bool(spec.get("hot", True)),
                example=str(spec.get("example", "")),
            )
        )
    return entries


_ENTRIES = _build_entries()
_ENTRY_BY_KEY: dict[str, ConfigEntry] = {entry.key: entry for entry in _ENTRIES}
_ALIASES: dict[str, ConfigEntry] = {}


def _normalize_alias(text: str) -> str:
    return str(text or "").strip().lower()


def _compact_alias(text: str) -> str:
    normalized = _normalize_alias(text)
    return re.sub(r"[\s_\-:/]+", "", normalized)


def _register_alias(alias: str, entry: ConfigEntry) -> None:
    for candidate in {_normalize_alias(alias), _compact_alias(alias)}:
        if candidate:
            _ALIASES[candidate] = entry


for _entry in _ENTRIES:
    _register_alias(_entry.key, _entry)
    _register_alias(_entry.field_name, _entry)
    _register_alias(_entry.display_name, _entry)
    _register_alias(_entry.field_name.removeprefix("personification_"), _entry)
    for _alias in _entry.help_aliases:
        _register_alias(str(_alias or ""), _entry)


def get_config_entries(scope: str | None = None) -> list[ConfigEntry]:
    if scope is None:
        return list(_ENTRIES)
    normalized = str(scope or "").strip().lower()
    return [entry for entry in _ENTRIES if entry.scope == normalized]


def get_global_runtime_config_keys() -> list[str]:
    return [entry.key for entry in _ENTRIES if entry.scope == GLOBAL_SCOPE]


def resolve_config_entry(key: str) -> ConfigEntry | None:
    normalized = _normalize_alias(key)
    compact = _compact_alias(key)
    if not normalized:
        return None
    return _ALIASES.get(normalized) or _ALIASES.get(compact)


def get_entry_default_value(entry: ConfigEntry, plugin_config: Any) -> Any:
    if entry.scope == GLOBAL_SCOPE:
        return getattr(type(plugin_config), entry.field_name, entry.default)
    return entry.default


def read_config_value(
    entry: ConfigEntry,
    *,
    plugin_config: Any,
    group_config: dict[str, Any] | None = None,
) -> Any:
    if entry.scope == GLOBAL_SCOPE:
        return getattr(plugin_config, entry.field_name, entry.default)
    group_payload = group_config if isinstance(group_config, dict) else {}
    return group_payload.get(entry.field_name, entry.default)


def describe_choices(entry: ConfigEntry) -> str:
    if entry.value_type == "bool":
        return "开 / 关"
    if entry.key == "tool_web_search_mode":
        return "开启 / 实时 / 缓存 / 关闭"
    if entry.choices:
        return ", ".join(entry.choices)
    if entry.value_type == "int":
        lower = entry.min_value if entry.min_value is not None else "-"
        upper = entry.max_value if entry.max_value is not None else "-"
        return f"整数 ({lower}..{upper})"
    if entry.value_type == "float":
        lower = entry.min_value if entry.min_value is not None else "-"
        upper = entry.max_value if entry.max_value is not None else "-"
        return f"数字 ({lower}..{upper})"
    if entry.value_type == "dict":
        return "JSON 对象"
    if entry.value_type == "list":
        return "JSON 数组"
    return "自由文本"


def format_config_value(value: Any) -> str:
    if isinstance(value, bool):
        return "开" if value else "关"
    if value is None:
        return "未设置"
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def get_entry_label(entry: ConfigEntry) -> str:
    return str(entry.display_name or entry.key)


def config_entry_matches_scope(entry: ConfigEntry, scope: str) -> bool:
    return entry.scope == str(scope or "").strip().lower()


def iter_config_aliases(entry: ConfigEntry) -> Iterable[str]:
    yield entry.key
    yield entry.field_name
    yield entry.display_name
    yield entry.field_name.removeprefix("personification_")
    for alias in entry.help_aliases:
        yield alias
