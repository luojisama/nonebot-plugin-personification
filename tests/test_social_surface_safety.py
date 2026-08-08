from __future__ import annotations

import ast
import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from ._loader import load_personification_module


ROOT = Path(__file__).resolve().parents[1]
db = load_personification_module("plugin.personification.core.db")
agent_runtime = load_personification_module("plugin.personification.core.services.agent_runtime")
tts_matchers = load_personification_module("plugin.personification.handlers.tts_matchers")
yaml_processor = load_personification_module(
    "plugin.personification.handlers.yaml_pipeline.processor"
)
pipeline_context = load_personification_module(
    "plugin.personification.handlers.reply_pipeline.pipeline_context"
)


class _Logger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def debug(self, message: str) -> None:
        self.messages.append(str(message))

    def warning(self, message: str) -> None:
        self.messages.append(str(message))


def test_yaml_translation_forward_blocks_before_transport() -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    logger = _Logger()

    class _Bot:
        self_id = "bot-42"

        async def call_api(self, api: str, **kwargs):  # noqa: ANN003, ANN202
            calls.append((api, kwargs))
            return {"data": {"message_id": "should-not-exist"}}

    result = asyncio.run(
        yaml_processor._send_translation_forward(
            _Bot(),
            SimpleNamespace(group_id=100, user_id=200),
            "原文：正常内容\n译文：Authorization: Bearer secret-token-value",
            logger=logger,
        )
    )

    assert result is False
    assert calls == []
    assert any("surface=reply_translation_forward" in message for message in logger.messages)
    assert all("secret-token-value" not in message for message in logger.messages)


@pytest.mark.parametrize("control", ["[SILENCE]", "[NO_REPLY]", "<output><message>x</message></output>"])
def test_ack_and_translation_guards_reject_control_outputs(control: str) -> None:
    logger = _Logger()
    assert pipeline_context.guard_reply_ack_text(
        control,
        fallback="fallback ack",
        logger=logger,
    ) == ""
    result = asyncio.run(
        yaml_processor._send_translation_forward(
            SimpleNamespace(),
            SimpleNamespace(group_id=100, user_id=200),
            control,
            logger=logger,
        )
    )
    assert result is False


@pytest.mark.parametrize("control", ["prefix [SILENCE] suffix", "prefix <NO_REPLY> suffix"])
def test_visible_guards_reject_embedded_control_markers(control: str) -> None:
    assert pipeline_context.guard_reply_ack_text(
        control,
        fallback="fallback ack",
        logger=_Logger(),
    ) == ""


def test_yaml_direct_output_guard_preserves_pending_media_order() -> None:
    source = (ROOT / "handlers" / "yaml_pipeline" / "processor.py").read_text(encoding="utf-8")
    direct_guard = 'surface="yaml_direct_output"'

    assert direct_guard in source
    assert source.index("await _commit_pending_actions()") < source.index(direct_guard)
    assert 'allow_direct_media=False' in source[source.index(direct_guard) : source.index(direct_guard) + 160]


def test_yaml_route_schedules_semantic_pending_topic_before_early_return() -> None:
    source = (ROOT / "handlers" / "reply_pipeline" / "processor.py").read_text(
        encoding="utf-8"
    )
    schedule = "await schedule_pending_topic_extraction(hook_ctx)"
    yaml_call = "await runtime.process_yaml_response_logic("

    assert source.index(schedule) < source.index(yaml_call)


def test_yaml_ack_uses_shared_safety_guard_before_send() -> None:
    tree = ast.parse((ROOT / "handlers" / "yaml_pipeline" / "processor.py").read_text(encoding="utf-8"))
    ack_sender = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_ack_sender"
    )
    calls = [
        node.func.id
        for node in ast.walk(ack_sender)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]

    assert "_guard_reply_ack_text" in calls
    guard_line = next(
        node.lineno
        for node in ast.walk(ack_sender)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_guard_reply_ack_text"
    )
    send_line = next(
        node.lineno
        for node in ast.walk(ack_sender)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_send_reply"
    )
    assert guard_line < send_line


def test_new_scheduled_task_blocks_and_persists_safety_status(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    db_path = db.init_db_sync(tmp_path)
    logger = _Logger()

    class _Scheduler:
        def __init__(self) -> None:
            self.jobs: list[object] = []

        def add_job(self, func, **_kwargs) -> None:  # noqa: ANN001
            self.jobs.append(func)

    class _Bot:
        self_id = "bot-task"

        def __init__(self) -> None:
            self.messages: list[str] = []

        async def send_private_msg(self, *, user_id: int, message: str):  # noqa: ANN202
            del user_id
            self.messages.append(message)
            return {"data": {"message_id": "unexpected"}}

    class _Ledger:
        def __init__(self) -> None:
            self.calls = 0

        async def dispatch(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
            self.calls += 1
            raise AssertionError("blocked task must not reach the ledger")

    monkeypatch.setattr(agent_runtime, "load_builtin_skillpacks_sync", lambda **_kwargs: None)
    monkeypatch.setattr(agent_runtime, "register_generated_skills", lambda **_kwargs: None)
    scheduler = _Scheduler()
    bot = _Bot()
    ledger = _Ledger()
    registry = agent_runtime.build_agent_tool_registry(
        plugin_config=SimpleNamespace(personification_use_skillpacks=True),
        logger=logger,
        get_now=lambda: None,
        scheduler=scheduler,
        data_dir=tmp_path,
        get_bots=lambda: {bot.self_id: bot},
        qq_outbound_ledger=ledger,
    )
    create_task = registry.get("create_user_task")
    assert create_task is not None

    async def _run() -> None:
        await create_task.handler(
            user_id="200",
            description="安全测试",
            cron="0 8 * * *",
            action="send_message",
            params={"message": "Authorization: Bearer secret-token-value"},
        )
        await scheduler.jobs[0]()  # type: ignore[operator]

    asyncio.run(_run())

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT last_status FROM user_tasks WHERE user_id=? AND task_id=?",
            ("200", "task_001"),
        ).fetchone()
    assert row == ("safety_blocked",)
    assert bot.messages == []
    assert ledger.calls == 0
    assert any("surface=scheduled_user_task" in message for message in logger.messages)
    assert all("secret-token-value" not in message for message in logger.messages)


def test_scheduled_task_guard_rejects_control_marker() -> None:
    task: dict[str, object] = {}
    assert agent_runtime.guard_scheduled_user_task_message(
        task,
        "[SILENCE]",
        logger=_Logger(),
    ) == ""
    assert task["last_status"] == "safety_blocked"


def test_restart_task_path_guards_before_any_send() -> None:
    tree = ast.parse((ROOT / "__init__.py").read_text(encoding="utf-8"))
    restore = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_restore_user_tasks"
    )
    guard_line = next(
        node.lineno
        for node in ast.walk(restore)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "guard_scheduled_user_task_message"
    )
    send_lines = [
        node.lineno
        for node in ast.walk(restore)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "send_private_msg"
    ]

    assert send_lines
    assert guard_line < min(send_lines)


class _MatcherFinished(Exception):
    pass


class _Matcher:
    def __init__(self) -> None:
        self.handler_func = None
        self.finished: list[object] = []

    def handle(self):  # noqa: ANN201
        def _decorator(func):  # noqa: ANN001
            self.handler_func = func
            return func

        return _decorator

    async def finish(self, message: object) -> None:
        self.finished.append(message)
        raise _MatcherFinished


def _register_tts_handler(monkeypatch, service, logger: _Logger) -> _Matcher:  # noqa: ANN001
    matcher = _Matcher()
    monkeypatch.setattr(tts_matchers, "on_command", lambda *_args, **_kwargs: matcher)
    tts_matchers.register_tts_matchers(
        plugin_config=SimpleNamespace(personification_tts_command_prefixes=["说"]),
        message_segment_cls=SimpleNamespace(),
        logger=logger,
        tts_service=service,
        load_prompt=lambda _group_id: {},
        get_group_style=lambda _group_id: "",
        group_message_event_cls=type("GroupEvent", (), {}),
    )
    assert matcher.handler_func is not None
    return matcher


def test_tts_planner_visible_message_is_guarded(monkeypatch) -> None:  # noqa: ANN001
    logger = _Logger()

    class _Service:
        def __init__(self) -> None:
            self.decide_calls = 0
            self.sent = False

        def is_enabled(self) -> bool:
            return True

        def is_configured(self) -> bool:
            return True

        async def decide_tts_delivery(self, **kwargs):  # noqa: ANN003, ANN202
            self.decide_calls += 1
            assert kwargs["text"] == "用户原文保持原样"
            return SimpleNamespace(
                action="block",
                visible_message="Authorization: Bearer secret-token-value",
                style_hint="",
            )

        async def send_tts(self, **_kwargs):  # noqa: ANN202
            self.sent = True
            return True

    service = _Service()
    matcher = _register_tts_handler(monkeypatch, service, logger)
    args = SimpleNamespace(extract_plain_text=lambda: "用户原文保持原样")

    with pytest.raises(_MatcherFinished):
        asyncio.run(matcher.handler_func(SimpleNamespace(), SimpleNamespace(), args))

    assert matcher.finished == ["这段先不读啦。"]
    assert service.decide_calls == 1
    assert service.sent is False
    assert any("surface=tts_command_visible_message" in message for message in logger.messages)
    assert all("secret-token-value" not in str(message) for message in matcher.finished)


def test_tts_keeps_user_text_verbatim_and_hides_send_exception(monkeypatch) -> None:  # noqa: ANN001
    logger = _Logger()
    user_text = "Authorization: Bearer user-provided-token"

    class _Service:
        def __init__(self) -> None:
            self.decision_text = ""
            self.send_text = ""

        def is_enabled(self) -> bool:
            return True

        def is_configured(self) -> bool:
            return True

        async def decide_tts_delivery(self, **kwargs):  # noqa: ANN003, ANN202
            self.decision_text = kwargs["text"]
            return SimpleNamespace(action="voice", visible_message="", style_hint="自然")

        async def send_tts(self, **kwargs):  # noqa: ANN003, ANN202
            self.send_text = kwargs["text"]
            raise RuntimeError("Authorization: Bearer backend-secret-token")

    service = _Service()
    matcher = _register_tts_handler(monkeypatch, service, logger)
    args = SimpleNamespace(extract_plain_text=lambda: user_text)

    with pytest.raises(_MatcherFinished):
        asyncio.run(matcher.handler_func(SimpleNamespace(), SimpleNamespace(), args))

    assert service.decision_text == user_text
    assert service.send_text == user_text
    assert matcher.finished == ["这次没读出来。"]
    assert all("backend-secret-token" not in str(message) for message in matcher.finished)


def test_tts_parse_error_does_not_expose_parser_exception() -> None:
    _parsed, error = tts_matchers._parse_tts_command_args('"unterminated')

    assert error == "参数没解析明白，检查一下引号吧。"
