from __future__ import annotations

import shutil
import time
import uuid
from pathlib import Path
from typing import Any, NoReturn

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

import json

from ...core import webui_audit_log
from ...core.operation_diagnostics import (
    OperationDetail,
    OperationStep,
    detail as operation_detail,
    diagnostic as operation_diagnostic,
    exception_diagnostic,
    step as operation_step,
)
from ...core.sticker_library import (
    compute_file_hash,
    list_local_sticker_files,
    load_sticker_metadata,
    normalize_sticker_entry,
    resolve_sticker_dir,
    save_sticker_metadata_sync,
    sticker_metadata_path,
)


def _load_raw_manifest(sticker_dir: Path, *, strict: bool = False) -> dict[str, Any]:
    """读 stickers.json 原文，跳过 normalize 的兜底逻辑。
    用于判断条目是否真的有用户/labeler 写过 description。
    """
    path = sticker_metadata_path(sticker_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        if strict:
            raise
        return {}
    if isinstance(data, dict):
        return data
    if strict:
        raise ValueError("sticker manifest root must be an object")
    return {}


from ..deps import AdminIdentity, require_admin


_ALLOWED_UPLOAD_SUFFIXES: frozenset[str] = frozenset({
    ".jpg", ".jpeg", ".png", ".webp", ".gif",
})
_MAX_UPLOAD_BYTES = 4 * 1024 * 1024


def _operation_result(report: dict[str, Any], **fields: Any) -> dict[str, Any]:
    result = dict(report)
    result.update(fields)
    result["diagnostic"] = dict(report)
    return result


def _raise_operation(status_code: int, report: dict[str, Any]) -> NoReturn:
    raise HTTPException(status_code=status_code, detail=report)


def _exception_report(
    exc: BaseException,
    *,
    runtime: Any,
    code: str,
    phase: str,
    title: str,
    message: str,
    suggestion: str,
    operation_id: str,
    details: tuple[OperationDetail, ...] = (),
    steps: tuple[OperationStep, ...] = (),
    retryable: bool | None = None,
    partial: bool = False,
    outcome_unknown: bool = False,
) -> dict[str, Any]:
    report = exception_diagnostic(
        exc,
        phase=phase,
        title=title,
        message=message,
        suggestion=suggestion,
        operation_id=operation_id,
        retryable=retryable,
    )
    report["code"] = code
    report["partial"] = bool(partial)
    report["outcome_unknown"] = bool(outcome_unknown)
    report["details"].extend(item.to_dict() for item in details)
    report["steps"] = [item.to_dict() for item in steps]
    logger = getattr(runtime, "logger", None)
    if logger is not None:
        logger.warning(
            f"[webui] sticker operation failed: code={code} "
            f"exception={type(exc).__name__} trace={report['trace_id']}"
        )
    return report


def _validation_report(
    *,
    code: str,
    phase: str,
    title: str,
    message: str,
    suggestion: str,
    operation_id: str,
    details: tuple[OperationDetail, ...] = (),
    steps: tuple[OperationStep, ...] = (),
) -> dict[str, Any]:
    return operation_diagnostic(
        ok=False,
        code=code,
        phase=phase,
        title=title,
        message=message,
        details=details,
        steps=steps,
        suggestion=suggestion,
        retryable=True,
        operation_id=operation_id,
    )


def _safe_target_or_error(
    name: str,
    sticker_dir: Path,
    *,
    operation_id: str,
    operation_label: str,
) -> Path:
    try:
        path = _resolve_safe_file(name, sticker_dir)
    except HTTPException:
        _raise_operation(
            400,
            _validation_report(
                code="sticker_invalid_filename",
                phase="file_validation",
                title="表情包文件名无效",
                message="文件名必须是表情包目录内的单个文件名。",
                suggestion="刷新表情包列表后重新选择文件，不要提交目录或路径。",
                operation_id=operation_id,
                steps=(
                    operation_step("file_validation", "校验表情包文件", "error", "文件名未通过安全校验。"),
                    operation_step("operation", operation_label, "skipped", "未修改文件或 metadata。"),
                ),
            ),
        )
    if path.suffix.lower() not in _ALLOWED_UPLOAD_SUFFIXES:
        _raise_operation(
            400,
            _validation_report(
                code="sticker_unsupported_file_type",
                phase="file_validation",
                title="不支持的表情包文件类型",
                message="目标文件不是受支持的表情包格式。",
                suggestion="仅操作 JPEG、PNG、WebP 或 GIF 文件。",
                operation_id=operation_id,
                details=(operation_detail("扩展名", path.suffix.lower() or "无", "error"),),
                steps=(
                    operation_step("file_validation", "校验表情包文件", "error", "扩展名不受支持。"),
                    operation_step("operation", operation_label, "skipped", "未修改文件或 metadata。"),
                ),
            ),
        )
    return path


def _multipart_available() -> bool:
    """FastAPI 的 File/Form 依赖 python-multipart；缺失时不能注册 upload 端点，
    否则 startup 会失败。这里探测一次，缺包则降级跳过上传功能（其他端点照常）。
    """
    try:
        import python_multipart  # noqa: F401
        return True
    except Exception:
        try:
            import multipart  # noqa: F401  兼容旧版 python-multipart 包名
            return True
        except Exception:
            return False


def _sticker_dir(runtime) -> Path:
    cfg = getattr(runtime, "plugin_config", None)
    raw_path = getattr(cfg, "personification_sticker_path", None) or "data/stickers"
    return resolve_sticker_dir(raw_path, create=True)


def _resolve_safe_file(name: str, sticker_dir: Path) -> Path:
    """防路径穿越；返回绝对路径，若不在 sticker_dir 范围内抛 400。"""
    raw = str(name or "").strip()
    if not raw or raw in {".", ".."} or "/" in raw or "\\" in raw:
        raise HTTPException(status_code=400, detail="文件名不合法")
    candidate = (sticker_dir / raw).resolve()
    sticker_root = sticker_dir.resolve()
    try:
        candidate.relative_to(sticker_root)
    except ValueError:
        raise HTTPException(status_code=400, detail="文件路径越界")
    return candidate


def build_sticker_router(*, runtime) -> APIRouter:
    router = APIRouter(prefix="/api/stickers", tags=["stickers"])

    @router.get("")
    async def list_stickers(_: AdminIdentity = Depends(require_admin)) -> dict:
        sticker_dir = _sticker_dir(runtime)
        metadata = load_sticker_metadata(sticker_dir)
        raw_manifest = _load_raw_manifest(sticker_dir)
        files = list_local_sticker_files(sticker_dir, include_gif=True)
        items: list[dict[str, Any]] = []
        for file_path in files:
            entry = metadata.get(file_path.name) or {}
            if not isinstance(entry, dict):
                entry = {}
            raw_entry = raw_manifest.get(file_path.name) if isinstance(raw_manifest.get(file_path.name), dict) else {}
            # labeled 判定：原始 manifest 中 description 非空才算"已标"
            # （load_sticker_metadata 会用文件名兜底填 description，不能直接用规范化后的结果判断）
            raw_description = str((raw_entry or {}).get("description", "") or "").strip()
            items.append({
                "filename": file_path.name,
                "size_bytes": file_path.stat().st_size if file_path.exists() else 0,
                "thumbnail_url": f"/personification/api/stickers/file/{file_path.name}",
                "description": raw_description or str(entry.get("description", "") or ""),
                "mood_tags": list(entry.get("mood_tags") or []),
                "scene_tags": list(entry.get("scene_tags") or []),
                "proactive_send": bool(entry.get("proactive_send", False)),
                "use_hint": str(entry.get("use_hint", "") or ""),
                "avoid_hint": str(entry.get("avoid_hint", "") or ""),
                "weight": float(entry.get("weight", 1.0) or 1.0),
                "style": str(entry.get("style", "") or ""),
                "labeled_at": str(entry.get("labeled_at", "") or ""),
                "labeled": bool(raw_description),
            })
        items.sort(key=lambda x: x["filename"].lower())
        return {
            "stickers": items,
            "total": len(items),
            "labeled_count": sum(1 for it in items if it["labeled"]),
            "sticker_dir": str(sticker_dir),
        }

    @router.get("/file/{name}")
    async def get_sticker_file(
        name: str,
        _: AdminIdentity = Depends(require_admin),
    ):
        sticker_dir = _sticker_dir(runtime)
        path = _resolve_safe_file(name, sticker_dir)
        if not path.exists() or not path.is_file():
            raise HTTPException(status_code=404, detail="文件不存在")
        if path.suffix.lower() not in _ALLOWED_UPLOAD_SUFFIXES:
            raise HTTPException(status_code=400, detail="不支持的文件类型")
        return FileResponse(path)

    @router.patch("/{name}")
    async def update_sticker(
        name: str,
        body: dict = Body(default_factory=dict),
        _: AdminIdentity = Depends(require_admin),
    ) -> dict:
        operation_id = uuid.uuid4().hex
        try:
            sticker_dir = _sticker_dir(runtime)
            path = _safe_target_or_error(
                name,
                sticker_dir,
                operation_id=operation_id,
                operation_label="保存表情包 metadata",
            )
            if not path.exists() or not path.is_file():
                _raise_operation(
                    404,
                    operation_diagnostic(
                        ok=False,
                        code="sticker_not_found",
                        phase="file_validation",
                        title="表情包文件不存在",
                        message="目标表情包已不存在，metadata 未修改。",
                        details=(operation_detail("文件名", name, "error"),),
                        steps=(
                            operation_step("file_validation", "确认表情包文件", "error", "未找到目标文件。"),
                            operation_step("metadata_save", "保存表情包 metadata", "skipped", "未写入 manifest。"),
                        ),
                        suggestion="刷新表情包列表后重新选择。",
                        retryable=False,
                        operation_id=operation_id,
                    ),
                )
            _load_raw_manifest(sticker_dir, strict=True)
            metadata = load_sticker_metadata(sticker_dir)
            existing = metadata.get(name) if isinstance(metadata.get(name), dict) else {}
            merged = dict(existing)
            editable_keys = (
                "description", "mood_tags", "scene_tags", "proactive_send",
                "use_hint", "avoid_hint", "weight", "style",
            )
            for key in editable_keys:
                if key in body:
                    merged[key] = body[key]
            try:
                merged = normalize_sticker_entry(merged, file_name=name)
            except (TypeError, ValueError) as exc:
                _raise_operation(
                    400,
                    _validation_report(
                        code="sticker_metadata_invalid",
                        phase="metadata_validation",
                        title="表情包 metadata 无效",
                        message="提交的 metadata 字段类型或取值不符合要求。",
                        suggestion="刷新编辑内容并检查标签、权重等字段后重试。",
                        operation_id=operation_id,
                        details=(operation_detail("异常类型", type(exc).__name__, "error"),),
                        steps=(
                            operation_step("file_validation", "确认表情包文件", "ok", "目标文件存在。"),
                            operation_step("metadata_validation", "校验表情包 metadata", "error", "metadata 未通过规范化校验。"),
                            operation_step("metadata_save", "保存表情包 metadata", "skipped", "未写入 manifest。"),
                        ),
                    ),
                )
            metadata[name] = merged
            try:
                save_sticker_metadata_sync(sticker_dir, metadata)
            except Exception as exc:
                report = _exception_report(
                    exc,
                    runtime=runtime,
                    code="sticker_metadata_save_failed",
                    phase="metadata_save",
                    title="表情包 metadata 保存失败",
                    message="服务器未能确认 metadata 已完整保存。",
                    suggestion="刷新列表核对当前 metadata；确认未生效后再重试。",
                    operation_id=operation_id,
                    details=(operation_detail("文件名", name, "info"),),
                    steps=(
                        operation_step("file_validation", "确认表情包文件", "ok", "目标文件存在。"),
                        operation_step("metadata_validation", "校验表情包 metadata", "ok", "提交字段已规范化。"),
                        operation_step("metadata_save", "保存表情包 metadata", "error", "manifest 写入未明确完成。"),
                    ),
                    retryable=False,
                    outcome_unknown=True,
                )
                _raise_operation(500, report)
        except HTTPException:
            raise
        except Exception as exc:
            report = _exception_report(
                exc,
                runtime=runtime,
                code="sticker_update_failed",
                phase="filesystem_access",
                title="表情包保存异常中断",
                message="服务器访问表情包文件时发生内部异常。",
                suggestion="根据 Trace ID 检查脱敏日志并确认文件状态后再试。",
                operation_id=operation_id,
                details=(operation_detail("文件名", name, "info"),),
                steps=(
                    operation_step("file_validation", "确认表情包文件", "error", "文件系统检查未完成。"),
                    operation_step("metadata_save", "保存表情包 metadata", "skipped", "未确认 manifest 写入。"),
                ),
            )
            _raise_operation(500, report)
        report = operation_diagnostic(
            ok=True,
            code="sticker_metadata_saved",
            phase="operation_complete",
            title="表情包 metadata 已保存",
            message="编辑内容已规范化并写入 manifest。",
            details=(operation_detail("文件名", name, "ok"),),
            steps=(
                operation_step("file_validation", "确认表情包文件", "ok", "目标文件存在。"),
                operation_step("metadata_validation", "校验表情包 metadata", "ok", "提交字段已规范化。"),
                operation_step("metadata_save", "保存表情包 metadata", "ok", "manifest 已写入。"),
            ),
            suggestion="无需重复保存。",
            operation_id=operation_id,
        )
        return _operation_result(report, success=True, filename=name, entry=merged)

    @router.delete("/{name}")
    async def delete_sticker(
        name: str,
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        operation_id = uuid.uuid4().hex
        moved = False
        safe_trash_path = ""
        try:
            sticker_dir = _sticker_dir(runtime)
            path = _safe_target_or_error(
                name,
                sticker_dir,
                operation_id=operation_id,
                operation_label="移动文件并更新 manifest",
            )
            if not path.exists() or not path.is_file():
                _raise_operation(
                    404,
                    operation_diagnostic(
                        ok=False,
                        code="sticker_not_found",
                        phase="file_validation",
                        title="表情包文件不存在",
                        message="目标表情包已不存在，本次未执行回收操作。",
                        details=(operation_detail("文件名", name, "error"),),
                        steps=(
                            operation_step("file_validation", "确认表情包文件", "error", "未找到目标文件。"),
                            operation_step("trash_move", "移动到回收目录", "skipped", "没有移动文件。"),
                            operation_step("manifest_save", "从 manifest 移除", "skipped", "没有修改 manifest。"),
                        ),
                        suggestion="刷新表情包列表确认当前状态。",
                        retryable=False,
                        operation_id=operation_id,
                    ),
                )
            trash_day = time.strftime("%Y%m%d")
            trash_dir = sticker_dir / "trash" / trash_day
            safe_trash_path = f"trash/{trash_day}/{name}"
            _load_raw_manifest(sticker_dir, strict=True)
            metadata = load_sticker_metadata(sticker_dir)
            try:
                trash_dir.mkdir(parents=True, exist_ok=True)
                dest = trash_dir / name
                shutil.move(str(path), str(dest))
                moved = True
            except Exception as exc:
                source_exists = path.exists()
                destination_exists = bool(safe_trash_path) and (trash_dir / name).exists()
                uncertain = destination_exists or not source_exists
                report = _exception_report(
                    exc,
                    runtime=runtime,
                    code="sticker_trash_move_failed",
                    phase="trash_move",
                    title="表情包移动到回收目录失败",
                    message="服务器未能明确完成文件移动。",
                    suggestion=(
                        "刷新列表并检查回收目录中的文件状态；确认文件仍在原位后才可重试。"
                        if uncertain
                        else "检查表情包目录权限或同名文件冲突后重试。"
                    ),
                    operation_id=operation_id,
                    details=(operation_detail("文件名", name, "info"),),
                    steps=(
                        operation_step("file_validation", "确认表情包文件", "ok", "目标文件存在。"),
                        operation_step("trash_move", "移动到回收目录", "unknown" if uncertain else "error", "文件移动未明确完成。"),
                        operation_step("manifest_save", "从 manifest 移除", "skipped", "manifest 未修改。"),
                    ),
                    retryable=not uncertain,
                    partial=destination_exists,
                    outcome_unknown=uncertain,
                )
                _raise_operation(500, report)
            metadata.pop(name, None)
            try:
                save_sticker_metadata_sync(sticker_dir, metadata)
            except Exception as exc:
                report = _exception_report(
                    exc,
                    runtime=runtime,
                    code="sticker_delete_manifest_partial",
                    phase="manifest_save",
                    title="表情包只完成了文件回收",
                    message="文件已移到回收目录，但 manifest 更新失败。",
                    suggestion="保留已回收文件；修复 manifest 写入问题后刷新或执行扫描，不要重复删除。",
                    operation_id=operation_id,
                    details=(
                        operation_detail("文件名", name, "info"),
                        operation_detail("回收位置", safe_trash_path, "ok"),
                    ),
                    steps=(
                        operation_step("file_validation", "确认表情包文件", "ok", "目标文件存在。"),
                        operation_step("trash_move", "移动到回收目录", "ok", "文件已移到库内回收目录。"),
                        operation_step("manifest_save", "从 manifest 移除", "error", "manifest 未能保存。"),
                    ),
                    retryable=False,
                    partial=True,
                )
                webui_audit_log.record(
                    action="sticker_delete",
                    qq=admin.qq,
                    device_id=admin.device_id,
                    target=name,
                    detail={"trash_path": safe_trash_path, "code": report["code"], "trace_id": report["trace_id"]},
                    outcome="partial",
                )
                _raise_operation(500, report)
        except HTTPException:
            raise
        except Exception as exc:
            manifest_partial = moved
            report = _exception_report(
                exc,
                runtime=runtime,
                code="sticker_delete_manifest_partial" if manifest_partial else "sticker_delete_failed",
                phase="manifest_save" if manifest_partial else "filesystem_access",
                title="表情包只完成了文件回收" if manifest_partial else "表情包回收异常中断",
                message=(
                    "文件已移到回收目录，但 manifest 处理异常中断。"
                    if manifest_partial
                    else "服务器访问表情包库时发生内部异常。"
                ),
                suggestion=(
                    "保留已回收文件；修复 manifest 问题后刷新或执行扫描，不要重复删除。"
                    if manifest_partial
                    else "根据 Trace ID 检查脱敏日志并刷新列表确认文件状态。"
                ),
                operation_id=operation_id,
                details=(
                    operation_detail("文件名", name, "info"),
                    *((operation_detail("回收位置", safe_trash_path, "ok"),) if manifest_partial else ()),
                ),
                steps=(
                    operation_step("file_validation", "确认表情包文件", "ok" if manifest_partial else "error", "目标文件已确认。" if manifest_partial else "文件系统检查未完成。"),
                    operation_step("trash_move", "移动到回收目录", "ok" if manifest_partial else "skipped", "文件已移到库内回收目录。" if manifest_partial else "未确认文件移动。"),
                    operation_step("manifest_save", "从 manifest 移除", "error" if manifest_partial else "skipped", "manifest 处理异常中断。" if manifest_partial else "未确认 manifest 写入。"),
                ),
                retryable=not moved,
                partial=manifest_partial,
            )
            _raise_operation(500, report)
        webui_audit_log.record(
            action="sticker_delete",
            qq=admin.qq,
            device_id=admin.device_id,
            target=name,
            detail={"trash_path": safe_trash_path},
        )
        report = operation_diagnostic(
            ok=True,
            code="sticker_deleted",
            phase="operation_complete",
            title="表情包已移到回收目录",
            message="文件回收和 manifest 更新均已完成。",
            details=(
                operation_detail("文件名", name, "ok"),
                operation_detail("回收位置", safe_trash_path, "ok"),
            ),
            steps=(
                operation_step("file_validation", "确认表情包文件", "ok", "目标文件存在。"),
                operation_step("trash_move", "移动到回收目录", "ok", "文件已移到库内回收目录。"),
                operation_step("manifest_save", "从 manifest 移除", "ok", "manifest 已保存。"),
            ),
            suggestion="如需恢复，请从显示的库内相对位置手动恢复文件。",
            operation_id=operation_id,
        )
        return _operation_result(report, success=True, trash_path=safe_trash_path)

    if _multipart_available():
        @router.post("/upload")
        async def upload_sticker(
            file: UploadFile = File(...),
            description: str = Form(default=""),
            admin: AdminIdentity = Depends(require_admin),
        ) -> dict:
            operation_id = uuid.uuid4().hex
            filename = (file.filename or "").strip() or f"sticker_{int(time.time())}.png"
            suffix = Path(filename).suffix.lower()
            if suffix not in _ALLOWED_UPLOAD_SUFFIXES:
                _raise_operation(
                    400,
                    _validation_report(
                        code="sticker_upload_invalid_extension",
                        phase="file_validation",
                        title="上传文件类型不受支持",
                        message="上传文件必须使用 JPEG、PNG、WebP 或 GIF 扩展名。",
                        suggestion="选择受支持的图片文件后重新上传。",
                        operation_id=operation_id,
                        details=(operation_detail("扩展名", suffix or "无", "error"),),
                        steps=(
                            operation_step("file_validation", "校验上传文件", "error", "扩展名不受支持。"),
                            operation_step("file_write", "写入表情包文件", "skipped", "未写入文件。"),
                            operation_step("metadata_save", "保存上传 metadata", "skipped", "未写入 manifest。"),
                        ),
                    ),
                )
            if filename in {".", ".."} or "/" in filename or "\\" in filename:
                _raise_operation(
                    400,
                    _validation_report(
                        code="sticker_invalid_filename",
                        phase="file_validation",
                        title="上传文件名无效",
                        message="上传文件名必须是单个文件名，不能包含目录或路径。",
                        suggestion="重命名文件后重新选择上传。",
                        operation_id=operation_id,
                        steps=(
                            operation_step("file_validation", "校验上传文件", "error", "文件名未通过安全校验。"),
                            operation_step("file_write", "写入表情包文件", "skipped", "未写入文件。"),
                            operation_step("metadata_save", "保存上传 metadata", "skipped", "未写入 manifest。"),
                        ),
                    ),
                )
            try:
                payload = await file.read()
            except Exception as exc:
                report = _exception_report(
                    exc,
                    runtime=runtime,
                    code="sticker_upload_read_failed",
                    phase="file_validation",
                    title="读取上传文件失败",
                    message="服务器未能读取完整的上传内容。",
                    suggestion="重新选择文件后再试；若持续失败，请检查上传链路。",
                    operation_id=operation_id,
                    steps=(
                        operation_step("file_validation", "校验上传文件", "error", "未读取到完整上传内容。"),
                        operation_step("file_write", "写入表情包文件", "skipped", "未写入文件。"),
                        operation_step("metadata_save", "保存上传 metadata", "skipped", "未写入 manifest。"),
                    ),
                )
                _raise_operation(500, report)
            if not payload:
                _raise_operation(
                    400,
                    _validation_report(
                        code="sticker_upload_empty_file",
                        phase="file_validation",
                        title="上传文件为空",
                        message="上传内容不包含任何字节。",
                        suggestion="选择有效图片文件后重新上传。",
                        operation_id=operation_id,
                        details=(operation_detail("文件大小", 0, "error"),),
                        steps=(
                            operation_step("file_validation", "校验上传文件", "error", "文件内容为空。"),
                            operation_step("file_write", "写入表情包文件", "skipped", "未写入文件。"),
                            operation_step("metadata_save", "保存上传 metadata", "skipped", "未写入 manifest。"),
                        ),
                    ),
                )
            if len(payload) > _MAX_UPLOAD_BYTES:
                _raise_operation(
                    413,
                    _validation_report(
                        code="sticker_upload_too_large",
                        phase="file_validation",
                        title="上传文件超过大小限制",
                        message=f"单个表情包不能超过 {_MAX_UPLOAD_BYTES // 1024 // 1024} MB。",
                        suggestion="压缩图片或选择更小的文件后重新上传。",
                        operation_id=operation_id,
                        details=(
                            operation_detail("文件大小", len(payload), "error"),
                            operation_detail("大小上限", _MAX_UPLOAD_BYTES, "info"),
                        ),
                        steps=(
                            operation_step("file_validation", "校验上传文件", "error", "文件超过大小上限。"),
                            operation_step("file_write", "写入表情包文件", "skipped", "未写入文件。"),
                            operation_step("metadata_save", "保存上传 metadata", "skipped", "未写入 manifest。"),
                        ),
                    ),
                )
            try:
                sticker_dir = _sticker_dir(runtime)
                target = _resolve_safe_file(filename, sticker_dir)
                if target.exists():
                    target = _resolve_safe_file(
                        f"{Path(filename).stem}_{int(time.time())}{suffix}",
                        sticker_dir,
                    )
                target.write_bytes(payload)
            except Exception as exc:
                report = _exception_report(
                    exc,
                    runtime=runtime,
                    code="sticker_upload_write_failed",
                    phase="file_write",
                    title="表情包文件写入失败",
                    message="服务器未能把上传内容写入表情包库。",
                    suggestion="检查表情包目录空间和权限后重试；如列表出现同名文件，请先核对其大小。",
                    operation_id=operation_id,
                    details=(operation_detail("文件大小", len(payload), "info"),),
                    steps=(
                        operation_step("file_validation", "校验上传文件", "ok", "文件名、扩展名和大小校验通过。"),
                        operation_step("file_write", "写入表情包文件", "error", "文件写入未完成。"),
                        operation_step("metadata_save", "保存上传 metadata", "skipped", "未写入 manifest。"),
                    ),
                )
                _raise_operation(500, report)
            try:
                _load_raw_manifest(sticker_dir, strict=True)
                metadata = load_sticker_metadata(sticker_dir)
                metadata[target.name] = normalize_sticker_entry(
                    {"description": description},
                    file_name=target.name,
                    file_hash=compute_file_hash(payload),
                )
                save_sticker_metadata_sync(sticker_dir, metadata)
            except Exception as exc:
                report = _exception_report(
                    exc,
                    runtime=runtime,
                    code="sticker_upload_metadata_partial",
                    phase="metadata_save",
                    title="上传只完成了文件写入",
                    message="表情包文件已写入，但 manifest metadata 保存失败。",
                    suggestion="刷新列表核对新文件并执行扫描补齐 metadata，不要重复上传同一文件。",
                    operation_id=operation_id,
                    details=(
                        operation_detail("文件名", target.name, "ok"),
                        operation_detail("文件大小", len(payload), "ok"),
                    ),
                    steps=(
                        operation_step("file_validation", "校验上传文件", "ok", "文件名、扩展名和大小校验通过。"),
                        operation_step("file_write", "写入表情包文件", "ok", "文件已写入表情包库。"),
                        operation_step("metadata_save", "保存上传 metadata", "error", "manifest 未能保存。"),
                    ),
                    retryable=False,
                    partial=True,
                )
                webui_audit_log.record(
                    action="sticker_upload",
                    qq=admin.qq,
                    device_id=admin.device_id,
                    target=target.name,
                    detail={"size_bytes": len(payload), "code": report["code"], "trace_id": report["trace_id"]},
                    outcome="partial",
                )
                _raise_operation(500, report)
            webui_audit_log.record(
                action="sticker_upload",
                qq=admin.qq,
                device_id=admin.device_id,
                target=target.name,
                detail={"size_bytes": len(payload), "has_description": bool(description)},
            )
            report = operation_diagnostic(
                ok=True,
                code="sticker_uploaded",
                phase="operation_complete",
                title="表情包上传完成",
                message="文件和 manifest metadata 均已保存。",
                details=(
                    operation_detail("文件名", target.name, "ok"),
                    operation_detail("文件大小", len(payload), "ok"),
                    operation_detail("打标状态", "待打标" if not description else "已有描述", "info"),
                ),
                steps=(
                    operation_step("file_validation", "校验上传文件", "ok", "文件名、扩展名和大小校验通过。"),
                    operation_step("file_write", "写入表情包文件", "ok", "文件已写入表情包库。"),
                    operation_step("metadata_save", "保存上传 metadata", "ok", "manifest 已写入。"),
                ),
                suggestion="等待 labeler 处理待打标文件；已有描述的文件无需重复上传。",
                operation_id=operation_id,
            )
            return _operation_result(
                report,
                success=True,
                filename=target.name,
                size_bytes=len(payload),
                needs_labeling=not description,
            )
    else:
        # 优雅降级：缺 python-multipart 时只注册占位端点，
        # 用户调用 /upload 会得到 503 + 安装提示；其他端点（列表 / 编辑 / 删除 / 重扫）保持可用。
        @router.post("/upload")
        async def upload_sticker_unavailable(
            admin: AdminIdentity = Depends(require_admin),
        ) -> dict:
            operation_id = uuid.uuid4().hex
            _raise_operation(
                503,
                operation_diagnostic(
                    ok=False,
                    code="sticker_upload_dependency_missing",
                    phase="upload_dependency",
                    title="表情包上传功能未启用",
                    message="Bot 进程缺少处理 multipart 上传所需的依赖。",
                    details=(operation_detail("缺少依赖", "python-multipart", "error"),),
                    steps=(
                        operation_step("upload_dependency", "检查上传依赖", "error", "python-multipart 不可用。"),
                        operation_step("file_validation", "校验上传文件", "skipped", "请求未进入文件处理。"),
                    ),
                    suggestion="在 Bot 的 Python 环境安装 python-multipart 并重启后再试。",
                    retryable=False,
                    operation_id=operation_id,
                ),
            )

    @router.post("/rescan")
    async def rescan_stickers(
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        """触发 labeler 重新扫描表情包目录。
        mode=missing_only（默认）仅扫描无 description 的文件；force_all 全部重打标。
        实际打标逻辑在 sticker_labeler skill 启动钩子中；此处只是清空指定文件的 manifest 条目，
        让下次启动或后台 labeler 扫描时重新分析。
        """
        operation_id = uuid.uuid4().hex
        mode = str((body or {}).get("mode", "missing_only") or "missing_only")
        if mode not in {"missing_only", "force_all"}:
            _raise_operation(
                400,
                _validation_report(
                    code="sticker_rescan_invalid_mode",
                    phase="request_validation",
                    title="表情包扫描模式无效",
                    message="mode 只能是 missing_only 或 force_all。",
                    suggestion="重新选择“扫描未打标”或“全部重打标”。",
                    operation_id=operation_id,
                    details=(operation_detail("扫描模式", mode, "error"),),
                    steps=(
                        operation_step("request_validation", "校验扫描模式", "error", "扫描模式不受支持。"),
                        operation_step("manifest_read", "读取 manifest", "skipped", "未读取 manifest。"),
                        operation_step("manifest_save", "安排重新打标", "skipped", "未修改 manifest。"),
                    ),
                ),
            )
        rescan_stage = "manifest_read"
        try:
            sticker_dir = _sticker_dir(runtime)
            raw_manifest = _load_raw_manifest(sticker_dir, strict=True)
            files = list_local_sticker_files(sticker_dir, include_gif=True)
            cleared = 0
            meta_block = raw_manifest.get("_meta") if isinstance(raw_manifest.get("_meta"), dict) else None
            new_manifest: dict[str, Any] = {}
            for name, entry in raw_manifest.items():
                if name == "_meta":
                    continue
                entry_dict = entry if isinstance(entry, dict) else {}
                desc = str(entry_dict.get("description", "") or "").strip()
                if mode == "force_all":
                    new_manifest[name] = {}
                    cleared += 1
                elif not desc:
                    new_manifest[name] = {}
                    cleared += 1
                else:
                    new_manifest[name] = entry_dict
            existing_names = {file.name for file in files}
            for name in existing_names - set(new_manifest.keys()):
                new_manifest[name] = {}
                cleared += 1
            if meta_block:
                new_manifest["_meta"] = meta_block
            rescan_stage = "manifest_save"
            path = sticker_metadata_path(sticker_dir)
            path.write_text(json.dumps(new_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            is_manifest_data_error = isinstance(exc, (json.JSONDecodeError, ValueError))
            phase = "manifest_read" if is_manifest_data_error else rescan_stage
            code = "sticker_manifest_invalid" if is_manifest_data_error else "sticker_rescan_filesystem_failed"
            title = "表情包 manifest 无法读取" if is_manifest_data_error else "表情包扫描异常中断"
            manifest_read_ok = rescan_stage == "manifest_save"
            report = _exception_report(
                exc,
                runtime=runtime,
                code=code,
                phase=phase,
                title=title,
                message=(
                    "现有 manifest 不是有效的 JSON object，服务器没有覆盖它。"
                    if is_manifest_data_error
                    else "服务器读取或写入表情包库时发生异常。"
                ),
                suggestion=(
                    "先备份并修复 stickers.json，再重新扫描。"
                    if is_manifest_data_error
                    else "根据 Trace ID 检查脱敏日志与目录权限，确认 manifest 状态后再试。"
                ),
                operation_id=operation_id,
                details=(operation_detail("扫描模式", mode, "info"),),
                steps=(
                    operation_step("request_validation", "校验扫描模式", "ok", "扫描模式有效。"),
                    operation_step("manifest_read", "读取 manifest", "ok" if manifest_read_ok else "error", "已读取 manifest 和文件列表。" if manifest_read_ok else "未能取得可安全更新的 manifest。"),
                    operation_step("manifest_save", "安排重新打标", "unknown" if manifest_read_ok else "skipped", "manifest 写入未明确完成。" if manifest_read_ok else "未修改 manifest。"),
                ),
                retryable=False if is_manifest_data_error else None,
                outcome_unknown=manifest_read_ok,
            )
            _raise_operation(500, report)
        webui_audit_log.record(
            action="sticker_rescan",
            qq=admin.qq,
            device_id=admin.device_id,
            detail={"mode": mode, "scheduled": cleared},
        )
        report = operation_diagnostic(
            ok=True,
            code="sticker_rescan_scheduled",
            phase="operation_complete",
            title="表情包重新打标已安排",
            message=f"已为 {cleared} 个条目清空待重建 metadata。",
            details=(
                operation_detail("扫描模式", mode, "ok"),
                operation_detail("安排数量", cleared, "ok"),
                operation_detail("发现文件", len(files), "info"),
            ),
            steps=(
                operation_step("request_validation", "校验扫描模式", "ok", "扫描模式有效。"),
                operation_step("manifest_read", "读取 manifest", "ok", "已读取 manifest 和表情包文件列表。"),
                operation_step("manifest_save", "安排重新打标", "ok", "manifest 已保存。"),
            ),
            warnings=("本操作只清空 metadata；实际打标将在下次启动或后台 labeler 扫描时执行。",),
            suggestion="等待 labeler 执行，不要重复触发同一范围的扫描。",
            operation_id=operation_id,
        )
        return _operation_result(
            report,
            success=True,
            mode=mode,
            scheduled=cleared,
            hint="已清空对应条目；下次插件启动或后台 labeler 扫描时会自动重新打标。",
        )

    return router
