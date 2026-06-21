from __future__ import annotations

import json
from types import SimpleNamespace

from ._loader import load_personification_module

config_module = load_personification_module("plugin.personification.config")
config_manager = load_personification_module("plugin.personification.core.config_manager")
config_registry = load_personification_module("plugin.personification.core.config_registry")
env_writer = load_personification_module("plugin.personification.core.env_writer")
runtime_config = load_personification_module("plugin.personification.core.runtime_config")
data_store = load_personification_module("plugin.personification.core.data_store")


class _Logger:
    def info(self, *_args, **_kwargs) -> None:
        return None

    def warning(self, *_args, **_kwargs) -> None:
        return None

    def error(self, *_args, **_kwargs) -> None:
        return None


def _setup(tmp_path, monkeypatch):
    # 无 .env 文件，env.json 落在 tmp_path
    monkeypatch.setattr(runtime_config, "_iter_env_file_candidates", lambda: [])
    monkeypatch.setattr(env_writer, "_iter_env_file_candidates", lambda: [])
    data_store.init_data_store(SimpleNamespace(personification_data_dir=str(tmp_path)))
    return config_module.Config(personification_data_dir=str(tmp_path))


def test_webui_change_persists_across_restart(tmp_path, monkeypatch) -> None:
    """改一个不在 .env 的字段 → 模拟重启后值应保留（不回退到默认）。"""
    cfg = _setup(tmp_path, monkeypatch)
    res = env_writer.write_both("personification_probability", 0.66, cfg)
    assert res["dotenv_path"] is None  # 新字段不写 .env，避免重启回退
    assert res["env_json_path"]

    fresh = config_module.Config(personification_data_dir=str(tmp_path))
    assert fresh.personification_probability != 0.66
    config_manager.ConfigManager(plugin_config=fresh, logger=None).load()
    assert fresh.personification_probability == 0.66


def test_env_json_retains_webui_field_after_load(tmp_path, monkeypatch) -> None:
    cfg = _setup(tmp_path, monkeypatch)
    env_writer.write_both("personification_probability", 0.42, cfg)
    fresh = config_module.Config(personification_data_dir=str(tmp_path))
    config_manager.ConfigManager(plugin_config=fresh, logger=None).load()

    env_json = json.loads((tmp_path / "env.json").read_text(encoding="utf-8"))
    assert env_json.get("personification_probability") == 0.42


def test_env_json_wins_over_stale_env_file_after_restart(tmp_path, monkeypatch) -> None:
    cfg = _setup(tmp_path, monkeypatch)
    env_writer.write_both("personification_probability", 0.66, cfg)
    env_file = tmp_path / ".env.prod"
    env_file.write_text("personification_probability=0.11\n", encoding="utf-8")
    monkeypatch.setattr(runtime_config, "_iter_env_file_candidates", lambda: [env_file])
    monkeypatch.setattr(env_writer, "_iter_env_file_candidates", lambda: [env_file])

    fresh = config_module.Config(personification_data_dir=str(tmp_path), personification_probability=0.11)
    config_manager.ConfigManager(plugin_config=fresh, logger=None).load()

    assert fresh.personification_probability == 0.66
    env_json = json.loads((tmp_path / "env.json").read_text(encoding="utf-8"))
    assert env_json.get("personification_probability") == 0.66
    assert env_file.read_text(encoding="utf-8") == "personification_probability=0.11\n"
    assert list(tmp_path.glob(".env.prod.bak.*")) == []


def test_env_json_api_pools_wins_over_stale_runtime_managed_globals(tmp_path, monkeypatch) -> None:
    cfg = _setup(tmp_path, monkeypatch)
    old_pools = [
        {
            "name": "old_codex",
            "api_type": "openai_codex",
            "model": "gpt-5-codex",
            "priority": 1,
            "enabled": True,
        }
    ]
    new_pools = [
        {
            "name": "new_gemini",
            "api_type": "gemini_cli",
            "model": "auto-gemini-3",
            "priority": 1,
            "enabled": True,
        }
    ]
    env_writer.write_both("personification_api_pools", new_pools, cfg)
    runtime_path = tmp_path / "runtime_config.json"
    runtime_path.write_text(
        json.dumps({"managed_globals": {"api_pools": old_pools}}, ensure_ascii=False),
        encoding="utf-8",
    )

    fresh = config_module.Config(personification_data_dir=str(tmp_path))
    config_manager.ConfigManager(plugin_config=fresh, logger=None).load()
    runtime_config.load_plugin_runtime_config(fresh, _Logger(), path=runtime_path)

    assert fresh.personification_api_pools == new_pools
    info = runtime_config.get_runtime_load_info(fresh)
    assert "personification_api_pools" in info["env_json_fields"]
    assert "api_pools" in info["skipped_runtime_keys"]


def test_env_json_top_level_runtime_key_wins_over_runtime_config(tmp_path, monkeypatch) -> None:
    cfg = _setup(tmp_path, monkeypatch)
    env_writer.write_both("personification_web_search", False, cfg)
    runtime_path = tmp_path / "runtime_config.json"
    runtime_path.write_text(json.dumps({"web_search": True}, ensure_ascii=False), encoding="utf-8")

    fresh = config_module.Config(personification_data_dir=str(tmp_path))
    config_manager.ConfigManager(plugin_config=fresh, logger=None).load()
    runtime_config.load_plugin_runtime_config(fresh, _Logger(), path=runtime_path)

    assert fresh.personification_web_search is False
    info = runtime_config.get_runtime_load_info(fresh)
    assert "personification_web_search" in info["env_json_fields"]
    assert "web_search" in info["skipped_runtime_keys"]


def test_config_registry_extra_field_wins_over_stale_runtime_after_restart(tmp_path, monkeypatch) -> None:
    """config_registry_extra.py 注册的字段（如 qzone 月度上限）经 WebUI 改后，
    重启时也应胜过 runtime_config.json 里的旧 managed_globals，不回退到默认。

    守住 config_manager（按 _managed_field_names 应用 env.json）与
    runtime_config（按 _collect_env_json_fields 保护 env.json 字段）两套注册一致。
    """
    cfg = _setup(tmp_path, monkeypatch)
    field = "personification_qzone_monthly_limit"
    entry = next(e for e in config_registry.get_config_entries("global") if e.field_name == field)
    default_val = config_module.Config().personification_qzone_monthly_limit

    env_writer.write_both(field, 17, cfg)
    runtime_path = tmp_path / "runtime_config.json"
    runtime_path.write_text(
        json.dumps({"managed_globals": {entry.key: default_val}}, ensure_ascii=False),
        encoding="utf-8",
    )

    fresh = config_module.Config(personification_data_dir=str(tmp_path))
    assert fresh.personification_qzone_monthly_limit == default_val  # 重启初值=默认
    config_manager.ConfigManager(plugin_config=fresh, logger=None).load()
    runtime_config.load_plugin_runtime_config(fresh, _Logger(), path=runtime_path)

    assert fresh.personification_qzone_monthly_limit == 17  # env.json 值保留，未回退
    info = runtime_config.get_runtime_load_info(fresh)
    assert field in info["env_json_fields"]
    assert entry.key in info["skipped_runtime_keys"]
