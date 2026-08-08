from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from ...core import webui_audit_log
from ...core.data_transfer.service import DataTransferError, DataTransferService
from ...core.operation_diagnostics import detail, diagnostic, exception_diagnostic, step
from ..deps import AdminIdentity, require_admin
from ..schemas import DataExportRequest, DataImportApplyRequest, DataImportPlanRequest


_OPERATION_LABELS = {
    "create": "创建迁移包",
    "upload": "上传迁移包",
    "inspect": "检查迁移包",
    "dry-run": "预演数据导入",
    "apply": "应用数据导入",
    "rollback": "回滚数据导入",
}

_SUCCESS_STEPS = {
    "create": (
        ("scope", "确认导出作用域", "目标 Bot 与群成员关系已确认。"),
        ("package", "生成群安全包", "白名单 dataset 已写入迁移包。"),
    ),
    "upload": (
        ("receive", "接收上传文件", "上传内容已在大小限制内完整接收。"),
        ("store", "保存待验包文件", "文件已保存，尚未代表内容可信。"),
    ),
    "inspect": (
        ("archive", "检查压缩包安全", "路径、entry、大小与压缩比检查通过。"),
        ("manifest", "校验 manifest 与 scope", "声明、checksum 与群作用域检查通过。"),
    ),
    "dry-run": (
        ("package", "重新验证迁移包", "迁移包仍满足安全约束。"),
        ("target", "确认目标作用域", "目标 Bot、群和导入模式已绑定。"),
        ("plan", "生成导入计划", "短期 plan token 已绑定当前参数。"),
    ),
    "apply": (
        ("plan", "验证导入计划", "plan token 与迁移包及目标参数一致。"),
        ("snapshot", "保存目标 scope 前镜像", "回滚所需的目标群前镜像已建立。"),
        ("apply", "事务应用迁移数据", "主库与群内记忆数据已完成应用。"),
    ),
    "rollback": (
        ("journal", "读取迁移 journal", "已定位本次导入记录。"),
        ("restore", "恢复目标 scope", "仅本次导入影响的数据已恢复。"),
    ),
}


def _success_payload(operation: str, result: dict[str, Any], *, operation_id: str = "", mode: str = "") -> dict[str, Any]:
    labels = {
        "create": ("data_export_created", "群安全包已创建", "迁移包已生成，可以下载。"),
        "upload": ("data_import_uploaded", "迁移包已上传", "文件已接收，请继续执行安全验包。"),
        "inspect": ("data_import_inspected", "迁移包检查通过", "压缩包、manifest、checksum 与群作用域均有效。"),
        "dry-run": ("data_import_plan_ready", "导入预演已完成", "导入计划已绑定当前目标参数，尚未写入任何数据。"),
        "apply": ("data_import_applied", "数据导入已完成", "目标群数据已按预演计划完成应用。"),
        "rollback": ("data_import_rolled_back", "本次导入已回滚", "目标 scope 已恢复到本次导入前的状态。"),
    }
    code, title, message = labels[operation]
    operation_id = operation_id or str(result.get("task_id") or result.get("journal_id") or "")
    details = []
    if operation == "create":
        manifest = (result.get("metadata") or {}).get("manifest") or {}
        details.extend((detail("Dataset 数量", len(manifest.get("datasets") or []), "ok"), detail("任务状态", result.get("status") or "completed", "ok")))
    elif operation == "upload":
        details.append(detail("上传大小", int((result.get("metadata") or {}).get("size", 0) or 0), "info"))
    elif operation == "inspect":
        counts = result.get("counts") or {}
        details.extend((detail("Dataset 数量", len(counts), "ok"), detail("记录总数", sum(int(value or 0) for value in counts.values()), "info")))
    elif operation == "dry-run":
        changes = result.get("changes") or {}
        details.extend((detail("导入模式", mode or result.get("mode") or "merge", "info"), detail("计划变更数", sum(int(value or 0) for value in changes.values()), "info")))
    elif operation in {"apply", "rollback"}:
        details.append(detail("幂等命中", bool(result.get("idempotent")), "info"))
        if mode:
            details.append(detail("导入模式", mode, "info"))
    report = diagnostic(
        ok=True,
        code=code,
        phase=operation,
        title=title,
        message=message,
        details=details,
        steps=tuple(step(key, label, "ok", text) for key, label, text in _SUCCESS_STEPS[operation]),
        suggestion="下载并妥善保管迁移包。" if operation == "create" else "",
        operation_id=operation_id,
    )
    return {**result, **report}


def _transfer_error_spec(message: str) -> tuple[str, str, str, bool, bool, bool]:
    if message == "target scope is busy":
        return ("data_transfer_scope_busy", "目标 scope 正被另一项迁移占用。", "等待当前迁移结束后，使用相同参数重试。", True, False, False)
    if message in {"invalid plan token", "plan token expired", "plan token does not match package or target"}:
        return ("data_import_plan_invalid", "Dry-run plan token 无效、过期或与当前参数不匹配。", "重新执行 Dry-run，确认参数未变化后再导入。", False, False, False)
    if message in {"task not found", "upload missing"}:
        return ("data_transfer_task_not_found", "迁移任务或上传文件不存在。", "重新上传迁移包或重新创建任务。", False, False, False)
    if message == "journal not found":
        return ("data_import_journal_not_found", "找不到对应的导入 journal。", "刷新页面并确认 journal ID；不要尝试猜测其它记录。", False, False, False)
    if message == "scope snapshot missing":
        return ("data_import_snapshot_missing", "回滚所需的目标 scope 前镜像不存在。", "停止重复回滚，检查迁移 journal 与服务器脱敏日志后人工确认数据。", False, True, False)
    if message == "memory store is unavailable":
        return ("data_import_memory_unavailable", "群内画像或记忆存储当前不可用。", "服务已尝试自动恢复；先检查迁移 journal 的终态，再决定是否重新预演。", False, True, True)
    if message in {"bot_id and group_id are required", "target bot and group are required"}:
        return ("data_transfer_scope_required", "必须提供目标 Bot 与群作用域。", "补全 Bot QQ 和群号后重试。", False, False, False)
    if message in {"unsupported import mode", "target group must match package scope", "cross-bot import is denied"}:
        return ("data_import_target_rejected", "目标身份、群作用域或导入模式不符合迁移包约束。", "使用迁移包声明的群与来源 Bot，重新执行 Dry-run。", False, False, False)
    if message == "unknown dataset":
        return ("data_export_dataset_invalid", "导出 dataset 列表为空或包含不支持的项目。", "只选择当前版本支持的群安全 dataset。", False, False, False)
    if message == "export is not downloadable":
        return ("data_export_not_downloadable", "导出任务尚未完成或文件已不可用。", "刷新任务状态；若文件已过保留期，请重新创建。", False, False, False)
    if message in {"archive exceeds size limit", "expanded archive exceeds size limit", "archive entry exceeds safety limits", "too many archive entries"}:
        return ("data_import_archive_limit_exceeded", "迁移包超过文件、entry、展开大小或压缩比安全限制。", "重新生成更小的合法迁移包，不要绕过安全限制。", False, False, False)
    if message in {"unsafe or duplicate archive entry", "symlink entries are forbidden", "encrypted entries are forbidden", "archive contains undeclared files"}:
        return ("data_import_archive_unsafe", "迁移包包含不安全、重复、加密或未声明的 entry。", "仅上传由受信任版本生成的标准迁移包。", False, False, False)
    if message == "manifest missing" or message == "unsupported manifest" or message.startswith("invalid package:"):
        return ("data_import_manifest_invalid", "迁移包格式或 manifest 无法识别。", "用当前版本重新导出迁移包后再上传。", False, False, False)
    if message == "manifest checksum mismatch" or message.startswith("checksum mismatch:"):
        return ("data_import_integrity_failed", "迁移包 checksum 校验失败，内容可能损坏或被修改。", "不要继续使用该文件；从可信源重新导出并传输。", False, False, False)
    if message in {"memory id belongs to another group"} or message.endswith(" id conflicts with another group"):
        return ("data_import_cross_scope_conflict", "迁移数据的主键已属于其它群作用域。", "不要覆盖其它群数据；检查来源包与目标数据库后重新规划。", False, False, False)
    schema_markers = (
        "invalid ", "unsupported ", "dataset/file declaration mismatch", "must be a list",
        "forbidden fields", "cross-scope ", "cross-group row:",
    )
    if (
        message.startswith(schema_markers)
        or message.endswith(" must be a list")
        or message.endswith(" contains forbidden fields")
        or message in {"invalid datasets", "invalid or cross-group memory"}
    ):
        return ("data_import_schema_rejected", "迁移包的数据结构或群作用域校验未通过。", "不要修改包内文件；使用当前版本从正确群重新导出。", False, False, False)
    return ("data_transfer_rejected", "数据迁移请求未通过安全检查。", "检查输入与迁移包来源后重新执行对应步骤。", False, False, False)


def _data_transfer_error_diagnostic(exc: DataTransferError, operation: str, *, operation_id: str = "") -> dict[str, Any]:
    code, message, suggestion, retryable, partial, outcome_unknown = _transfer_error_spec(str(exc))
    label = _OPERATION_LABELS[operation]
    return diagnostic(
        ok=False,
        code=code,
        phase=operation,
        title=f"{label}未完成",
        message=message,
        details=(detail("安全状态", "未继续执行危险步骤" if not partial else "需要人工确认目标数据", "warn" if partial else "info"),),
        steps=(step(operation, label, "unknown" if outcome_unknown else "error", message),),
        suggestion=suggestion,
        retryable=retryable,
        partial=partial,
        outcome_unknown=outcome_unknown,
        operation_id=operation_id,
    )


def _unexpected_diagnostic(exc: BaseException, operation: str, *, operation_id: str = "") -> dict[str, Any]:
    uncertain = operation in {"apply", "rollback"}
    report = exception_diagnostic(
        exc,
        phase=operation,
        title=f"{_OPERATION_LABELS[operation]}异常中断",
        message=(
            "服务端异常后无法从本次响应确认最终数据状态。"
            if uncertain else "服务器处理迁移操作时发生内部异常。"
        ),
        suggestion=(
            "先检查迁移 journal 与脱敏日志，确认目标 scope 状态；禁止直接重复执行。"
            if uncertain else "根据 Trace ID 检查脱敏日志，修复服务状态后再重试。"
        ),
        operation_id=operation_id,
        retryable=False if uncertain else None,
    )
    code_operation = operation.replace("-", "_")
    report["code"] = f"data_import_{code_operation}_outcome_unknown" if uncertain else f"data_transfer_{code_operation}_{report['code']}"
    report["steps"] = [step(operation, _OPERATION_LABELS[operation], "unknown" if uncertain else "error", report["message"]).to_dict()]
    report["partial"] = uncertain
    report["outcome_unknown"] = uncertain
    return report


def _membership_diagnostic(exc: HTTPException, operation: str, *, operation_id: str = "") -> dict[str, Any]:
    raw = str(exc.detail or "")
    if raw == "目标 Bot 不在指定群中":
        code, message, retryable = "data_transfer_membership_denied", "目标 Bot 不在指定群中，未执行迁移操作。", False
    elif raw == "目标 Bot 当前未连接，无法确认数据作用域":
        code, message, retryable = "data_transfer_bot_disconnected", "目标 Bot 当前未连接，无法确认群作用域。", True
    else:
        code, message, retryable = "data_transfer_membership_unavailable", "当前无法确认目标 Bot 的群成员关系。", True
    return diagnostic(
        ok=False,
        code=code,
        phase=operation,
        title=f"{_OPERATION_LABELS[operation]}未开始",
        message=message,
        details=(detail("写入状态", "尚未写入迁移数据", "ok"),),
        steps=(step("membership", "确认 Bot 群成员关系", "error", message),),
        suggestion="确认 Bot 在线且仍在目标群后重试。",
        retryable=retryable,
        operation_id=operation_id,
    )


def _error_status(report: dict[str, Any], default: int = 400) -> int:
    code = str(report.get("code") or "")
    if code == "data_transfer_scope_busy":
        return 409
    if code in {"data_transfer_task_not_found", "data_import_journal_not_found"}:
        return 404
    if code == "data_import_archive_limit_exceeded":
        return 413
    return default


def _service(runtime) -> DataTransferService:
    cfg = getattr(runtime, "plugin_config", None)
    from ...core.paths import get_data_dir
    from ...core.db import resolve_db_path
    data_dir = get_data_dir(cfg)
    bundle = getattr(runtime, "runtime_bundle", None)
    return DataTransferService(
        data_dir=data_dir / "data_transfer",
        db_path=resolve_db_path(cfg),
        memory_store=getattr(bundle, "memory_store", None),
        user_policy_service=getattr(bundle, "user_policy_service", None),
    )


def _audit(action: str, admin: AdminIdentity, target: str, detail: dict, outcome: str = "ok") -> None:
    webui_audit_log.record(action=action, qq=admin.qq, device_id=admin.device_id, target=target, detail=detail, outcome=outcome)


def _connected_bot_ids(runtime) -> set[str]:
    try:
        bots = runtime.get_bots() if callable(getattr(runtime, "get_bots", None)) else {}
    except Exception:
        bots = {}
    values = bots.values() if isinstance(bots, dict) else []
    return {str(getattr(bot, "self_id", "") or "").strip() for bot in values if str(getattr(bot, "self_id", "") or "").strip()}


def _require_connected_bot(runtime, bot_id: str) -> None:
    if str(bot_id or "").strip() not in _connected_bot_ids(runtime):
        raise HTTPException(status_code=400, detail="目标 Bot 当前未连接，无法确认数据作用域")


async def _require_group_membership(runtime, bot_id: str, group_id: str) -> None:
    _require_connected_bot(runtime, bot_id)
    bots = runtime.get_bots() if callable(getattr(runtime, "get_bots", None)) else {}
    bot = next((value for value in bots.values() if str(getattr(value, "self_id", "")) == str(bot_id)), None)
    if bot is None or not callable(getattr(bot, "get_group_list", None)):
        raise HTTPException(status_code=400, detail="无法检查目标 Bot 的群成员关系")
    try:
        groups = await bot.get_group_list()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="目标群成员关系检查失败") from exc
    if not any(str(item.get("group_id", "")) == str(group_id) for item in (groups or []) if isinstance(item, dict)):
        raise HTTPException(status_code=400, detail="目标 Bot 不在指定群中")


def build_data_transfer_router(*, runtime) -> APIRouter:
    router = APIRouter(prefix="/api/data-transfer", tags=["data-transfer"])

    @router.post("/exports/create")
    async def create_export(body: DataExportRequest, admin: AdminIdentity = Depends(require_admin)) -> dict:
        try:
            await _require_group_membership(runtime, body.bot_id, body.group_id)
        except HTTPException as exc:
            report = _membership_diagnostic(exc, "create")
            _audit("data_export_create", admin, body.group_id, {"code": report["code"]}, "denied")
            raise HTTPException(status_code=exc.status_code, detail=report) from exc
        try:
            result = await asyncio.to_thread(_service(runtime).create_export, bot_id=body.bot_id, group_id=body.group_id, datasets=body.datasets)
        except DataTransferError as exc:
            report = _data_transfer_error_diagnostic(exc, "create")
            _audit("data_export_create", admin, body.group_id, {"code": report["code"]}, "denied")
            raise HTTPException(status_code=_error_status(report), detail=report) from exc
        except Exception as exc:
            report = _unexpected_diagnostic(exc, "create")
            _audit("data_export_create", admin, body.group_id, {"code": report["code"], "trace_id": report["trace_id"]}, "failed")
            raise HTTPException(status_code=500, detail=report) from exc
        _audit("data_export_create", admin, body.group_id, {"task_id": result["task_id"], "datasets": body.datasets or []})
        return _success_payload("create", result)

    @router.get("/exports/{task_id}/status")
    async def export_status(task_id: str, _: AdminIdentity = Depends(require_admin)) -> dict:
        try:
            return _service(runtime).task_status(task_id)
        except DataTransferError as exc:
            report = _data_transfer_error_diagnostic(exc, "create", operation_id=task_id)
            raise HTTPException(status_code=_error_status(report, 404), detail=report) from exc

    @router.get("/exports/{task_id}/download")
    async def download_export(task_id: str, admin: AdminIdentity = Depends(require_admin)):
        try:
            path = _service(runtime).export_path(task_id)
        except DataTransferError as exc:
            report = _data_transfer_error_diagnostic(exc, "create", operation_id=task_id)
            raise HTTPException(status_code=_error_status(report, 404), detail=report) from exc
        _audit("data_export_download", admin, task_id, {})
        return FileResponse(path, media_type="application/zip", filename=path.name)

    @router.post("/imports/upload")
    async def upload_import(file: UploadFile = File(...), admin: AdminIdentity = Depends(require_admin)) -> dict:
        try:
            result = await asyncio.to_thread(_service(runtime).store_upload, file.file, filename=file.filename or "package.zip")
        except DataTransferError as exc:
            report = _data_transfer_error_diagnostic(exc, "upload")
            _audit("data_import_upload", admin, "", {"code": report["code"]}, "denied")
            raise HTTPException(status_code=_error_status(report), detail=report) from exc
        except Exception as exc:
            report = _unexpected_diagnostic(exc, "upload")
            _audit("data_import_upload", admin, "", {"code": report["code"], "trace_id": report["trace_id"]}, "failed")
            raise HTTPException(status_code=500, detail=report) from exc
        _audit("data_import_upload", admin, result["task_id"], {"size": result["metadata"].get("size", 0)})
        return _success_payload("upload", result)

    @router.get("/imports/{task_id}/inspect")
    async def inspect_import(task_id: str, _: AdminIdentity = Depends(require_admin)) -> dict:
        try:
            result = await asyncio.to_thread(_service(runtime).inspect, task_id)
        except DataTransferError as exc:
            report = _data_transfer_error_diagnostic(exc, "inspect", operation_id=task_id)
            raise HTTPException(status_code=_error_status(report), detail=report) from exc
        except Exception as exc:
            report = _unexpected_diagnostic(exc, "inspect", operation_id=task_id)
            raise HTTPException(status_code=500, detail=report) from exc
        return _success_payload("inspect", result, operation_id=task_id)

    @router.post("/imports/{task_id}/dry-run")
    async def dry_run(task_id: str, body: DataImportPlanRequest, admin: AdminIdentity = Depends(require_admin)) -> dict:
        try:
            await _require_group_membership(runtime, body.target_bot_id, body.target_group_id)
        except HTTPException as exc:
            report = _membership_diagnostic(exc, "dry-run", operation_id=task_id)
            _audit("data_import_dry_run", admin, task_id, {"mode": body.mode, "code": report["code"]}, "denied")
            raise HTTPException(status_code=exc.status_code, detail=report) from exc
        try:
            result = await asyncio.to_thread(_service(runtime).dry_run, task_id, target_bot_id=body.target_bot_id, target_group_id=body.target_group_id, mode=body.mode, allow_same_identity=body.allow_same_identity)
        except DataTransferError as exc:
            report = _data_transfer_error_diagnostic(exc, "dry-run", operation_id=task_id)
            _audit("data_import_dry_run", admin, task_id, {"mode": body.mode, "code": report["code"]}, "denied")
            raise HTTPException(status_code=_error_status(report), detail=report) from exc
        except Exception as exc:
            report = _unexpected_diagnostic(exc, "dry-run", operation_id=task_id)
            _audit("data_import_dry_run", admin, task_id, {"mode": body.mode, "code": report["code"], "trace_id": report["trace_id"]}, "failed")
            raise HTTPException(status_code=500, detail=report) from exc
        _audit("data_import_dry_run", admin, task_id, {"mode": body.mode})
        return _success_payload("dry-run", result, operation_id=task_id, mode=body.mode)

    @router.post("/imports/{task_id}/apply")
    async def apply_import(task_id: str, body: DataImportApplyRequest, admin: AdminIdentity = Depends(require_admin)) -> dict:
        try:
            await _require_group_membership(runtime, body.target_bot_id, body.target_group_id)
        except HTTPException as exc:
            report = _membership_diagnostic(exc, "apply", operation_id=task_id)
            _audit("data_import_apply", admin, task_id, {"mode": body.mode, "code": report["code"]}, "denied")
            raise HTTPException(status_code=exc.status_code, detail=report) from exc
        try:
            result = await asyncio.to_thread(_service(runtime).apply, task_id, target_bot_id=body.target_bot_id, target_group_id=body.target_group_id, mode=body.mode, allow_same_identity=body.allow_same_identity, plan_token=body.plan_token)
        except DataTransferError as exc:
            report = _data_transfer_error_diagnostic(exc, "apply", operation_id=task_id)
            outcome = "unknown" if report["outcome_unknown"] else "denied"
            _audit("data_import_apply", admin, task_id, {"mode": body.mode, "code": report["code"]}, outcome)
            raise HTTPException(status_code=_error_status(report), detail=report) from exc
        except Exception as exc:
            report = _unexpected_diagnostic(exc, "apply", operation_id=task_id)
            _audit("data_import_apply", admin, task_id, {"mode": body.mode, "code": report["code"], "trace_id": report["trace_id"]}, "unknown")
            raise HTTPException(status_code=500, detail=report) from exc
        _audit("data_import_apply", admin, task_id, {"mode": body.mode, "journal_id": result["journal_id"]})
        return _success_payload("apply", result, operation_id=str(result["journal_id"]), mode=body.mode)

    @router.post("/targets/check")
    async def check_target(body: DataImportPlanRequest, _: AdminIdentity = Depends(require_admin)) -> dict:
        try:
            target = _service(runtime).validate_target_scope(target_bot_id=body.target_bot_id, target_group_id=body.target_group_id)
            await _require_group_membership(runtime, target["bot_id"], target["group_id"])
        except DataTransferError as exc:
            report = _data_transfer_error_diagnostic(exc, "dry-run")
            raise HTTPException(status_code=_error_status(report), detail=report) from exc
        except HTTPException as exc:
            report = _membership_diagnostic(exc, "dry-run")
            raise HTTPException(status_code=exc.status_code, detail=report) from exc
        return {"valid": True, "target": target}

    @router.post("/imports/{journal_id}/rollback")
    async def rollback_import(journal_id: str, admin: AdminIdentity = Depends(require_admin)) -> dict:
        try:
            result = await asyncio.to_thread(_service(runtime).rollback, journal_id)
        except DataTransferError as exc:
            report = _data_transfer_error_diagnostic(exc, "rollback", operation_id=journal_id)
            _audit("data_import_rollback", admin, journal_id, {"code": report["code"]}, "denied")
            raise HTTPException(status_code=_error_status(report), detail=report) from exc
        except Exception as exc:
            report = _unexpected_diagnostic(exc, "rollback", operation_id=journal_id)
            _audit("data_import_rollback", admin, journal_id, {"code": report["code"], "trace_id": report["trace_id"]}, "unknown")
            raise HTTPException(status_code=500, detail=report) from exc
        _audit("data_import_rollback", admin, journal_id, {"idempotent": result["idempotent"]})
        return _success_payload("rollback", result, operation_id=journal_id)

    return router
