from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ._loader import load_personification_module


qzone_service = load_personification_module("plugin.personification.core.qzone_service")

BOT_ID = "99999"
OWNER_ID = "20001"
ACTOR_ID = "30001"


class _Logger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def _record(self, message: object) -> None:
        self.messages.append(str(message))

    info = _record
    warning = _record
    error = _record


def _config():  # noqa: ANN202
    return SimpleNamespace(
        personification_qzone_enabled=True,
        personification_qzone_cookie=(
            f"uin=o{BOT_ID}; skey=sk; p_uin=o{BOT_ID}; p_skey=ps;"
        ),
    )


def _authorization(**overrides):  # noqa: ANN003, ANN202
    permissions = {
        "blocked": False,
        "allow_context_read": True,
        "allow_qzone": True,
        "allow_visible_reaction": True,
        "allow_reply": True,
    }
    permissions.update(overrides)
    return SimpleNamespace(**permissions)


def _feed(*, owner: str = OWNER_ID):
    return {
        "owner_uin": owner,
        "feed_id": "feed1",
        "topic_id": f"{owner}_feed1__1",
        "unikey": f"http://user.qzone.qq.com/{owner}/mood/feed1",
        "appid": "311",
    }


def _install_recording_client(monkeypatch):  # noqa: ANN001, ANN202
    calls: list[tuple[str, str]] = []

    class _Response:
        status_code = 200

        def __init__(self, text: str) -> None:
            self.text = text

    class _Client:
        def __init__(self, **_kwargs) -> None:  # noqa: ANN003
            return None

        async def __aenter__(self):  # noqa: ANN201
            return self

        async def __aexit__(self, *_args) -> None:  # noqa: ANN003
            return None

        async def get(self, url, **_kwargs):  # noqa: ANN001, ANN003, ANN201
            calls.append(("get", str(url)))
            return _Response(
                '{"code":0,"msglist":[{"uin":"20001","tid":"feed1","content":"ok"}]}'
            )

        async def post(self, url, **_kwargs):  # noqa: ANN001, ANN003, ANN201
            calls.append(("post", str(url)))
            return _Response('{"code":0}')

    monkeypatch.setattr(qzone_service.httpx, "AsyncClient", _Client)
    return calls


def test_blocked_sinks_do_not_call_qzone_api(monkeypatch) -> None:  # noqa: ANN001
    calls = _install_recording_client(monkeypatch)

    async def deny(_user_id: str):  # noqa: ANN202
        return _authorization(
            allow_context_read=False,
            allow_qzone=False,
            allow_visible_reaction=False,
            allow_reply=False,
        )

    service = qzone_service.QzoneSocialService(_config(), _Logger(), deny)

    async def run():  # noqa: ANN202
        fetch_result = await service.fetch_user_feeds(target_uin=OWNER_ID, bot_id=BOT_ID)
        like_result = await service.like_feed(feed=_feed(), bot_id=BOT_ID)
        forward_result = await service.forward_feed(feed=_feed(), bot_id=BOT_ID)
        comment_result = await service.comment_feed(
            feed=_feed(),
            bot_id=BOT_ID,
            content="blocked",
        )
        return fetch_result, like_result, tuple(forward_result), comment_result

    fetch_result, like_result, forward_result, comment_result = asyncio.run(run())

    assert fetch_result == (False, "policy_blocked", [])
    assert like_result == (False, "policy_blocked")
    assert forward_result == (False, "policy_blocked")
    assert comment_result == (False, "policy_blocked")
    assert calls == []


def test_comment_reply_requires_actor_allow_reply(monkeypatch) -> None:  # noqa: ANN001
    calls = _install_recording_client(monkeypatch)
    authorized_users: list[str] = []

    async def authorize(user_id: str):  # noqa: ANN202
        authorized_users.append(user_id)
        if user_id == ACTOR_ID:
            return _authorization(allow_reply=False)
        return _authorization()

    service = qzone_service.QzoneSocialService(_config(), _Logger(), authorize)
    result = asyncio.run(
        service.comment_feed(
            feed=_feed(),
            bot_id=BOT_ID,
            content="reply",
            reply_to_comment={"user_id": ACTOR_ID, "comment_id": "comment1"},
        )
    )

    assert result == (False, "policy_blocked")
    assert authorized_users == [OWNER_ID, ACTOR_ID]
    assert calls == []


def test_authorizer_exception_fails_closed_without_raw_error(monkeypatch) -> None:  # noqa: ANN001
    calls = _install_recording_client(monkeypatch)
    logger = _Logger()
    raw_error = "private-policy-store-detail"

    async def fail(_user_id: str):  # noqa: ANN202
        raise RuntimeError(raw_error)

    service = qzone_service.QzoneSocialService(_config(), logger, fail)
    result = asyncio.run(service.like_feed(feed=_feed(), bot_id=BOT_ID))

    assert result == (False, "policy_blocked")
    assert calls == []
    assert all(raw_error not in message for message in logger.messages)


def test_bot_self_fetch_bypasses_user_policy(monkeypatch) -> None:  # noqa: ANN001
    calls = _install_recording_client(monkeypatch)
    authorized_users: list[str] = []

    async def deny(user_id: str):  # noqa: ANN202
        authorized_users.append(user_id)
        return _authorization(allow_context_read=False, allow_qzone=False)

    service = qzone_service.QzoneSocialService(_config(), _Logger(), deny)
    ok, message, feeds = asyncio.run(
        service.fetch_user_feeds(target_uin=BOT_ID, bot_id=BOT_ID)
    )

    assert (ok, message) == (True, "ok")
    assert len(feeds) == 1
    assert authorized_users == []
    assert [method for method, _url in calls] == ["get"]


def test_bot_self_feed_actions_bypass_user_policy(monkeypatch) -> None:  # noqa: ANN001
    calls = _install_recording_client(monkeypatch)
    authorized_users: list[str] = []

    async def deny(user_id: str):  # noqa: ANN202
        authorized_users.append(user_id)
        return _authorization(
            allow_qzone=False,
            allow_visible_reaction=False,
            allow_reply=False,
        )

    service = qzone_service.QzoneSocialService(_config(), _Logger(), deny)

    async def run():  # noqa: ANN202
        feed = _feed(owner=BOT_ID)
        like_result = await service.like_feed(feed=feed, bot_id=BOT_ID)
        forward_result = await service.forward_feed(feed=feed, bot_id=BOT_ID)
        comment_result = await service.comment_feed(
            feed=feed,
            bot_id=BOT_ID,
            content="self feed",
        )
        return like_result, tuple(forward_result), comment_result

    like_result, forward_result, comment_result = asyncio.run(run())

    assert like_result == (True, "ok")
    assert forward_result == (True, "ok")
    assert comment_result == (True, "ok")
    assert authorized_users == []
    assert [method for method, _url in calls] == ["post", "post", "post"]


def test_allowed_policy_reaches_each_qzone_api(monkeypatch) -> None:  # noqa: ANN001
    calls = _install_recording_client(monkeypatch)
    authorized_users: list[str] = []

    async def allow(user_id: str):  # noqa: ANN202
        authorized_users.append(user_id)
        return _authorization()

    service = qzone_service.build_qzone_social_service(
        _config(),
        _Logger(),
        user_policy_authorizer=allow,
    )

    async def run():  # noqa: ANN202
        fetch_result = await service.fetch_user_feeds(target_uin=OWNER_ID, bot_id=BOT_ID)
        like_result = await service.like_feed(feed=_feed(), bot_id=BOT_ID)
        forward_result = await service.forward_feed(feed=_feed(), bot_id=BOT_ID)
        comment_result = await service.comment_feed(
            feed=_feed(),
            bot_id=BOT_ID,
            content="allowed",
        )
        return fetch_result, like_result, tuple(forward_result), comment_result

    fetch_result, like_result, forward_result, comment_result = asyncio.run(run())

    assert fetch_result[:2] == (True, "ok")
    assert like_result == (True, "ok")
    assert forward_result == (True, "ok")
    assert comment_result == (True, "ok")
    assert authorized_users == [OWNER_ID, OWNER_ID, OWNER_ID, OWNER_ID]
    assert [method for method, _url in calls] == ["get", "post", "post", "post"]
