from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ._loader import load_personification_module


FAKE_SERVER = Path(__file__).resolve().parent / "fixtures" / "fake_mcp_server.py"


def _init_store(tmp_path: Path, monkeypatch) -> None:
    paths = load_personification_module("plugin.personification.core.paths")
    data_store = load_personification_module("plugin.personification.core.data_store")
    management = load_personification_module("plugin.personification.core.mcp_management")
    monkeypatch.setattr(paths, "get_data_dir", lambda _cfg=None: tmp_path)
    monkeypatch.setattr(management, "get_data_dir", lambda _cfg=None: tmp_path)
    data_store.init_data_store(SimpleNamespace(personification_data_dir=str(tmp_path)))


def test_stdio_client_uses_json_lines_and_paginates() -> None:
    compat = load_personification_module("plugin.personification.skill_runtime.mcp_compat")

    async def run():
        async with compat.McpStdioClient(
            command=sys.executable,
            args=[str(FAKE_SERVER)],
            env=dict(),
            cwd=str(FAKE_SERVER.parent),
            timeout=5,
        ) as client:
            tools = await client.list_tools()
            result = await client.call_tool("read_demo", {"query": "hello"})
            return client.protocol_version, client.server_info, client.capabilities, tools, result

    version, server_info, capabilities, tools, result = asyncio.run(run())
    assert version == "2025-11-25"
    assert server_info["name"] == "fake-mcp"
    assert capabilities["tools"]["listChanged"] is True
    assert [tool["name"] for tool in tools] == ["read_demo", "write_demo"]
    assert tools[0]["title"] == "Read Demo"
    assert tools[0]["outputSchema"]["type"] == "object"
    assert "called:read_demo" in result


def test_stdio_client_accepts_explicit_2025_06_18_compatibility() -> None:
    compat = load_personification_module("plugin.personification.skill_runtime.mcp_compat")

    async def run() -> str:
        async with compat.McpStdioClient(
            command=sys.executable,
            args=[str(FAKE_SERVER)],
            env={"FAKE_MCP_PROTOCOL": "2025-06-18"},
            cwd=str(FAKE_SERVER.parent),
            timeout=5,
        ) as client:
            return client.protocol_version

    assert asyncio.run(run()) == "2025-06-18"


@pytest.mark.parametrize(
    "result",
    [
        {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}},
        {"protocolVersion": "2025-11-25", "capabilities": {}},
    ],
)
def test_stdio_client_rejects_unsupported_negotiation_or_missing_tools(result: dict) -> None:
    compat = load_personification_module("plugin.personification.skill_runtime.mcp_compat")
    client = compat.McpStdioClient(command="unused", args=[], env={}, cwd=None, timeout=1)

    async def fake_request(_method, _params):
        return result

    async def fake_notify(_method, _params):
        return None

    client.request = fake_request
    client.notify = fake_notify
    with pytest.raises(compat.McpProtocolError):
        asyncio.run(client.initialize())


def test_stdio_tools_list_preserves_opaque_cursor_and_rejects_unterminated_pages(monkeypatch) -> None:
    compat = load_personification_module("plugin.personification.skill_runtime.mcp_compat")
    client = compat.McpStdioClient(command="unused", args=[], env={}, cwd=None, timeout=1)
    cursors: list[dict] = []

    async def opaque_request(_method, params):
        cursors.append(dict(params))
        if not params:
            return {"tools": [], "nextCursor": " opaque+/= cursor "}
        return {"tools": []}

    client.request = opaque_request
    assert asyncio.run(client.list_tools()) == []
    assert cursors == [{}, {"cursor": " opaque+/= cursor "}]

    monkeypatch.setattr(compat, "MAX_TOOLS_LIST_PAGES", 2)
    calls = 0

    async def endless_request(_method, _params):
        nonlocal calls
        calls += 1
        return {"tools": [], "nextCursor": f"page-{calls}"}

    client.request = endless_request
    with pytest.raises(compat.McpProtocolError, match="page limit"):
        asyncio.run(client.list_tools())


def test_stdio_tools_list_rejects_tool_limit(monkeypatch) -> None:
    compat = load_personification_module("plugin.personification.skill_runtime.mcp_compat")
    client = compat.McpStdioClient(command="unused", args=[], env={}, cwd=None, timeout=1)
    monkeypatch.setattr(compat, "MAX_TOOLS", 1)

    async def fake_request(_method, _params):
        return {"tools": [{"name": "one"}, {"name": "two"}]}

    client.request = fake_request
    with pytest.raises(compat.McpProtocolError, match="too many tools"):
        asyncio.run(client.list_tools())


def test_build_launch_plan_pins_npm_and_separates_secret(monkeypatch) -> None:
    management = load_personification_module("plugin.personification.core.mcp_management")
    monkeypatch.setattr(management.shutil, "which", lambda name: f"/bin/{name}" if name == "npx" else None)
    plan = management.build_launch_plan(
        {
            "registryType": "npm",
            "identifier": "@demo/server",
            "version": "1.2.3",
            "transport": {"type": "stdio"},
            "environmentVariables": [
                {"name": "TOKEN", "isRequired": True, "isSecret": True},
                {"name": "MODE", "default": "safe"},
            ],
            "packageArguments": [{"type": "named", "name": "scope", "isRequired": True}],
        },
        {"TOKEN": "secret", "scope": "public"},
    )
    assert plan["command"] == "/bin/npx"
    assert plan["args"] == ["--yes", "@demo/server@1.2.3", "--scope=public"]
    assert plan["env"] == {"MODE": "safe"}
    assert plan["secrets"] == {"TOKEN": "secret"}
    serialized_public = json.dumps({key: value for key, value in plan.items() if key != "secrets"})
    assert '"TOKEN": "secret"' not in serialized_public

    with pytest.raises(ValueError):
        management.build_launch_plan(
            {"registryType": "npm", "identifier": "demo", "version": "latest", "transport": {"type": "stdio"}},
            {},
        )


def test_build_launch_plan_rejects_secret_argv_and_unsafe_package_metadata() -> None:
    management = load_personification_module("plugin.personification.core.mcp_management")
    base = {
        "registryType": "npm",
        "identifier": "demo-server",
        "version": "1.2.3",
        "transport": {"type": "stdio"},
    }

    with pytest.raises(ValueError, match="Secret package arguments"):
        management.build_launch_plan(
            {**base, "packageArguments": [{"type": "named", "name": "--token", "isSecret": True}]},
            {"--token": "secret"},
        )
    for override in (
        {"identifier": "--help"},
        {"registryBaseUrl": "https://packages.example.com"},
        {"runtimeArguments": [{"type": "positional", "value": "--inspect"}]},
        {"version": "1.x"},
        {"version": "1.2"},
        {"version": "^1.2.3"},
        {"version": "next"},
        {"version": "01.2.3"},
    ):
        with pytest.raises(ValueError):
            management.build_launch_plan({**base, **override}, {})


def test_build_launch_plan_requires_exact_pep440_and_fails_closed_on_file_hash(monkeypatch) -> None:
    management = load_personification_module("plugin.personification.core.mcp_management")
    monkeypatch.setattr(management.shutil, "which", lambda name: "/bin/uvx" if name == "uvx" else None)
    base = {
        "registryType": "pypi",
        "identifier": "demo-server",
        "transport": {"type": "stdio"},
    }
    plan = management.build_launch_plan({**base, "version": "1.2.3rc1.post2"}, {})
    assert plan["args"] == ["--from", "demo-server==1.2.3rc1.post2", "demo-server"]

    for version in ("1", "1.2", "latest", ">=1.2.3", "stable", "v1.2.3"):
        with pytest.raises(ValueError):
            management.build_launch_plan({**base, "version": version}, {})

    with pytest.raises(ValueError, match="fileSha256"):
        management.build_launch_plan(
            {**base, "version": "1.2.3", "fileSha256": "a" * 64},
            {},
        )


def test_publisher_read_only_annotation_never_auto_enables_tool() -> None:
    management = load_personification_module("plugin.personification.core.mcp_management")
    policy = management._tool_policy(
        "install-1",
        "mcp_demo_",
        {
            "name": "read_demo",
            "title": "Read Demo",
            "description": "read",
            "inputSchema": {"type": "object", "properties": {}},
            "outputSchema": {"type": "object", "properties": {"value": {"type": "string"}}},
            "annotations": {"readOnlyHint": True},
        },
    )
    assert policy["publisher_read_only"] is True
    assert policy["title"] == "Read Demo"
    assert policy["output_schema"]["properties"]["value"]["type"] == "string"
    assert policy["annotations"] == {"readOnlyHint": True}
    assert policy["enabled"] is False
    assert policy["side_effect"] == "unknown"
    assert policy["risk_level"] == "review"


def test_secret_store_is_separate_and_deletable(tmp_path: Path) -> None:
    management = load_personification_module("plugin.personification.core.mcp_management")
    store = management.McpSecretStore(SimpleNamespace(personification_data_dir=str(tmp_path), personification_mcp_secret_file=""))
    store.set("install-1", {"TOKEN": "top-secret"})
    assert store.get("install-1") == {"TOKEN": "top-secret"}
    assert "top-secret" in store.path.read_text(encoding="utf-8")
    store.delete("install-1")
    assert store.get("install-1") == {}


def test_secret_store_fails_closed_on_corruption(tmp_path: Path) -> None:
    management = load_personification_module("plugin.personification.core.mcp_management")
    store = management.McpSecretStore(SimpleNamespace(personification_data_dir=str(tmp_path), personification_mcp_secret_file=""))
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("not-json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="unreadable"):
        store.get("install-1")
    with pytest.raises(RuntimeError, match="unreadable"):
        store.set("install-1", {"TOKEN": "must-not-overwrite"})
    assert store.path.read_text(encoding="utf-8") == "not-json"


def test_mcp_tool_schema_migrates_old_database_without_losing_authorization(tmp_path: Path) -> None:
    db = load_personification_module("plugin.personification.core.db")
    db_path = tmp_path / db.DB_FILENAME
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """CREATE TABLE mcp_tool_policies (
                installation_id TEXT NOT NULL,
                remote_name TEXT NOT NULL,
                registered_name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                parameters_json TEXT NOT NULL DEFAULT '{}',
                enabled INTEGER NOT NULL DEFAULT 0,
                risk_level TEXT NOT NULL DEFAULT 'admin',
                side_effect TEXT NOT NULL DEFAULT 'unknown',
                publisher_read_only INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL,
                PRIMARY KEY (installation_id, remote_name)
            )"""
        )
        conn.execute(
            """INSERT INTO mcp_tool_policies(
                installation_id,remote_name,registered_name,description,parameters_json,
                enabled,risk_level,side_effect,publisher_read_only,updated_at
            ) VALUES('old-install','read_demo','mcp_old_read','old','{}',1,'high','external',1,1)"""
        )
        conn.commit()

    db.init_db_sync(tmp_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(mcp_tool_policies)")}
        row = conn.execute("SELECT * FROM mcp_tool_policies WHERE installation_id='old-install'").fetchone()
    assert {"title", "output_schema_json", "annotations_json"} <= columns
    assert row is not None
    assert row["enabled"] == 1
    assert row["title"] == ""
    assert json.loads(row["output_schema_json"]) == {}


def test_store_catalog_sync_preserves_authorization_and_removes_deleted_tools(tmp_path: Path, monkeypatch) -> None:
    _init_store(tmp_path, monkeypatch)
    management = load_personification_module("plugin.personification.core.mcp_management")
    store = management.McpStore()
    item = {
        "installation_id": "install-sync",
        "source_id": "official",
        "source_url": management.OFFICIAL_MCP_REGISTRY,
        "server_name": "io.example/sync",
        "server_title": "Sync",
        "server_version": "1.0.0",
        "package_type": "npm",
        "package_identifier": "sync-server",
        "command": "npx",
        "args": ["--yes", "sync-server@1.0.0"],
        "env": {},
        "secret_names": [],
        "name_prefix": "mcp_sync_",
        "metadata": {},
        "created_by": "10001",
    }
    store.save_installation(
        item,
        [
            {"remote_name": "read_demo", "registered_name": "mcp_sync_read_demo", "description": "old", "parameters": {"type": "object"}, "enabled": True, "risk_level": "high", "side_effect": "external", "publisher_read_only": True},
            {"remote_name": "deleted_demo", "registered_name": "mcp_sync_deleted_demo", "description": "deleted", "parameters": {"type": "object"}, "enabled": True, "risk_level": "high", "side_effect": "external", "publisher_read_only": False},
        ],
    )
    result = store.sync_tools(
        "install-sync",
        "mcp_sync_",
        [
            {"name": "read_demo", "title": "Read", "description": "new", "inputSchema": {"type": "object"}, "outputSchema": {"type": "object"}, "annotations": {"readOnlyHint": True}},
            {"name": "new_demo", "title": "New", "description": "new tool", "inputSchema": {"type": "object"}, "annotations": {}},
        ],
    )
    assert result == {"added": 1, "updated": 1, "removed": 1, "total": 2}
    policies = {tool["remote_name"]: tool for tool in store.tools("install-sync")}
    assert set(policies) == {"read_demo", "new_demo"}
    assert policies["read_demo"]["enabled"] is True
    assert policies["read_demo"]["risk_level"] == "high"
    assert policies["read_demo"]["title"] == "Read"
    assert policies["read_demo"]["output_schema"] == {"type": "object"}
    assert policies["read_demo"]["outputSchema"] == {"type": "object"}
    assert policies["read_demo"]["inputSchema"] == {"type": "object"}
    assert policies["new_demo"]["enabled"] is False


def test_builtin_social_mcp_is_seeded_disabled_and_preserves_authorization(tmp_path: Path, monkeypatch) -> None:
    _init_store(tmp_path, monkeypatch)
    management = load_personification_module("plugin.personification.core.mcp_management")
    registry_mod = load_personification_module("plugin.personification.agent.tool_registry")
    config = SimpleNamespace(
        personification_data_dir=str(tmp_path),
        personification_mcp_secret_file="",
        personification_skill_mcp_timeout=5,
        personification_mcp_registry_sources=[],
        personification_mcp_registry_timeout=5,
    )
    registry = registry_mod.ToolRegistry()
    runtime = SimpleNamespace(plugin_config=config, runtime_bundle=SimpleNamespace(tool_registry=registry))
    manager = management.McpRuntimeManager(runtime, registry)

    item = manager.public_installation("builtin_social_platform_research")
    assert item is not None
    assert item["package_type"] == "builtin"
    assert item["desired_enabled"] is False
    assert item["builtin"] is True
    assert item["deletable"] is False
    assert {tool["remote_name"] for tool in item["tools"]} == {
        "social_content_search",
        "social_content_read",
        "research_game_slang",
    }
    assert all(tool["enabled"] is False for tool in item["tools"])
    assert all(tool["risk_level"] == "low" and tool["side_effect"] == "none" for tool in item["tools"])

    manager.store.set_tool_enabled(
        item["installation_id"],
        "social_content_search",
        True,
        approve_side_effect=True,
        trusted_read_only=True,
    )
    manager.store.set_installation_enabled(item["installation_id"], True)
    manager.store.ensure_builtin_social()
    restored = manager.public_installation(item["installation_id"])
    assert restored is not None and restored["desired_enabled"] is True
    search_policy = next(tool for tool in restored["tools"] if tool["remote_name"] == "social_content_search")
    assert search_policy["enabled"] is True
    assert search_policy["risk_level"] == "low"
    assert search_policy["side_effect"] == "none"

    with management.connect_sync() as conn:
        platforms = conn.execute(
            "SELECT platform,desired_enabled,revision FROM mcp_builtin_platforms ORDER BY platform"
        ).fetchall()
    assert [row["platform"] for row in platforms] == ["bilibili", "douyin", "tieba", "xiaoheihe"]
    assert all(row["desired_enabled"] == 0 and row["revision"] == 0 for row in platforms)


def test_builtin_social_mcp_uses_fixed_launch_and_cannot_be_deleted(tmp_path: Path, monkeypatch) -> None:
    _init_store(tmp_path, monkeypatch)
    management = load_personification_module("plugin.personification.core.mcp_management")
    registry_mod = load_personification_module("plugin.personification.agent.tool_registry")
    config = SimpleNamespace(
        personification_data_dir=str(tmp_path),
        personification_mcp_secret_file="",
        personification_skill_mcp_timeout=5,
        personification_mcp_registry_sources=[],
        personification_mcp_registry_timeout=5,
    )
    registry = registry_mod.ToolRegistry()
    runtime = SimpleNamespace(plugin_config=config, runtime_bundle=SimpleNamespace(tool_registry=registry))
    manager = management.McpRuntimeManager(runtime, registry)
    item = manager.store.get_installation("builtin_social_platform_research")
    assert item is not None

    with management.connect_sync() as conn:
        conn.execute(
            "UPDATE mcp_installations SET command='malicious',args_json='[\"--bad\"]' WHERE installation_id=?",
            (item["installation_id"],),
        )
        conn.commit()
    tampered = manager.store.get_installation(item["installation_id"])
    client = manager._client(tampered)
    assert client.command == sys.executable
    assert len(client.args) == 1
    assert client.args[0].endswith("native_mcp\\social_research\\entrypoint.py") or client.args[0].endswith(
        "native_mcp/social_research/entrypoint.py"
    )
    assert client.env["PERSONIFICATION_SOCIAL_DATA_DIR"].endswith("mcp\\social_platform") or client.env[
        "PERSONIFICATION_SOCIAL_DATA_DIR"
    ].endswith("mcp/social_platform")

    with pytest.raises(ValueError, match="cannot be deleted"):
        asyncio.run(manager.delete(item["installation_id"]))


def test_builtin_social_mcp_stays_running_for_webui_control_without_tool_authorization(
    tmp_path: Path, monkeypatch
) -> None:
    _init_store(tmp_path, monkeypatch)
    management = load_personification_module("plugin.personification.core.mcp_management")
    registry_mod = load_personification_module("plugin.personification.agent.tool_registry")
    config = SimpleNamespace(
        personification_data_dir=str(tmp_path),
        personification_mcp_secret_file="",
        personification_skill_mcp_timeout=8,
        personification_mcp_registry_sources=[],
        personification_mcp_registry_timeout=5,
    )
    registry = registry_mod.ToolRegistry()
    runtime = SimpleNamespace(plugin_config=config, runtime_bundle=SimpleNamespace(tool_registry=registry))
    manager = management.McpRuntimeManager(runtime, registry)

    async def run() -> tuple[dict, dict]:
        try:
            installation = await manager.toggle_installation("builtin_social_platform_research", True)
            status = await manager.builtin_request("personification/builtin/status", {})
            return installation, status
        finally:
            await manager.shutdown()

    installation, status = asyncio.run(run())
    assert installation["process_state"] == "running"
    assert all(tool["enabled"] is False for tool in installation["tools"])
    assert set(status["platforms"]) == {"bilibili", "douyin", "tieba", "xiaoheihe"}


def test_builtin_social_agent_tool_requires_one_ready_platform(tmp_path: Path, monkeypatch) -> None:
    _init_store(tmp_path, monkeypatch)
    management = load_personification_module("plugin.personification.core.mcp_management")
    registry_mod = load_personification_module("plugin.personification.agent.tool_registry")
    config = SimpleNamespace(
        personification_data_dir=str(tmp_path),
        personification_mcp_secret_file="",
    )
    registry = registry_mod.ToolRegistry()
    runtime = SimpleNamespace(plugin_config=config, runtime_bundle=SimpleNamespace(tool_registry=registry))
    manager = management.McpRuntimeManager(runtime, registry)

    class _Client:
        is_running = True

        async def request(self, method, params):  # noqa: ANN001
            assert method == "personification/builtin/status"
            assert params == {}
            return {
                "platforms": {
                    "bilibili": {"enabled": True, "state": "ready"},
                    "douyin": {"enabled": True, "state": "login_required"},
                }
            }

        async def call_tool(self, remote_name, arguments):  # noqa: ANN001
            raise AssertionError(f"tool must not run without a ready platform: {remote_name} {arguments}")

    manager._clients[management.BUILTIN_SOCIAL_MCP_ID] = _Client()

    with pytest.raises(RuntimeError, match="no ready platform"):
        asyncio.run(
            manager._call_managed_tool(
                management.BUILTIN_SOCIAL_MCP_ID,
                "social_content_read",
                {"platform": "douyin", "content_id": "D1"},
            )
        )


def test_builtin_social_result_syncs_target_and_queues_only_extra_claims(tmp_path: Path, monkeypatch) -> None:
    _init_store(tmp_path, monkeypatch)
    management = load_personification_module("plugin.personification.core.mcp_management")
    registry_mod = load_personification_module("plugin.personification.agent.tool_registry")
    config = SimpleNamespace(
        personification_data_dir=str(tmp_path),
        personification_mcp_secret_file="",
        personification_slang_max_claims=20,
    )
    registry = registry_mod.ToolRegistry()
    tool_caller = object()
    runtime = SimpleNamespace(
        plugin_config=config,
        runtime_bundle=SimpleNamespace(
            tool_registry=registry,
            reply_processor_deps=SimpleNamespace(
                runtime=SimpleNamespace(lite_tool_caller=tool_caller)
            ),
        ),
    )
    manager = management.McpRuntimeManager(runtime, registry)
    claims = [
        {"term": "刘涛", "aliases": [], "content_key": "bilibili:B1"},
        {"term": "六甲昵称", "aliases": ["刘涛"], "content_key": "douyin:D1"},
        {"term": "大红", "aliases": [], "content_key": "tieba:T1"},
    ]

    class _Pipeline:
        def __init__(self, *, tool_caller, max_claims):  # noqa: ANN001
            assert tool_caller is not None
            assert max_claims == 20

        async def extract_claims(self, packet, *, target_term):  # noqa: ANN001
            assert packet["packet_id"] == "packet-one"
            assert target_term == "刘涛"
            return claims

    class _Store:
        ingested: list[dict] = []

        def __init__(self, _thresholds=None):  # noqa: ANN001
            pass

        async def ingest_claims(self, values, *, semantic_pipeline, model_route):  # noqa: ANN001
            type(self).ingested = list(values)
            assert isinstance(semantic_pipeline, _Pipeline)
            assert model_route == "builtin_social_research_game_slang"
            return [{"sense_id": "sense-target", "status": "understand_only"}]

    class _Queue:
        scheduled: list[dict] = []

        def schedule_claims(self, values, *, target_term):  # noqa: ANN001
            self.scheduled = list(values)
            assert target_term == "刘涛"
            return len(self.scheduled)

    fake_queue = _Queue()
    monkeypatch.setattr(management, "SlangLearningPipeline", _Pipeline)
    monkeypatch.setattr(management, "MemeLearningStore", _Store)
    manager._discovery_queue = lambda: fake_queue

    raw = json.dumps({"schema_version": 1, "packet_id": "packet-one", "items": []})
    result = json.loads(
        asyncio.run(
            manager._postprocess_builtin_social_result(
                "research_game_slang",
                {"term": "刘涛"},
                raw,
            )
        )
    )

    assert [item["term"] for item in _Store.ingested] == ["刘涛", "六甲昵称"]
    assert [item["term"] for item in fake_queue.scheduled] == ["大红"]
    assert result["slang_claims"] == claims
    assert result["target_senses"][0]["sense_id"] == "sense-target"
    assert result["background_claims_queued"] == 1


def test_stdio_request_timeout_is_absolute_during_notifications() -> None:
    compat = load_personification_module("plugin.personification.skill_runtime.mcp_compat")
    client = compat.McpStdioClient(command="unused", args=[], env={}, cwd=None, timeout=1)
    client.timeout = 0.03

    async def fake_write(_payload):
        return None

    async def fake_read():
        await asyncio.sleep(0)
        return {"jsonrpc": "2.0", "method": "notifications/progress"}

    client._write_message = fake_write
    client._read_message = fake_read
    with pytest.raises((TimeoutError, asyncio.TimeoutError)):
        asyncio.run(client.request("tools/list", {}))


def test_package_digest_binds_full_registry_metadata() -> None:
    management = load_personification_module("plugin.personification.core.mcp_management")
    package = {
        "registryType": "npm",
        "identifier": "demo-server",
        "version": "1.0.0",
        "transport": {"type": "stdio"},
        "packageArguments": [{"type": "named", "name": "--mode", "default": "safe"}],
    }
    original = management._package_digest(package)
    changed = management._package_digest({**package, "packageArguments": [{"type": "named", "name": "--mode", "default": "unsafe"}]})
    assert original != changed


def test_registry_search_preserves_cursor_and_returns_current_metadata() -> None:
    management = load_personification_module("plugin.personification.core.mcp_management")
    client = management.McpRegistryClient(SimpleNamespace(personification_mcp_registry_timeout=5))
    cursor = " opaque+/= cursor value " * 100
    captured: dict = {}

    async def fake_get(url, params=None, *, fresh=False):
        captured.update({"url": url, "params": params, "fresh": fresh})
        return {
            "servers": [{
                "server": {
                    "$schema": "https://example.test/server.schema.json",
                    "name": "io.example/demo",
                    "title": "Demo",
                    "description": "Demo server",
                    "version": "1.2.3",
                    "websiteUrl": "https://example.test/demo",
                    "repository": {"source": "github", "url": "https://github.com/example/demo"},
                    "packages": [{"registryType": "npm", "transport": {"type": "stdio"}}],
                },
                "_meta": {"io.modelcontextprotocol.registry/official": {"status": "deprecated", "statusMessage": "use v2"}},
            }],
            "metadata": {"nextCursor": "next+/="},
        }

    client._get = fake_get
    result = asyncio.run(client.search({"id": "official", "url": management.OFFICIAL_MCP_REGISTRY}, "demo", cursor=cursor))
    assert captured["params"]["cursor"] == cursor
    assert result["next_cursor"] == "next+/="
    server = result["servers"][0]
    assert server["status"] == "deprecated"
    assert server["repository"]["source"] == "github"
    assert server["website"] == "https://example.test/demo"
    assert server["schema"].endswith("server.schema.json")

    with pytest.raises(ValueError, match="cursor is too large"):
        asyncio.run(client.search({"id": "official", "url": management.OFFICIAL_MCP_REGISTRY}, "", cursor="x" * 16385))


def test_registry_detail_can_bypass_cache_and_exposes_file_hash() -> None:
    management = load_personification_module("plugin.personification.core.mcp_management")
    client = management.McpRegistryClient(SimpleNamespace(personification_mcp_registry_timeout=5))
    fresh_values: list[bool] = []

    async def fake_get(_url, _params=None, *, fresh=False):
        fresh_values.append(fresh)
        return {
            "server": {
                "$schema": "https://example.test/schema.json",
                "name": "io.example/demo",
                "description": "Demo",
                "version": "1.2.3",
                "websiteUrl": "https://example.test",
                "repository": {"source": "github", "url": "https://github.com/example/demo"},
                "packages": [{
                    "registryType": "npm",
                    "identifier": "demo-server",
                    "version": "1.2.3",
                    "transport": {"type": "stdio"},
                    "fileSha256": "b" * 64,
                }],
            },
            "_meta": {"io.modelcontextprotocol.registry/official": {"status": "active"}},
        }

    client._get = fake_get
    detail_result = asyncio.run(
        client.detail({"id": "official", "url": management.OFFICIAL_MCP_REGISTRY}, "io.example/demo", fresh=True)
    )
    assert fresh_values == [True]
    assert detail_result["server"]["status"] == "active"
    assert detail_result["server"]["website"] == "https://example.test"
    assert detail_result["packages"][0]["fileSha256"] == "b" * 64
    assert detail_result["packages"][0]["supported"] is False
    assert "fails closed" in detail_result["packages"][0]["unsupported_reason"]


def test_runtime_manager_registers_only_enabled_policy(tmp_path: Path, monkeypatch) -> None:
    _init_store(tmp_path, monkeypatch)
    management = load_personification_module("plugin.personification.core.mcp_management")
    executor = load_personification_module("plugin.personification.agent.runtime.executor")
    registry_mod = load_personification_module("plugin.personification.agent.tool_registry")
    monkeypatch.setattr(management, "get_data_dir", lambda _cfg=None: tmp_path)
    config = SimpleNamespace(
        personification_data_dir=str(tmp_path),
        personification_mcp_secret_file="",
        personification_skill_mcp_timeout=5,
        personification_mcp_registry_sources=[],
        personification_mcp_registry_timeout=5,
    )
    registry = registry_mod.ToolRegistry()
    runtime = SimpleNamespace(plugin_config=config, runtime_bundle=SimpleNamespace(tool_registry=registry))
    manager = management.McpRuntimeManager(runtime, registry)
    item = {
        "installation_id": "install-1",
        "source_id": "official",
        "source_url": management.OFFICIAL_MCP_REGISTRY,
        "server_name": "io.example/demo",
        "server_title": "Demo",
        "server_version": "1.0.0",
        "package_type": "pypi",
        "package_identifier": "demo",
        "command": sys.executable,
        "args": [str(FAKE_SERVER)],
        "env": {},
        "secret_names": [],
        "name_prefix": "mcp_demo_",
        "metadata": {},
        "created_by": "10001",
    }
    tools = [
        {"remote_name": "read_demo", "registered_name": "mcp_demo_read_demo", "description": "read", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}, "enabled": True, "risk_level": "low", "side_effect": "none", "publisher_read_only": True},
        {"remote_name": "write_demo", "registered_name": "mcp_demo_write_demo", "description": "write", "parameters": {"type": "object", "properties": {}}, "enabled": False, "risk_level": "admin", "side_effect": "unknown", "publisher_read_only": False},
        {"remote_name": "deleted_demo", "registered_name": "mcp_demo_deleted_demo", "description": "deleted", "parameters": {"type": "object", "properties": {}}, "enabled": True, "risk_level": "high", "side_effect": "external", "publisher_read_only": False},
    ]
    manager.store.save_installation(item, tools)

    async def stale_handler(**_kwargs):
        return "stale"

    registry.register(registry_mod.AgentTool(name="mcp_demo_deleted_demo", description="stale", parameters={}, handler=stale_handler, metadata={"mcp_installation_id": "install-1"}))
    manager._tool_names["install-1"] = {"mcp_demo_deleted_demo"}

    async def run():
        await manager.activate("install-1")
        read_tool = registry.get("mcp_demo_read_demo")
        assert read_tool is not None
        assert read_tool.local is True
        assert registry.get("mcp_demo_write_demo") is None
        assert registry.get("mcp_demo_deleted_demo") is None
        assert {tool["remote_name"] for tool in manager.store.tools("install-1")} == {"read_demo", "write_demo"}
        running = manager.public_installation("install-1")
        assert running["run_allowed"] is True
        assert running["process_state"] == "running"
        assert running["authorized_count"] == 1
        assert running["registered_count"] == 1
        assert running["effective_count"] == 1
        assert running["tools"][0]["authorized"] is True
        assert running["tools"][0]["registered"] is True
        assert running["tools"][0]["effective"] is True
        result = await executor._invoke_tool_handler(
            tool_name=read_tool.name,
            tool=read_tool,
            tool_args={"query": "hello"},
        )
        stopped = await manager.toggle_installation("install-1", False)
        assert stopped["authorized_count"] == 1
        assert stopped["registered_count"] == 0
        assert stopped["effective_count"] == 0
        assert stopped["tools"][0]["authorized"] is True
        await manager.shutdown()
        return result

    assert "called:read_demo" in asyncio.run(run())


def test_runtime_manager_fails_closed_when_required_secret_disappears(tmp_path: Path, monkeypatch) -> None:
    _init_store(tmp_path, monkeypatch)
    management = load_personification_module("plugin.personification.core.mcp_management")
    registry_mod = load_personification_module("plugin.personification.agent.tool_registry")
    config = SimpleNamespace(
        personification_data_dir=str(tmp_path),
        personification_mcp_secret_file="",
        personification_skill_mcp_timeout=5,
    )
    registry = registry_mod.ToolRegistry()
    runtime = SimpleNamespace(plugin_config=config, runtime_bundle=SimpleNamespace(tool_registry=registry))
    manager = management.McpRuntimeManager(runtime, registry)
    manager.store.save_installation(
        {
            "installation_id": "install-secret",
            "source_id": "official",
            "source_url": management.OFFICIAL_MCP_REGISTRY,
            "server_name": "io.example/secret",
            "server_title": "Secret",
            "server_version": "1.0.0",
            "package_type": "npm",
            "package_identifier": "secret-server",
            "command": sys.executable,
            "args": [str(FAKE_SERVER)],
            "env": {},
            "secret_names": ["TOKEN"],
            "name_prefix": "mcp_secret_",
            "metadata": {},
            "created_by": "10001",
        },
        [{"remote_name": "read_demo", "registered_name": "mcp_secret_read_demo", "description": "read", "parameters": {"type": "object"}, "enabled": True, "risk_level": "review", "side_effect": "unknown", "publisher_read_only": True}],
    )
    manager.secrets.set("install-secret", {"TOKEN": "configured"})

    async def run() -> None:
        await manager.activate("install-secret")
        assert registry.get("mcp_secret_read_demo") is not None
        manager.secrets.delete("install-secret")
        assert await manager.refresh_process_states() == 1
        assert registry.get("mcp_secret_read_demo") is None
        current = manager.public_installation("install-secret")
        assert current["secrets_ready"] is False
        assert current["run_allowed"] is False
        assert current["effective_count"] == 0
        with pytest.raises(RuntimeError, match="Secret"):
            await manager.toggle_installation("install-secret", True)

    asyncio.run(run())


def test_runtime_manager_resyncs_catalog_then_closes_server_without_authorized_tools(tmp_path: Path, monkeypatch) -> None:
    _init_store(tmp_path, monkeypatch)
    management = load_personification_module("plugin.personification.core.mcp_management")
    registry_mod = load_personification_module("plugin.personification.agent.tool_registry")
    config = SimpleNamespace(personification_data_dir=str(tmp_path), personification_mcp_secret_file="")
    registry = registry_mod.ToolRegistry()
    runtime = SimpleNamespace(plugin_config=config, runtime_bundle=SimpleNamespace(tool_registry=registry))
    manager = management.McpRuntimeManager(runtime, registry)
    item = {
        "installation_id": "install-ready",
        "source_id": "official",
        "source_url": management.OFFICIAL_MCP_REGISTRY,
        "server_name": "io.example/ready",
        "server_title": "Ready",
        "server_version": "1.0.0",
        "package_type": "npm",
        "package_identifier": "ready-server",
        "command": sys.executable,
        "args": [str(FAKE_SERVER)],
        "env": {},
        "secret_names": [],
        "name_prefix": "mcp_ready_",
        "metadata": {},
        "created_by": "10001",
    }
    manager.store.save_installation(
        item,
        [{
            "remote_name": "read_demo",
            "registered_name": "mcp_ready_read_demo",
            "description": "read",
            "parameters": {"type": "object", "properties": {}},
            "enabled": False,
            "risk_level": "review",
            "side_effect": "unknown",
            "publisher_read_only": True,
        }],
    )
    asyncio.run(manager.activate("install-ready"))
    assert manager.store.get_installation("install-ready")["observed_status"] == "ready"
    assert manager._clients == {}
    policies = {tool["remote_name"]: tool for tool in manager.store.tools("install-ready")}
    assert set(policies) == {"read_demo", "write_demo"}
    assert policies["read_demo"]["title"] == "Read Demo"
    assert policies["read_demo"]["enabled"] is False
    assert policies["write_demo"]["enabled"] is False
    metadata = manager.store.get_installation("install-ready")["metadata"]
    assert metadata["protocol_version"] == "2025-11-25"
    assert metadata["server_info"]["name"] == "fake-mcp"


def test_runtime_manager_withdraws_tools_before_call_when_process_exited(tmp_path: Path, monkeypatch) -> None:
    _init_store(tmp_path, monkeypatch)
    management = load_personification_module("plugin.personification.core.mcp_management")
    registry_mod = load_personification_module("plugin.personification.agent.tool_registry")
    monkeypatch.setattr(management, "get_data_dir", lambda _cfg=None: tmp_path)
    config = SimpleNamespace(
        personification_data_dir=str(tmp_path),
        personification_mcp_secret_file="",
        personification_skill_mcp_timeout=5,
    )
    registry = registry_mod.ToolRegistry()
    runtime = SimpleNamespace(plugin_config=config, runtime_bundle=SimpleNamespace(tool_registry=registry))
    manager = management.McpRuntimeManager(runtime, registry)
    manager.store.save_installation(
        {
            "installation_id": "install-exit",
            "source_id": "official",
            "source_url": management.OFFICIAL_MCP_REGISTRY,
            "server_name": "io.example/exit",
            "server_title": "Exit",
            "server_version": "1.0.0",
            "package_type": "pypi",
            "package_identifier": "exit-server",
            "command": sys.executable,
            "args": [str(FAKE_SERVER)],
            "env": {},
            "secret_names": [],
            "name_prefix": "mcp_exit_",
            "metadata": {},
            "created_by": "10001",
        },
        [{"remote_name": "read_demo", "registered_name": "mcp_exit_read_demo", "description": "read", "parameters": {"type": "object"}, "enabled": True, "risk_level": "high", "side_effect": "external", "publisher_read_only": True}],
    )

    async def run():
        await manager.activate("install-exit")
        tool = registry.get("mcp_exit_read_demo")
        assert tool is not None
        manager._clients["install-exit"]._transport_failed = True
        with pytest.raises(RuntimeError, match="unavailable"):
            await tool.handler()
        return tool

    tool = asyncio.run(run())
    assert registry.get("mcp_exit_read_demo") is None
    public = manager.public_installation("install-exit")
    assert public["process_state"] == "error"
    assert public["authorized_count"] == 1
    assert public["registered_count"] == 0
    assert public["effective_count"] == 0
    assert public["last_error"] == "MCP process exited unexpectedly"

    manager.store.set_status("install-exit", "running")
    registry.register(tool)
    manager._tool_names["install-exit"] = {"mcp_exit_read_demo"}
    assert asyncio.run(manager.refresh_process_states()) == 1
    assert registry.get("mcp_exit_read_demo") is None
    detached = manager.public_installation("install-exit")
    assert detached["process_state"] == "error"
    assert detached["last_error"] == "MCP process is not attached to current runtime"


def test_registry_sources_include_official_and_https_custom() -> None:
    management = load_personification_module("plugin.personification.core.mcp_management")
    sources = management.mcp_registry_sources(
        SimpleNamespace(
            personification_mcp_registry_sources=[
                {"name": "trusted", "url": "https://registry.example.com/"},
                {"name": "unsafe", "url": "http://localhost:8080"},
                {"name": "credentials", "url": "https://user:pass@registry.example.com"},
                {"name": "query", "url": "https://registry.example.com?token=secret"},
            ]
        )
    )
    assert sources[0]["id"] == "official"
    assert len(sources) == 2
    assert sources[1]["url"] == "https://registry.example.com"


def test_store_requires_explicit_risk_approval_to_enable_tool(tmp_path: Path, monkeypatch) -> None:
    _init_store(tmp_path, monkeypatch)
    management = load_personification_module("plugin.personification.core.mcp_management")
    store = management.McpStore()
    item = {
        "installation_id": "install-approval",
        "source_id": "official",
        "source_url": management.OFFICIAL_MCP_REGISTRY,
        "server_name": "io.example/approval",
        "server_title": "Approval",
        "server_version": "1.0.0",
        "package_type": "npm",
        "package_identifier": "approval-server",
        "command": "npx",
        "args": ["--yes", "approval-server@1.0.0"],
        "env": {},
        "secret_names": [],
        "name_prefix": "mcp_approval_",
        "metadata": {},
        "created_by": "10001",
    }
    store.save_installation(
        item,
        [{
            "remote_name": "read_demo",
            "registered_name": "mcp_approval_read_demo",
            "description": "read",
            "parameters": {"type": "object", "properties": {}},
            "enabled": False,
            "risk_level": "review",
            "side_effect": "unknown",
            "publisher_read_only": True,
        }],
    )
    with pytest.raises(ValueError, match="explicit risk approval"):
        store.set_tool_enabled("install-approval", "read_demo", True)
    store.set_tool_enabled("install-approval", "read_demo", True, approve_side_effect=True)
    policy = store.tools("install-approval")[0]
    assert policy["enabled"] is True
    assert policy["side_effect"] == "external"


def test_runtime_manager_serializes_lifecycle_operations(tmp_path: Path, monkeypatch) -> None:
    management = load_personification_module("plugin.personification.core.mcp_management")
    registry_mod = load_personification_module("plugin.personification.agent.tool_registry")
    config = SimpleNamespace(personification_data_dir=str(tmp_path), personification_mcp_secret_file="")
    registry = registry_mod.ToolRegistry()
    runtime = SimpleNamespace(plugin_config=config, runtime_bundle=SimpleNamespace(tool_registry=registry))
    manager = management.McpRuntimeManager(runtime, registry)
    active = 0
    max_active = 0

    manager.store.set_installation_enabled = lambda *_args, **_kwargs: None
    manager.public_installation = lambda installation_id: {"installation_id": installation_id}

    async def fake_activate(_installation_id: str) -> None:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        active -= 1

    manager._activate_unlocked = fake_activate

    async def run() -> None:
        await asyncio.gather(
            manager.toggle_installation("one", True),
            manager.toggle_installation("two", True),
        )

    asyncio.run(run())
    assert max_active == 1


def test_get_mcp_manager_reuses_manager_for_same_registry(tmp_path: Path) -> None:
    management = load_personification_module("plugin.personification.core.mcp_management")
    registry_mod = load_personification_module("plugin.personification.agent.tool_registry")
    registry = registry_mod.ToolRegistry()
    config = SimpleNamespace(personification_data_dir=str(tmp_path), personification_mcp_secret_file="")
    runtime_one = SimpleNamespace(plugin_config=config, runtime_bundle=SimpleNamespace(tool_registry=registry))
    runtime_two = SimpleNamespace(plugin_config=config, runtime_bundle=SimpleNamespace(tool_registry=registry))
    first = management.get_mcp_manager(runtime_one)
    second = management.get_mcp_manager(runtime_two)
    assert first is second
    assert second.runtime is runtime_two


def test_custom_reload_replaces_stale_sources_and_preserves_managed_mcp(tmp_path: Path, monkeypatch) -> None:
    reload_mod = load_personification_module("plugin.personification.skill_runtime.reload")
    registry_mod = load_personification_module("plugin.personification.agent.tool_registry")
    registry = registry_mod.ToolRegistry()

    async def noop(**_kwargs):
        return ""

    registry.register(registry_mod.AgentTool(name="base", description="base", parameters={}, handler=noop, metadata={"source_kind": "bundled"}))
    registry.register(registry_mod.AgentTool(name="stale", description="stale", parameters={}, handler=noop, metadata={"source_kind": "local"}))
    registry.register(registry_mod.AgentTool(name="managed", description="managed", parameters={}, handler=noop, metadata={"source_kind": "mcp_managed"}))

    async def fake_load(_root, candidate, _logger, **_kwargs):
        candidate.register(registry_mod.AgentTool(name="fresh", description="fresh", parameters={}, handler=noop, metadata={"source_kind": "generated"}))

    monkeypatch.setattr(reload_mod, "load_custom_skills", fake_load)
    logger = SimpleNamespace(info=lambda *_a, **_k: None, warning=lambda *_a, **_k: None)
    config = SimpleNamespace(personification_data_dir=str(tmp_path), personification_skills_path="")
    bundle = SimpleNamespace(
        tool_registry=registry,
        reply_processor_deps=SimpleNamespace(runtime=SimpleNamespace(agent_tool_caller=None, vision_caller=None, knowledge_store=None)),
        scheduler=None,
    )
    runtime = SimpleNamespace(plugin_config=config, logger=logger, get_bots=lambda: {}, runtime_bundle=bundle)
    asyncio.run(reload_mod.reload_custom_skills_for_runtime(runtime))
    assert {tool.name for tool in registry.all()} == {"base", "managed", "fresh"}


def test_full_runtime_reload_is_serial_and_restores_mcp(tmp_path: Path, monkeypatch) -> None:
    reload_mod = load_personification_module("plugin.personification.skill_runtime.reload")
    management = load_personification_module("plugin.personification.core.mcp_management")
    registry_mod = load_personification_module("plugin.personification.agent.tool_registry")
    registry = registry_mod.ToolRegistry()
    events: list[str] = []
    active = 0
    max_active = 0

    def reload_base():
        events.append("base")

    async def reload_custom(_runtime):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        events.append("custom")
        await asyncio.sleep(0.01)
        active -= 1
        return 2

    class FakeManager:
        async def reload(self):
            events.append("mcp")
            return {"running": 1, "ready": 0, "failed": 0}

    monkeypatch.setattr(reload_mod, "reload_custom_skills_for_runtime", reload_custom)
    monkeypatch.setattr(management, "get_mcp_manager", lambda _runtime: FakeManager())
    bundle = SimpleNamespace(tool_registry=registry, reload_runtime_services=reload_base)
    runtime = SimpleNamespace(
        plugin_config=SimpleNamespace(personification_data_dir=str(tmp_path)),
        logger=SimpleNamespace(),
        get_bots=lambda: {},
        runtime_bundle=bundle,
    )

    async def run():
        return await asyncio.gather(
            reload_mod.reload_all_runtime_services(runtime),
            reload_mod.reload_all_runtime_services(runtime),
        )

    results = asyncio.run(run())
    assert max_active == 1
    assert events == ["base", "custom", "mcp", "base", "custom", "mcp"]
    assert all(result["mcp"]["failed"] == 0 for result in results)
