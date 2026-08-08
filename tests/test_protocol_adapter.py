from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ._loader import load_personification_module

adapter_module = load_personification_module("plugin.personification.core.protocol_adapter")


class _Bot:
    def __init__(self, app_name: str, app_version: str = "") -> None:
        self.self_id = "10000"
        self.app_name = app_name
        self.app_version = app_version
        self.calls: list[tuple[str, dict]] = []
        self.fail_once: dict[str, BaseException] = {}
        self.responses: dict[str, object] = {}

    async def call_api(self, action: str, **params):  # noqa: ANN001, ANN201
        self.calls.append((action, params))
        if action == "get_version_info":
            return {
                "app_name": self.app_name,
                "app_version": self.app_version,
                "protocol_version": "v11",
            }
        failure = self.fail_once.pop(action, None)
        if failure is not None:
            raise failure
        if action in self.responses:
            return self.responses[action]
        if action == "get_cookies":
            return {"cookies": "uin=o10000; p_skey=secret;"}
        return None


class _ActionNotFoundError(RuntimeError):
    retcode = 404


def setup_function(_fn) -> None:  # noqa: ANN001
    adapter_module.reset_protocol_adapters()


def _config(mode: str = "auto") -> SimpleNamespace:
    return SimpleNamespace(personification_protocol_extensions=mode)


def test_matrix_distinguishes_napcat_and_llonebot_paths() -> None:
    napcat = adapter_module.get_protocol_adapter(_Bot("NapCat.Onebot"), _config())
    napcat_matrix = asyncio.run(napcat.matrix())
    assert napcat_matrix.identity.implementation == "napcat"
    assert napcat_matrix.get("group.announcement.delete").selected_path == "_del_group_notice"

    llonebot = adapter_module.get_protocol_adapter(_Bot("LLOneBot", "7.12.3"), _config())
    llonebot_matrix = asyncio.run(llonebot.matrix())
    assert llonebot_matrix.identity.implementation == "llonebot"
    assert llonebot_matrix.get("group.announcement.delete").selected_path == "_delete_group_notice"
    assert llonebot_matrix.get("message.input_status").state.value == "available"


def test_transient_failure_is_not_misclassified_as_unsupported() -> None:
    bot = _Bot("NapCat.Onebot")
    bot.fail_once["get_cookies"] = TimeoutError("slow")
    adapter = adapter_module.get_protocol_adapter(bot, _config())
    first = asyncio.run(adapter.export_cookies(domain="qzone.qq.com"))
    assert first.status == "degraded"
    assert first.code == "timeout"
    assert adapter._path_health["get_cookies"].state.value == "degraded"
    matrix = asyncio.run(adapter.matrix())
    assert matrix.get("qzone.cookie_export").state.value == "degraded"


def test_cookie_export_uses_standard_action_for_both_implementations() -> None:
    for app_name in ("NapCat.Onebot", "LLOneBot"):
        adapter_module.reset_protocol_adapters()
        bot = _Bot(app_name, "7.12.3")
        result = asyncio.run(
            adapter_module.get_protocol_adapter(bot, _config()).export_cookies(domain="qzone.qq.com")
        )
        assert result.ok is True
        assert result.data["cookies"].startswith("uin=o10000")
        assert bot.calls == [("get_cookies", {"domain": "qzone.qq.com"})]


def test_extensions_disabled_preserves_standard_capabilities() -> None:
    adapter = adapter_module.get_protocol_adapter(_Bot("NapCat.Onebot"), _config("none"))
    matrix = asyncio.run(adapter.matrix())
    assert matrix.get("message.reaction").state.value == "disabled"
    assert matrix.get("message.recall").state.value == "available"


def test_recall_message_calls_delete_msg_once_with_int_and_accepts_none_success() -> None:
    bot = _Bot("NapCat.Onebot")
    adapter = adapter_module.get_protocol_adapter(bot, _config())

    result = asyncio.run(adapter.recall_message(message_id="123"))

    assert result.status == "succeeded"
    assert result.code == "ok"
    assert result.data is None
    assert result.selected_path == "delete_msg"
    assert bot.calls == [("delete_msg", {"message_id": 123})]


def test_recall_message_accepts_negative_int32() -> None:
    bot = _Bot("LLOneBot", "7.12.3")
    adapter = adapter_module.get_protocol_adapter(bot, _config())

    result = asyncio.run(adapter.recall_message(message_id=-(2**31)))

    assert result.ok is True
    assert bot.calls == [("delete_msg", {"message_id": -(2**31)})]


def test_recall_message_rejects_invalid_ids_without_calling_api() -> None:
    bot = _Bot("NapCat.Onebot")
    adapter = adapter_module.get_protocol_adapter(bot, _config())
    invalid_ids = (
        True,
        False,
        0,
        2**31,
        -(2**31) - 1,
        "opaque",
        " 123",
        "1.5",
        "2147483648",
        "-2147483649",
    )

    for message_id in invalid_ids:
        result = asyncio.run(adapter.recall_message(message_id=message_id))
        assert result.status == "definite_failure"
        assert result.code == "invalid_message_id"

    assert bot.calls == []


def test_recall_message_timeout_is_degraded_without_retry() -> None:
    bot = _Bot("NapCat.Onebot")
    bot.fail_once["delete_msg"] = TimeoutError("slow")
    adapter = adapter_module.get_protocol_adapter(bot, _config())

    result = asyncio.run(adapter.recall_message(message_id=123))

    assert result.status == "degraded"
    assert result.code == "timeout"
    assert result.selected_path == "delete_msg"
    assert bot.calls == [("delete_msg", {"message_id": 123})]

    second = asyncio.run(adapter.recall_message(message_id=124))
    assert second.status == "succeeded"
    assert bot.calls == [
        ("delete_msg", {"message_id": 123}),
        ("delete_msg", {"message_id": 124}),
    ]


def test_recall_message_action_not_found_is_unavailable_without_fallback() -> None:
    bot = _Bot("NapCat.Onebot")
    bot.fail_once["delete_msg"] = _ActionNotFoundError("missing")
    adapter = adapter_module.get_protocol_adapter(bot, _config())

    result = asyncio.run(adapter.recall_message(message_id=123))

    assert result.status == "unavailable"
    assert result.code == "action_not_found"
    assert result.selected_path == "delete_msg"
    assert bot.calls == [("delete_msg", {"message_id": 123})]


def test_group_standard_reads_and_member_writes_use_onebot_contract() -> None:
    bot = _Bot("NapCat.Onebot")
    bot.responses.update(
        {
            "get_group_info": {
                "group_id": 20001,
                "group_name": "测试群",
                "group_memo": "群简介",
                "member_count": 3,
                "max_member_count": 200,
                "owner_id": 10001,
            },
            "get_group_member_info": {
                "group_id": 20001,
                "user_id": 10002,
                "nickname": "成员",
                "card": "群名片",
                "role": "admin",
                "title": "头衔",
            },
            "get_group_member_list": [
                {"group_id": 20001, "user_id": 10002, "nickname": "成员"},
                {"group_id": 20001, "user_id": "bad"},
            ],
        }
    )
    adapter = adapter_module.get_protocol_adapter(bot, _config())

    group = asyncio.run(adapter.get_group_info(group_id="20001"))
    member = asyncio.run(adapter.get_group_member_info(group_id="20001", user_id="10002"))
    members = asyncio.run(adapter.get_group_member_list(group_id="20001"))
    card = asyncio.run(adapter.set_group_card(group_id="20001", user_id="10002", card="新名片"))
    clear_card = asyncio.run(adapter.set_group_card(group_id="20001", user_id="10002", card=""))
    title = asyncio.run(
        adapter.set_group_special_title(
            group_id="20001",
            user_id="10002",
            special_title="新头衔",
        )
    )

    assert group.data["group_name"] == "测试群"
    assert group.data["owner_id"] == "10001"
    assert member.data["card"] == "群名片"
    assert member.data["role"] == "admin"
    assert members.data == [
        {
            "group_id": "20001",
            "user_id": "10002",
            "nickname": "成员",
            "card": "",
            "card_or_nickname": "",
            "sex": "",
            "age": 0,
            "area": "",
            "level": "",
            "qq_level": 0,
            "join_time": 0,
            "last_sent_time": 0,
            "role": "member",
            "title": "",
            "title_expire_time": 0,
            "card_changeable": False,
            "is_robot": False,
            "shut_up_timestamp": 0,
        }
    ]
    assert card.ok and clear_card.ok and title.ok
    assert ("get_group_info", {"group_id": 20001, "no_cache": True}) in bot.calls
    assert ("get_group_member_info", {"group_id": 20001, "user_id": 10002, "no_cache": True}) in bot.calls
    assert ("get_group_member_list", {"group_id": 20001}) in bot.calls
    assert ("set_group_card", {"group_id": 20001, "user_id": 10002, "card": "新名片"}) in bot.calls
    assert ("set_group_card", {"group_id": 20001, "user_id": 10002, "card": ""}) in bot.calls
    assert (
        "set_group_special_title",
        {"group_id": 20001, "user_id": 10002, "special_title": "新头衔"},
    ) in bot.calls


def test_group_notice_paths_and_payload_are_normalized_per_implementation() -> None:
    for app_name, delete_action in (
        ("NapCat.Onebot", "_del_group_notice"),
        ("LLOneBot", "_delete_group_notice"),
    ):
        adapter_module.reset_protocol_adapters()
        bot = _Bot(app_name, "8.0.13")
        bot.responses["_get_group_notice"] = [
            {
                "fid": "notice-1",
                "sender_id": 10001,
                "publish_time": 123,
                "message": {
                    "text": "公告正文",
                    "image": {"image_id": "img-1", "width": "20", "height": "30"},
                },
                "settings": {"pinned": True, "confirm_required": True},
            }
        ]
        adapter = adapter_module.get_protocol_adapter(bot, _config())

        read_result = asyncio.run(adapter.get_group_notices(group_id="20001"))
        delete_result = asyncio.run(
            adapter.delete_group_notice(group_id="20001", notice_id="notice-1")
        )

        assert read_result.ok is True
        assert read_result.selected_path == "_get_group_notice"
        assert read_result.data[0]["notice_id"] == "notice-1"
        assert read_result.data[0]["message"] == {
            "text": "公告正文",
            "images": [{"id": "img-1", "width": 20, "height": 30}],
        }
        assert "fid" not in str(read_result.data)
        assert delete_result.ok is True
        assert delete_result.selected_path == delete_action
        assert (delete_action, {"group_id": 20001, "notice_id": "notice-1"}) in bot.calls
        assert not any("folder" in params for _action, params in bot.calls)


def test_group_notice_delete_timeout_is_not_retried_or_fallback() -> None:
    bot = _Bot("NapCat.Onebot")
    bot.fail_once["_del_group_notice"] = TimeoutError("slow")
    adapter = adapter_module.get_protocol_adapter(bot, _config())

    result = asyncio.run(
        adapter.delete_group_notice(group_id="20001", notice_id="notice-1")
    )

    assert result.status == "degraded"
    assert result.code == "timeout"
    assert bot.calls == [
        ("get_version_info", {}),
        ("_del_group_notice", {"group_id": 20001, "notice_id": "notice-1"}),
    ]


def test_group_extensions_disabled_keep_standard_member_actions_only() -> None:
    bot = _Bot("NapCat.Onebot")
    adapter = adapter_module.get_protocol_adapter(bot, _config("none"))
    matrix = asyncio.run(adapter.matrix())

    assert matrix.get("group.member.list").state.value == "available"
    assert matrix.get("group.member.card.write").state.value == "available"
    assert matrix.get("group.announcement.read").state.value == "disabled"
    notices = asyncio.run(adapter.get_group_notices(group_id="20001"))
    assert notices.status == "unavailable"
    assert bot.calls == []


def test_unknown_implementation_does_not_probe_notice_delete() -> None:
    bot = _Bot("UnknownOneBot")
    adapter = adapter_module.get_protocol_adapter(bot, _config())

    result = asyncio.run(
        adapter.delete_group_notice(group_id="20001", notice_id="notice-1")
    )

    assert result.status == "unavailable"
    assert bot.calls == [("get_version_info", {})]
