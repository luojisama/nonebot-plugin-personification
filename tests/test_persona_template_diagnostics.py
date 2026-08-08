from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ._loader import load_personification_module
from .test_webui_smoke import _build_client, _login_as_admin, _runtime_context  # noqa: F401


def _valid_template() -> str:
    return """
name: 测试角色
tts:
  voice: default_zh
status: |
  心情: 平静
nick_name:
  - 测试角色
ack_phrases:
  - 嗯
  - 行
  - 我看看
initial_message: 你好
mute_keyword:
  - 闭嘴
input: |
  {time}
  {trigger_reason}
  {history_new}
  {history_last}
  {status}
  {schedule_instruction}
  <output><message>消息正文</message></output>
system: |
  ## 角色身份
  测试角色。
  ## 性格与行为模式
  平静自然。
  ## 群聊说话方式
  像群友一样简短说话。
  ## 角色关系与称呼
  按当前关系自然称呼。
  ## 资料冲突与缺口
  没有足够资料时保守表达。
""".strip()


def _set_runtime(_runtime_context, bundle: object) -> None:  # noqa: ANN001
    current = _runtime_context.app_module.get_runtime_context()
    _runtime_context.app_module.set_runtime_context(
        plugin_config=_runtime_context.plugin_config,
        superusers={"10001"},
        get_bots=current.get_bots,
        logger=SimpleNamespace(info=lambda *_a, **_k: None, warning=lambda *_a, **_k: None),
        runtime_bundle=bundle,
    )


@pytest.mark.parametrize(
    "phase",
    ["alias_planning", "research", "synthesis", "repair", "validation", "avatar_review", "persistence"],
)
def test_async_persona_build_stage_failure_is_structured_and_preserves_progress(
    _runtime_context,
    monkeypatch,
    phase: str,
) -> None:  # noqa: ANN001
    route_mod = load_personification_module("plugin.personification.webui.routes.persona_template_routes")

    async def _caller(*_args, **_kwargs):  # noqa: ANN001, ANN202
        return "unused"

    async def _failed_build(*, progress, **_kwargs):  # noqa: ANN001, ANN202
        await progress("template_synthesis", "正在处理", 67)
        cause = RuntimeError("raw-model-output https://secret.example/?token=do-not-leak")
        validation = (
            {
                "field_errors": [{"field": "input", "code": "runtime_placeholders_missing", "message": "input 缺少运行占位符。"}],
                "field_warnings": [],
            }
            if phase == "validation"
            else None
        )
        raise route_mod._PersonaTemplateStageError(
            phase,
            cause,
            partial=phase == "persistence",
            validation=validation,
        )

    monkeypatch.setattr(route_mod, "_run_persona_template_build", _failed_build)
    _set_runtime(_runtime_context, SimpleNamespace(call_ai_api=_caller))
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)

    started = client.post(
        "/personification/api/persona-template/build-task",
        json={"work_title": "测试作品", "character_name": "测试角色"},
    )
    assert started.status_code == 200, started.text
    task_id = started.json()["task_id"]
    body = started.json()
    for _ in range(50):
        response = client.get(f"/personification/api/persona-template/tasks/{task_id}")
        assert response.status_code == 200, response.text
        body = response.json()
        if body["status"] == "error":
            break
        time.sleep(0.01)

    assert body["status"] == "error"
    assert body["stage"] == phase
    assert body["progress"] == 67
    assert body["code"] == f"persona_template_build_{phase}_failed"
    assert body["diagnostic"]["phase"] == phase
    assert body["diagnostic"]["operation_id"] == task_id
    assert body["steps"] == body["diagnostic"]["steps"]
    assert body["partial"] is (phase == "persistence")
    if phase == "validation":
        assert body["diagnostic"]["field_errors"][0]["field"] == "input"
    serialized = json.dumps(body, ensure_ascii=False)
    assert "raw-model-output" not in serialized
    assert "secret.example" not in serialized
    assert "do-not-leak" not in serialized


def test_persona_history_validation_returns_field_errors_and_warnings(
    _runtime_context,
) -> None:  # noqa: ANN001
    history_mod = load_personification_module("plugin.personification.core.persona_template_history")
    record = history_mod.record_persona_template_result(
        {
            "work_title": "测试作品",
            "character_name": "测试角色",
            "template": _valid_template(),
            "template_valid": True,
        }
    )
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)

    invalid = client.put(
        f"/personification/api/persona-template/history/{record['record_id']}",
        json={"template": "name: [https://secret.example/?token=do-not-leak"},
    )
    assert invalid.status_code == 400, invalid.text
    detail = invalid.json()["detail"]
    assert detail["code"] == "persona_template_history_validation_failed"
    assert detail["phase"] == "history_validation"
    assert detail["field_errors"][0]["field"] == "yaml"
    assert detail["field_errors"][0]["code"] == "parse_failed"
    assert "secret.example" not in invalid.text
    assert "do-not-leak" not in invalid.text

    saved = client.put(
        f"/personification/api/persona-template/history/{record['record_id']}",
        json={"template": _valid_template()},
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["result"]["template_valid"] is True
    assert body["code"] == "persona_template_history_updated"
    assert body["field_errors"] == []
    assert {item["field"] for item in body["field_warnings"]} >= {"system"}
    assert body["diagnostic"]["warnings"]


def test_persona_template_apply_reports_success_and_runtime_reload_partial(
    _runtime_context,
    tmp_path: Path,
) -> None:  # noqa: ANN001
    calls: list[str] = []

    async def _caller(*_args, **_kwargs):  # noqa: ANN001, ANN202
        return "unused"

    target = tmp_path / "active.yaml"
    _runtime_context.plugin_config.personification_prompt_path = str(target)
    _set_runtime(
        _runtime_context,
        SimpleNamespace(
            call_ai_api=_caller,
            save_plugin_runtime_config=lambda: calls.append("save"),
            reload_runtime_services=lambda: calls.append("reload"),
        ),
    )
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    result = {"work_title": "测试作品", "character_name": "测试角色", "template": _valid_template()}

    applied = client.post("/personification/api/persona-template/apply", json={"result": result})
    assert applied.status_code == 200, applied.text
    body = applied.json()
    assert body["applied"] is True
    assert body["path"] == str(target.resolve())
    assert body["code"] == "persona_template_applied"
    assert [item["status"] for item in body["steps"]] == ["ok", "ok", "ok", "ok"]
    assert calls == ["save", "reload"]

    def _reload_failed() -> None:
        raise RuntimeError("traceback https://secret.example/?token=do-not-leak")

    _set_runtime(
        _runtime_context,
        SimpleNamespace(
            call_ai_api=_caller,
            save_plugin_runtime_config=lambda: None,
            reload_runtime_services=_reload_failed,
        ),
    )
    failed_client = _build_client(_runtime_context)
    _login_as_admin(failed_client, _runtime_context)
    failed = failed_client.post("/personification/api/persona-template/apply", json={"result": result})
    assert failed.status_code == 500, failed.text
    detail = failed.json()["detail"]
    assert detail["code"] == "persona_template_apply_runtime_reload_partial"
    assert detail["phase"] == "runtime_reload"
    assert detail["partial"] is True
    assert [item["status"] for item in detail["steps"]] == ["ok", "ok", "ok", "ok", "error"]
    assert target.is_file()
    assert "secret.example" not in failed.text
    assert "do-not-leak" not in failed.text


def test_persona_template_apply_file_write_failure_is_structured(
    _runtime_context,
    tmp_path: Path,
) -> None:  # noqa: ANN001
    async def _caller(*_args, **_kwargs):  # noqa: ANN001, ANN202
        return "unused"

    blocked_parent = tmp_path / "blocked"
    blocked_parent.write_text("not a directory", encoding="utf-8")
    _runtime_context.plugin_config.personification_prompt_path = str(blocked_parent / "active.yaml")
    _set_runtime(_runtime_context, SimpleNamespace(call_ai_api=_caller))
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)

    failed = client.post(
        "/personification/api/persona-template/apply",
        json={"result": {"template": _valid_template()}},
    )
    assert failed.status_code == 500, failed.text
    detail = failed.json()["detail"]
    assert detail["code"] == "persona_template_apply_file_write_failed"
    assert detail["phase"] == "file_write"
    assert detail["partial"] is False
    assert [item["status"] for item in detail["steps"]] == ["ok", "ok", "error", "skipped", "skipped"]


def test_persona_history_delete_reports_cleanup_partial_without_raw_error(
    _runtime_context,
    monkeypatch,
) -> None:  # noqa: ANN001
    route_mod = load_personification_module("plugin.personification.webui.routes.persona_template_routes")
    history_mod = load_personification_module("plugin.personification.core.persona_template_history")
    revision = "d" * 32
    record = history_mod.record_persona_template_result(
        {
            "work_title": "测试作品",
            "character_name": "测试角色",
            "revision": revision,
            "template": _valid_template(),
            "template_valid": True,
        }
    )
    candidate_dir = (
        Path(route_mod.get_data_dir(_runtime_context.plugin_config))
        / "persona_avatar_candidates"
        / revision
    )
    candidate_dir.mkdir(parents=True, exist_ok=True)

    def _cleanup_failed(_path: Path) -> None:
        raise PermissionError("https://secret.example/?token=do-not-leak")

    monkeypatch.setattr(route_mod.shutil, "rmtree", _cleanup_failed)
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    deleted = client.delete(f"/personification/api/persona-template/history/{record['record_id']}")

    assert deleted.status_code == 200, deleted.text
    body = deleted.json()
    assert body["success"] is True
    assert body["ok"] is False
    assert body["code"] == "persona_template_delete_cleanup_partial"
    assert body["partial"] is True
    assert [item["status"] for item in body["steps"]] == ["ok", "error"]
    assert history_mod.get_persona_template_record(record["record_id"]) is None
    assert candidate_dir.is_dir()
    assert "secret.example" not in deleted.text
    assert "do-not-leak" not in deleted.text


def test_persona_build_success_and_frontend_render_operation_diagnostic() -> None:
    route_mod = load_personification_module("plugin.personification.webui.routes.persona_template_routes")
    report = route_mod._build_success_diagnostic(
        {
            "sources": [{"title": "safe"}],
            "subagents": [{"name": "facts"}],
            "avatar_candidates": [],
            "signature_candidates": [{"text": "safe"}],
            "template_warnings": ["建议补充资料来源。"],
        },
        operation_id="task-safe",
        build_mode="source",
    )
    assert report["code"] == "persona_template_build_complete"
    assert report["operation_id"] == "task-safe"
    assert [item["status"] for item in report["steps"]] == ["ok", "ok", "ok", "ok", "ok", "ok", "ok"]

    source = (Path(__file__).resolve().parents[1] / "webui" / "static" / "app-persona-builder.js").read_text(encoding="utf-8")
    assert 'rememberAdminOperation("persona-template"' in source
    assert 'renderAdminOperations("persona-template","人设构建与应用诊断")' in source
    assert "rememberAdminOperation(\"persona-template\",last" in source
    assert '"构建失败：" + e.message' not in source
