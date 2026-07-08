from __future__ import annotations

from types import SimpleNamespace

from ._loader import load_personification_module

event_rules = load_personification_module("plugin.personification.handlers.event_rules")
group_context = load_personification_module("plugin.personification.core.group_context")
builtin_hooks = load_personification_module("plugin.personification.core.builtin_hooks")
target_inference = load_personification_module("plugin.personification.core.target_inference")
db = load_personification_module("plugin.personification.core.db")
data_store = load_personification_module("plugin.personification.core.data_store")
utils = load_personification_module("plugin.personification.utils")


def test_target_inference_treats_reply_to_non_bot_as_others() -> None:
    event = SimpleNamespace(
        message=[],
        reply=SimpleNamespace(message_id="m-human", sender=SimpleNamespace(user_id="10001")),
    )

    result = target_inference.infer_message_target(
        event,
        bot_self_id="bot-1",
        recent_group_msgs=[
            {"message_id": "m-human", "user_id": "10001", "is_bot": False},
            {"message_id": "m-bot", "user_id": "bot-1", "is_bot": True},
        ],
    )

    assert result == target_inference.TARGET_OTHERS


def test_target_inference_treats_reply_to_bot_as_bot_target() -> None:
    event = SimpleNamespace(
        message=[],
        reply=SimpleNamespace(message_id="m-bot", sender=SimpleNamespace(user_id="bot-1")),
    )

    result = target_inference.infer_message_target(
        event,
        bot_self_id="bot-1",
        recent_group_msgs=[{"message_id": "m-bot", "user_id": "bot-1", "is_bot": True}],
    )

    assert result == target_inference.TARGET_BOT


def test_target_inference_normalizes_constants_for_downstream_labels() -> None:
    assert target_inference.normalize_message_target_for_review(target_inference.TARGET_BOT) == "bot"
    assert target_inference.normalize_message_target_for_plan(target_inference.TARGET_BOT) == "bot"
    assert target_inference.normalize_message_target_for_review(target_inference.TARGET_OTHERS) == "others"
    assert target_inference.normalize_message_target_for_plan(target_inference.TARGET_OTHERS) == "someone_else"
    assert target_inference.normalize_message_target_for_review(target_inference.TARGET_UNCLEAR) == "uncertain"


def test_record_message_marks_same_bot_generic_output_as_plugin_source() -> None:
    captured: dict[str, object] = {}

    def _record_group_msg(*args, **kwargs):  # noqa: ANN001
        captured["args"] = args
        captured["kwargs"] = kwargs
        return 1

    event = SimpleNamespace(
        group_id="123",
        user_id="bot-1",
        self_id="bot-1",
        sender=SimpleNamespace(card="", nickname="bot"),
        message_id="msg-1",
        message=None,
        reply=None,
        get_plaintext=lambda: "其他插件生成的结果",
    )

    group_id, should_analyze = event_rules.resolve_record_message(
        event,
        get_custom_title=lambda _uid: "",
        record_group_msg=_record_group_msg,
    )

    assert group_id == "123"
    assert should_analyze is False
    assert captured["kwargs"]["source_kind"] == "plugin"


def test_record_user_plugin_command_as_context_only_source() -> None:
    captured: dict[str, object] = {}

    def _record_group_msg(*args, **kwargs):  # noqa: ANN001
        captured["args"] = args
        captured["kwargs"] = kwargs
        return 250

    event = SimpleNamespace(
        group_id="123",
        user_id="10001",
        self_id="bot-1",
        sender=SimpleNamespace(card="甲", nickname="甲"),
        message_id="msg-cmd",
        message=None,
        reply=None,
        get_plaintext=lambda: "/天气 北京",
    )

    group_id, should_analyze = event_rules.resolve_record_message(
        event,
        get_custom_title=lambda _uid: "",
        record_group_msg=_record_group_msg,
    )

    assert group_id == "123"
    assert should_analyze is False
    assert captured["args"][2] == "[用户调用其它插件/命令] /天气 北京"
    assert captured["kwargs"]["source_kind"] == "plugin_command"


def test_group_context_renders_plugin_source() -> None:
    text = group_context.render_group_context_structured(
        [
            {
                "nickname": "bot",
                "user_id": "bot-1",
                "content": "查询结果",
                "source_kind": "plugin",
                "message_id": "m1",
            }
        ]
    )

    assert "来源=其他插件输出" in text


def test_group_context_renders_plugin_command_source() -> None:
    text = group_context.render_group_context_structured(
        [
            {
                "nickname": "甲",
                "user_id": "10001",
                "content": "[用户调用其它插件/命令] /天气 北京",
                "source_kind": "plugin_command",
                "message_id": "m1",
            }
        ]
    )

    assert "来源=用户调用其它插件/命令" in text


def test_group_conversation_context_tracks_quote_chain_and_bot_replies() -> None:
    context = group_context.build_group_conversation_context(
        recent_messages=[
            {
                "message_id": "m1",
                "nickname": "甲",
                "user_id": "u1",
                "content": "先说第一句",
            },
            {
                "message_id": "m2",
                "reply_to_msg_id": "m1",
                "nickname": "bot",
                "user_id": "bot-1",
                "content": "我接了一句",
                "source_kind": "bot_reply",
            },
            {
                "message_id": "m3",
                "reply_to_msg_id": "m2",
                "nickname": "乙",
                "user_id": "u2",
                "content": "那继续呢",
            },
        ],
        trigger_msg_id="m3",
        trigger_user_id="u2",
        bot_self_id="bot-1",
        repeat_clusters=[{"text": "那继续呢", "count": 2}],
        bot_recent_replies=["我接了一句"],
    )

    rendered = group_context.render_group_conversation_context(context)

    assert [item["message_id"] for item in context.quote_chain] == ["m1", "m2", "m3"]
    assert context.speaker_relations["u2"] == "乙"
    assert "引用链" in rendered
    assert "bot 最近回复：我接了一句" in rendered
    assert "近段发言线索" in rendered


def test_record_group_msg_assigns_reply_to_same_thread(tmp_path) -> None:
    cfg = SimpleNamespace(personification_data_dir=str(tmp_path))
    data_store.init_data_store(cfg)
    db.init_db_sync(tmp_path)

    utils.record_group_msg(
        "g1",
        "甲",
        "今天先聊一下构建速度",
        user_id="u1",
        message_id="m1",
        time=1000,
    )
    utils.record_group_msg(
        "g1",
        "乙",
        "我回复这个线程",
        user_id="u2",
        message_id="m2",
        reply_to_msg_id="m1",
        reply_to_user_id="u1",
        time=1010,
    )

    first = utils.get_group_msg_by_message_id("g1", "m1")
    second = utils.get_group_msg_by_message_id("g1", "m2")

    assert first["thread_id"]
    assert second["thread_id"] == first["thread_id"]


def test_group_context_prioritizes_current_thread() -> None:
    context = group_context.build_group_conversation_context(
        recent_messages=[
            {"message_id": "a1", "thread_id": "ta", "nickname": "甲", "user_id": "u1", "content": "A 线程第一句"},
            {"message_id": "b1", "thread_id": "tb", "nickname": "乙", "user_id": "u2", "content": "B 线程第一句"},
            {"message_id": "a2", "thread_id": "ta", "nickname": "甲", "user_id": "u1", "content": "A 线程继续"},
        ],
        trigger_msg_id="a2",
        trigger_user_id="u1",
    )

    rendered = group_context.render_group_conversation_context(context)

    assert context.current_thread_id == "ta"
    assert [item["message_id"] for item in context.current_thread_messages] == ["a1", "a2"]
    assert "当前对话线程" in rendered
    assert "其他同时进行的群聊线程" in rendered


def test_group_context_renders_short_term_topic_state() -> None:
    context = group_context.build_group_conversation_context(
        recent_messages=[
            {
                "message_id": "a1",
                "thread_id": "ta",
                "nickname": "甲",
                "user_id": "u1",
                "content": "先聊构建速度",
            },
            {
                "message_id": "a2",
                "thread_id": "ta",
                "reply_to_msg_id": "a1",
                "reply_to_user_id": "u1",
                "nickname": "bot",
                "user_id": "bot-1",
                "content": "我刚接了一句",
                "source_kind": "bot_reply",
            },
            {
                "message_id": "b1",
                "thread_id": "tb",
                "nickname": "乙",
                "user_id": "u2",
                "content": "另一个话题",
            },
            {
                "message_id": "a3",
                "thread_id": "ta",
                "reply_to_msg_id": "a2",
                "reply_to_user_id": "bot-1",
                "mentioned_ids": ["bot-1"],
                "nickname": "甲",
                "user_id": "u1",
                "content": "那继续怎么优化",
            },
        ],
        trigger_msg_id="a3",
        trigger_user_id="u1",
        bot_self_id="bot-1",
    )

    rendered = group_context.render_group_conversation_context(context)
    trace_detail = group_context.render_topic_state_trace_detail(context.topic_state)

    assert context.topic_state.current_thread_id == "ta"
    assert context.topic_state.current_speaker == "甲"
    assert context.topic_state.reply_to_speaker == "bot"
    assert context.topic_state.is_reply_to_bot is True
    assert context.topic_state.bot_in_current_thread is True
    assert context.topic_state.parallel_thread_count == 1
    assert "本轮短期话题状态" in rendered
    assert "当前消息回复对象：bot（bot）" in rendered
    assert "同时存在其它线程：1 个" in rendered
    assert "topic_thread=ta" in trace_detail
    assert "reply_to_bot=true" in trace_detail


def test_recent_group_context_hook_uses_short_term_topic_state(monkeypatch) -> None:
    def _fake_group_context_window(*_args, **_kwargs):  # noqa: ANN001
        return [
            {
                "message_id": "m1",
                "thread_id": "thread-a",
                "nickname": "甲",
                "user_id": "u1",
                "content": "刚才那个方案我觉得可以再收窄",
            },
            {
                "message_id": "m2",
                "thread_id": "thread-a",
                "nickname": "bot",
                "user_id": "bot-1",
                "content": "我接了一句",
                "source_kind": "bot_reply",
            },
            {
                "message_id": "m3",
                "thread_id": "thread-a",
                "reply_to_msg_id": "m2",
                "reply_to_user_id": "bot-1",
                "mentioned_ids": ["bot-1"],
                "nickname": "甲",
                "user_id": "u1",
                "content": "那你说下一步怎么做",
            },
        ]

    monkeypatch.setattr(builtin_hooks, "build_group_context_window", _fake_group_context_window)

    rendered = builtin_hooks._format_recent_group_context(
        "123",
        trigger_msg_id="m3",
        reply_to_msg_id="m2",
        trigger_user_id="u1",
        bot_self_id="bot-1",
    )

    assert "本轮短期话题状态" in rendered
    assert "当前消息回复对象：bot（bot）" in rendered
    assert "bot 是否在当前线程最近发过言：是" in rendered
