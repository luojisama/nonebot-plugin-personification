from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import Any, Callable

from .data_store import get_data_store
from .evolves import EvolvesEngine
from .memory_store import MemoryStore
from .search_ranker import safe_float


_STATE_NAMESPACE = "background_intelligence_state"


class BackgroundIntelligence:
    def __init__(
        self,
        *,
        plugin_config: Any,
        memory_store: MemoryStore,
        memory_decay_scheduler: Any,
        logger: Any = None,
        migrator_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.plugin_config = plugin_config
        self.memory_store = memory_store
        self.memory_decay_scheduler = memory_decay_scheduler
        self.logger = logger
        self.migrator_factory = migrator_factory
        self.evolves_engine = EvolvesEngine(memory_store, logger=logger)
        self._pending_tasks: dict[str, asyncio.Task[Any]] = {}
        self._task_lock = asyncio.Lock()

    def enabled(self) -> bool:
        return bool(getattr(self.plugin_config, "personification_background_intelligence_enabled", True))

    def evolves_enabled(self) -> bool:
        return self.enabled() and bool(
            getattr(self.plugin_config, "personification_background_evolves_enabled", True)
        )

    def crystals_enabled(self) -> bool:
        return self.enabled() and bool(
            getattr(self.plugin_config, "personification_background_crystals_enabled", True)
        )

    def debounce_seconds(self) -> int:
        return max(5, int(getattr(self.plugin_config, "personification_background_debounce_seconds", 90) or 90))

    def schedule_new_memory(self, *, memory_id: str, group_id: str = "") -> None:
        if not self.enabled() or not self.memory_store.palace_enabled():
            return
        payload = self.memory_store.get_memory_item(memory_id)
        if not isinstance(payload, dict):
            return
        salience = safe_float(payload.get("salience", 0), 0)
        confidence = safe_float(payload.get("confidence", 0), 0)
        if salience < 0.48 and confidence < 0.58:
            return
        group_value = str(group_id or payload.get("group_id", "") or "")
        self._schedule_debounced(
            key=f"memory:{memory_id}",
            coro_factory=lambda: self._handle_new_memory(memory_id=memory_id, group_id=group_value),
        )
        if group_value:
            self._schedule_debounced(
                key=f"crystal:{group_value}",
                coro_factory=lambda: self._run_crystal_check(group_id=group_value),
            )

    def schedule_recall_reinforcement(self, memory_ids: list[str]) -> None:
        if not self.enabled():
            return
        normalized = [str(item or "").strip() for item in memory_ids if str(item or "").strip()]
        if not normalized:
            return
        self._schedule_debounced(
            key=f"recall:{','.join(normalized[:4])}",
            coro_factory=lambda: self._reinforce_recall_hits(normalized),
        )

    async def run_periodic_maintenance(self) -> dict[str, Any]:
        if not self.enabled() or not self.memory_store.palace_enabled():
            return {"status": "disabled"}
        result: dict[str, Any] = {
            "status": "ok",
            "decayed": 0,
            "pruned_search_stats": 0,
            "crystals_updated": 0,
            "migrator_checked": False,
            "repaired": True,
        }
        try:
            if bool(getattr(self.plugin_config, "personification_memory_decay_enabled", True)):
                result["decayed"] = int(await asyncio.to_thread(self.memory_decay_scheduler.run_once))
                self._update_state(last_decay_at=time.time())
        except Exception as exc:
            self._log_warning(f"[background_intelligence] memory_decay failed: {exc}")
            result["status"] = "partial"
        try:
            result["pruned_search_stats"] = int(await asyncio.to_thread(self.memory_store.prune_search_stats_now))
        except Exception as exc:
            self._log_warning(f"[background_intelligence] search stats prune failed: {exc}")
            result["status"] = "partial"
        try:
            if self.crystals_enabled() and bool(
                getattr(self.plugin_config, "personification_memory_consolidation_enabled", True)
            ):
                crystal_info = await self._run_crystal_check()
                result["crystals_updated"] = int(crystal_info.get("updated", 0))
        except Exception as exc:
            self._log_warning(f"[background_intelligence] crystal maintenance failed: {exc}")
            result["status"] = "partial"
        try:
            await asyncio.to_thread(self.memory_store.initialize)
        except Exception as exc:
            self._log_warning(f"[background_intelligence] repair initialize failed: {exc}")
            result["status"] = "partial"
        if self.migrator_factory is not None:
            try:
                await asyncio.to_thread(self.migrator_factory().migrate_once)
                result["migrator_checked"] = True
            except Exception as exc:
                self._log_warning(f"[background_intelligence] migrator check failed: {exc}")
                result["status"] = "partial"
        self._update_state(last_periodic_at=time.time())
        return result

    async def run_evolves_for_group(self, group_id: str) -> dict[str, Any]:
        items = self.memory_store.list_recent_memories(group_id=str(group_id or ""), limit=12)
        relation_count = 0
        processed = 0
        for item in items:
            memory_id = str(item.get("memory_id", "") or "")
            if not memory_id:
                continue
            processed += 1
            decisions = await asyncio.to_thread(
                self.evolves_engine.process_memory,
                memory_id,
                group_id=str(group_id or ""),
            )
            relation_count += len(decisions)
        self._update_state(last_evolves_at=time.time())
        return {"processed": processed, "relations": relation_count}

    async def run_crystal_now(self, group_id: str = "") -> dict[str, Any]:
        return await self._run_crystal_check(group_id=str(group_id or ""))

    def get_status(self) -> dict[str, Any]:
        state = self._load_state()
        now = time.time()
        hour_key = self._hour_bucket(now)
        day_key = self._day_bucket(now)
        hourly = int(state.get("llm_tasks_per_hour", {}).get(hour_key, 0) or 0)
        daily = int(state.get("llm_tasks_per_day", {}).get(day_key, 0) or 0)
        return {
            "enabled": self.enabled(),
            "evolves_enabled": self.evolves_enabled(),
            "crystals_enabled": self.crystals_enabled(),
            "debounce_seconds": self.debounce_seconds(),
            "max_llm_tasks_per_hour": int(
                getattr(self.plugin_config, "personification_background_max_llm_tasks_per_hour", 6) or 6
            ),
            "max_llm_tasks_per_day": int(
                getattr(self.plugin_config, "personification_background_max_llm_tasks_per_day", 24) or 24
            ),
            "llm_tasks_used_this_hour": hourly,
            "llm_tasks_used_today": daily,
            "last_decay_at": float(state.get("last_decay_at", 0) or 0),
            "last_periodic_at": float(state.get("last_periodic_at", 0) or 0),
            "last_evolves_at": float(state.get("last_evolves_at", 0) or 0),
            "last_crystal_at": float(state.get("last_crystal_at", 0) or 0),
            "last_recall_reinforce_at": float(state.get("last_recall_reinforce_at", 0) or 0),
            "pending_tasks": sorted(self._pending_tasks.keys()),
        }

    async def close(self) -> None:
        async with self._task_lock:
            tasks = list(self._pending_tasks.values())
            self._pending_tasks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                continue
            except Exception:
                continue

    def _schedule_debounced(
        self,
        *,
        key: str,
        coro_factory: Callable[[], Any],
    ) -> None:
        asyncio.create_task(self._schedule_debounced_async(key=key, coro_factory=coro_factory))

    async def _schedule_debounced_async(
        self,
        *,
        key: str,
        coro_factory: Callable[[], Any],
    ) -> None:
        async def _runner() -> None:
            try:
                await asyncio.sleep(self.debounce_seconds())
                await coro_factory()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log_warning(f"[background_intelligence] task {key} failed: {exc}")
            finally:
                async with self._task_lock:
                    current = self._pending_tasks.get(key)
                    if current is asyncio.current_task():
                        self._pending_tasks.pop(key, None)

        async with self._task_lock:
            existing = self._pending_tasks.get(key)
            if existing is not None and not existing.done():
                existing.cancel()
            task = asyncio.create_task(_runner())
            self._pending_tasks[key] = task

    async def _handle_new_memory(self, *, memory_id: str, group_id: str) -> None:
        if self.evolves_enabled():
            await asyncio.to_thread(
                self.evolves_engine.process_memory,
                memory_id,
                group_id=group_id,
            )
            self._update_state(last_evolves_at=time.time())
        payload = self.memory_store.get_memory_item(memory_id)
        if not isinstance(payload, dict):
            return
        entity_tags = list(payload.get("entity_tags") or [])
        if not entity_tags and list(payload.get("topic_tags") or []):
            enriched = dict(payload)
            enriched["entity_tags"] = list(payload.get("topic_tags") or [])[:8]
            enriched["revision"] = int(enriched.get("revision", 1) or 1) + 1
            await asyncio.to_thread(self.memory_store.write_memory_item, enriched)

    async def _reinforce_recall_hits(self, memory_ids: list[str]) -> None:
        for memory_id in memory_ids[:6]:
            try:
                await asyncio.to_thread(
                    self.memory_store.report_memory_feedback,
                    memory_id=memory_id,
                    outcome="used",
                )
            except Exception as exc:
                self._log_warning(f"[background_intelligence] recall reinforce failed {memory_id}: {exc}")
        self._update_state(last_recall_reinforce_at=time.time())

    async def _run_crystal_check(self, group_id: str = "") -> dict[str, Any]:
        if not self.crystals_enabled():
            return {"updated": 0, "groups": 0}
        candidates = self.memory_store.list_recent_memories(
            group_id=str(group_id or ""),
            limit=60,
            min_confidence=0.62,
        )
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in candidates:
            if str(item.get("source_kind", "") or "") == "crystal":
                continue
            topic = self._pick_crystal_topic(item)
            if not topic:
                continue
            grouped[topic].append(item)
        updated = 0
        for topic, items in grouped.items():
            if len(items) < 3:
                continue
            if not self._crystal_candidate_is_stable(items):
                continue
            summary = self._build_crystal_summary(topic, items)
            if not summary:
                continue
            latest = max(items, key=lambda item: safe_float(item.get("updated_at", item.get("time_created", 0)), 0))
            payload = {
                "memory_id": self._crystal_id(group_id=str(group_id or ""), topic=topic),
                "memory_type": "semantic",
                "palace_zone": "topic",
                "summary": summary,
                "source_kind": "crystal",
                "source_refs": [str(item.get("memory_id", "") or "") for item in items[:8]],
                "source_memory_ids": [str(item.get("memory_id", "") or "") for item in items[:8]],
                "status": "unreviewed",
                "crystal_status": "unreviewed",
                "group_id": str(group_id or latest.get("group_id", "") or ""),
                "topic_tags": [topic],
                "entity_tags": list(latest.get("entity_tags") or [])[:8],
                "snippets": [str(item.get("summary", "") or "")[:120] for item in items[:3]],
                "supports_recall": True,
                "supports_autofill": False,
                "salience": 0.38,
                "stability": 0.52,
                "confidence": 0.46,
                "reinforcement_count": len(items),
                "time_created": time.time(),
                "group_scope": "isolated" if str(group_id or latest.get("group_id", "") or "") else "shared",
                "cross_group_allowed": False,
                "time_sensitivity": "normal",
            }
            await asyncio.to_thread(self.memory_store.write_memory_item, payload)
            updated += 1
        self._update_state(last_crystal_at=time.time())
        return {"updated": updated, "groups": len(grouped)}

    def _pick_crystal_topic(self, payload: dict[str, Any]) -> str:
        entity_tags = [str(item or "").strip() for item in payload.get("entity_tags") or [] if str(item or "").strip()]
        if entity_tags:
            return entity_tags[0][:64]
        topic_tags = [str(item or "").strip() for item in payload.get("topic_tags") or [] if str(item or "").strip()]
        if topic_tags:
            return topic_tags[0][:64]
        return ""

    def _crystal_candidate_is_stable(self, items: list[dict[str, Any]]) -> bool:
        source_kinds = {
            str(item.get("source_kind", "") or "conversation")
            for item in items
            if str(item.get("source_kind", "") or "conversation")
        }
        times = [safe_float(item.get("time_created", 0), 0) for item in items if safe_float(item.get("time_created", 0), 0) > 0]
        has_time_span = (max(times) - min(times)) >= 3600 if len(times) >= 2 else False
        challenge_count = 0
        for item in items:
            conflicts = list(item.get("conflict_refs") or [])
            if conflicts:
                challenge_count += 1
            if str(item.get("superseded_by", "") or ""):
                challenge_count += 1
        if challenge_count >= 2:
            return False
        return len(source_kinds) >= 2 or has_time_span

    def _build_crystal_summary(self, topic: str, items: list[dict[str, Any]]) -> str:
        selected = sorted(
            items,
            key=lambda item: (
                safe_float(item.get("confidence", 0), 0),
                safe_float(item.get("salience", 0), 0),
                safe_float(item.get("time_created", 0), 0),
            ),
            reverse=True,
        )[:3]
        snippets = [str(item.get("summary", "") or "").strip(" 。；;") for item in selected if str(item.get("summary", "") or "").strip()]
        if len(snippets) < 3:
            return ""
        return f"关于{topic}：{snippets[0]}；{snippets[1]}；{snippets[2]}。"

    def _crystal_id(self, *, group_id: str, topic: str) -> str:
        group_part = str(group_id or "global").strip() or "global"
        topic_part = str(topic or "topic").strip().replace(" ", "_")[:48] or "topic"
        return f"crystal:{group_part}:{topic_part}"

    def _load_state(self) -> dict[str, Any]:
        data = get_data_store().load_sync(_STATE_NAMESPACE)
        return data if isinstance(data, dict) else {}

    def _update_state(self, **patch: Any) -> dict[str, Any]:
        def _mutate(current: object) -> dict[str, Any]:
            data = current if isinstance(current, dict) else {}
            data.update(patch)
            self._trim_budget_state(data)
            return data

        updated = get_data_store().mutate_sync(_STATE_NAMESPACE, _mutate)
        return updated if isinstance(updated, dict) else {}

    def _trim_budget_state(self, state: dict[str, Any]) -> None:
        hourly = state.get("llm_tasks_per_hour")
        if not isinstance(hourly, dict):
            state["llm_tasks_per_hour"] = {}
        else:
            keep_hour_keys = set(sorted(hourly.keys())[-36:])
            state["llm_tasks_per_hour"] = {key: hourly[key] for key in keep_hour_keys}
        daily = state.get("llm_tasks_per_day")
        if not isinstance(daily, dict):
            state["llm_tasks_per_day"] = {}
        else:
            keep_day_keys = set(sorted(daily.keys())[-14:])
            state["llm_tasks_per_day"] = {key: daily[key] for key in keep_day_keys}

    def _hour_bucket(self, ts: float) -> str:
        return time.strftime("%Y%m%d%H", time.localtime(ts))

    def _day_bucket(self, ts: float) -> str:
        return time.strftime("%Y%m%d", time.localtime(ts))

    def _log_warning(self, message: str) -> None:
        log = getattr(self.logger, "warning", None)
        if callable(log):
            log(message)
