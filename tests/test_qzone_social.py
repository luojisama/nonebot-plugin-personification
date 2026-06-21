from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

from ._loader import load_personification_module

proactive_flow = load_personification_module("plugin.personification.flows.proactive_flow")
qzone_flow = load_personification_module("plugin.personification.flows.qzone_social_flow")
qzone_service = load_personification_module("plugin.personification.core.qzone_service")
admin_commands = load_personification_module("plugin.personification.handlers.persona_admin_commands")


class _PersonaStore:
    def __init__(self, snippets: dict[str, str]) -> None:
        self.snippets = snippets

    def get_persona_snippet(self, user_id: str, max_chars: int = 150) -> str:
        return self.snippets.get(str(user_id), "")[:max_chars]


class _RecordingPersonaStore(_PersonaStore):
    def __init__(self) -> None:
        super().__init__({})
        self.records: list[tuple[str, str]] = []

    async def record_message(self, user_id: str, text: str) -> None:
        self.records.append((str(user_id), str(text)))


class _Logger:
    def warning(self, _message: str) -> None:
        return None


class _Bot:
    self_id = "99999"


class _NoFriendBot:
    self_id = "99999"

    async def get_friend_list(self):  # noqa: ANN201
        return []


class _InboundQzoneService:
    def __init__(self) -> None:
        self.replies: list[str] = []
        self.reply_targets: list[str] = []

    async def fetch_user_feeds(self, **_kwargs):  # noqa: ANN003
        return True, "ok", [
            {
                "feed_key": "99999:feed1",
                "feed_id": "feed1",
                "owner_uin": "99999",
                "topic_id": "99999_feed1__1",
                "content": "今天也普通地发一条",
                "raw": {
                    "commentlist": [
                        {
                            "uin": "20001",
                            "nickname": "好友",
                            "content": "真寻今天也在吗",
                            "created_time": 1710000010,
                            "commentid": "c1",
                        }
                    ]
                },
            }
        ]

    async def comment_feed(self, *, content: str, **_kwargs):  # noqa: ANN003
        self.replies.append(str(content))
        reply_to_comment = _kwargs.get("reply_to_comment")
        if isinstance(reply_to_comment, dict):
            self.reply_targets.append(str(reply_to_comment.get("comment_id", "") or ""))
        return True, "ok"


def test_qzone_reply_content_targets_commenter_name() -> None:
    text = qzone_service._format_qzone_reply_content(
        "你木飞了",
        {"nickname": "白咲零", "user_id": "20001"},
    )
    assert text == "@{uin:20001,nick:白咲零,who:1} 你木飞了"


def test_qzone_reply_content_does_not_double_prefix() -> None:
    text = qzone_service._format_qzone_reply_content(
        "回复 白咲零: 已经收到了",
        {"nickname": "白咲零", "user_id": "20001"},
    )
    assert text == "回复 白咲零: 已经收到了"


def test_qzone_comment_feed_uses_threaded_subreply_payload(monkeypatch) -> None:  # noqa: ANN001
    calls: list[dict[str, object]] = []

    class _Response:
        status_code = 200
        text = '{"code":0}'

    class _Client:
        def __init__(self, **_kwargs) -> None:  # noqa: ANN003
            return None

        async def __aenter__(self):  # noqa: ANN201
            return self

        async def __aexit__(self, *_args) -> None:  # noqa: ANN003
            return None

        async def post(self, url, *, params=None, data=None, headers=None):  # noqa: ANN001, ANN201
            calls.append({"url": url, "params": params, "data": data, "headers": headers})
            return _Response()

    monkeypatch.setattr(qzone_service.httpx, "AsyncClient", _Client)
    service = qzone_service.QzoneSocialService(
        SimpleNamespace(
            personification_qzone_enabled=True,
            personification_qzone_cookie="uin=o99999; skey=sk; p_uin=o99999; p_skey=ps;",
        ),
        _Logger(),
    )

    ok, msg = asyncio.run(
        service.comment_feed(
            feed={
                "owner_uin": "99999",
                "feed_id": "feed1",
                "topic_id": "99999_feed1__1",
                "appid": "311",
                "raw": {"t1_source": "", "t1_subtype": "55702"},
            },
            bot_id="99999",
            content="测试收到",
            reply_to_comment={"user_id": "20001", "comment_id": "c1", "nickname": "白咲零"},
        )
    )

    assert ok is True
    assert msg == "ok"
    assert calls[0]["url"] == "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_re_feeds"
    data = calls[0]["data"]
    assert data["t1_uin"] == "99999"
    assert data["t1_tid"] == "feed1"
    assert data["t2_uin"] == "20001"
    assert data["t2_tid"] == "c1"
    assert data["subdotype"] == "55702"
    assert data["commentid"] == "c1"
    assert data["replyUin"] == "20001"
    assert data["content"] == "测试收到"


def test_qzone_comment_feed_falls_back_to_top_level_rich_mention(monkeypatch) -> None:  # noqa: ANN001
    calls: list[dict[str, object]] = []

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

        async def post(self, url, *, params=None, data=None, headers=None):  # noqa: ANN001, ANN201
            calls.append({"url": url, "params": params, "data": data, "headers": headers})
            if len(calls) == 1:
                return _Response('{"code":-1,"message":"bad subreply"}')
            return _Response('{"code":0}')

    monkeypatch.setattr(qzone_service.httpx, "AsyncClient", _Client)
    service = qzone_service.QzoneSocialService(
        SimpleNamespace(
            personification_qzone_enabled=True,
            personification_qzone_cookie="uin=o99999; skey=sk; p_uin=o99999; p_skey=ps;",
        ),
        _Logger(),
    )

    ok, msg = asyncio.run(
        service.comment_feed(
            feed={
                "owner_uin": "99999",
                "feed_id": "feed1",
                "topic_id": "99999_feed1__1",
                "appid": "311",
            },
            bot_id="99999",
            content="测试收到",
            reply_to_comment={"user_id": "20001", "comment_id": "c1", "nickname": "白咲零"},
        )
    )

    assert ok is True
    assert msg == "ok"
    assert [call["url"] for call in calls] == [
        "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_re_feeds",
        "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_re_feeds",
    ]
    assert calls[0]["data"]["content"] == "测试收到"
    assert calls[1]["data"]["content"] == "@{uin:20001,nick:白咲零,who:1} 测试收到"


def test_proactive_candidates_require_profile_and_not_favorability_threshold() -> None:
    candidates = proactive_flow._build_candidates(
        all_user_data={
            "10001": {"favorability": 1.0, "nickname": "low"},
            "10002": {"favorability": 99.0, "nickname": "high"},
            "group_1": {"favorability": 100.0},
        },
        proactive_state={
            "10001": {"last_interaction": 1000},
            "10002": {"last_interaction": 1000},
        },
        inner_state={},
        emotion_state={},
        now=datetime.fromtimestamp(100000),
        threshold=60.0,
        idle_hours=0,
        friend_ids={"10001", "10002"},
        persona_store=_PersonaStore({"10001": "已有画像"}),
        require_user_profile=True,
    )

    assert [item["user_id"] for item in candidates] == ["10001"]
    assert candidates[0]["favorability"] == 1.0


def test_proactive_candidates_skip_unanswered_proactive_message() -> None:
    candidates = proactive_flow._build_candidates(
        all_user_data={
            "10001": {"favorability": 99.0, "nickname": "no-reply"},
            "10002": {"favorability": 1.0, "nickname": "replied"},
        },
        proactive_state={
            "10001": {"last_interaction": 900, "last_proactive_at": 1000},
            "10002": {"last_interaction": 1100, "last_proactive_at": 1000},
        },
        inner_state={},
        emotion_state={},
        now=datetime.fromtimestamp(100000),
        threshold=60.0,
        idle_hours=0,
        friend_ids={"10001", "10002"},
        persona_store=_PersonaStore({"10001": "已有画像", "10002": "已有画像"}),
        require_user_profile=True,
    )

    assert [item["user_id"] for item in candidates] == ["10002"]


def test_proactive_history_records_recent_messages() -> None:
    state: dict[str, object] = {}

    proactive_flow._append_proactive_history(state, message="第一句", sent_at=100)
    proactive_flow._append_proactive_history(state, message="第二句", sent_at=200, max_items=1)

    assert state["last_proactive_message"] == "第二句"
    assert state["proactive_history"] == [{"at": 200.0, "message": "第二句"}]
    assert "第二句" in proactive_flow._format_proactive_history_for_state(state)


def test_qzone_feed_normalization_extracts_text_images_and_keys() -> None:
    feed = qzone_service._normalize_qzone_feed(
        {
            "tid": "abc",
            "uin": "12345",
            "nickname": "好友",
            "content": "今天<br>不错",
            "pic": [{"url1": "//img.example/a.jpg"}],
            "created_time": 1710000000,
        },
        target_uin="12345",
    )

    assert feed is not None
    assert feed["feed_key"] == "12345:abc"
    assert feed["content"] == "今天 不错"
    assert feed["images"] == ["https://img.example/a.jpg"]
    assert feed["topic_id"] == "12345_abc__1"


def test_qzone_comment_extraction_normalizes_user_and_content() -> None:
    comments = qzone_service._extract_qzone_comments(
        {
            "commentlist": [
                {
                    "uin": "22222",
                    "nickname": "留言者",
                    "content": "看到啦<br>挺好",
                    "created_time": 1710000010,
                    "commentid": "c1",
                }
            ]
        }
    )

    assert comments[0]["comment_key"].startswith("22222:c1")
    assert comments[0]["nickname"] == "留言者"
    assert comments[0]["content"] == "看到啦 挺好"


def test_qzone_comment_extraction_accepts_runtime_alias_fields() -> None:
    comments = qzone_service._extract_qzone_comments(
        {
            "commentlist": [
                {
                    "useruin": "22222",
                    "postername": "留言者",
                    "html": "看到啦<br>挺好",
                    "createTime": 1710000010,
                    "commentId": "c1",
                }
            ]
        }
    )

    assert len(comments) == 1
    assert comments[0]["comment_key"].startswith("22222:c1")
    assert comments[0]["nickname"] == "留言者"
    assert comments[0]["content"] == "看到啦 挺好"
    assert comments[0]["created_at"] == 1710000010


def test_qzone_comment_extraction_flattens_nested_reply_to_bot() -> None:
    comments = qzone_service._extract_qzone_comments(
        {
            "commentlist": [
                {
                    "uin": "99999",
                    "nickname": "白咲真寻",
                    "content": "aaaa，明天我也想先躺平一下",
                    "created_time": 1710000001,
                    "commentid": "bot-c1",
                    "replylist": [
                        {
                            "uin": "20001",
                            "nickname": "白咲零",
                            "content": "bbbb",
                            "created_time": 1710000100,
                            "commentid": "u-c2",
                        }
                    ],
                }
            ]
        }
    )

    assert [item["user_id"] for item in comments] == ["99999", "20001"]
    assert comments[1]["content"] == "bbbb"
    assert comments[1]["parent_user_id"] == "99999"
    assert comments[1]["parent_comment_id"] == "bot-c1"
    assert comments[1]["reply_to_user_id"] == "99999"


def test_qzone_test_target_can_use_open_user_without_friend_profile() -> None:
    candidates = qzone_flow._collect_candidates(
        friend_profiles={},
        proactive_state={},
        persona_store=_PersonaStore({}),
        persona_snippet_max_chars=80,
        target_user_id="3330186224",
        allow_open_user=True,
    )

    assert candidates == [
        {
            "user_id": "3330186224",
            "nickname": "3330186224",
            "persona_snippet": "暂无画像",
            "last_interaction": 0.0,
            "is_friend": False,
        }
    ]


def test_qzone_non_test_target_still_requires_bot_friend() -> None:
    candidates = qzone_flow._collect_candidates(
        friend_profiles={},
        proactive_state={},
        persona_store=_PersonaStore({}),
        persona_snippet_max_chars=80,
        target_user_id="3330186224",
        allow_open_user=False,
    )

    assert candidates == []


def test_qzone_regular_scan_falls_back_to_bot_friends_without_private_state() -> None:
    candidates = qzone_flow._collect_candidates(
        friend_profiles={
            "20002": {"nickname": "二号好友"},
            "20001": {"nickname": "一号好友"},
        },
        proactive_state={},
        persona_store=_PersonaStore({"20001": "画像一"}),
        persona_snippet_max_chars=80,
    )

    assert [item["user_id"] for item in candidates] == ["20001", "20002"]
    assert candidates[0]["is_friend"] is True
    assert candidates[0]["persona_snippet"] == "画像一"


def test_qzone_profile_evidence_records_once_with_source_label() -> None:
    store = _RecordingPersonaStore()
    state: dict[str, object] = {}
    result: dict[str, object] = {}

    asyncio.run(
        qzone_flow._record_qzone_profile_evidence(
            persona_store=store,
            user_id="20001",
            evidence_key="feed:20001:abc",
            kind="动态",
            content="今天去了便利店",
            image_summary="一张饮料和便当的照片",
            state=state,
            result=result,
            logger=_Logger(),
        )
    )
    asyncio.run(
        qzone_flow._record_qzone_profile_evidence(
            persona_store=store,
            user_id="20001",
            evidence_key="feed:20001:abc",
            kind="动态",
            content="今天去了便利店",
            state=state,
            result=result,
            logger=_Logger(),
        )
    )

    assert len(store.records) == 1
    assert store.records[0][0] == "20001"
    assert "[QQ空间动态]" in store.records[0][1]
    assert "图片摘要：一张饮料和便当的照片" in store.records[0][1]
    assert result["profile_records"] == 1


def test_qzone_inbound_comment_replies_once_and_records_profile() -> None:
    store = _RecordingPersonaStore()
    service = _InboundQzoneService()
    state: dict[str, object] = {}
    result: dict[str, object] = {"inbound_comments": 0, "replied": 0, "profile_records": 0}

    async def _call_ai(_messages):  # noqa: ANN001
        return '{"action":"reply","reply":"在呀，刚看到","reason":"自然接话"}'

    kwargs = dict(
        bot=_Bot(),
        qzone_social_service=service,
        friend_profiles={"20001": {"nickname": "好友"}},
        proactive_state={},
        persona_store=store,
        persona_snippet_max_chars=80,
        system_prompt="你是绪山真寻。",
        call_ai_api=_call_ai,
        inner_state={},
        emotion_state={},
        max_feeds=20,
        max_comments_per_feed=20,
        count_checked_feeds=True,
        state=state,
        result=result,
        logger=_Logger(),
    )

    asyncio.run(qzone_flow._scan_bot_space_comments(**kwargs))
    asyncio.run(qzone_flow._scan_bot_space_comments(**kwargs))

    assert service.replies == ["在呀，刚看到"]
    assert service.reply_targets == ["c1"]
    assert result["inbound_comments"] == 1
    assert result["replied"] == 1
    assert len(store.records) == 1
    assert "[QQ空间留言]" in store.records[0][1]


def test_qzone_inbound_comment_does_not_require_friend_profile() -> None:
    store = _RecordingPersonaStore()
    service = _InboundQzoneService()

    async def _call_ai(_messages):  # noqa: ANN001
        return '{"action":"reply","reply":"刚看见这条","reason":"自然接话"}'

    original_normalize_state = qzone_flow._normalize_state
    original_save_inbound_state = qzone_flow._save_inbound_state
    qzone_flow._normalize_state = lambda _now: {
        "bot_space_comment_baselines": {
            "99999:feed1": {"at": 1710000000.0, "max_created_at": 1710000000.0}
        },
        "comment_actions": {},
        "comment_replies": {},
        "profile_records": {},
    }
    qzone_flow._save_inbound_state = lambda _state, _result: None
    try:
        result = asyncio.run(
            qzone_flow.scan_qzone_inbound_messages(
                bot=_NoFriendBot(),
                plugin_config=SimpleNamespace(
                    personification_persona_snippet_max_chars=80,
                    personification_qzone_inbound_max_feeds_per_scan=20,
                    personification_qzone_inbound_max_comments_per_feed=20,
                    personification_qzone_third_party_chime_in_enabled=True,
                    personification_qzone_outbound_reply_enabled=False,
                ),
                qzone_social_service=service,
                load_prompt=lambda: "你是绪山真寻。",
                call_ai_api=_call_ai,
                load_proactive_state=lambda: {},
                get_now=lambda: datetime.fromtimestamp(1710000100),
                logger=_Logger(),
                persona_store=store,
            )
        )
    finally:
        qzone_flow._normalize_state = original_normalize_state
        qzone_flow._save_inbound_state = original_save_inbound_state

    assert result["ok"] is True
    assert result.get("skipped") is not True
    assert result["inbound_comments"] == 1
    assert result["replied"] == 1
    assert service.replies == ["刚看见这条"]


def test_qzone_inbound_first_scan_baselines_existing_comments() -> None:
    store = _RecordingPersonaStore()
    service = _InboundQzoneService()
    state: dict[str, object] = {}
    result: dict[str, object] = {"inbound_comments": 0, "replied": 0, "profile_records": 0}
    calls: list[object] = []

    async def _call_ai(_messages):  # noqa: ANN001
        calls.append(_messages)
        return '{"action":"reply","reply":"不该补回旧留言","reason":"old"}'

    kwargs = dict(
        bot=_Bot(),
        qzone_social_service=service,
        friend_profiles={},
        proactive_state={},
        persona_store=store,
        persona_snippet_max_chars=80,
        system_prompt="你是绪山真寻。",
        call_ai_api=_call_ai,
        inner_state={},
        emotion_state={},
        max_feeds=20,
        max_comments_per_feed=20,
        count_checked_feeds=True,
        process_existing_comments=False,
        state=state,
        result=result,
        logger=_Logger(),
    )

    asyncio.run(qzone_flow._scan_bot_space_comments(**kwargs))
    asyncio.run(qzone_flow._scan_bot_space_comments(**kwargs))

    assert calls == []
    assert service.replies == []
    assert result["inbound_comments"] == 0
    assert "99999:feed1" in state["bot_space_comment_baselines"]


def test_qzone_outbound_reply_interval_gate_uses_config_minutes() -> None:
    state: dict[str, object] = {}

    assert qzone_flow._outbound_reply_scan_due(state, now_ts=1000.0, interval_minutes=3) is True
    qzone_flow._mark_outbound_reply_scan(state, now_ts=1000.0)
    assert qzone_flow._outbound_reply_scan_due(state, now_ts=1179.0, interval_minutes=3) is False
    assert qzone_flow._outbound_reply_scan_due(state, now_ts=1180.0, interval_minutes=3) is True


def test_qzone_comment_reply_prompt_respects_third_party_chime_switch() -> None:
    captured: dict[str, str] = {}

    async def _call_ai(messages):  # noqa: ANN001
        captured["prompt"] = str(messages[-1]["content"])
        return '{"action":"ignore","reply":"","reason":"third party"}'

    decision = asyncio.run(
        qzone_flow._decide_bot_comment_reply(
            feed={"content": "今天发一条"},
            comment={"user_id": "20001", "nickname": "好友", "content": "你们刚才说的那个呢"},
            commenter_profile={"nickname": "好友", "persona_snippet": "熟人"},
            system_prompt="你是绪山真寻。",
            call_ai_api=_call_ai,
            inner_state={},
            emotion_memory="",
            allow_third_party_chime_in=False,
        )
    )

    assert decision["action"] == "ignore"
    assert "不要插入第三方对话" in captured["prompt"]


def test_qzone_comment_reply_prompt_marks_reply_to_bot_continuation() -> None:
    captured: dict[str, str] = {}

    async def _call_ai(messages):  # noqa: ANN001
        captured["prompt"] = str(messages[-1]["content"])
        return '{"action":"ignore","reply":"","reason":"stop here"}'

    decision = asyncio.run(
        qzone_flow._decide_bot_comment_reply(
            feed={"content": "aaaa"},
            comment={
                "user_id": "20001",
                "nickname": "白咲零",
                "content": "bbbb",
                "reply_to_user_id": "99999",
            },
            commenter_profile={"nickname": "白咲零", "persona_snippet": "熟人"},
            system_prompt="你是绪山真寻。",
            call_ai_api=_call_ai,
            inner_state={},
            emotion_memory="",
            bot_id="99999",
            is_reply_to_bot=True,
            previous_bot_reply="aaaa，明天我也想先躺平一下",
            recent_thread="我：aaaa，明天我也想先躺平一下\n白咲零 回复我（当前）：bbbb",
            interaction_rounds=1,
        )
    )

    assert decision["action"] == "ignore"
    assert "当前是评论回响场景" in captured["prompt"]
    assert "你上一句评论：aaaa，明天我也想先躺平一下" in captured["prompt"]
    assert "判断对方这句是否还有继续聊下去的价值" in captured["prompt"]
    assert "白咲零 回复我（当前）：bbbb" in captured["prompt"]


def test_qzone_outbound_reply_to_bot_can_be_ignored_by_llm() -> None:
    class _OutboundService:
        def __init__(self) -> None:
            self.replies: list[str] = []

        async def fetch_user_feeds(self, **_kwargs):  # noqa: ANN003
            return True, "ok", [
                {
                    "feed_key": "20001:feed1",
                    "feed_id": "feed1",
                    "owner_uin": "20001",
                    "topic_id": "20001_feed1__1",
                    "content": "aaaa",
                    "raw": {
                        "commentlist": [
                            {
                                "uin": "99999",
                                "nickname": "白咲真寻",
                                "content": "aaaa，明天我也想先躺平一下",
                                "created_time": 1710000001,
                                "commentid": "bot-c1",
                            },
                            {
                                "uin": "20001",
                                "nickname": "白咲零",
                                "content": "bbbb",
                                "created_time": 1710000100,
                                "commentid": "u-c2",
                                "replyuin": "99999",
                            },
                        ]
                    },
                }
            ]

        async def comment_feed(self, *, content: str, **_kwargs):  # noqa: ANN003
            self.replies.append(str(content))
            return True, "ok"

    service = _OutboundService()
    captured: dict[str, str] = {}

    async def _call_ai(messages):  # noqa: ANN001
        captured["prompt"] = str(messages[-1]["content"])
        return '{"action":"ignore","reply":"","reason":"no need"}'

    state: dict[str, object] = {
        "bot_outbound_comments": {
            "20001:feed1": {
                "target_uin": "20001",
                "bot_comment": "aaaa，明天我也想先躺平一下",
                "last_bot_reply": "aaaa，明天我也想先躺平一下",
                "recorded_at": 1710000000,
                "last_seen_ts": 1710000001,
                "reply_chain_count": 0,
            }
        },
        "bot_outbound_replies": {},
    }
    result: dict[str, object] = {"inbound_comments": 0, "replied": 0, "failed": 0}

    asyncio.run(
        qzone_flow._scan_bot_outbound_comment_replies(
            bot=_Bot(),
            qzone_social_service=service,
            plugin_config=SimpleNamespace(
                personification_qzone_outbound_reply_lookback_hours=999999,
                personification_qzone_outbound_reply_max_feeds=30,
            ),
            friend_profiles={"20001": {"nickname": "白咲零"}},
            proactive_state={},
            persona_store=_PersonaStore({}),
            persona_snippet_max_chars=80,
            system_prompt="你是绪山真寻。",
            call_ai_api=_call_ai,
            inner_state={},
            emotion_state={},
            state=state,
            result=result,
            logger=_Logger(),
        )
    )

    assert result["inbound_comments"] == 1
    assert result["replied"] == 0
    assert service.replies == []
    assert "当前是评论回响场景" in captured["prompt"]
    assert "对方留言：bbbb" in captured["prompt"]
    assert "20001:feed1:20001:u-c2:bbbb:1710000100" in state["bot_outbound_replies"]
    assert state["bot_outbound_replies"]["20001:feed1:20001:u-c2:bbbb:1710000100"]["action"] == "ignore"


def test_qzone_social_limits_use_zero_as_unlimited() -> None:
    assert qzone_flow._limit_reached(0, 9999) is False
    assert qzone_flow._limit_reached(2, 2) is True
    assert qzone_flow._limit_reached(2, 1) is False


def test_qzone_admin_target_accepts_at_and_plain_qq() -> None:
    at_message = [
        SimpleNamespace(type="at", data={"qq": "12345678"}),
        SimpleNamespace(type="text", data={"text": " "}),
    ]

    assert admin_commands._extract_qzone_target_user_id([], at_message) == "12345678"
    assert admin_commands._extract_qzone_target_user_id(["测试", "87654321"], None) == "87654321"
