from __future__ import annotations

import json
import logging

from ._loader import load_personification_module

provider_router = load_personification_module("plugin.personification.core.provider_router")
env_writer = load_personification_module("plugin.personification.core.env_writer")
runtime_config = load_personification_module("plugin.personification.core.runtime_config")

_LOG = logging.getLogger("test_api_pool_revert")


def _pools(n: int) -> list[dict]:
    return [
        {
            "name": f"p{i}",
            "api_type": "openai",
            "api_url": "https://api.example.com/v1",
            "api_key": f"sk-{i}",
            "model": "gpt-4o",
            "priority": i,
            "enabled": True,
        }
        for i in range(n)
    ]


class _Cfg:
    def __init__(self, pools, data_dir: str) -> None:
        self.personification_api_pools = pools
        self.personification_data_dir = data_dir
        self.personification_api_type = "openai"
        self.personification_api_url = ""
        self.personification_api_key = ""
        self.personification_model = ""


def test_runtime_value_not_reverted_when_env_has_more(tmp_path, monkeypatch) -> None:
    # 内存里被显式减到 2，.env 文件里残留 3，过去会被顶回 3。
    env_file = tmp_path / ".env"
    env_file.write_text(
        "personification_api_pools=" + json.dumps(_pools(3), separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runtime_config, "_iter_env_file_candidates", lambda: [env_file])
    cfg = _Cfg(_pools(2), str(tmp_path))
    providers = provider_router.load_api_pool_config(cfg, _LOG)
    assert len(providers) == 2


def test_env_fallback_when_runtime_empty(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "personification_api_pools=" + json.dumps(_pools(3), separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runtime_config, "_iter_env_file_candidates", lambda: [env_file])
    cfg = _Cfg(None, str(tmp_path))
    providers = provider_router.load_api_pool_config(cfg, _LOG)
    assert len(providers) == 3


def test_env_fallback_ignored_when_legacy_primary_is_complete(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "personification_api_pools=" + json.dumps(_pools(3), separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runtime_config, "_iter_env_file_candidates", lambda: [env_file])
    cfg = _Cfg(None, str(tmp_path))
    cfg.personification_api_url = "https://legacy.example/v1"
    cfg.personification_api_key = "legacy-key"
    cfg.personification_model = "gpt-4o-mini"

    providers = provider_router.load_api_pool_config(cfg, _LOG)

    assert providers == []


def test_write_targets_file_already_holding_key(tmp_path, monkeypatch) -> None:
    # api_pools 在 .env 里；.env.prod 存在但无该 key。写入应就地更新 .env，
    # 而不是在 .env.prod 里新建一份导致读到旧值。
    env_prod = tmp_path / ".env.prod"
    env_plain = tmp_path / ".env"
    env_prod.write_text("OTHER=1\n", encoding="utf-8")
    env_plain.write_text(
        "personification_api_pools=" + json.dumps(_pools(3), separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    # 候选顺序：.env.prod 在前（与真实一致），但含 key 的是 .env
    monkeypatch.setattr(env_writer, "_iter_env_file_candidates", lambda: [env_prod, env_plain])
    monkeypatch.setattr(runtime_config, "_iter_env_file_candidates", lambda: [env_prod, env_plain])

    target = env_writer.write_dotenv("personification_api_pools", _pools(2))
    assert target == env_plain
    assert "OTHER=1" in env_prod.read_text(encoding="utf-8")
    assert "personification_api_pools" not in env_prod.read_text(encoding="utf-8")
    # 读回来应是新写入的 2 个
    raw = runtime_config.read_env_file_value("personification_api_pools")
    assert len(provider_router.parse_api_pool_config(raw, _LOG)) == 2
