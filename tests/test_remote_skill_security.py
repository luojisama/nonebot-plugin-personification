from __future__ import annotations

import asyncio
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from ._loader import load_personification_module


data_store = load_personification_module("plugin.personification.core.data_store")
remote_skill_review = load_personification_module("plugin.personification.core.remote_skill_review")
runtime_commands = load_personification_module("plugin.personification.handlers.runtime_commands")
source_resolver = load_personification_module("plugin.personification.skill_runtime.source_resolver")
custom_loader = load_personification_module("plugin.personification.skill_runtime.custom_loader")
skill_isolation = load_personification_module("plugin.personification.skill_runtime.skill_isolation")
tool_registry = load_personification_module("plugin.personification.agent.tool_registry")
mcp_compat = load_personification_module("plugin.personification.skill_runtime.mcp_compat")


class _Logger:
    def info(self, *_args, **_kwargs) -> None:  # noqa: ANN002, ANN003
        return None

    def warning(self, *_args, **_kwargs) -> None:  # noqa: ANN002, ANN003
        return None


def _config(tmp_path: Path, source_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(
        personification_data_dir=str(tmp_path),
        personification_skill_cache_dir=str(tmp_path / "skill-cache"),
        personification_skill_update_interval=3600,
        personification_skill_sources=[
            {
                "name": "reviewed-skill",
                "source": str(source_dir),
                "kind": "dir",
                "enabled": True,
            }
        ],
        personification_skill_remote_enabled=True,
        personification_skill_require_admin_review=True,
        personification_skill_allow_unsafe_external=False,
    )


def test_remote_skill_approval_is_bound_to_current_content_digest(tmp_path: Path) -> None:
    source_dir = tmp_path / "remote-skill"
    source_dir.mkdir()
    (source_dir / "skill.yaml").write_text("name: reviewed_skill\n", encoding="utf-8")
    handler = source_dir / "handler.py"
    handler.write_text("def run():\n    return 'v1'\n", encoding="utf-8")
    cfg = _config(tmp_path, source_dir)
    data_store.init_data_store(cfg)
    logger = _Logger()

    prepared_v1 = asyncio.run(remote_skill_review.prepare_remote_skill_reviews(
        cfg.personification_skill_sources,
        cfg,
        logger,
        data_dir=tmp_path,
    ))
    digest_v1 = prepared_v1[0]["content_digest"]
    matched, approved_items = remote_skill_review.review_remote_skill_sources(
        prepared_v1,
        logger,
        selector="pending",
        status="approved",
        operator="10001",
    )
    approved, pending = remote_skill_review.filter_approved_remote_sources(
        prepared_v1,
        logger,
        require_confirmation=True,
    )

    assert matched == 1
    assert approved_items[0]["approved_digest"] == digest_v1
    assert len(approved) == 1
    assert pending == []

    handler.write_text("def run():\n    return 'v2'\n", encoding="utf-8")
    prepared_v2 = asyncio.run(remote_skill_review.prepare_remote_skill_reviews(
        cfg.personification_skill_sources,
        cfg,
        logger,
        data_dir=tmp_path,
    ))
    approved_after_change, pending_after_change = remote_skill_review.filter_approved_remote_sources(
        prepared_v2,
        logger,
        require_confirmation=True,
    )

    assert prepared_v2[0]["content_digest"] != digest_v1
    assert approved_after_change == []
    assert pending_after_change[0]["status"] == "pending"
    assert pending_after_change[0]["approved_digest"] == digest_v1


def test_remote_skill_cannot_be_approved_before_content_is_prepared(tmp_path: Path) -> None:
    source_dir = tmp_path / "remote-skill"
    source_dir.mkdir()
    cfg = _config(tmp_path, source_dir)
    data_store.init_data_store(cfg)

    matched, items = remote_skill_review.review_remote_skill_sources(
        cfg.personification_skill_sources,
        _Logger(),
        selector="pending",
        status="approved",
        operator="10001",
    )

    assert matched == 0
    assert items == []


def test_qq_remote_skill_install_keeps_unsafe_external_disabled(tmp_path: Path) -> None:
    cfg = _config(tmp_path, tmp_path / "remote-skill")
    cfg.personification_skill_sources = []
    cfg.personification_skill_remote_enabled = False
    cfg.personification_skill_require_admin_review = False
    saved: list[bool] = []

    changed, message = runtime_commands.install_remote_skill_source(
        entry={
            "name": "reviewed-skill",
            "source": "https://github.com/example/reviewed-skill",
            "ref": "main",
            "enabled": True,
        },
        plugin_config=cfg,
        save_plugin_runtime_config=lambda: saved.append(True),
        logger=_Logger(),
        operator_user_id="10001",
        auto_approve=True,
    )

    assert changed is True
    assert saved == [True]
    assert cfg.personification_skill_remote_enabled is True
    assert cfg.personification_skill_require_admin_review is True
    assert cfg.personification_skill_allow_unsafe_external is False
    assert "不会跳过 content digest 审核" in message


def test_resolved_skill_uses_content_addressed_snapshot(tmp_path: Path) -> None:
    source_dir = tmp_path / "remote-skill"
    source_dir.mkdir()
    handler = source_dir / "handler.py"
    handler.write_text("def run():\n    return 'v1'\n", encoding="utf-8")
    (source_dir / "ignored.pyc").write_bytes(b"unapproved-bytecode")
    (source_dir / "__pycache__").mkdir()
    (source_dir / "__pycache__" / "handler.pyc").write_bytes(b"cached-bytecode")
    cfg = _config(tmp_path, source_dir)

    first = asyncio.run(source_resolver.resolve_skill_sources(
        plugin_config=cfg,
        logger=_Logger(),
        cache_dir=tmp_path / "skill-cache",
    ))[0]
    handler.write_text("def run():\n    return 'v2'\n", encoding="utf-8")
    second = asyncio.run(source_resolver.resolve_skill_sources(
        plugin_config=cfg,
        logger=_Logger(),
        cache_dir=tmp_path / "skill-cache",
    ))[0]

    assert first.root != source_dir
    assert second.root != first.root
    assert first.root.name == first.content_digest
    assert second.root.name == second.content_digest
    assert "v1" in (first.root / "handler.py").read_text(encoding="utf-8")
    assert "v2" in (second.root / "handler.py").read_text(encoding="utf-8")
    assert not (first.root / "ignored.pyc").exists()
    assert not (first.root / "__pycache__").exists()


def test_remote_skill_entrypoint_and_python_paths_cannot_escape_digest_root(tmp_path: Path) -> None:
    approved_root = tmp_path / "approved"
    skill_dir = approved_root / "skill"
    skill_dir.mkdir(parents=True)
    outside = tmp_path / "outside.py"
    outside.write_text("def run():\n    return 'outside'\n", encoding="utf-8")

    resolved = custom_loader._resolve_script_path(
        skill_dir,
        {"entrypoint": "../../outside.py"},
        allowed_root=approved_root,
    )

    assert resolved is None
    with pytest.raises(ValueError, match="escapes approved root"):
        asyncio.run(skill_isolation.run_skill_in_subprocess(
            script_path=outside,
            function_name="run",
            kwargs={},
            skill_dir=skill_dir,
            container_root=approved_root,
            python_paths=[str(tmp_path)],
            isolation={"mode": "process"},
        ))


def test_process_skill_rechecks_approved_digest_before_each_call(tmp_path: Path) -> None:
    approved_root = tmp_path / "approved"
    skill_dir = approved_root / "skill"
    skill_dir.mkdir(parents=True)
    script = skill_dir / "main.py"
    script.write_text("def run():\n    return 'approved'\n", encoding="utf-8")
    digest = source_resolver.skill_source_content_digest(approved_root)
    registry = tool_registry.ToolRegistry()

    asyncio.run(custom_loader._register_skill(
        registry,
        _Logger(),
        skill_dir,
        {
            "name": "digest_guarded",
            "description": "digest guarded",
            "parameters": {"type": "object", "properties": {}},
            "script_path": script,
            "runtime": None,
            "container_root": approved_root,
            "trusted": False,
            "source_kind": "remote",
            "plugin_config": SimpleNamespace(personification_skill_allow_unsafe_external=False),
            "isolation": {"mode": "process", "timeout": 5},
            "content_digest": digest,
        },
    ))
    script.write_text("def run():\n    return 'changed'\n", encoding="utf-8")

    tool = registry.get("digest_guarded")
    assert tool is not None
    assert tool.metadata["source_kind"] == "remote"
    result = asyncio.run(tool.handler())
    assert result == "isolated skill blocked: approved content digest changed"


def test_build_tools_entrypoint_gets_authoritative_source_kind(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    skill_dir = tmp_path / "local-skill"
    skill_dir.mkdir()
    script = skill_dir / "main.py"
    script.write_text("# loaded through a fake module\n", encoding="utf-8")

    async def _noop(**_kwargs):  # noqa: ANN001
        return "ok"

    module = SimpleNamespace(
        build_tools=lambda _runtime: [
            tool_registry.AgentTool(
                name="web_search",
                description="same-name local override",
                parameters={"type": "object", "properties": {}},
                handler=_noop,
                metadata={"source_kind": "bundled"},
            )
        ]
    )
    monkeypatch.setattr(custom_loader, "_load_handler_module", lambda *_args, **_kwargs: module)
    registry = tool_registry.ToolRegistry()

    asyncio.run(custom_loader._register_skill(
        registry,
        _Logger(),
        skill_dir,
        {
            "name": "local-skill",
            "script_path": script,
            "runtime": SimpleNamespace(),
            "trusted": True,
            "source_kind": "local",
            "plugin_config": SimpleNamespace(personification_skill_allow_unsafe_external=True),
            "isolation": {"mode": "inprocess"},
        },
    ))

    tool = registry.get("web_search")
    assert tool is not None
    assert tool.metadata["source_kind"] == "local"


def test_failed_register_entrypoint_still_stamps_partial_tool_source(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    skill_dir = tmp_path / "partial-skill"
    skill_dir.mkdir()
    script = skill_dir / "main.py"
    script.write_text("# loaded through a fake module\n", encoding="utf-8")

    async def _noop(**_kwargs):  # noqa: ANN001
        return "ok"

    def _register(_runtime, registry):  # noqa: ANN001
        registry.register(
            tool_registry.AgentTool(
                name="web_search",
                description="partial same-name override",
                parameters={"type": "object", "properties": {}},
                handler=_noop,
                metadata={"source_kind": "bundled"},
            )
        )
        raise RuntimeError("register failed after partial mutation")

    monkeypatch.setattr(
        custom_loader,
        "_load_handler_module",
        lambda *_args, **_kwargs: SimpleNamespace(register=_register),
    )
    registry = tool_registry.ToolRegistry()

    asyncio.run(custom_loader._register_skill(
        registry,
        _Logger(),
        skill_dir,
        {
            "name": "partial-skill",
            "script_path": script,
            "runtime": SimpleNamespace(),
            "trusted": True,
            "source_kind": "local",
            "plugin_config": SimpleNamespace(personification_skill_allow_unsafe_external=True),
            "isolation": {"mode": "inprocess"},
        },
    ))

    tool = registry.get("web_search")
    assert tool is not None
    assert tool.metadata["source_kind"] == "local"


def test_register_entrypoint_in_place_mutation_loses_builtin_provenance(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    skill_dir = tmp_path / "mutating-skill"
    skill_dir.mkdir()
    script = skill_dir / "main.py"
    script.write_text("# loaded through a fake module\n", encoding="utf-8")

    async def _builtin_handler(**_kwargs):  # noqa: ANN001
        return "builtin"

    async def _mutated_handler(**_kwargs):  # noqa: ANN001
        return "mutated"

    def _register(_runtime, registry):  # noqa: ANN001
        existing = registry.get("web_search")
        existing.handler = _mutated_handler
        existing.description = "mutated in place"

    monkeypatch.setattr(
        custom_loader,
        "_load_handler_module",
        lambda *_args, **_kwargs: SimpleNamespace(register=_register),
    )
    registry = tool_registry.ToolRegistry()
    registry.register(
        tool_registry.AgentTool(
            name="web_search",
            description="builtin",
            parameters={"type": "object", "properties": {}},
            handler=_builtin_handler,
            metadata={"source_kind": "builtin"},
        )
    )

    asyncio.run(custom_loader._register_skill(
        registry,
        _Logger(),
        skill_dir,
        {
            "name": "mutating-skill",
            "script_path": script,
            "runtime": SimpleNamespace(),
            "trusted": True,
            "source_kind": "local",
            "plugin_config": SimpleNamespace(personification_skill_allow_unsafe_external=True),
            "isolation": {"mode": "inprocess"},
        },
    ))

    tool = registry.get("web_search")
    assert tool is not None
    assert tool.metadata["source_kind"] == "local"
    assert asyncio.run(tool.handler()) == "mutated"


def test_remote_skill_approval_uses_digest_compare_and_set(tmp_path: Path) -> None:
    source_dir = tmp_path / "remote-skill"
    source_dir.mkdir()
    (source_dir / "handler.py").write_text("def run():\n    return 'v1'\n", encoding="utf-8")
    cfg = _config(tmp_path, source_dir)
    data_store.init_data_store(cfg)
    logger = _Logger()
    prepared = asyncio.run(remote_skill_review.prepare_remote_skill_reviews(
        cfg.personification_skill_sources,
        cfg,
        logger,
        data_dir=tmp_path,
    ))
    stale_item = remote_skill_review.list_remote_skill_reviews(prepared, logger)[0]
    store = data_store.get_data_store()

    def _change_digest(current):  # noqa: ANN001
        payload = dict(current or {})
        items = dict(payload.get("items") or {})
        item = dict(items[stale_item["key"]])
        item["content_digest"] = "f" * 64
        items[stale_item["key"]] = item
        payload["items"] = items
        return payload

    store.mutate_sync("remote_skill_reviews", _change_digest)
    matched, items = remote_skill_review.review_remote_skill_sources(
        prepared,
        logger,
        selector=stale_item["key"],
        status="approved",
        operator="10001",
    )

    assert matched == 0
    assert items == []
    stored = store.load_sync("remote_skill_reviews")
    assert stored["items"][stale_item["key"]]["content_digest"] == "f" * 64


def test_github_tree_source_identity_keeps_prepared_digest(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    extracted = tmp_path / "extracted" / "repo" / "skills" / "demo"
    extracted.mkdir(parents=True)
    (extracted / "handler.py").write_text("def run():\n    return 'ok'\n", encoding="utf-8")
    source = {
        "name": "tree-source",
        "source": "https://github.com/example/repo/tree/main/skills/demo",
        "enabled": True,
    }
    cfg = SimpleNamespace(
        personification_data_dir=str(tmp_path),
        personification_skill_sources=[source],
        personification_skill_cache_dir="",
        personification_skill_update_interval=3600,
    )
    data_store.init_data_store(cfg)

    async def _prepare_source_dir(*, source, **_kwargs):  # noqa: ANN001
        assert source["ref"] == "main"
        assert source["subdir"] == "skills/demo"
        return extracted

    monkeypatch.setattr(source_resolver, "_prepare_source_dir", _prepare_source_dir)
    prepared = asyncio.run(remote_skill_review.prepare_remote_skill_reviews(
        [source],
        cfg,
        _Logger(),
        data_dir=tmp_path,
    ))

    assert prepared[0]["ref"] == "main"
    assert prepared[0]["subdir"] == "skills/demo"
    assert len(prepared[0]["content_digest"]) == 64
    reviews = remote_skill_review.list_remote_skill_reviews(prepared, _Logger())
    assert reviews[0]["content_digest"] == prepared[0]["content_digest"]

    lookalike = source_resolver.parse_skill_sources(
        [{"source": "https://github.com.evil.example/owner/repo/tree/main/skills/demo"}],
        _Logger(),
    )[0]
    assert lookalike["ref"] == ""
    assert lookalike["subdir"] == ""
    assert source_resolver._normalize_remote_url(lookalike["source"], lookalike) is None

    explicit_port = source_resolver.parse_skill_sources(
        [{"source": "https://github.com:443/owner/repo/tree/main/skills/demo"}],
        _Logger(),
    )[0]
    assert explicit_port["ref"] == "main"
    assert explicit_port["subdir"] == "skills/demo"
    assert source_resolver._normalize_remote_url(explicit_port["source"], explicit_port) == (
        "https://codeload.github.com/owner/repo/zip/main"
    )


def test_expired_remote_source_fetch_failure_does_not_load_old_cache(
    tmp_path: Path, monkeypatch  # noqa: ANN001
) -> None:
    source = {
        "name": "remote",
        "source": "https://github.com/example/remote",
        "ref": "main",
        "subdir": "",
        "kind": "auto",
        "enabled": True,
    }
    cache_dir = tmp_path / "skill-cache"
    source_cache = cache_dir / source_resolver._source_cache_name(source)
    extracted = source_cache / "extracted"
    extracted.mkdir(parents=True)
    (extracted / "handler.py").write_text("def run():\n    return 'old'\n", encoding="utf-8")
    (source_cache / "package.zip").write_bytes(b"old")
    (source_cache / "manifest.json").write_text(
        json.dumps({"fetched_at": 1}),
        encoding="utf-8",
    )

    async def _download_failed(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("network unavailable")

    monkeypatch.setattr(source_resolver, "_resolve_remote_download_url", lambda url: asyncio.sleep(0, result=url))
    monkeypatch.setattr(source_resolver, "_download_zip", _download_failed)

    resolved = asyncio.run(source_resolver._prepare_source_dir(
        source=source,
        cache_dir=cache_dir,
        logger=_Logger(),
        update_interval=1,
    ))

    assert resolved is None


def test_remote_mcp_paths_and_digest_are_bound_to_approved_root(tmp_path: Path) -> None:
    approved_root = tmp_path / "approved"
    skill_dir = approved_root / "skill"
    skill_dir.mkdir(parents=True)
    server = skill_dir / "server.py"
    server.write_text("print('server')\n", encoding="utf-8")
    digest = source_resolver.skill_source_content_digest(approved_root)

    escaped = mcp_compat.normalize_mcp_config(
        skill_dir=skill_dir,
        raw={"command": sys.executable, "args": ["../../outside.py"], "cwd": "../../"},
        allowed_root=approved_root,
        restrict_paths=True,
    )
    normalized = mcp_compat.normalize_mcp_config(
        skill_dir=skill_dir,
        raw={"command": "python", "args": ["server.py"], "cwd": "."},
        allowed_root=approved_root,
        restrict_paths=True,
    )

    assert escaped is None
    assert normalized is not None
    assert normalized["args"] == [str(server.resolve())]

    mcp_compat.register_mcp_endpoint(
        registered_name="digest_guarded_mcp",
        remote_name="run",
        command="python",
        args=[str(server.resolve())],
        env={},
        cwd=str(skill_dir.resolve()),
        timeout=5,
        approved_root=str(approved_root),
        content_digest=digest,
    )
    try:
        server.write_text("print('changed')\n", encoding="utf-8")
        with pytest.raises(mcp_compat.McpProtocolError, match="digest changed"):
            asyncio.run(mcp_compat.call_registered_mcp_tool("digest_guarded_mcp", {}))
    finally:
        mcp_compat._REGISTERED_MCP_TOOLS.pop("digest_guarded_mcp", None)


def test_remote_mcp_production_registration_preserves_digest_guard(tmp_path: Path) -> None:
    approved_root = tmp_path / "approved"
    skill_dir = approved_root / "skill"
    skill_dir.mkdir(parents=True)
    server = skill_dir / "server.py"
    fixture = Path(__file__).resolve().parent / "fixtures" / "fake_mcp_server.py"
    server.write_bytes(fixture.read_bytes())
    digest = source_resolver.skill_source_content_digest(approved_root)
    registry = tool_registry.ToolRegistry()
    config = {
        "command": sys.executable,
        "args": [str(server)],
        "env": {},
        "cwd": str(skill_dir),
        "timeout": 5,
    }

    count = asyncio.run(mcp_compat.register_mcp_tools(
        registry=registry,
        logger=_Logger(),
        skill_dir=skill_dir,
        base_name="remote_mcp",
        config=config,
        approved_root=approved_root,
        content_digest=digest,
    ))
    try:
        assert count == 2
        endpoint = mcp_compat.get_registered_mcp_endpoint("read_demo")
        assert endpoint is not None
        assert endpoint["approved_root"] == str(approved_root)
        assert endpoint["content_digest"] == digest

        server.write_text("print('changed')\n", encoding="utf-8")
        tool = registry.get("read_demo")
        assert tool is not None
        with pytest.raises(mcp_compat.McpProtocolError, match="digest changed"):
            asyncio.run(tool.handler(query="demo"))
    finally:
        mcp_compat._REGISTERED_MCP_TOOLS.pop("read_demo", None)
        mcp_compat._REGISTERED_MCP_TOOLS.pop("write_demo", None)


def test_data_store_mutation_is_atomic_across_threads(tmp_path: Path) -> None:
    cfg = SimpleNamespace(personification_data_dir=str(tmp_path))
    store = data_store.init_data_store(cfg)
    store.save_sync("atomic-counter", {"value": 0})

    def _increment() -> None:
        store.mutate_sync(
            "atomic-counter",
            lambda current: {"value": int((current or {}).get("value", 0)) + 1},
        )

    with ThreadPoolExecutor(max_workers=6) as executor:
        list(executor.map(lambda _index: _increment(), range(30)))

    assert store.load_sync("atomic-counter") == {"value": 30}
