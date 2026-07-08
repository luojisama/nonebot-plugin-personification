from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from ._loader import load_personification_module


admin_commands = load_personification_module("plugin.personification.handlers.persona_admin_commands")
persona_template_routes = load_personification_module("plugin.personification.webui.routes.persona_template_routes")


class _Finish(Exception):
    pass


class _Matcher:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.finished: str = ""

    async def send(self, message: str) -> None:
        self.sent.append(str(message))

    async def finish(self, message: str = "") -> None:
        self.finished = str(message)
        raise _Finish


class _Bot:
    self_id = "99999"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call_api(self, api: str, **kwargs):  # noqa: ANN001, ANN201
        self.calls.append((api, kwargs))
        return {"ok": True}


class _Logger:
    def warning(self, *_args, **_kwargs) -> None:
        return None

    def debug(self, *_args, **_kwargs) -> None:
        return None


async def _fake_build(*, work_title, character_name, progress=None, **_kwargs):  # noqa: ANN003, ANN001
    if progress:
        await progress("query_moegirl", "正在查询萌娘百科...", 12)
        await progress("relation_mapping", "正在梳理关系...", 44)
        await progress("template_synthesis", "正在生成人设模板...", 78)
    export_path = Path(_kwargs["runtime"].plugin_config.personification_data_dir) / "template.yaml.txt"
    export_path.write_text("name: 测试角色\n", encoding="utf-8")
    return {
        "work_title": work_title,
        "character_name": character_name,
        "duration_ms": 123,
        "sources": [{"source": "萌娘百科", "title": character_name, "summary": "摘要"}],
        "subagents": [],
        "template": "name: 测试角色\nsystem: 测试",
        "template_valid": True,
        "template_errors": [],
        "template_warnings": [],
        "export_path": str(export_path),
    }


def _bundle(tmp_path: Path, bot: _Bot) -> SimpleNamespace:
    return SimpleNamespace(
        plugin_config=SimpleNamespace(personification_data_dir=str(tmp_path)),
        logger=_Logger(),
        get_bots=lambda: {"99999": bot},
    )


def test_persona_template_command_sends_group_forward_and_file(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(persona_template_routes, "_run_persona_template_build", _fake_build)
    bot = _Bot()
    matcher = _Matcher()
    event = SimpleNamespace(group_id=10001, user_id=20001)

    try:
        asyncio.run(
            admin_commands.handle_persona_template_command(
                matcher,
                bot=bot,
                bundle=_bundle(tmp_path, bot),
                event=event,
                tokens=["测试作品", "测试角色"],
            )
        )
    except _Finish:
        pass

    call_names = [name for name, _ in bot.calls]
    assert "send_group_forward_msg" in call_names
    assert "upload_group_file" in call_names
    assert "正在查询萌娘百科..." in matcher.sent
    assert "正在梳理关系..." in matcher.sent
    assert "正在生成人设模板..." in matcher.sent
    assert "人设模板已生成" in matcher.finished


def test_persona_template_command_sends_private_forward_and_file(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(persona_template_routes, "_run_persona_template_build", _fake_build)
    bot = _Bot()
    matcher = _Matcher()
    event = SimpleNamespace(user_id=20001)

    try:
        asyncio.run(
            admin_commands.handle_persona_template_command(
                matcher,
                bot=bot,
                bundle=_bundle(tmp_path, bot),
                event=event,
                tokens=["测试作品", "测试角色"],
            )
        )
    except _Finish:
        pass

    call_names = [name for name, _ in bot.calls]
    assert "send_private_forward_msg" in call_names
    assert "upload_private_file" in call_names


def test_persona_template_command_formats_empty_timeout_error(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    async def _timeout_build(**_kwargs):  # noqa: ANN003
        raise TimeoutError()

    monkeypatch.setattr(persona_template_routes, "_run_persona_template_build", _timeout_build)
    bot = _Bot()
    matcher = _Matcher()
    event = SimpleNamespace(group_id=10001, user_id=20001)

    try:
        asyncio.run(
            admin_commands.handle_persona_template_command(
                matcher,
                bot=bot,
                bundle=_bundle(tmp_path, bot),
                event=event,
                tokens=["测试作品", "测试角色"],
            )
        )
    except _Finish:
        pass

    assert matcher.finished == "人设构建失败：构建过程超时，请稍后重试或缩短作品/角色检索范围。"
