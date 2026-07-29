from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from ._loader import load_personification_module


directory = load_personification_module("plugin.personification.core.group_directory")
utils = load_personification_module("plugin.personification.utils")


class _Store:
    def __init__(self) -> None:
        self.data: dict[str, object] = {}

    def load_sync(self, name: str):
        return self.data.get(name, {})

    def mutate_sync(self, name: str, mutate):
        value = mutate(self.data.get(name, {}))
        self.data[name] = value
        return value


class _Bot:
    def __init__(self, self_id: str, response) -> None:
        self.self_id = self_id
        self.response = response
        self.list_calls = 0
        self.probes: list[dict] = []

    async def get_group_list(self):
        self.list_calls += 1
        return self.response

    async def get_group_info(self, **kwargs):
        self.probes.append(kwargs)
        return {"group_id": kwargs["group_id"], "group_name": f"探测{kwargs['group_id']}"}


@pytest.fixture
def store(monkeypatch):
    value = _Store()
    monkeypatch.setattr(directory, "get_data_store", lambda: value)
    monkeypatch.setattr(utils, "load_whitelist", lambda: [])
    monkeypatch.setattr(utils, "load_group_configs", lambda: {})
    return value


@pytest.mark.parametrize(
    "wrapped",
    [
        [{"group_id": 1, "group_name": "一群"}],
        {"data": [{"group_id": 1, "group_name": "一群"}]},
        {"groups": [{"group_id": 1, "group_name": "一群"}]},
        {"group_list": [{"group_id": 1, "group_name": "一群"}]},
        {"data": {"groups": [{"group_id": 1, "group_name": "一群"}]}},
    ],
)
def test_group_list_wrappers_are_supported(store, wrapped) -> None:
    bot = _Bot("10", wrapped)
    runtime = SimpleNamespace(
        plugin_config=SimpleNamespace(personification_whitelist=[]),
        get_bots=lambda: {"10": bot},
        runtime_bundle=None,
    )
    rows = asyncio.run(directory.discover_group_union(runtime))
    assert rows[0]["group_id"] == "1"
    assert rows[0]["group_name"] == "一群"


def test_all_bots_are_queried_and_same_group_remains_scoped(store) -> None:
    first = _Bot("10", {"data": [{"group_id": 1, "group_name": "甲群"}]})
    second = _Bot("20", {"groups": [{"group_id": 2, "group_name": "乙群"}]})
    runtime = SimpleNamespace(
        plugin_config=SimpleNamespace(personification_whitelist=[]),
        get_bots=lambda: {"10": first, "20": second},
        runtime_bundle=None,
    )
    rows = asyncio.run(directory.discover_group_union(runtime, probe_limit=0))
    assert {row["group_id"] for row in rows} == {"1", "2"}
    assert first.list_calls == second.list_calls == 1
    assert {entry["bot_self_id"] for entry in store.data["group_directory"].values()} == {"10", "20"}


def test_group_config_only_is_in_union_and_probe_is_bounded(store, monkeypatch) -> None:
    monkeypatch.setattr(utils, "load_group_configs", lambda: {"100": {"enabled": False}, "101": {"enabled": True}, "not-a-number": {}})
    bot = _Bot("10", [])
    runtime = SimpleNamespace(
        plugin_config=SimpleNamespace(personification_whitelist=[]),
        get_bots=lambda: {"10": bot},
        runtime_bundle=None,
    )
    rows = asyncio.run(directory.discover_group_union(runtime, probe_limit=1))
    assert {row["group_id"] for row in rows} == {"100", "101", "not-a-number"}
    assert bot.probes == [{"group_id": 100, "no_cache": True}]
    assert "group_config" in next(row for row in rows if row["group_id"] == "101")["sources"]
    assert all(row["bot_self_ids"] == [] for row in rows if row["group_id"] != "100")
    assert all(entry["group_id"] == "100" for entry in store.data.get("group_directory", {}).values())


def test_default_discovery_does_not_probe_non_members(store, monkeypatch) -> None:
    monkeypatch.setattr(utils, "load_group_configs", lambda: {"100": {"enabled": True}})
    bot = _Bot("10", [])
    runtime = SimpleNamespace(
        plugin_config=SimpleNamespace(personification_whitelist=[]),
        get_bots=lambda: {"10": bot},
        runtime_bundle=None,
    )

    rows = asyncio.run(directory.discover_group_union(runtime))

    assert [row["group_id"] for row in rows] == ["100"]
    assert bot.list_calls == 1
    assert bot.probes == []
    assert rows[0]["bot_self_ids"] == []


def test_record_observed_group_keeps_provenance_and_freshness(store) -> None:
    directory.record_observed_group("10", "100", source="event", observed_at=10)
    value = directory.record_observed_group("10", "100", source="profile_memory", observed_at=20)
    assert value["provenance"] == {"event": 10.0, "profile_memory": 20.0}
    assert value["first_seen_at"] == 10.0
    assert value["last_seen_at"] == 20.0


def test_union_preserves_membership_without_copying_global_sources(store, monkeypatch) -> None:
    monkeypatch.setattr(utils, "load_group_configs", lambda: {"200": {"enabled": True}})
    first = _Bot("10", [{"group_id": 100, "group_name": "甲群"}])
    second = _Bot("20", [])
    runtime = SimpleNamespace(
        plugin_config=SimpleNamespace(personification_whitelist=[]),
        get_bots=lambda: {"10": first, "20": second},
        runtime_bundle=None,
    )
    rows = asyncio.run(directory.discover_group_union(runtime, probe_limit=0))
    by_id = {row["group_id"]: row for row in rows}
    assert by_id["100"]["bot_self_ids"] == ["10"]
    assert by_id["100"]["memberships"][0]["bot_id"] == "10"
    assert by_id["200"]["bot_self_ids"] == []
    assert by_id["200"]["memberships"] == []
