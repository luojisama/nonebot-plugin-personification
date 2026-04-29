from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from .model_router import normalize_model_overrides

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

    def normalize_value(self, raw: Any) -> Any:
        if isinstance(raw, bool) and self.value_type == "bool":
            value = raw
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
            key="model_builtin_search_enabled",
            field_name="personification_model_builtin_search_enabled",
            display_name="模型内置搜索",
            value_type="bool",
            default=False,
            scope=GLOBAL_SCOPE,
            description="允许主模型直接使用 provider 原生 builtin search。",
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
            description="允许在支持的视频路由上启用视频理解。",
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
            description="允许 agent 在用户明确要求画图/生成图片时调用 Codex 图片生成工具。",
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
            description="Codex 图片生成请求的 GPT Image 模型名；默认 gpt-image-2。仅用于 Codex 后端 image_generation 托管工具，不走 OpenAI API。",
            category="config",
            help_aliases=("图片生成模型", "image_gen_model", "image2", "gpt-image-2"),
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
            description="Codex 图片生成后台任务的等待秒数；仅影响 generate_image 工具。",
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
            default=True,
            scope=GLOBAL_SCOPE,
            description="自动语音和命令语音在合成前交由 LLM 判断 voice/text/block，并进行语义违禁阻断。",
            category="config",
            help_aliases=("语音LLM决策", "tts_llm_decision", "语音审查"),
            parser=_bool_parser,
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
            default=False,
            scope=GLOBAL_SCOPE,
            description="允许 Bot 在合适的时候主动发起私聊。",
            category="config",
            help_aliases=("主动消息", "主动私聊", "拟人主动消息"),
            parser=_bool_parser,
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
            default=False,
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
    return "自由文本"


def format_config_value(value: Any) -> str:
    if isinstance(value, bool):
        return "开" if value else "关"
    if value is None:
        return "未设置"
    if isinstance(value, dict):
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
