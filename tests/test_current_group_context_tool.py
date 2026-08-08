from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from ._loader import load_personification_module


group_tool = load_personification_module(
    "plugin.personification.core.current_group_context_tool"
)
registry_mod = load_personification_module("plugin.personification.agent.tool_registry")
protocol_adapter = load_personification_module("plugin.personification.core.protocol_adapter")


class _Bot:
    self_id = "10000"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call_api(self, action: str, **params):  # noqa: ANN001, ANN202
        self.calls.append((action, params))
        if action == "get_version_info":
            return {"app_name": "NapCat.Onebot", "app_version": "4.8.0"}
        if action == "get_group_info":
            return {
                "group_id": 20001,
                "group_name": "当前群",
                "group_memo": "当前群简介",
                "member_count": 80,
                "max_member_count": 200,
                "avatar_url": "https://private.invalid/group.png",
            }
        if action == "get_group_member_list":
            return [
                {
                    "group_id": 20001,
                    "user_id": 10000 + index,
                    "nickname": f"成员{index}",
                    "role": "owner" if index == 0 else "member",
                    "last_sent_time": index,
                }
                for index in range(60)
            ]
        if action == "_get_group_notice":
            return [
                {
                    "fid": "internal-notice-id",
                    "sender_id": 10001,
                    "publish_time": 123,
                    "message": {"text": "群公告正文", "images": [{"id": "image-id"}]},
                    "settings": {"pinned": True},
                }
            ]
        raise AssertionError(action)


def setup_function(_fn) -> None:  # noqa: ANN001
    protocol_adapter.reset_protocol_adapters()


def test_current_group_tool_is_event_scoped_read_only_and_bounded() -> None:
    bot = _Bot()
    tool = group_tool.build_current_group_context_tool(
        bot=bot,
        group_id="20001",
        plugin_config=SimpleNamespace(personification_protocol_extensions="auto"),
    )

    profile = json.loads(asyncio.run(tool.handler(section="profile")))
    members = json.loads(asyncio.run(tool.handler(section="members", limit=99)))
    announcements = json.loads(asyncio.run(tool.handler(section="announcements", limit=20)))

    assert profile["ok"] is True
    assert profile["group_id"] == "20001"
    assert profile["data"]["group_name"] == "当前群"
    assert "avatar_url" not in profile["data"]
    assert len(members["data"]) == 50
    assert members["data"][0]["role"] == "owner"
    assert announcements["data"] == [
        {
            "sender_id": "10001",
            "publish_time": 123,
            "text": "群公告正文",
            "image_count": 1,
            "pinned": True,
            "confirm_required": False,
        }
    ]
    assert "notice_id" not in str(announcements)
    assert "internal-notice-id" not in str(announcements)
    assert "group_id" not in tool.parameters["properties"]
    assert tool.metadata["side_effect"] == "none"
    assert tool.metadata["source_kind"] == "first_party_runtime"
    assert tool.metadata["retryable"] is True
    assert not any(action.startswith(("set_", "_del", "_delete")) for action, _params in bot.calls)


def test_current_group_tool_registers_only_for_numeric_group_events() -> None:
    bot = _Bot()
    registry = registry_mod.ToolRegistry()
    group_tool.register_current_group_context_tool(
        registry,
        bot=bot,
        event=SimpleNamespace(user_id=10001),
    )
    assert registry.all() == []

    group_tool.register_current_group_context_tool(
        registry,
        bot=bot,
        event=SimpleNamespace(group_id=20001, user_id=10001),
    )
    assert [tool.name for tool in registry.active()] == ["get_current_group_context"]


def test_current_group_tool_filters_policy_blocked_members_before_agent_evidence() -> None:
    bot = _Bot()

    async def _authorize(user_id: str):
        allowed = user_id != "10000"
        return SimpleNamespace(blocked=not allowed, allow_context_read=allowed)

    tool = group_tool.build_current_group_context_tool(
        bot=bot,
        group_id="20001",
        plugin_config=SimpleNamespace(personification_protocol_extensions="auto"),
        policy_authorizer=_authorize,
    )

    members = json.loads(asyncio.run(tool.handler(section="members", limit=3)))

    assert members["ok"] is True
    assert [item["user_id"] for item in members["data"]] == ["10059", "10058", "10057"]
    assert "10000" not in str(members)


def test_normal_and_yaml_paths_register_same_current_group_tool() -> None:
    root = Path(__file__).resolve().parents[1]
    normal = (root / "handlers" / "reply_pipeline" / "pipeline_context.py").read_text(
        encoding="utf-8"
    )
    yaml = (root / "handlers" / "yaml_pipeline" / "processor.py").read_text(
        encoding="utf-8"
    )
    skill = (root / "skills" / "skillpacks" / "group_info_tool" / "skill.yaml").read_text(
        encoding="utf-8"
    )

    assert "register_current_group_context_tool(" in normal
    assert "register_current_group_context_tool(" in yaml
    assert "enabled: false" in skill
