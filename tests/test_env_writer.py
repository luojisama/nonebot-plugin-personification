from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from dotenv import dotenv_values

from ._loader import load_personification_module

env_writer = load_personification_module("plugin.personification.core.env_writer")
runtime_config = load_personification_module("plugin.personification.core.runtime_config")
qzone_service = load_personification_module("plugin.personification.core.qzone_service")


def _make_plugin_config(tmp_path: Path, **extra) -> SimpleNamespace:
    cfg = SimpleNamespace(personification_data_dir=str(tmp_path), **extra)
    return cfg


def test_serialize_value_handles_basic_types() -> None:
    s = env_writer._serialize_value
    assert s(True) == "true"
    assert s(False) == "false"
    assert s(42) == "42"
    assert s(0.5) == "0.5"
    assert s(None) == ""
    assert s("hello") == "hello"
    assert s({"a": 1}) == '{"a":1}'
    assert s([1, 2]) == "[1,2]"


def test_write_dotenv_updates_existing_key_and_creates_backup(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.prod"
    env_file.write_text(
        "# 顶部注释\n"
        "personification_probability=0.3\n"
        "personification_global_enabled=true\n"
        "# 中间注释\n"
        "personification_agent_max_steps=5\n",
        encoding="utf-8",
    )
    result = env_writer.write_dotenv("personification_probability", 0.5, target=env_file)
    assert result == env_file
    text = env_file.read_text(encoding="utf-8")
    parsed = dotenv_values(str(env_file))
    assert parsed["personification_probability"] == "0.5"
    assert parsed["personification_global_enabled"] == "true"
    assert parsed["personification_agent_max_steps"] == "5"
    assert "# 顶部注释" in text
    assert "# 中间注释" in text
    backups = list(tmp_path.glob(".env.prod.bak.*"))
    assert backups, "backup file should exist after write"


def test_write_dotenv_restricts_source_and_backup_permissions(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / ".env.prod"
    env_file.write_text("personification_qzone_cookie=old\n", encoding="utf-8")
    restricted: list[Path] = []

    monkeypatch.setattr(
        env_writer,
        "_restrict_sensitive_file_permissions",
        lambda path: restricted.append(Path(path)),
    )

    env_writer.write_dotenv("personification_qzone_cookie", "new-cookie", target=env_file)

    backups = list(tmp_path.glob(".env.prod.bak.*"))
    assert env_file in restricted
    assert backups and backups[0] in restricted


def test_qzone_cookie_persist_restricts_env_permissions(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / ".env.prod"
    env_file.write_text("personification_qzone_cookie=old\n", encoding="utf-8")
    restricted: list[Path] = []

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        qzone_service,
        "_restrict_sensitive_file_permissions",
        lambda path: restricted.append(Path(path).resolve()),
    )

    qzone_service._persist_cookie_to_env(
        "uin=o10001; p_skey=secret;",
        SimpleNamespace(error=lambda *_a, **_k: None),
    )

    assert env_file.resolve() in restricted


def test_write_dotenv_appends_when_key_missing(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.prod"
    env_file.write_text("personification_global_enabled=true\n", encoding="utf-8")
    env_writer.write_dotenv("personification_probability", 0.42, target=env_file, backup=False)
    parsed = dotenv_values(str(env_file))
    assert parsed["personification_probability"] == "0.42"
    assert parsed["personification_global_enabled"] == "true"


def test_write_dotenv_skips_when_value_unchanged(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.prod"
    env_file.write_text(
        "# 顶部注释\n"
        "personification_probability=0.5\n"
        "personification_global_enabled=true\n",
        encoding="utf-8",
    )
    original_mtime = env_file.stat().st_mtime_ns
    # 写入完全一致的值：不应产生备份，也不应改写文件
    result = env_writer.write_dotenv("personification_probability", 0.5, target=env_file)
    assert result == env_file
    backups = list(tmp_path.glob(".env.prod.bak.*"))
    assert backups == [], "未变化时不应产生备份文件"
    assert env_file.stat().st_mtime_ns == original_mtime, "未变化时不应改写源文件"
    # 值实际变化后才会备份
    result = env_writer.write_dotenv("personification_probability", 0.7, target=env_file)
    assert result == env_file
    backups = list(tmp_path.glob(".env.prod.bak.*"))
    assert len(backups) == 1, "值变化后应产生 1 份备份"
    parsed = dotenv_values(str(env_file))
    assert parsed["personification_probability"] == "0.7"


def test_write_env_json_creates_and_merges(tmp_path: Path) -> None:
    cfg = _make_plugin_config(tmp_path)
    path = env_writer.write_env_json("personification_probability", 0.5, cfg)
    assert Path(path).exists()
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["personification_probability"] == 0.5

    env_writer.write_env_json("personification_agent_max_steps", 7, cfg)
    data2 = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data2["personification_probability"] == 0.5
    assert data2["personification_agent_max_steps"] == 7


def test_write_both_keeps_env_file_read_only_when_key_exists(tmp_path: Path) -> None:
    # WebUI 保存只写 env.json；.env.prod 中已有同名字段也不再被改写。
    env_file = tmp_path / ".env.prod"
    env_file.write_text("# header\npersonification_probability=0.3\n", encoding="utf-8")
    cfg = _make_plugin_config(tmp_path)
    original = env_writer._resolve_dotenv_target
    env_writer._resolve_dotenv_target = lambda field_name="": env_file
    try:
        result = env_writer.write_both("personification_probability", 0.6, cfg)
    finally:
        env_writer._resolve_dotenv_target = original
    assert result["errors"] == []
    assert result["dotenv_path"] is None
    assert dotenv_values(str(env_file))["personification_probability"] == "0.3"
    assert list(tmp_path.glob(".env.prod.bak.*")) == []
    json_data = json.loads(Path(result["env_json_path"]).read_text(encoding="utf-8"))
    assert json_data["personification_probability"] == 0.6


def test_write_both_new_field_goes_env_json_only(tmp_path: Path) -> None:
    # 字段不在 .env 中 → 不新增到 .env（避免重启回退），只写 env.json
    env_file = tmp_path / ".env.prod"
    env_file.write_text("# header\n", encoding="utf-8")
    cfg = _make_plugin_config(tmp_path)
    original = env_writer._resolve_dotenv_target
    env_writer._resolve_dotenv_target = lambda field_name="": env_file
    try:
        result = env_writer.write_both("personification_probability", 0.6, cfg)
    finally:
        env_writer._resolve_dotenv_target = original
    assert result["errors"] == []
    assert result["dotenv_path"] is None
    assert "personification_probability" not in dotenv_values(str(env_file))
    json_data = json.loads(Path(result["env_json_path"]).read_text(encoding="utf-8"))
    assert json_data["personification_probability"] == 0.6


def test_env_candidates_prefer_project_root_over_plugin_local_env(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / "bot"
    plugin_root = project_root / "plugin" / "personification"
    plugin_root.mkdir(parents=True)
    project_env = project_root / ".env.prod"
    plugin_env = plugin_root / ".env.prod"
    project_env.write_text("personification_probability=0.3\n", encoding="utf-8")
    plugin_env.write_text("personification_probability=0.8\n", encoding="utf-8")

    monkeypatch.setattr(runtime_config, "_get_plugin_root", lambda: plugin_root)
    monkeypatch.chdir(plugin_root)

    candidates = runtime_config._iter_env_file_candidates()

    assert candidates[0] == project_env
    assert plugin_env not in candidates
    assert runtime_config.read_env_file_value("personification_probability") == "0.3"


def test_write_both_writes_env_json_without_touching_project_or_plugin_env(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / "bot"
    plugin_root = project_root / "plugin" / "personification"
    data_dir = tmp_path / "data"
    plugin_root.mkdir(parents=True)
    project_env = project_root / ".env.prod"
    plugin_env = plugin_root / ".env.prod"
    old_value = [{"name": "old_root", "api_type": "openai", "model": "old"}]
    stale_value = [{"name": "stale_plugin", "api_type": "openai", "model": "stale"}]
    new_value = [{"name": "new_root", "api_type": "openai", "model": "new"}]
    project_env.write_text(
        "personification_api_pools=" + json.dumps(old_value, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    plugin_env.write_text(
        "personification_api_pools=" + json.dumps(stale_value, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(runtime_config, "_get_plugin_root", lambda: plugin_root)
    monkeypatch.chdir(plugin_root)
    cfg = _make_plugin_config(data_dir)

    result = env_writer.write_both("personification_api_pools", new_value, cfg)

    assert result["errors"] == []
    assert result["dotenv_path"] is None
    assert dotenv_values(str(project_env))["personification_api_pools"] == json.dumps(
        old_value,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    assert dotenv_values(str(plugin_env))["personification_api_pools"] == json.dumps(
        stale_value,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    assert json.loads(Path(result["env_json_path"]).read_text(encoding="utf-8"))[
        "personification_api_pools"
    ] == new_value
    assert json.loads(runtime_config.read_env_file_value("personification_api_pools")) == old_value
    assert list(project_root.glob(".env.prod.bak.*")) == []


def test_write_both_ignores_plugin_local_env_when_project_env_exists(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / "bot"
    plugin_root = project_root / "plugin" / "personification"
    data_dir = tmp_path / "data"
    plugin_root.mkdir(parents=True)
    project_env = project_root / ".env.prod"
    plugin_env = plugin_root / ".env.prod"
    project_env.write_text("OTHER=1\n", encoding="utf-8")
    plugin_env.write_text("personification_probability=0.8\n", encoding="utf-8")

    monkeypatch.setattr(runtime_config, "_get_plugin_root", lambda: plugin_root)
    monkeypatch.chdir(plugin_root)
    cfg = _make_plugin_config(data_dir)

    result = env_writer.write_both("personification_probability", 0.6, cfg)

    assert result["errors"] == []
    assert result["dotenv_path"] is None
    assert "personification_probability" not in dotenv_values(str(project_env))
    assert dotenv_values(str(plugin_env))["personification_probability"] == "0.8"
    assert json.loads(Path(result["env_json_path"]).read_text(encoding="utf-8"))[
        "personification_probability"
    ] == 0.6


def test_resolve_value_sources_reports_env_json_when_no_env_file(tmp_path: Path, monkeypatch) -> None:
    cfg = _make_plugin_config(tmp_path, personification_probability=0.5)
    # 屏蔽真实 .env 扫描
    monkeypatch.setattr(env_writer, "read_env_file_value", lambda _key: "")
    env_writer.write_env_json("personification_probability", 0.5, cfg)
    sources = env_writer.resolve_value_sources("personification_probability", cfg)
    assert sources["env_file"] is None
    assert sources["env_json"] == 0.5
    assert sources["active_source"] == "env_json"
    assert sources["current"] == 0.5


def test_resolve_value_sources_env_json_wins_over_env_file(tmp_path: Path, monkeypatch) -> None:
    cfg = _make_plugin_config(tmp_path, personification_probability=0.7)
    monkeypatch.setattr(env_writer, "read_env_file_value", lambda key: "0.7" if key == "personification_probability" else "")
    env_writer.write_env_json("personification_probability", 0.3, cfg)
    sources = env_writer.resolve_value_sources("personification_probability", cfg)
    assert sources["env_file"] == "0.7"
    assert sources["env_json"] == 0.3
    assert sources["active_source"] == "env_json"
