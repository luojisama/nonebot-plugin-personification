from __future__ import annotations

import asyncio
import hmac
import inspect
import json
import re
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import connect_sync, get_db_path


QQ_OUTBOUND_STATUSES = frozenset({"sent", "failed", "unknown"})
DEFAULT_RECALL_WINDOW_SECONDS = 5 * 60
DEFAULT_SOCIAL_RECALL_SURFACES = frozenset(
    {
        "normal_reply",
        "yaml_reply",
        "reply_text",
        "reply_media",
        "reply_sticker",
        "reply_ack",
        "reply_tts",
        "reply_translation_forward",
        "agent_action",
        "agent_action_text",
        "agent_action_image",
        "agent_action_sticker",
        "agent_action_qq_expression",
        "proactive_private",
        "proactive_group",
        "proactive_group_idle",
        "scheduled_user_task",
        "social_topic_followup",
        "social_news_private",
        "social_news_group",
        "social_greeting",
        "social_festival_greeting",
    }
)

_MESSAGE_ID_KEYS = ("message_id", "msg_id", "messageId")
_DATA_URI_RE = re.compile(
    r"(?i)data:[a-z0-9.+-]+/[a-z0-9.+-]+(?:;[^,\s]*)?,[^\s\]\[<>{}\"']*"
)
_BASE64_SCHEME_RE = re.compile(r"(?i)base64://[a-z0-9+/_=-]+")
_BASE64_ASSIGNMENT_RE = re.compile(
    r"(?i)([\"']?(?:base64|b64|b64_json|image_b64)[\"']?\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;}\]]+)"
)
_FILE_REFERENCE_RE = re.compile(
    r"(?i)([\"']?(?:file|path)[\"']?\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;}\]]+)"
)
_URL_RE = re.compile(r"(?i)\b(?:(?:https?|ftp|file)://|www\.)[^\s<>\]\[{}\"']+")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+")
_CREDENTIAL_RE = re.compile(
    r"(?i)([\"']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|"
    r"cookie|password|passwd|secret|credential|token|p_skey|skey|pskey|session[_-]?id)"
    r"[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;}\]]+)"
)
_LONG_BASE64_RE = re.compile(
    r"(?<![a-zA-Z0-9+/_=-])[a-zA-Z0-9+/_-]{24,}={0,2}(?![a-zA-Z0-9+/_=-])"
)
_PADDED_BASE64_RE = re.compile(
    r"(?<![a-zA-Z0-9+/_=-])[a-zA-Z0-9+/_-]{8,}={1,2}(?![a-zA-Z0-9+/_=-])"
)
_SAFE_ERROR_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,31}")


@dataclass(frozen=True)
class OutboundContext:
    operation_id: str
    bot_id: str
    conversation_kind: str
    conversation_id: str
    user_target: str
    surface: str


@dataclass(frozen=True)
class SendReceipt:
    id: int
    operation_id: str
    part_index: int
    bot_id: str
    conversation_kind: str
    conversation_id: str
    message_id: str | None
    user_target: str
    surface: str
    status: str
    preview: str
    content_hmac: str
    error_code: str
    created_at: float
    updated_at: float
    recalled_at: float

    @property
    def ledger_id(self) -> int:
        return self.id


def build_outbound_context(
    *,
    bot: Any,
    event: Any,
    surface: str,
    operation_id: str = "",
    user_target: str = "",
) -> OutboundContext:
    bot_id = str(getattr(bot, "self_id", "") or getattr(event, "self_id", "") or "").strip()
    group_id = str(getattr(event, "group_id", "") or "").strip()
    event_user_id = str(getattr(event, "user_id", "") or "").strip()
    conversation_kind = "group" if group_id else "private"
    conversation_id = group_id or event_user_id
    resolved_operation_id = str(operation_id or "").strip() or f"qq:{uuid.uuid4().hex}"
    return OutboundContext(
        operation_id=resolved_operation_id,
        bot_id=bot_id,
        conversation_kind=conversation_kind,
        conversation_id=conversation_id,
        user_target=str(user_target or event_user_id).strip(),
        surface=str(surface or "").strip(),
    )


def _preview_source(content: Any) -> str:
    if isinstance(content, (bytes, bytearray, memoryview)):
        return "[binary]"
    try:
        return str(content or "")
    except Exception:
        return f"[{type(content).__name__}]"


def sanitize_outbound_preview(content: Any, *, limit: int = 120) -> str:
    text = _preview_source(content)
    text = _DATA_URI_RE.sub("[DATA_URI]", text)
    text = _BASE64_SCHEME_RE.sub("[BASE64]", text)
    text = _BASE64_ASSIGNMENT_RE.sub(r"\1[BASE64]", text)
    text = _FILE_REFERENCE_RE.sub(r"\1[FILE]", text)
    text = _URL_RE.sub("[URL]", text)
    text = _BEARER_RE.sub("Bearer [CREDENTIAL]", text)
    text = _CREDENTIAL_RE.sub(r"\1[CREDENTIAL]", text)
    text = _PADDED_BASE64_RE.sub("[BASE64]", text)
    text = _LONG_BASE64_RE.sub("[BASE64]", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[: max(0, min(120, int(limit)))]


def _content_bytes(content: Any) -> bytes:
    if isinstance(content, bytes):
        return content
    if isinstance(content, (bytearray, memoryview)):
        return bytes(content)
    if isinstance(content, str):
        return content.encode("utf-8")
    try:
        serialized = json.dumps(
            content,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        serialized = _preview_source(content)
    return serialized.encode("utf-8", errors="replace")


def _normalize_message_id(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    normalized = str(value).strip()
    if not normalized or len(normalized) > 160 or any(ord(char) < 32 for char in normalized):
        return None
    return normalized


def parse_onebot_message_id(result: Any) -> str | None:
    seen: set[int] = set()

    def _visit(value: Any, depth: int) -> str | None:
        if depth > 8:
            return None
        if isinstance(value, Mapping):
            identity = id(value)
            if identity in seen:
                return None
            seen.add(identity)
            for key in _MESSAGE_ID_KEYS:
                if key in value:
                    message_id = _normalize_message_id(value[key])
                    if message_id is not None:
                        return message_id
            if "data" in value:
                return _visit(value["data"], depth + 1)
            return None
        if isinstance(value, (list, tuple)):
            identity = id(value)
            if identity in seen:
                return None
            seen.add(identity)
            for item in value:
                message_id = _visit(item, depth + 1)
                if message_id is not None:
                    return message_id
        return None

    return _visit(result, 0)


def _normalize_scope(
    bot_id: Any,
    conversation_kind: Any,
    conversation_id: Any,
) -> tuple[str, str, str]:
    normalized_bot_id = str(bot_id or "").strip()
    normalized_kind = str(conversation_kind or "").strip().lower()
    normalized_conversation_id = str(conversation_id or "").strip()
    if not normalized_bot_id:
        raise ValueError("bot_id is required")
    if normalized_kind not in {"group", "private"}:
        raise ValueError("conversation_kind must be group or private")
    if not normalized_conversation_id:
        raise ValueError("conversation_id is required")
    return normalized_bot_id, normalized_kind, normalized_conversation_id


def _normalize_context(context: OutboundContext) -> OutboundContext:
    if not isinstance(context, OutboundContext):
        raise TypeError("context must be OutboundContext")
    operation_id = str(context.operation_id or "").strip()
    if not operation_id:
        raise ValueError("operation_id is required")
    bot_id, conversation_kind, conversation_id = _normalize_scope(
        context.bot_id,
        context.conversation_kind,
        context.conversation_id,
    )
    surface = str(context.surface or "").strip()
    if not surface:
        raise ValueError("surface is required")
    return OutboundContext(
        operation_id=operation_id,
        bot_id=bot_id,
        conversation_kind=conversation_kind,
        conversation_id=conversation_id,
        user_target=str(context.user_target or "").strip(),
        surface=surface,
    )


def _row_to_receipt(row: Any) -> SendReceipt:
    return SendReceipt(
        id=int(row["id"]),
        operation_id=str(row["operation_id"]),
        part_index=int(row["part_index"]),
        bot_id=str(row["bot_id"]),
        conversation_kind=str(row["conversation_kind"]),
        conversation_id=str(row["conversation_id"]),
        message_id=str(row["message_id"]) if row["message_id"] is not None else None,
        user_target=str(row["user_target"]),
        surface=str(row["surface"]),
        status=str(row["status"]),
        preview=str(row["preview"]),
        content_hmac=str(row["content_hmac"]),
        error_code=str(row["error_code"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        recalled_at=float(row["recalled_at"]),
    )


def _exception_error_code(exc: BaseException) -> str:
    exception_type = type(exc).__name__[:64] or "Exception"
    for attribute in ("retcode", "status_code", "error_code", "code"):
        value = getattr(exc, attribute, None)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, int):
            return f"{exception_type}:{attribute}={value}"[:120]
        if isinstance(value, str) and _SAFE_ERROR_TOKEN_RE.fullmatch(value):
            return f"{exception_type}:{attribute}={value}"[:120]
    return exception_type


class QQOutboundLedger:
    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        content_hmac_key: bytes | bytearray | memoryview | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db_path = Path(db_path or get_db_path())
        self._clock = clock
        if content_hmac_key is None:
            self._content_hmac_key: bytes | None = None
        else:
            key = bytes(content_hmac_key)
            if len(key) != 32:
                raise ValueError("content_hmac_key must be exactly 32 bytes")
            self._content_hmac_key = key

    def _timestamp(self, value: float | None = None) -> float:
        return float(self._clock() if value is None else value)

    def _content_hmac(self, content: Any) -> str:
        if self._content_hmac_key is None:
            return ""
        return hmac.new(self._content_hmac_key, _content_bytes(content), "sha256").hexdigest()

    def begin(
        self,
        context: OutboundContext,
        content: Any,
        *,
        now: float | None = None,
    ) -> SendReceipt:
        normalized = _normalize_context(context)
        timestamp = self._timestamp(now)
        preview = sanitize_outbound_preview(content)
        content_hmac = self._content_hmac(content)
        with connect_sync(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing_scope = conn.execute(
                    """
                    SELECT bot_id, conversation_kind, conversation_id
                    FROM qq_outbound_ledger
                    WHERE operation_id=?
                    ORDER BY part_index ASC
                    LIMIT 1
                    """,
                    (normalized.operation_id,),
                ).fetchone()
                if existing_scope is not None and (
                    str(existing_scope["bot_id"]) != normalized.bot_id
                    or str(existing_scope["conversation_kind"]) != normalized.conversation_kind
                    or str(existing_scope["conversation_id"]) != normalized.conversation_id
                ):
                    raise ValueError("operation_id is already bound to another conversation scope")
                recall_seal = conn.execute(
                    """
                    SELECT 1
                    FROM qq_recall_operations
                    WHERE outbound_operation_id=?
                    LIMIT 1
                    """,
                    (normalized.operation_id,),
                ).fetchone()
                if recall_seal is not None:
                    raise RuntimeError("operation_id is sealed by a recall claim")
                row = conn.execute(
                    """
                    SELECT COALESCE(MAX(part_index), -1) + 1 AS next_part
                    FROM qq_outbound_ledger
                    WHERE operation_id=?
                    """,
                    (normalized.operation_id,),
                ).fetchone()
                part_index = int(row["next_part"] if row is not None else 0)
                cursor = conn.execute(
                    """
                    INSERT INTO qq_outbound_ledger(
                        operation_id, part_index, bot_id, conversation_kind,
                        conversation_id, message_id, user_target, surface, status,
                        preview, content_hmac, error_code, created_at, updated_at, recalled_at
                    ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, 'unknown', ?, ?, '', ?, ?, 0)
                    """,
                    (
                        normalized.operation_id,
                        part_index,
                        normalized.bot_id,
                        normalized.conversation_kind,
                        normalized.conversation_id,
                        normalized.user_target,
                        normalized.surface,
                        preview,
                        content_hmac,
                        timestamp,
                        timestamp,
                    ),
                )
                ledger_id = int(cursor.lastrowid)
                receipt_row = conn.execute(
                    "SELECT * FROM qq_outbound_ledger WHERE id=?",
                    (ledger_id,),
                ).fetchone()
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        if receipt_row is None:
            raise RuntimeError("qq outbound ledger insert was not readable")
        return _row_to_receipt(receipt_row)

    def _update_dispatch(
        self,
        receipt: SendReceipt,
        *,
        status: str,
        message_id: str | None,
        error_code: str,
        now: float | None,
    ) -> SendReceipt:
        if status not in QQ_OUTBOUND_STATUSES:
            raise ValueError("invalid qq outbound status")
        timestamp = self._timestamp(now)
        with connect_sync(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE qq_outbound_ledger
                SET status=?, message_id=?, error_code=?, updated_at=?
                WHERE id=? AND bot_id=? AND conversation_kind=? AND conversation_id=?
                  AND status='unknown' AND recalled_at=0
                """,
                (
                    status,
                    message_id,
                    str(error_code or "")[:120],
                    timestamp,
                    receipt.id,
                    receipt.bot_id,
                    receipt.conversation_kind,
                    receipt.conversation_id,
                ),
            )
            row = conn.execute(
                "SELECT * FROM qq_outbound_ledger WHERE id=?",
                (receipt.id,),
            ).fetchone()
            conn.commit()
        if row is None:
            raise RuntimeError("qq outbound ledger row disappeared")
        return _row_to_receipt(row)

    async def dispatch(
        self,
        context: OutboundContext,
        content: Any,
        send: Callable[[], Any],
        *,
        now: float | None = None,
    ) -> SendReceipt:
        receipt = await asyncio.to_thread(self.begin, context, content, now=now)
        try:
            result = send()
            if inspect.isawaitable(result):
                result = await result
        except asyncio.CancelledError as exc:
            await asyncio.to_thread(
                self._update_dispatch,
                receipt,
                status="unknown",
                message_id=None,
                error_code=_exception_error_code(exc),
                now=now,
            )
            raise
        except Exception as exc:
            await asyncio.to_thread(
                self._update_dispatch,
                receipt,
                status="unknown",
                message_id=None,
                error_code=_exception_error_code(exc),
                now=now,
            )
            raise

        message_id = parse_onebot_message_id(result)
        if message_id is None:
            return await asyncio.to_thread(
                self._update_dispatch,
                receipt,
                status="unknown",
                message_id=None,
                error_code="message_id_missing",
                now=now,
            )
        return await asyncio.to_thread(
            self._update_dispatch,
            receipt,
            status="sent",
            message_id=message_id,
            error_code="",
            now=now,
        )

    def list_recall_candidates(
        self,
        *,
        bot_id: str,
        conversation_kind: str,
        conversation_id: str,
        since: float | None = None,
        until: float | None = None,
        now: float | None = None,
        window_seconds: float = DEFAULT_RECALL_WINDOW_SECONDS,
        limit: int = 20,
        surfaces: Iterable[str] | None = None,
        user_target: str | None = None,
    ) -> list[SendReceipt]:
        normalized_bot_id, normalized_kind, normalized_conversation_id = _normalize_scope(
            bot_id,
            conversation_kind,
            conversation_id,
        )
        normalized_limit = max(0, min(500, int(limit)))
        if normalized_limit == 0:
            return []
        end_at = float(until) if until is not None else self._timestamp(now)
        start_at = (
            float(since)
            if since is not None
            else end_at - max(0.0, float(window_seconds))
        )
        if start_at > end_at:
            return []
        allowed_surfaces = {
            str(surface or "").strip()
            for surface in (
                DEFAULT_SOCIAL_RECALL_SURFACES if surfaces is None else surfaces
            )
            if str(surface or "").strip()
        }
        if not allowed_surfaces:
            return []
        placeholders = ",".join("?" for _ in allowed_surfaces)
        params: list[Any] = [
            normalized_bot_id,
            normalized_kind,
            normalized_conversation_id,
            start_at,
            end_at,
        ]
        target_clause = ""
        if user_target is not None:
            target_clause = " AND user_target=?"
            params.append(str(user_target or "").strip())
        params.extend((*sorted(allowed_surfaces), normalized_limit))
        with connect_sync(self.db_path) as conn:
            rows = conn.execute(
                f"""
                WITH ranked AS (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY message_id
                        ORDER BY created_at DESC, id DESC
                    ) AS message_rank
                    FROM qq_outbound_ledger
                    WHERE bot_id=? AND conversation_kind=? AND conversation_id=?
                      AND message_id IS NOT NULL AND message_id<>''
                )
                SELECT * FROM ranked
                WHERE message_rank=1 AND status='sent' AND recalled_at=0
                  AND created_at>=? AND created_at<=?{target_clause}
                  AND surface IN ({placeholders})
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        candidates: list[SendReceipt] = []
        seen_message_ids: set[str] = set()
        for row in rows:
            receipt = _row_to_receipt(row)
            if receipt.message_id is None or receipt.message_id in seen_message_ids:
                continue
            seen_message_ids.add(receipt.message_id)
            candidates.append(receipt)
        return candidates

    def mark_recalled(
        self,
        ledger_id: int,
        *,
        bot_id: str,
        conversation_kind: str,
        conversation_id: str,
        recalled_at: float | None = None,
    ) -> bool:
        normalized_bot_id, normalized_kind, normalized_conversation_id = _normalize_scope(
            bot_id,
            conversation_kind,
            conversation_id,
        )
        timestamp = self._timestamp(recalled_at)
        if timestamp <= 0:
            raise ValueError("recalled_at must be positive")
        with connect_sync(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE qq_outbound_ledger
                SET recalled_at=?, updated_at=?
                WHERE id=? AND bot_id=? AND conversation_kind=? AND conversation_id=?
                  AND status='sent' AND recalled_at=0
                  AND message_id IS NOT NULL AND message_id<>''
                """,
                (
                    timestamp,
                    timestamp,
                    int(ledger_id),
                    normalized_bot_id,
                    normalized_kind,
                    normalized_conversation_id,
                ),
            )
            conn.commit()
        return cursor.rowcount == 1

    def list_recent(
        self,
        *,
        bot_id: str = "",
        conversation_kind: str = "",
        conversation_id: str = "",
        status: str = "",
        recalled: bool | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Return a redacted administrator view of recent outbound receipts."""

        normalized_status = str(status or "").strip().lower()
        if normalized_status and normalized_status not in QQ_OUTBOUND_STATUSES:
            raise ValueError("invalid qq outbound status")
        normalized_kind = str(conversation_kind or "").strip().lower()
        if normalized_kind and normalized_kind not in {"group", "private"}:
            raise ValueError("conversation_kind must be group or private")
        clauses = ["1=1"]
        params: list[Any] = []
        if str(bot_id or "").strip():
            clauses.append("ledger.bot_id=?")
            params.append(str(bot_id).strip())
        if normalized_kind:
            clauses.append("ledger.conversation_kind=?")
            params.append(normalized_kind)
        if str(conversation_id or "").strip():
            clauses.append("ledger.conversation_id=?")
            params.append(str(conversation_id).strip())
        if normalized_status:
            clauses.append("ledger.status=?")
            params.append(normalized_status)
        if recalled is True:
            clauses.append("ledger.recalled_at>0")
        elif recalled is False:
            clauses.append("ledger.recalled_at=0")
        params.append(max(1, min(500, int(limit))))
        with connect_sync(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT ledger.operation_id, ledger.part_index, ledger.bot_id,
                       ledger.conversation_kind, ledger.conversation_id,
                       ledger.surface, ledger.message_id, ledger.status,
                       ledger.preview, ledger.created_at, ledger.updated_at,
                       ledger.recalled_at,
                       COALESCE(recall.status, '') AS recall_status
                FROM qq_outbound_ledger AS ledger
                LEFT JOIN qq_recall_operations AS recall
                  ON recall.outbound_operation_id=ledger.operation_id
                WHERE {' AND '.join(clauses)}
                ORDER BY ledger.created_at DESC, ledger.id DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [
            {
                "operation_id": str(row["operation_id"] or ""),
                "part_index": int(row["part_index"] or 0),
                "bot_id": str(row["bot_id"] or ""),
                "conversation_kind": str(row["conversation_kind"] or ""),
                "conversation_id": str(row["conversation_id"] or ""),
                "surface": str(row["surface"] or ""),
                "message_id": str(row["message_id"] or ""),
                "status": str(row["status"] or ""),
                "preview": str(row["preview"] or ""),
                "created_at": float(row["created_at"] or 0),
                "updated_at": float(row["updated_at"] or 0),
                "recalled_at": float(row["recalled_at"] or 0),
                "recall_status": str(row["recall_status"] or ""),
            }
            for row in rows
        ]


__all__ = [
    "DEFAULT_RECALL_WINDOW_SECONDS",
    "DEFAULT_SOCIAL_RECALL_SURFACES",
    "OutboundContext",
    "QQOutboundLedger",
    "QQ_OUTBOUND_STATUSES",
    "SendReceipt",
    "build_outbound_context",
    "parse_onebot_message_id",
    "sanitize_outbound_preview",
]
