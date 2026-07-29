from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from ..agent.tool_registry import AgentTool, ToolRegistry
from .db import connect_sync
from .message_relations import extract_reply_message_id, extract_reply_sender_id
from .protocol_adapter import ProtocolResult, get_protocol_adapter
from .qq_outbound import (
    DEFAULT_RECALL_WINDOW_SECONDS,
    DEFAULT_SOCIAL_RECALL_SURFACES,
    QQOutboundLedger,
)


_INT32_MIN = -(2**31)
_INT32_MAX = 2**31 - 1
_DECIMAL_MESSAGE_ID_RE = re.compile(r"-?\d+")
_RECALL_ITEM_STATUSES = frozenset(
    {"dispatching", "succeeded", "definite_failure", "unknown"}
)


@dataclass(frozen=True)
class RecallItem:
    ledger_id: int
    message_id: int
    part_index: int
    surface: str


@dataclass(frozen=True)
class RecallClaim:
    recall_id: int
    outbound_operation_id: str
    bot_id: str
    conversation_kind: str
    conversation_id: str
    items: tuple[RecallItem, ...]


@dataclass(frozen=True)
class QQRecallResult:
    status: str
    code: str
    outbound_operation_id: str = ""
    total_count: int = 0
    recalled_count: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "succeeded"


def normalize_recall_message_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        message_id = value
    elif isinstance(value, str):
        raw = value.strip()
        if not _DECIMAL_MESSAGE_ID_RE.fullmatch(raw):
            return None
        try:
            message_id = int(raw)
        except ValueError:
            return None
    else:
        return None
    if message_id == 0 or message_id < _INT32_MIN or message_id > _INT32_MAX:
        return None
    return message_id


def _normalize_scope(bot: Any, event: Any) -> tuple[str, str, str] | None:
    bot_id = str(getattr(bot, "self_id", "") or "").strip()
    group_id = str(getattr(event, "group_id", "") or "").strip()
    user_id = str(getattr(event, "user_id", "") or "").strip()
    message_type = str(getattr(event, "message_type", "") or "").strip().lower()
    if not bot_id or bot_id == "0":
        return None
    if group_id:
        if message_type == "private" or not group_id.isdigit() or int(group_id) <= 0:
            return None
        return bot_id, "group", group_id
    if message_type == "group" or not user_id.isdigit() or int(user_id) <= 0:
        return None
    return bot_id, "private", user_id


def _trusted_quoted_message_id(bot: Any, event: Any) -> int | None:
    reply = getattr(event, "reply", None)
    if reply is None:
        return None
    if extract_reply_sender_id(reply) != str(getattr(bot, "self_id", "") or "").strip():
        return None
    return normalize_recall_message_id(extract_reply_message_id(event))


def _safe_error_code(value: Any) -> str:
    text = re.sub(r"[^A-Za-z0-9_.:-]+", "_", str(value or "").strip())
    return text[:120]


class QQRecallService:
    def __init__(
        self,
        ledger: QQOutboundLedger,
        *,
        plugin_config: Any = None,
        logger: Any = None,
        protocol_adapter_getter: Callable[..., Any] = get_protocol_adapter,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(ledger, QQOutboundLedger):
            raise TypeError("ledger must be QQOutboundLedger")
        self.ledger = ledger
        self.plugin_config = plugin_config
        self.logger = logger
        self._protocol_adapter_getter = protocol_adapter_getter
        self._clock = clock

    def _selection_sync(
        self,
        *,
        bot_id: str,
        conversation_kind: str,
        conversation_id: str,
        requester_user_id: str,
        actor_kind: str,
        trigger_kind: str,
        cutoff: float,
        current_operation_id: str,
        preferred_message_id: int | None,
        window_seconds: float,
        surfaces: Iterable[str] | None,
        claim: bool,
        preferred_operation_id: str = "",
    ) -> RecallClaim | QQRecallResult:
        allowed_surfaces = sorted(
            {
                str(surface or "").strip()
                for surface in (
                    DEFAULT_SOCIAL_RECALL_SURFACES if surfaces is None else surfaces
                )
                if str(surface or "").strip()
            }
        )
        if not allowed_surfaces:
            return QQRecallResult("no_candidate", "surface_not_allowed")
        now = float(self._clock())
        end_at = min(float(cutoff or now), now)
        start_at = end_at - max(0.0, float(window_seconds))
        placeholders = ",".join("?" for _ in allowed_surfaces)
        where = (
            "bot_id=? AND conversation_kind=? AND conversation_id=? "
            "AND status='sent' AND message_id IS NOT NULL AND message_id<>'' "
            "AND created_at>=? AND created_at<=? AND updated_at<=? "
            f"AND surface IN ({placeholders})"
        )
        params: list[Any] = [
            bot_id,
            conversation_kind,
            conversation_id,
            start_at,
            end_at,
            end_at,
            *allowed_surfaces,
        ]
        if actor_kind != "admin":
            where += " AND user_target=?"
            params.append(requester_user_id)
        excluded_operation = str(current_operation_id or "").strip()
        if excluded_operation:
            where += " AND operation_id<>?"
            params.append(excluded_operation)
        if preferred_message_id is not None:
            where += " AND message_id=?"
            params.append(str(preferred_message_id))
        exact_operation_id = str(preferred_operation_id or "").strip()
        if exact_operation_id:
            where += " AND operation_id=?"
            params.append(exact_operation_id)

        with connect_sync(self.ledger.db_path) as conn:
            if claim:
                conn.execute("BEGIN IMMEDIATE")
            candidate = conn.execute(
                f"""
                SELECT operation_id, MAX(created_at) AS latest_at, MAX(id) AS latest_id
                FROM qq_outbound_ledger
                WHERE {where}
                GROUP BY operation_id
                ORDER BY latest_at DESC, latest_id DESC
                LIMIT 1
                """,
                tuple(params),
            ).fetchone()
            if candidate is None:
                if claim:
                    conn.commit()
                return QQRecallResult("no_candidate", "no_candidate")

            outbound_operation_id = str(candidate["operation_id"])
            existing = conn.execute(
                """
                SELECT status, total_count, recalled_count
                FROM qq_recall_operations
                WHERE outbound_operation_id=?
                """,
                (outbound_operation_id,),
            ).fetchone()
            if existing is not None:
                if claim:
                    conn.commit()
                return QQRecallResult(
                    "no_candidate",
                    "already_attempted",
                    outbound_operation_id=outbound_operation_id,
                    total_count=int(existing["total_count"]),
                    recalled_count=int(existing["recalled_count"]),
                )

            all_operation_rows = conn.execute(
                """
                SELECT bot_id, conversation_kind, conversation_id, surface,
                       status, message_id, user_target, created_at, updated_at,
                       recalled_at
                FROM qq_outbound_ledger
                WHERE operation_id=?
                """,
                (outbound_operation_id,),
            ).fetchall()
            if not all_operation_rows or any(
                str(row["bot_id"]) != bot_id
                or str(row["conversation_kind"]) != conversation_kind
                or str(row["conversation_id"]) != conversation_id
                for row in all_operation_rows
            ):
                if claim:
                    conn.commit()
                return QQRecallResult(
                    "no_candidate",
                    "operation_scope_mismatch",
                    outbound_operation_id=outbound_operation_id,
                )
            if any(
                str(row["surface"] or "").strip() not in allowed_surfaces
                for row in all_operation_rows
            ):
                if claim:
                    conn.commit()
                return QQRecallResult(
                    "no_candidate",
                    "operation_surface_not_allowed",
                    outbound_operation_id=outbound_operation_id,
                )
            if any(
                str(row["status"]) != "sent"
                or row["message_id"] is None
                or not str(row["message_id"]).strip()
                for row in all_operation_rows
            ):
                if claim:
                    conn.commit()
                return QQRecallResult(
                    "no_candidate",
                    "operation_incomplete",
                    outbound_operation_id=outbound_operation_id,
                )
            if any(float(row["recalled_at"] or 0) > 0 for row in all_operation_rows):
                if claim:
                    conn.commit()
                return QQRecallResult(
                    "no_candidate",
                    "already_recalled",
                    outbound_operation_id=outbound_operation_id,
                )
            if any(
                float(row["created_at"]) < start_at
                or float(row["created_at"]) > end_at
                or float(row["updated_at"]) > end_at
                for row in all_operation_rows
            ):
                if claim:
                    conn.commit()
                return QQRecallResult(
                    "no_candidate",
                    "operation_outside_window",
                    outbound_operation_id=outbound_operation_id,
                )
            if actor_kind != "admin" and any(
                str(row["user_target"] or "").strip() != requester_user_id
                for row in all_operation_rows
            ):
                if claim:
                    conn.commit()
                return QQRecallResult(
                    "no_candidate",
                    "operation_target_mismatch",
                    outbound_operation_id=outbound_operation_id,
                )

            operation_where = where
            operation_params = list(params)
            if preferred_message_id is not None:
                operation_where = operation_where.rsplit(" AND message_id=?", 1)[0]
                operation_params.pop()
            rows = conn.execute(
                f"""
                SELECT id, message_id, part_index, surface, recalled_at
                FROM qq_outbound_ledger
                WHERE operation_id=? AND {operation_where}
                ORDER BY part_index DESC, id DESC
                """,
                (outbound_operation_id, *operation_params),
            ).fetchall()
            if excluded_operation:
                supplemental_where = (
                    "operation_id=? AND bot_id=? AND conversation_kind=? "
                    "AND conversation_id=? AND status='sent' AND recalled_at=0 "
                    "AND message_id IS NOT NULL AND message_id<>'' AND surface='reply_ack'"
                )
                supplemental_params: list[Any] = [
                    excluded_operation,
                    bot_id,
                    conversation_kind,
                    conversation_id,
                ]
                if actor_kind != "admin":
                    supplemental_where += " AND user_target=?"
                    supplemental_params.append(requester_user_id)
                supplemental_rows = conn.execute(
                    f"""
                    SELECT id, message_id, part_index, surface, recalled_at
                    FROM qq_outbound_ledger
                    WHERE {supplemental_where}
                    ORDER BY part_index DESC, id DESC
                    """,
                    tuple(supplemental_params),
                ).fetchall()
                rows = [*supplemental_rows, *rows]
            if not rows or any(float(row["recalled_at"] or 0) > 0 for row in rows):
                if claim:
                    conn.commit()
                return QQRecallResult(
                    "no_candidate",
                    "already_recalled",
                    outbound_operation_id=outbound_operation_id,
                )

            items: list[RecallItem] = []
            for row in rows:
                message_id = normalize_recall_message_id(row["message_id"])
                if message_id is None:
                    if claim:
                        conn.commit()
                    return QQRecallResult(
                        "definite_failure",
                        "invalid_message_id",
                        outbound_operation_id=outbound_operation_id,
                        total_count=len(rows),
                    )
                items.append(
                    RecallItem(
                        ledger_id=int(row["id"]),
                        message_id=message_id,
                        part_index=int(row["part_index"]),
                        surface=str(row["surface"]),
                    )
                )

            if not claim:
                return RecallClaim(
                    recall_id=0,
                    outbound_operation_id=outbound_operation_id,
                    bot_id=bot_id,
                    conversation_kind=conversation_kind,
                    conversation_id=conversation_id,
                    items=tuple(items),
                )

            try:
                cursor = conn.execute(
                    """
                    INSERT INTO qq_recall_operations(
                        outbound_operation_id, bot_id, conversation_kind,
                        conversation_id, requester_user_id, actor_kind, trigger_kind,
                        status, total_count, recalled_count, error_code,
                        requested_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'dispatching', ?, 0, '', ?, ?)
                    """,
                    (
                        outbound_operation_id,
                        bot_id,
                        conversation_kind,
                        conversation_id,
                        requester_user_id,
                        actor_kind,
                        trigger_kind,
                        len(items),
                        now,
                        now,
                    ),
                )
                recall_id = int(cursor.lastrowid)
                conn.executemany(
                    """
                    INSERT INTO qq_recall_items(
                        recall_operation_id, ledger_id, message_id, part_index,
                        status, error_code, updated_at
                    ) VALUES (?, ?, ?, ?, 'dispatching', '', ?)
                    """,
                    [
                        (
                            recall_id,
                            item.ledger_id,
                            str(item.message_id),
                            item.part_index,
                            now,
                        )
                        for item in items
                    ],
                )
                conn.commit()
            except sqlite3.IntegrityError:
                conn.rollback()
                return QQRecallResult(
                    "no_candidate",
                    "already_attempted",
                    outbound_operation_id=outbound_operation_id,
                    total_count=len(items),
                )
        return RecallClaim(
            recall_id=recall_id,
            outbound_operation_id=outbound_operation_id,
            bot_id=bot_id,
            conversation_kind=conversation_kind,
            conversation_id=conversation_id,
            items=tuple(items),
        )

    async def preview_latest(
        self,
        *,
        bot: Any,
        event: Any,
        requester_user_id: str,
        actor_kind: str = "user",
        cutoff: float | None = None,
        current_operation_id: str = "",
        window_seconds: float = DEFAULT_RECALL_WINDOW_SECONDS,
    ) -> QQRecallResult:
        scope = _normalize_scope(bot, event)
        requester = str(requester_user_id or "").strip()
        if scope is None or (actor_kind != "admin" and not requester):
            return QQRecallResult("no_candidate", "invalid_scope")
        preferred_message_id = _trusted_quoted_message_id(bot, event)
        selected = await asyncio.to_thread(
            self._selection_sync,
            bot_id=scope[0],
            conversation_kind=scope[1],
            conversation_id=scope[2],
            requester_user_id=requester,
            actor_kind="admin" if actor_kind == "admin" else "user",
            trigger_kind="admin_command" if actor_kind == "admin" else "agent",
            cutoff=float(cutoff or self._clock()),
            current_operation_id=current_operation_id,
            preferred_message_id=preferred_message_id,
            window_seconds=window_seconds,
            surfaces=None,
            claim=False,
        )
        if isinstance(selected, RecallClaim):
            return QQRecallResult(
                "candidate",
                "candidate_available",
                outbound_operation_id=selected.outbound_operation_id,
                total_count=len(selected.items),
            )
        return selected

    @staticmethod
    def _map_protocol_result(result: ProtocolResult) -> tuple[str, str]:
        if result.status == "succeeded":
            return "succeeded", "ok"
        if result.status == "unavailable":
            return "definite_failure", result.code or "unavailable"
        if result.status == "definite_failure" and result.code in {
            "invalid_message_id",
            "action_not_found",
        }:
            return "definite_failure", result.code
        return "unknown", result.code or "outcome_unknown"

    def recover_interrupted_dispatches(self) -> int:
        """Finalize claims left dispatching by a previous process without retrying them."""
        now = float(self._clock())
        with connect_sync(self.ledger.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            operation_rows = conn.execute(
                "SELECT id FROM qq_recall_operations WHERE status='dispatching'"
            ).fetchall()
            operation_ids = [int(row["id"]) for row in operation_rows]
            if not operation_ids:
                conn.commit()
                return 0
            placeholders = ",".join("?" for _ in operation_ids)
            conn.execute(
                f"""
                UPDATE qq_recall_items
                SET status='unknown', error_code='process_interrupted', updated_at=?
                WHERE recall_operation_id IN ({placeholders}) AND status='dispatching'
                """,
                (now, *operation_ids),
            )
            conn.execute(
                f"""
                UPDATE qq_recall_operations
                SET status='unknown',
                    recalled_count=(
                        SELECT COUNT(*) FROM qq_recall_items
                        WHERE recall_operation_id=qq_recall_operations.id
                          AND status='succeeded'
                    ),
                    error_code='process_interrupted', updated_at=?
                WHERE id IN ({placeholders}) AND status='dispatching'
                """,
                (now, *operation_ids),
            )
            conn.commit()
        return len(operation_ids)

    def _finalize_item_sync(
        self,
        claim: RecallClaim,
        item: RecallItem,
        *,
        status: str,
        error_code: str,
    ) -> None:
        if status not in _RECALL_ITEM_STATUSES - {"dispatching"}:
            raise ValueError("invalid recall item status")
        now = float(self._clock())
        with connect_sync(self.ledger.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE qq_recall_items
                SET status=?, error_code=?, updated_at=?
                WHERE recall_operation_id=? AND ledger_id=? AND status='dispatching'
                """,
                (status, _safe_error_code(error_code), now, claim.recall_id, item.ledger_id),
            )
            if cursor.rowcount == 1 and status == "succeeded":
                conn.execute(
                    """
                    UPDATE qq_outbound_ledger
                    SET recalled_at=?, updated_at=?
                    WHERE id=? AND bot_id=? AND conversation_kind=? AND conversation_id=?
                      AND status='sent' AND recalled_at=0 AND message_id=?
                    """,
                    (
                        now,
                        now,
                        item.ledger_id,
                        claim.bot_id,
                        claim.conversation_kind,
                        claim.conversation_id,
                        str(item.message_id),
                    ),
                )
            conn.commit()

    def _mark_remaining_unknown_sync(self, claim: RecallClaim, *, error_code: str) -> None:
        now = float(self._clock())
        with connect_sync(self.ledger.db_path) as conn:
            conn.execute(
                """
                UPDATE qq_recall_items
                SET status='unknown', error_code=?, updated_at=?
                WHERE recall_operation_id=? AND status='dispatching'
                """,
                (_safe_error_code(error_code), now, claim.recall_id),
            )
            conn.commit()

    def _finish_operation_sync(self, claim: RecallClaim) -> QQRecallResult:
        now = float(self._clock())
        with connect_sync(self.ledger.db_path) as conn:
            rows = conn.execute(
                """
                SELECT status, error_code
                FROM qq_recall_items
                WHERE recall_operation_id=?
                ORDER BY part_index DESC, id DESC
                """,
                (claim.recall_id,),
            ).fetchall()
            statuses = [str(row["status"]) for row in rows]
            recalled_count = sum(status == "succeeded" for status in statuses)
            if not statuses or "dispatching" in statuses or "unknown" in statuses:
                final_status = "unknown"
            elif recalled_count == len(statuses):
                final_status = "succeeded"
            elif recalled_count:
                final_status = "partial"
            else:
                final_status = "definite_failure"
            error_code = next(
                (
                    str(row["error_code"])
                    for row in rows
                    if str(row["error_code"] or "").strip()
                ),
                "",
            )
            conn.execute(
                """
                UPDATE qq_recall_operations
                SET status=?, recalled_count=?, error_code=?, updated_at=?
                WHERE id=? AND status='dispatching'
                """,
                (final_status, recalled_count, error_code, now, claim.recall_id),
            )
            conn.commit()
        return QQRecallResult(
            final_status,
            error_code or ("ok" if final_status == "succeeded" else final_status),
            outbound_operation_id=claim.outbound_operation_id,
            total_count=len(statuses),
            recalled_count=recalled_count,
        )

    async def recall_latest(
        self,
        *,
        bot: Any,
        event: Any,
        requester_user_id: str,
        actor_kind: str = "user",
        cutoff: float | None = None,
        current_operation_id: str = "",
        window_seconds: float = DEFAULT_RECALL_WINDOW_SECONDS,
    ) -> QQRecallResult:
        scope = _normalize_scope(bot, event)
        requester = str(requester_user_id or "").strip()
        normalized_actor = "admin" if actor_kind == "admin" else "user"
        if scope is None or (normalized_actor != "admin" and not requester):
            return QQRecallResult("no_candidate", "invalid_scope")
        preferred_message_id = _trusted_quoted_message_id(bot, event)
        selection_task = asyncio.create_task(
            asyncio.to_thread(
                self._selection_sync,
                bot_id=scope[0],
                conversation_kind=scope[1],
                conversation_id=scope[2],
                requester_user_id=requester,
                actor_kind=normalized_actor,
                trigger_kind="admin_command" if normalized_actor == "admin" else "agent",
                cutoff=float(cutoff or self._clock()),
                current_operation_id=current_operation_id,
                preferred_message_id=preferred_message_id,
                window_seconds=window_seconds,
                surfaces=None,
                claim=True,
            )
        )
        try:
            selected = await asyncio.shield(selection_task)
        except asyncio.CancelledError:
            selected = await selection_task
            if isinstance(selected, RecallClaim):
                await asyncio.to_thread(
                    self._mark_remaining_unknown_sync,
                    selected,
                    error_code="cancelled_during_claim",
                )
                await asyncio.to_thread(self._finish_operation_sync, selected)
            raise
        if isinstance(selected, QQRecallResult):
            return selected

        return await self._execute_claim(bot=bot, claim=selected)

    async def recall_operation(
        self,
        *,
        bot: Any,
        operation_id: str,
        conversation_kind: str,
        conversation_id: str,
        requester_user_id: str,
        cutoff: float | None = None,
        window_seconds: float = DEFAULT_RECALL_WINDOW_SECONDS,
    ) -> QQRecallResult:
        """Claim and recall one exact administrator-selected operation."""

        bot_id = str(getattr(bot, "self_id", "") or "").strip()
        normalized_kind = str(conversation_kind or "").strip().lower()
        normalized_conversation_id = str(conversation_id or "").strip()
        normalized_operation_id = str(operation_id or "").strip()
        if (
            not bot_id
            or bot_id == "0"
            or normalized_kind not in {"group", "private"}
            or not normalized_conversation_id.isdigit()
            or int(normalized_conversation_id) <= 0
            or not normalized_operation_id
            or len(normalized_operation_id) > 200
            or any(ord(char) < 32 for char in normalized_operation_id)
        ):
            return QQRecallResult("no_candidate", "invalid_scope")
        selection_task = asyncio.create_task(
            asyncio.to_thread(
                self._selection_sync,
                bot_id=bot_id,
                conversation_kind=normalized_kind,
                conversation_id=normalized_conversation_id,
                requester_user_id=str(requester_user_id or "").strip(),
                actor_kind="admin",
                trigger_kind="admin_command",
                cutoff=float(cutoff or self._clock()),
                current_operation_id="",
                preferred_message_id=None,
                window_seconds=window_seconds,
                surfaces=None,
                claim=True,
                preferred_operation_id=normalized_operation_id,
            )
        )
        try:
            selected = await asyncio.shield(selection_task)
        except asyncio.CancelledError:
            selected = await selection_task
            if isinstance(selected, RecallClaim):
                await asyncio.to_thread(
                    self._mark_remaining_unknown_sync,
                    selected,
                    error_code="cancelled_during_claim",
                )
                await asyncio.to_thread(self._finish_operation_sync, selected)
            raise
        if isinstance(selected, QQRecallResult):
            return selected
        return await self._execute_claim(bot=bot, claim=selected)

    async def _execute_claim(
        self,
        *,
        bot: Any,
        claim: RecallClaim,
    ) -> QQRecallResult:

        adapter = self._protocol_adapter_getter(
            bot,
            plugin_config=self.plugin_config,
            logger=self.logger,
        )
        try:
            for item in claim.items:
                protocol_result = await adapter.recall_message(message_id=item.message_id)
                item_status, code = self._map_protocol_result(protocol_result)
                await asyncio.to_thread(
                    self._finalize_item_sync,
                    claim,
                    item,
                    status=item_status,
                    error_code=code,
                )
                if item_status == "unknown":
                    await asyncio.to_thread(
                        self._mark_remaining_unknown_sync,
                        claim,
                        error_code="not_attempted_after_unknown",
                    )
                    break
        except asyncio.CancelledError:
            cleanup_task = asyncio.create_task(
                asyncio.to_thread(
                    self._mark_remaining_unknown_sync,
                    claim,
                    error_code="cancelled",
                )
            )
            await asyncio.shield(cleanup_task)
            finish_task = asyncio.create_task(
                asyncio.to_thread(self._finish_operation_sync, claim)
            )
            await asyncio.shield(finish_task)
            raise
        except Exception as exc:
            cleanup_task = asyncio.create_task(
                asyncio.to_thread(
                    self._mark_remaining_unknown_sync,
                    claim,
                    error_code=f"internal_{type(exc).__name__}",
                )
            )
            await asyncio.shield(cleanup_task)
            return await asyncio.to_thread(self._finish_operation_sync, claim)
        result = await asyncio.to_thread(self._finish_operation_sync, claim)
        if self.logger is not None:
            self.logger.info(
                "[qq_recall] "
                f"status={result.status} code={result.code} "
                f"scope={claim.conversation_kind} total={result.total_count} "
                f"recalled={result.recalled_count}"
            )
        return result


def build_qq_recall_tool(
    *,
    executor: Any,
    bot: Any,
    event: Any,
    cutoff: float,
) -> AgentTool:
    async def _recall_latest_own_output() -> str:
        service = getattr(executor, "qq_recall_service", None)
        if service is None:
            return json.dumps(
                {"ok": False, "queued": False, "reason": "recall_unavailable"},
                ensure_ascii=False,
            )
        preview = await service.preview_latest(
            bot=bot,
            event=event,
            requester_user_id=str(getattr(event, "user_id", "") or ""),
            actor_kind="user",
            cutoff=cutoff,
            current_operation_id=str(getattr(executor, "operation_id", "") or ""),
        )
        if preview.status != "candidate":
            return json.dumps(
                {"ok": False, "queued": False, "reason": preview.code},
                ensure_ascii=False,
            )
        queue = getattr(executor, "queue_action", None)
        if not callable(queue):
            return json.dumps(
                {"ok": False, "queued": False, "reason": "commit_context_unavailable"},
                ensure_ascii=False,
            )
        queue("recall_latest_qq_operation", {})
        return json.dumps(
            {
                "ok": True,
                "queued": True,
                "kind": "message_recall",
                "final_reply_instruction": "撤回动作已进入最终提交；不要再发送文字或 ACK。",
            },
            ensure_ascii=False,
        )

    return AgentTool(
        name="recall_latest_own_output",
        description=(
            "撤回你在当前 QQ 群或私聊里刚刚发给当前用户的最近一次完整输出。"
            "仅当对方明确要求你撤回、收回或删除你刚才发出的消息时调用。"
            "工具不接受消息 ID、群号、QQ 号或正文；不要尝试撤回别人的消息或其它会话的消息。"
            "调用成功后保持沉默，不要再确认已撤回。"
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        handler=_recall_latest_own_output,
        local=True,
        metadata={
            "source_kind": "first_party_runtime",
            "intent_tags": ["conversation_action", "message_recall"],
            "evidence_kind": "action",
            "requires_network": False,
            "requires_image": False,
            "latency_class": "fast",
            "risk_level": "low",
            "side_effect": "message_recall",
            "final_behavior": "silence_on_success",
            "retryable": False,
            "ack_behavior": "suppress",
        },
        per_session_quota=1,
    )


def register_qq_recall_tool(
    registry: ToolRegistry,
    *,
    executor: Any,
    bot: Any,
    event: Any,
    cutoff: float,
) -> None:
    if getattr(executor, "qq_recall_service", None) is None:
        return
    registry.register(
        build_qq_recall_tool(
            executor=executor,
            bot=bot,
            event=event,
            cutoff=cutoff,
        )
    )


__all__ = [
    "QQRecallResult",
    "QQRecallService",
    "RecallClaim",
    "RecallItem",
    "build_qq_recall_tool",
    "normalize_recall_message_id",
    "register_qq_recall_tool",
]
