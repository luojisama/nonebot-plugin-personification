from __future__ import annotations

import re
from pathlib import Path

from ._loader import load_personification_module

config_registry = load_personification_module("plugin.personification.core.config_registry")

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent / "nonebot_plugin_personification"

# config.py 中刻意不注册到 WebUI 的字段（无 personification_ 前缀的废弃别名除外，
# 它们本来就匹配不到字段扫描正则）。当前没有豁免项：新增 config 字段时必须
# 同步注册到 config_registry 或 config_registry_extra。
_REGISTRY_EXEMPT_FIELDS: frozenset[str] = frozenset()


def _config_field_names() -> set[str]:
    text = (_PLUGIN_ROOT / "config.py").read_text(encoding="utf-8")
    return set(re.findall(r"^\s+(personification_\w+)\s*:", text, re.M))


def test_every_config_field_registered() -> None:
    registered = {
        entry.field_name
        for entry in config_registry.get_config_entries()
        if entry.field_name.startswith("personification_")
    }
    missing = _config_field_names() - registered - _REGISTRY_EXEMPT_FIELDS
    assert not missing, f"config.py 字段未注册到 config_registry（WebUI 不可编辑）: {sorted(missing)}"


def test_registered_global_fields_exist_on_config_model() -> None:
    config_module = load_personification_module("plugin.personification.config")
    model_fields = set(config_module.Config.model_fields)
    orphans = {
        entry.field_name
        for entry in config_registry.get_config_entries("global")
        if entry.field_name.startswith("personification_") and entry.field_name not in model_fields
    }
    assert not orphans, f"registry 注册了 config.py 不存在的字段: {sorted(orphans)}"


def test_entry_keys_unique() -> None:
    entries = config_registry.get_config_entries()
    keys = [entry.key for entry in entries]
    dupes = {key for key in keys if keys.count(key) > 1}
    assert not dupes, f"ConfigEntry key 重复: {sorted(dupes)}"


def test_extra_entries_normalize_roundtrip() -> None:
    entries = {entry.key: entry for entry in config_registry.get_config_entries()}
    # bool / int / float / choices / json 各抽一个补充条目验证 parser 正常工作
    assert entries["tts_enabled"].normalize_value("开") is True
    assert entries["group_idle_minutes"].normalize_value("45") == 45
    assert entries["qzone_probability"].normalize_value("0.5") == 0.5
    assert entries["image_detail"].normalize_value("LOW") == "low"
    assert entries["whitelist"].normalize_value('["123"]') == ["123"]
    assert entries["favorability_attitudes"].normalize_value('{"初见":"礼貌"}') == {"初见": "礼貌"}


def test_normalize_value_accepts_parsed_list_and_dict() -> None:
    # WebUI 的 API Provider 池编辑器直接提交已解析的数组（FastAPI 解析 JSON body
    # 后是 Python list），normalize_value 必须接受，不能因 str(list) 的 Python repr
    # 解析失败而拒绝保存（历史上导致"保存后回退"）。
    entries = {entry.field_name: entry for entry in config_registry.get_config_entries()}
    pools_entry = entries["personification_api_pools"]
    pools = [
        {"name": "main", "api_type": "openai", "api_key": "sk-x", "model": "m", "priority": 1, "enabled": True},
        {"name": "backup", "api_type": "anthropic", "api_key": "sk-y", "model": "c", "priority": 2, "enabled": False},
    ]
    out = pools_entry.normalize_value(pools)
    assert isinstance(out, list) and len(out) == 2
    assert out[0]["api_key"] == "sk-x" and out[1]["enabled"] is False
    # 空列表与 JSON 字符串两种输入也要继续工作
    assert pools_entry.normalize_value([]) == []
    assert len(pools_entry.normalize_value('[{"name":"x","api_type":"openai"}]')) == 1
    overrides_entry = entries["personification_model_overrides"]
    assert overrides_entry.normalize_value({"intent": "gpt-4o-mini"}) == {"intent": "gpt-4o-mini"}


def test_extra_entries_secret_and_advanced_inference() -> None:
    entries = {entry.key: entry for entry in config_registry.get_config_entries()}
    assert entries["tts_api_key"].secret is True
    assert entries["qzone_cookie"].secret is True
    assert entries["codex_auth_path"].secret is True
    assert entries["api_key"].secret is True
    # 遗留单模型配置应折叠为高级字段
    assert entries["api_type"].advanced is True
    assert entries["model"].advanced is True
