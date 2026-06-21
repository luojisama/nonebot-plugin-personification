from __future__ import annotations

from ._loader import load_personification_module

config_registry = load_personification_module("plugin.personification.core.config_registry")
config_mod = load_personification_module("plugin.personification.config")


def test_all_entries_have_non_empty_group_and_kind() -> None:
    entries = config_registry.get_config_entries()
    assert entries, "registry must not be empty"
    for entry in entries:
        assert entry.group, f"{entry.key} missing group"
        assert entry.group != "其他", f"{entry.key} falls back to 其他 group"
        assert entry.kind, f"{entry.key} missing kind"


def test_kind_inference_matches_value_type() -> None:
    entries = {entry.key: entry for entry in config_registry.get_config_entries()}
    # bool 全部映射成 toggle
    for entry in entries.values():
        if entry.value_type == "bool":
            assert entry.kind == "toggle", f"{entry.key} bool should be toggle"
    # secret 优先级高于 path/select
    assert entries["fallback_api_key"].kind == "secret"
    assert entries["fallback_api_key"].secret is True
    assert entries["fallback_auth_path"].secret is True
    # tts_voice_clone_path 不是密钥但是文件路径
    assert entries["tts_voice_clone_path"].kind == "path"
    # JSON 列表
    assert entries["api_pools"].kind == "json"
    # 整数
    assert entries["agent_max_steps"].kind == "int"


def test_required_fields_marked() -> None:
    entries = {entry.key: entry for entry in config_registry.get_config_entries()}
    assert entries["global_enabled"].required is True
    assert entries["api_pools"].required is True
    # 非核心字段默认 False
    assert entries["tts_global_enabled"].required is False


def test_group_routing() -> None:
    entries = {entry.key: entry for entry in config_registry.get_config_entries()}
    assert entries["tts_global_enabled"].group == "TTS 语音"
    assert entries["qzone_social_enabled"].group == "QQ 空间"
    assert entries["image_gen_enabled"].group == "图像生成"
    assert entries["memory_enabled"].group == "记忆"
    assert entries["fallback_enabled"].group == "模型回退"
    assert entries["proactive_enabled"].group == "主动私聊"
    assert entries["api_pools"].group == "模型路由"
    assert entries["global_enabled"].group == "核心开关"
    assert entries["response_review_enabled"].group == "回复审阅"
    assert entries["turn_planner_enabled"].group == "意图规划"


def test_advanced_inference_from_field_name() -> None:
    entries = {entry.key: entry for entry in config_registry.get_config_entries()}
    # 关键词匹配字段会被自动标 advanced
    assert entries["agent_max_steps"].advanced is True  # _max_steps 命中
    assert entries["parallel_research_max_workers"].advanced is True  # _workers 命中
    assert entries["response_timeout"].advanced is True  # _timeout 命中
    # 基础字段不应被误标
    assert entries["global_enabled"].advanced is False
    assert entries["api_pools"].advanced is False
    assert entries["sticker_path"].advanced is False
    assert entries["tts_global_enabled"].advanced is False


def test_detailed_descriptions_applied_to_core_entries() -> None:
    entries = {entry.key: entry for entry in config_registry.get_config_entries()}
    # 27 项重写过的描述都应不为空且超过 30 字
    must_have = ["global_enabled", "api_pools", "agent_max_steps", "probability",
                 "memory_palace_enabled", "persona_enabled", "tts_mode", "image_host_allowlist"]
    for key in must_have:
        desc = entries[key].description
        assert len(desc) >= 30, f"{key} description too short: {desc}"
    # 这些字段应当带 example
    assert entries["api_pools"].example  # 应有示例
    assert entries["probability"].example
    assert entries["agent_max_steps"].example


def test_memory_and_labeler_defaults_match_long_term_memory_design() -> None:
    cfg = config_mod.Config()
    entries = {entry.key: entry for entry in config_registry.get_config_entries()}

    assert cfg.personification_labeler_model == ""
    assert entries["labeler_model"].default == ""
    assert "留空复用主模型" in entries["labeler_model"].description

    assert cfg.personification_memory_palace_enabled is True
    assert entries["memory_palace_enabled"].default is True
    assert entries["memory_recall_top_k"].default == 12
    assert entries["memory_recall_top_k"].max_value == 32
    assert entries["private_history_turns"].default == 60
    assert entries["private_history_turns"].max_value == 240
    assert entries["memory_search_scan_limit"].default == 800
    assert entries["memory_capture_policy"].choices == ("balanced", "conservative", "all")
    assert entries["agent_memory_write_enabled"].default is True
    assert entries["memory_vector_backend"].choices == ("sqlite_exact", "disabled")
    assert entries["memory_rag_enabled"].default is True
    assert entries["memory_rag_candidate_limit"].default == 80


def test_secret_inference_for_key_and_auth_path_fields() -> None:
    entries = {entry.key: entry for entry in config_registry.get_config_entries()}
    # api_key 后缀字段必须 secret
    assert entries["fallback_api_key"].secret is True
    assert entries["video_fallback_api_key"].secret is True
    # auth_path 字段也是密钥
    assert entries["fallback_auth_path"].secret is True
    assert entries["video_fallback_auth_path"].secret is True
    # URL 字段不是 secret
    assert entries["fallback_api_url"].secret is False
    # 普通 toggle 不 secret
    assert entries["tts_global_enabled"].secret is False
