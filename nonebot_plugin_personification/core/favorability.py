from __future__ import annotations

import copy
import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Callable

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None


DEFAULT_FAVORABILITY_EVENT_DELTAS: dict[str, float] = {
    "group_good_atmosphere": 0.20,
    "user_interesting_chat": 0.12,
    "user_reply_interaction": 0.03,
    "user_perm_blacklist": -30.0,
    "user_perm_blacklist_removed": 0.0,
    "manual_adjust": 0.0,
    "daily_decay": -0.20,
}

DEFAULT_FAVORABILITY_LEVELS: dict[str, float] = {
    "初见": 0.0,
    "面熟": 10.0,
    "初识": 20.0,
    "普通": 35.0,
    "熟悉": 50.0,
    "信赖": 65.0,
    "知心": 75.0,
    "深厚": 85.0,
    "挚友": 92.0,
    "亲密": 98.0,
}

DEFAULT_FAVORABILITY_ATTITUDES: dict[str, str] = {
    "初见": "保持基本礼貌，态度温和但不过于亲热。",
    "面熟": "表现得比较客气，愿意倾听并给出简单回应。",
    "初识": "态度随和，偶尔会分享一些有趣的小事，语气活泼。",
    "普通": "像普通朋友一样轻松交流，会主动接话。",
    "熟悉": "言谈举止比较随意，经常互相调侃，表现得很开心。",
    "信赖": "非常信任对方，说话很贴心，会表达关心。",
    "知心": "默契十足，有很多共同话题，语气变得亲近。",
    "深厚": "关系非常深厚，会主动分享心情，给对方支持。",
    "挚友": "无话不谈，对对方充满热情和信任。",
    "亲密": "非常亲昵，语气温柔，充满宠溺和爱护。",
}

_STORE_NAME = "favorability_profiles"
_STORE_META_KEY = "__favorability_store_meta__"
_SCHEMA_VERSION = 3
_EXTERNAL_MIGRATION_VERSION = 1
_RECENT_EVENT_IDS_LIMIT = 256
_GROUP_BASELINE_SCORE = 35.0
_MAINTENANCE_EVENT_TYPES = frozenset({"daily_decay", "baseline_migration"})
_EXTERNAL_MIGRATION_FIELDS = (
    "favorability",
    "custom_title",
    "nickname",
    "is_perm_blacklisted",
    "blacklist_count",
    "last_update",
    "daily_fav_count",
    "last_interesting_date",
    "daily_interesting_count",
)


def get_data_store() -> Any:
    from .data_store import get_data_store as _get_data_store

    return _get_data_store()


def _safe_float(value: Any, default: float = 0.0) -> float:
    fallback = default
    try:
        fallback = float(default)
    except (TypeError, ValueError, OverflowError):
        fallback = 0.0
    if not math.isfinite(fallback):
        fallback = 0.0
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    return number if math.isfinite(number) else fallback


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _clamp_score(value: Any, default: float = 0.0) -> float:
    fallback = _safe_float(default, 0.0)
    return round(max(0.0, min(100.0, _safe_float(value, fallback))), 2)


def _configured_timezone(plugin_config: Any = None) -> Any:
    name = str(getattr(plugin_config, "personification_timezone", "Asia/Shanghai") or "Asia/Shanghai").strip()
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            try:
                return ZoneInfo("Asia/Shanghai")
            except Exception:  # pragma: no cover
                pass
    return timezone(timedelta(hours=8))


def _date_string(now: Any = None, plugin_config: Any = None) -> str:
    if now is not None and hasattr(now, "strftime"):
        try:
            if isinstance(now, datetime) and now.tzinfo is not None:
                return now.astimezone(_configured_timezone(plugin_config)).strftime("%Y-%m-%d")
            return str(now.strftime("%Y-%m-%d"))
        except Exception:
            pass
    tz = _configured_timezone(plugin_config)
    if now is not None and not isinstance(now, str):
        ts = _safe_float(now, time.time())
        return datetime.fromtimestamp(ts, tz=tz).strftime("%Y-%m-%d")
    return datetime.now(tz).strftime("%Y-%m-%d")


def _timestamp(now: Any = None) -> int:
    if now is not None and hasattr(now, "timestamp"):
        try:
            return int(now.timestamp())
        except Exception:
            pass
    if now is not None and not isinstance(now, str):
        return int(_safe_float(now, time.time()))
    return int(time.time())


def _is_group_key(user_id: str) -> bool:
    text = str(user_id or "").strip()
    return text.startswith("group_") and not text.startswith("group_private_")


def _event_log_limit(plugin_config: Any = None) -> int:
    limit = _safe_int(
        getattr(plugin_config, "personification_favorability_event_log_limit", 50),
        50,
    )
    return max(0, min(500, limit))


def default_favorability_for_id(user_id: str, plugin_config: Any = None) -> float:
    if _is_group_key(user_id):
        return _clamp_score(
            getattr(plugin_config, "personification_favorability_group_default_score", _GROUP_BASELINE_SCORE),
            _GROUP_BASELINE_SCORE,
        )
    return _clamp_score(
        getattr(plugin_config, "personification_favorability_default_score", 0.0),
        0.0,
    )


def _legacy_relationship_activity_at(profile: dict[str, Any]) -> int:
    if "last_relationship_activity_at" in profile:
        return max(0, _safe_int(profile.get("last_relationship_activity_at", 0), 0))
    events = profile.get("favorability_events")
    relationship_times: list[int] = []
    maintenance_only = False
    if isinstance(events, list):
        maintenance_only = bool(events)
        for item in events:
            if not isinstance(item, dict):
                continue
            event_type = str(item.get("type", "") or "").strip()
            if event_type in _MAINTENANCE_EVENT_TYPES:
                continue
            maintenance_only = False
            relationship_times.append(max(0, _safe_int(item.get("timestamp", 0), 0)))
    if relationship_times:
        return max(relationship_times)
    if maintenance_only:
        return max(0, _safe_int(profile.get("created_at", 0), 0))
    return max(
        0,
        _safe_int(profile.get("last_favorability_event_at", 0), 0),
        _safe_int(profile.get("updated_at", 0), 0),
        _safe_int(profile.get("created_at", 0), 0),
    )


def normalize_favorability_profile(
    user_id: str,
    value: Any,
    *,
    plugin_config: Any = None,
    now_ts: int | None = None,
) -> dict[str, Any]:
    profile = dict(value) if isinstance(value, dict) else {}
    normalized_now = _timestamp(now_ts)
    relationship_activity_at = _legacy_relationship_activity_at(profile)
    default_score = default_favorability_for_id(user_id, plugin_config)
    profile["favorability"] = _clamp_score(profile.get("favorability", default_score), default_score)

    for key in (
        "daily_fav_count",
        "daily_interesting_count",
        "daily_positive_count",
        "daily_negative_count",
    ):
        profile[key] = round(max(0.0, _safe_float(profile.get(key, 0.0), 0.0)), 2)
    for key in (
        "last_update",
        "last_interesting_date",
        "daily_positive_date",
        "daily_negative_date",
        "last_favorability_event_date",
        "last_favorability_decay_date",
        "custom_title",
        "nickname",
    ):
        profile[key] = str(profile.get(key, "") or "").strip()
    for key in ("blacklist_count",):
        profile[key] = max(0, _safe_int(profile.get(key, 0) or 0, 0))
    for key in (
        "last_favorability_event_at",
        "last_favorability_decay_at",
        "external_migration_at",
    ):
        profile[key] = max(0, _safe_int(profile.get(key, 0) or 0, 0))
    events_raw = profile.get("favorability_events")
    events: list[dict[str, Any]] = []
    if isinstance(events_raw, list):
        for item in events_raw:
            if not isinstance(item, dict):
                continue
            event = dict(item)
            event["type"] = str(event.get("type", "") or "").strip()
            event["date"] = str(event.get("date", "") or "").strip()
            event["delta"] = round(_safe_float(event.get("delta", 0.0), 0.0), 2)
            event["requested_delta"] = round(
                _safe_float(event.get("requested_delta", event["delta"]), event["delta"]),
                2,
            )
            event["old"] = _clamp_score(event.get("old", 0.0), 0.0)
            event["new"] = _clamp_score(event.get("new", event["old"]), event["old"])
            event["timestamp"] = max(0, _safe_int(event.get("timestamp", 0) or 0, 0))
            event["capped"] = bool(event.get("capped", False))
            for text_key in ("reason", "actor", "group_id", "status", "event_id"):
                if text_key in event:
                    event[text_key] = str(event.get(text_key, "") or "").strip()
            if isinstance(event.get("metadata"), dict):
                event["metadata"] = copy.deepcopy(event["metadata"])
            events.append(event)
    limit = _event_log_limit(plugin_config)
    profile["favorability_events"] = events[-limit:] if limit else []
    recent_ids_raw = profile.get("recent_event_ids")
    recent_ids: list[str] = []
    if isinstance(recent_ids_raw, list):
        for item in recent_ids_raw:
            event_id = str(item or "").strip()[:128]
            if event_id and event_id not in recent_ids:
                recent_ids.append(event_id)
    profile["recent_event_ids"] = recent_ids[-_RECENT_EVENT_IDS_LIMIT:]
    profile["is_perm_blacklisted"] = bool(profile.get("is_perm_blacklisted", False))
    profile["created_at"] = max(
        0,
        _safe_int(profile.get("created_at", normalized_now), normalized_now),
    )
    profile["updated_at"] = max(
        0,
        _safe_int(profile.get("updated_at", normalized_now), normalized_now),
    )
    profile["revision"] = max(0, _safe_int(profile.get("revision", 0), 0))
    profile["last_relationship_activity_at"] = relationship_activity_at or profile["created_at"]
    profile["external_migration_version"] = max(
        0,
        _safe_int(profile.get("external_migration_version", 0), 0),
    )
    profile["schema_version"] = _SCHEMA_VERSION
    return profile


def normalize_favorability_event_deltas(value: Any) -> dict[str, float]:
    raw = value if isinstance(value, dict) else {}
    deltas: dict[str, float] = {}
    for event_type, default_delta in DEFAULT_FAVORABILITY_EVENT_DELTAS.items():
        deltas[event_type] = round(_safe_float(raw.get(event_type, default_delta), default_delta), 2)
    for event_type, raw_delta in raw.items():
        key = str(event_type or "").strip()
        if key and key not in deltas:
            deltas[key] = round(_safe_float(raw_delta, 0.0), 2)
    return deltas


def normalize_favorability_levels(value: Any) -> dict[str, float]:
    raw = value if isinstance(value, dict) and value else DEFAULT_FAVORABILITY_LEVELS
    levels: dict[str, float] = {}
    for name, threshold in raw.items():
        label = str(name or "").strip()
        if not label:
            continue
        levels[label] = _clamp_score(threshold, 0.0)
    if not levels:
        levels = dict(DEFAULT_FAVORABILITY_LEVELS)
    return dict(sorted(levels.items(), key=lambda item: item[1]))


def level_name_for_score(score: Any, levels: Any = None) -> str:
    value = _clamp_score(score, 0.0)
    resolved = normalize_favorability_levels(levels)
    selected = next(iter(resolved), "普通")
    for name, threshold in resolved.items():
        if value >= threshold:
            selected = name
        else:
            break
    return selected or "普通"


@dataclass
class ExternalFavorabilityAdapter:
    available: bool
    get_user_data: Callable[[str], dict[str, Any]]
    update_user_data: Callable[..., None]
    load_data: Callable[[], dict[str, dict[str, Any]]]
    get_level_name: Callable[[float], str]


_UNSET = object()


class FavorabilityService:
    """Plugin-owned favorability store with one-time compatibility migration.

    The old sign-in plugin remains a best-effort import source, but the
    personification plugin writes and reads its own profile document.
    """

    def __init__(
        self,
        *,
        plugin_config: Any = None,
        external: ExternalFavorabilityAdapter | None = None,
        logger: Any = None,
    ) -> None:
        self.plugin_config = plugin_config
        self.external = external or ExternalFavorabilityAdapter(
            available=False,
            get_user_data=lambda _uid: {},
            update_user_data=lambda *_a, **_k: None,
            load_data=lambda: {},
            get_level_name=lambda value: level_name_for_score(value),
        )
        self.logger = logger
        self._mirror_lock = RLock()

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.plugin_config, "personification_favorability_enabled", True))

    @property
    def external_available(self) -> bool:
        return bool(self.external.available)

    def current_date(self, now: Any = None) -> str:
        return _date_string(now, self.plugin_config)

    def default_score(self, user_id: str) -> float:
        return default_favorability_for_id(user_id, self.plugin_config)

    def _load_document(self) -> dict[str, Any]:
        raw = get_data_store().load_sync(_STORE_NAME)
        return dict(raw) if isinstance(raw, dict) else {}

    @staticmethod
    def _store_meta(document: dict[str, Any]) -> dict[str, Any]:
        raw = document.get(_STORE_META_KEY)
        return dict(raw) if isinstance(raw, dict) else {}

    def _log_external_failure(self, message: str) -> None:
        if self.logger is None:
            return
        try:
            self.logger.debug(message)
        except Exception:
            pass

    def _external_user_data(self, user_id: str) -> tuple[bool, dict[str, Any]]:
        if not self.external.available:
            return False, {}
        try:
            raw = self.external.load_data() or {}
        except Exception as exc:
            self._log_external_failure(f"拟人插件：读取外部签到好感度失败 user={user_id}: {exc}")
            return False, {}
        if not isinstance(raw, dict):
            return True, {}
        data = raw.get(str(user_id))
        return True, dict(data) if isinstance(data, dict) else {}

    def _external_all_data(self) -> tuple[bool, dict[str, dict[str, Any]]]:
        if not self.external.available:
            return False, {}
        try:
            raw = self.external.load_data() or {}
        except Exception as exc:
            self._log_external_failure(f"拟人插件：批量读取外部签到好感度失败: {exc}")
            return False, {}
        if not isinstance(raw, dict):
            return True, {}
        profiles: dict[str, dict[str, Any]] = {}
        for user_id, profile in raw.items():
            key = str(user_id or "").strip()
            if key and key != _STORE_META_KEY and isinstance(profile, dict):
                profiles[key] = dict(profile)
        return True, profiles

    def _external_candidate(
        self,
        user_id: str,
        document: dict[str, Any],
        *,
        now_ts: int,
    ) -> tuple[bool, dict[str, Any], int]:
        if not self.external.available:
            return False, {}, 0
        raw_profile = document.get(user_id)
        if isinstance(raw_profile, dict) and _safe_int(
            raw_profile.get("external_migration_version", 0),
            0,
        ) >= _EXTERNAL_MIGRATION_VERSION:
            return False, {}, 0
        meta = self._store_meta(document)
        if _safe_int(meta.get("external_load_migration_version", 0), 0) >= _EXTERNAL_MIGRATION_VERSION:
            migration_at = max(0, _safe_int(meta.get("external_load_migration_at", now_ts), now_ts))
            return True, {}, migration_at
        succeeded, external = self._external_user_data(user_id)
        return succeeded, external, now_ts if succeeded else 0

    def _merge_external_fields(self, local: dict[str, Any], external: dict[str, Any]) -> dict[str, Any]:
        if not external:
            return dict(local)
        merged = dict(local)
        for key in _EXTERNAL_MIGRATION_FIELDS:
            if key not in merged and key in external:
                merged[key] = copy.deepcopy(external[key])
        return merged

    @staticmethod
    def _should_migrate_group_baseline(user_id: str, profile: Any) -> bool:
        if not _is_group_key(user_id) or not isinstance(profile, dict):
            return False
        if _safe_int(profile.get("schema_version", 0), 0) >= _SCHEMA_VERSION:
            return False
        score = _finite_float(profile.get("favorability"))
        if score is None or score != 100.0:
            return False
        source = str(profile.get("source", "") or "").strip()
        # 旧版 bulk external migration 可能没有写 source；来源缺失的档案
        # 无法证明是插件自动默认值，必须保守保留。
        if source != "personification":
            return False
        if bool(profile.get("is_perm_blacklisted", False)):
            return False
        if _safe_int(profile.get("blacklist_count", 0), 0) > 0:
            return False
        if "favorability_events" not in profile:
            return True
        events = profile.get("favorability_events")
        if not isinstance(events, list):
            return False
        for event in events:
            if not isinstance(event, dict):
                return False
            event_type = str(event.get("type", "") or "").strip()
            if event_type == "manual_adjust" or event_type.startswith("user_perm_blacklist"):
                return False
            delta = _finite_float(event.get("delta", 0.0))
            status = str(event.get("status", "") or "").strip()
            if delta is None or delta != 0.0 or status != "clamped":
                return False
        return True

    def _prepare_profile(
        self,
        user_id: str,
        raw_profile: Any,
        *,
        now_ts: int,
        now_date: str,
        store_meta: dict[str, Any],
        external_attempted: bool = False,
        external_data: dict[str, Any] | None = None,
        external_migration_at: int = 0,
    ) -> dict[str, Any]:
        existed = isinstance(raw_profile, dict)
        raw = dict(raw_profile) if existed else {}
        already_migrated = _safe_int(raw.get("external_migration_version", 0), 0)
        if external_attempted and already_migrated < _EXTERNAL_MIGRATION_VERSION:
            incoming = dict(external_data or {})
            if incoming:
                if existed:
                    raw = self._merge_external_fields(raw, incoming)
                    raw.setdefault("source", "external_sign_in")
                else:
                    raw = copy.deepcopy(incoming)
                    raw["source"] = "external_sign_in"
            raw["external_migration_version"] = _EXTERNAL_MIGRATION_VERSION
            raw["external_migration_at"] = max(0, external_migration_at or now_ts)
        elif _safe_int(
            store_meta.get("external_load_migration_version", 0),
            0,
        ) >= _EXTERNAL_MIGRATION_VERSION and already_migrated < _EXTERNAL_MIGRATION_VERSION:
            raw["external_migration_version"] = _EXTERNAL_MIGRATION_VERSION
            raw["external_migration_at"] = max(
                0,
                _safe_int(store_meta.get("external_load_migration_at", now_ts), now_ts),
            )

        if not existed:
            raw.setdefault("favorability", default_favorability_for_id(user_id, self.plugin_config))
            raw.setdefault("source", "personification")
            raw.setdefault("created_at", now_ts)

        migrate_baseline = self._should_migrate_group_baseline(user_id, raw)
        profile = normalize_favorability_profile(
            user_id,
            raw,
            plugin_config=self.plugin_config,
            now_ts=now_ts,
        )
        if migrate_baseline:
            old_score = _clamp_score(profile.get("favorability", 100.0), 100.0)
            target_score = default_favorability_for_id(user_id, self.plugin_config)
            profile["favorability"] = target_score
            self._append_event(
                profile,
                event_type="baseline_migration",
                requested_delta=round(target_score - old_score, 2),
                applied_delta=round(target_score - old_score, 2),
                old_score=old_score,
                new_score=target_score,
                now_ts=now_ts,
                now_date=now_date,
                reason=f"旧版自动群默认值从 100 迁移到 {target_score:g}",
                actor="personification_migration",
                group_id=user_id.removeprefix("group_"),
                status="applied",
                capped=False,
                metadata={"from_default": 100.0, "to_default": target_score},
            )
            profile["last_favorability_event_at"] = now_ts
            profile["last_favorability_event_date"] = now_date
            profile = normalize_favorability_profile(
                user_id,
                profile,
                plugin_config=self.plugin_config,
                now_ts=now_ts,
            )
        return profile

    @staticmethod
    def _commit_profile(profile: dict[str, Any], previous: Any, *, now_ts: int) -> dict[str, Any]:
        old_revision = _safe_int(previous.get("revision", 0), 0) if isinstance(previous, dict) else 0
        profile["revision"] = max(0, old_revision) + 1
        profile["updated_at"] = now_ts
        profile["schema_version"] = _SCHEMA_VERSION
        return profile

    def _snapshot_from_document(self, document: dict[str, Any]) -> dict[str, dict[str, Any]]:
        now_ts = int(time.time())
        profiles: dict[str, dict[str, Any]] = {}
        for raw_key, raw_profile in document.items():
            key = str(raw_key or "").strip()
            if not key or key == _STORE_META_KEY or not isinstance(raw_profile, dict):
                continue
            profiles[key] = normalize_favorability_profile(
                key,
                raw_profile,
                plugin_config=self.plugin_config,
                now_ts=now_ts,
            )
        return profiles

    def peek_user_data(self, user_id: str) -> dict[str, Any] | None:
        """Return an in-memory normalized profile without creating or migrating it."""

        key = str(user_id or "").strip()
        if not key or key == _STORE_META_KEY:
            return None
        raw_profile = self._load_document().get(key)
        if not isinstance(raw_profile, dict):
            return None
        return copy.deepcopy(
            normalize_favorability_profile(
                key,
                raw_profile,
                plugin_config=self.plugin_config,
            )
        )

    def snapshot_profiles(self) -> dict[str, dict[str, Any]]:
        """Return all local profiles without external reads or persistence side effects."""

        return copy.deepcopy(self._snapshot_from_document(self._load_document()))

    def get_user_data(self, user_id: str) -> dict[str, Any]:
        key = str(user_id or "").strip()
        if not key or key == _STORE_META_KEY:
            return {}
        now_ts = int(time.time())
        now_date = self.current_date(now_ts)
        document = self._load_document()
        external_attempted, external_data, external_at = self._external_candidate(
            key,
            document,
            now_ts=now_ts,
        )
        raw_profile = document.get(key)
        preview = self._prepare_profile(
            key,
            raw_profile,
            now_ts=now_ts,
            now_date=now_date,
            store_meta=self._store_meta(document),
            external_attempted=external_attempted,
            external_data=external_data,
            external_migration_at=external_at,
        )
        if isinstance(raw_profile, dict) and preview == raw_profile:
            return copy.deepcopy(preview)

        result: dict[str, Any] = {}
        changed = False

        def _mutate(current: Any) -> dict[str, Any]:
            nonlocal result, changed
            data = dict(current) if isinstance(current, dict) else {}
            latest = data.get(key)
            profile = self._prepare_profile(
                key,
                latest,
                now_ts=now_ts,
                now_date=now_date,
                store_meta=self._store_meta(data),
                external_attempted=external_attempted,
                external_data=external_data,
                external_migration_at=external_at,
            )
            if not isinstance(latest, dict) or profile != latest:
                profile = self._commit_profile(profile, latest, now_ts=now_ts)
                data[key] = profile
                changed = True
            result = copy.deepcopy(profile)
            return data

        get_data_store().mutate_sync(_STORE_NAME, _mutate)
        if changed:
            self._mirror_latest(key)
        return result

    def _event_delta(self, event_type: str, delta: float | None = None) -> float:
        if delta is not None:
            return round(_safe_float(delta, 0.0), 2)
        configured = normalize_favorability_event_deltas(
            getattr(self.plugin_config, "personification_favorability_event_deltas", None)
        )
        return round(_safe_float(configured.get(str(event_type), 0.0), 0.0), 2)

    def _daily_cap_for_event(
        self,
        *,
        user_id: str,
        event_type: str,
        delta: float,
        daily_cap: float | None,
    ) -> float | None:
        if daily_cap is not None:
            configured_cap = _safe_float(daily_cap, 0.0)
            if configured_cap < 0:
                return None
            return max(0.0, configured_cap)
        if delta > 0:
            if _is_group_key(user_id) or str(event_type).startswith("group_"):
                return max(
                    0.0,
                    _safe_float(
                        getattr(self.plugin_config, "personification_favorability_group_daily_positive_cap", 10.0),
                        10.0,
                    ),
                )
            return max(
                0.0,
                _safe_float(
                    getattr(self.plugin_config, "personification_favorability_daily_positive_cap", 5.0),
                    5.0,
                ),
            )
        if delta < 0:
            return max(
                0.0,
                _safe_float(
                    getattr(self.plugin_config, "personification_favorability_daily_negative_cap", 30.0),
                    30.0,
                ),
            )
        return None

    def _apply_daily_cap(
        self,
        profile: dict[str, Any],
        *,
        user_id: str,
        event_type: str,
        delta: float,
        now_date: str,
        daily_cap: float | None,
    ) -> tuple[float, bool, str, float, float]:
        if delta == 0:
            return 0.0, False, "", 0.0, 0.0
        cap = self._daily_cap_for_event(
            user_id=user_id,
            event_type=event_type,
            delta=delta,
            daily_cap=daily_cap,
        )
        if cap is None:
            return delta, False, "", 0.0, 0.0
        bucket = "positive" if delta > 0 else "negative"
        date_key = f"daily_{bucket}_date"
        count_key = f"daily_{bucket}_count"
        if str(profile.get(date_key, "") or "") != now_date:
            profile[date_key] = now_date
            profile[count_key] = 0.0
        used = max(0.0, _safe_float(profile.get(count_key, 0.0), 0.0))
        remaining = max(0.0, cap - used)
        magnitude = abs(delta)
        applied_magnitude = min(magnitude, remaining)
        capped = applied_magnitude < magnitude
        applied = applied_magnitude if delta > 0 else -applied_magnitude
        return round(applied, 2), capped, bucket, round(used, 2), round(cap, 2)

    def _append_event(
        self,
        profile: dict[str, Any],
        *,
        event_type: str,
        requested_delta: float,
        applied_delta: float,
        old_score: float,
        new_score: float,
        now_ts: int,
        now_date: str,
        reason: str,
        actor: str,
        group_id: str,
        status: str,
        capped: bool,
        metadata: dict[str, Any] | None,
        event_id: str = "",
    ) -> None:
        event: dict[str, Any] = {
            "type": str(event_type or "").strip(),
            "delta": round(applied_delta, 2),
            "requested_delta": round(requested_delta, 2),
            "old": round(old_score, 2),
            "new": round(new_score, 2),
            "timestamp": now_ts,
            "date": now_date,
            "status": status,
            "capped": bool(capped),
        }
        if event_id:
            event["event_id"] = str(event_id)[:128]
        if reason:
            event["reason"] = str(reason)[:200]
        if actor:
            event["actor"] = str(actor)[:64]
        if group_id:
            event["group_id"] = str(group_id)[:64]
        if metadata:
            event["metadata"] = copy.deepcopy(metadata)
        events_raw = profile.get("favorability_events")
        events = list(events_raw) if isinstance(events_raw, list) else []
        events.append(event)
        limit = _event_log_limit(self.plugin_config)
        profile["favorability_events"] = events[-limit:] if limit else []

    def _apply_event_to_profile(
        self,
        profile: dict[str, Any],
        *,
        user_id: str,
        event_name: str,
        delta: float | None,
        reason: str,
        actor: str,
        group_id: str,
        now_ts: int,
        now_date: str,
        daily_cap: float | None,
        patch: dict[str, Any] | None,
        metadata: dict[str, Any] | None,
        event_id: str,
        target_score: Any = _UNSET,
        blacklist_state: bool | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        old_score = _clamp_score(
            profile.get("favorability", default_favorability_for_id(user_id, self.plugin_config)),
            default_favorability_for_id(user_id, self.plugin_config),
        )
        patch_data = dict(patch or {})
        if target_score is not _UNSET:
            target = _clamp_score(target_score, old_score)
            requested_delta = round(target - old_score, 2)
            daily_cap = -1.0
        elif blacklist_state is not None:
            already = bool(profile.get("is_perm_blacklisted", False))
            blacklist_count = max(0, _safe_int(profile.get("blacklist_count", 0), 0))
            if blacklist_state:
                requested_delta = 0.0 if already else self._event_delta(event_name, delta)
                patch_data.update(
                    {
                        "is_perm_blacklisted": True,
                        "blacklist_count": blacklist_count if already else blacklist_count + 1,
                    }
                )
            else:
                requested_delta = self._event_delta(event_name, delta)
                patch_data["is_perm_blacklisted"] = False
        else:
            requested_delta = self._event_delta(event_name, delta)

        normalized_event_id = str(event_id or "").strip()[:128]
        recent_ids = list(profile.get("recent_event_ids") or [])
        if normalized_event_id and normalized_event_id in recent_ids:
            return profile, {
                "applied": False,
                "status": "duplicate",
                "old": old_score,
                "new": old_score,
                "delta": 0.0,
                "requested_delta": requested_delta,
                "capped": False,
                "daily_used": 0.0,
                "daily_cap": 0.0,
                "event_type": event_name,
                "event_id": normalized_event_id,
            }, False

        if (
            event_name == "group_good_atmosphere"
            and str(profile.get("daily_positive_date", "") or "") != now_date
            and str(profile.get("last_update", "") or "") == now_date
        ):
            profile["daily_positive_date"] = now_date
            profile["daily_positive_count"] = profile.get("daily_fav_count", 0.0)
        elif (
            event_name == "user_interesting_chat"
            and str(profile.get("daily_positive_date", "") or "") != now_date
            and str(profile.get("last_interesting_date", "") or "") == now_date
        ):
            profile["daily_positive_date"] = now_date
            profile["daily_positive_count"] = profile.get("daily_interesting_count", 0.0)

        applied_delta, capped, bucket, used_before, cap = self._apply_daily_cap(
            profile,
            user_id=user_id,
            event_type=event_name,
            delta=requested_delta,
            now_date=now_date,
            daily_cap=daily_cap,
        )
        new_score = _clamp_score(old_score + applied_delta, old_score)
        applied_delta = round(new_score - old_score, 2)
        status = "applied"
        if requested_delta != 0 and applied_delta == 0:
            status = "capped" if capped else "clamped"
        if bucket and applied_delta:
            count_key = f"daily_{bucket}_count"
            profile[count_key] = round(max(0.0, used_before + abs(applied_delta)), 2)
            profile[f"daily_{bucket}_date"] = now_date

        if event_name == "group_good_atmosphere" and applied_delta > 0:
            legacy_daily = (
                0.0
                if str(profile.get("last_update", "") or "") != now_date
                else _safe_float(profile.get("daily_fav_count", 0.0), 0.0)
            )
            profile["last_update"] = now_date
            profile["daily_fav_count"] = round(max(0.0, legacy_daily) + applied_delta, 2)
        elif event_name == "user_interesting_chat" and applied_delta > 0:
            legacy_daily = (
                0.0
                if str(profile.get("last_interesting_date", "") or "") != now_date
                else _safe_float(profile.get("daily_interesting_count", 0.0), 0.0)
            )
            profile["last_interesting_date"] = now_date
            profile["daily_interesting_count"] = round(max(0.0, legacy_daily) + applied_delta, 2)

        profile.update(patch_data)
        profile["favorability"] = new_score
        profile["last_favorability_event_at"] = now_ts
        profile["last_favorability_event_date"] = now_date
        if event_name not in _MAINTENANCE_EVENT_TYPES:
            profile["last_relationship_activity_at"] = now_ts
        if normalized_event_id:
            recent_ids.append(normalized_event_id)
            profile["recent_event_ids"] = recent_ids[-_RECENT_EVENT_IDS_LIMIT:]
        self._append_event(
            profile,
            event_type=event_name,
            requested_delta=requested_delta,
            applied_delta=applied_delta,
            old_score=old_score,
            new_score=new_score,
            now_ts=now_ts,
            now_date=now_date,
            reason=reason,
            actor=actor,
            group_id=group_id,
            status=status,
            capped=capped,
            metadata=metadata,
            event_id=normalized_event_id,
        )
        profile = normalize_favorability_profile(
            user_id,
            profile,
            plugin_config=self.plugin_config,
            now_ts=now_ts,
        )
        return profile, {
            "applied": bool(applied_delta or patch_data),
            "status": status,
            "old": old_score,
            "new": new_score,
            "delta": applied_delta,
            "requested_delta": requested_delta,
            "capped": capped,
            "daily_used": round(used_before + abs(applied_delta), 2) if bucket else 0.0,
            "daily_cap": cap,
            "event_type": event_name,
            "event_id": normalized_event_id,
        }, True

    def _apply_event_atomic(
        self,
        user_id: str,
        event_name: str,
        *,
        delta: float | None = None,
        reason: str = "",
        actor: str = "",
        group_id: str = "",
        now: Any = None,
        daily_cap: float | None = None,
        patch: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        event_id: str = "",
        target_score: Any = _UNSET,
        blacklist_state: bool | None = None,
    ) -> dict[str, Any]:
        if not user_id or not event_name or not self.enabled:
            return {
                "applied": False,
                "status": "disabled" if not self.enabled else "invalid",
                "old": 0.0,
                "new": 0.0,
                "delta": 0.0,
                "requested_delta": 0.0,
                "capped": False,
            }
        now_ts = _timestamp(now)
        now_date = self.current_date(now)
        document = self._load_document()
        external_attempted, external_data, external_at = self._external_candidate(
            user_id,
            document,
            now_ts=now_ts,
        )
        result: dict[str, Any] = {}
        changed = False

        def _mutate(current: Any) -> dict[str, Any]:
            nonlocal result, changed
            data = dict(current) if isinstance(current, dict) else {}
            latest = data.get(user_id)
            profile = self._prepare_profile(
                user_id,
                latest,
                now_ts=now_ts,
                now_date=now_date,
                store_meta=self._store_meta(data),
                external_attempted=external_attempted,
                external_data=external_data,
                external_migration_at=external_at,
            )
            prepared_changed = not isinstance(latest, dict) or profile != latest
            profile, result, event_changed = self._apply_event_to_profile(
                profile,
                user_id=user_id,
                event_name=event_name,
                delta=delta,
                reason=reason,
                actor=actor,
                group_id=group_id,
                now_ts=now_ts,
                now_date=now_date,
                daily_cap=daily_cap,
                patch=patch,
                metadata=metadata,
                event_id=event_id,
                target_score=target_score,
                blacklist_state=blacklist_state,
            )
            if prepared_changed or event_changed:
                profile = self._commit_profile(profile, latest, now_ts=now_ts)
                data[user_id] = profile
                changed = True
            return data

        get_data_store().mutate_sync(_STORE_NAME, _mutate)
        if changed:
            self._mirror_latest(user_id)
        return result

    def apply_event(
        self,
        user_id: str,
        event_type: str,
        *,
        delta: float | None = None,
        reason: str = "",
        actor: str = "",
        group_id: str = "",
        now: Any = None,
        daily_cap: float | None = None,
        patch: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        event_id: str = "",
    ) -> dict[str, Any]:
        key = str(user_id or "").strip()
        event_name = str(event_type or "").strip()
        return self._apply_event_atomic(
            key,
            event_name,
            delta=delta,
            reason=reason,
            actor=actor,
            group_id=group_id,
            now=now,
            daily_cap=daily_cap,
            patch=patch,
            metadata=metadata,
            event_id=event_id,
        )

    def update_user_data(self, user_id: str, **patch: Any) -> None:
        key = str(user_id or "").strip()
        if not key or key == _STORE_META_KEY:
            return
        now_ts = int(time.time())
        now_date = self.current_date(now_ts)
        document = self._load_document()
        external_attempted, external_data, external_at = self._external_candidate(
            key,
            document,
            now_ts=now_ts,
        )

        def _mutate(current: Any) -> dict[str, Any]:
            data = dict(current) if isinstance(current, dict) else {}
            latest = data.get(key)
            profile = self._prepare_profile(
                key,
                latest,
                now_ts=now_ts,
                now_date=now_date,
                store_meta=self._store_meta(data),
                external_attempted=external_attempted,
                external_data=external_data,
                external_migration_at=external_at,
            )
            patch_data = dict(patch or {})
            if "favorability" in patch_data:
                old_score = _clamp_score(
                    profile.get("favorability", default_favorability_for_id(key, self.plugin_config)),
                    default_favorability_for_id(key, self.plugin_config),
                )
                patch_data["favorability"] = _clamp_score(patch_data["favorability"], old_score)
            profile.update(patch_data)
            profile["last_relationship_activity_at"] = now_ts
            profile = normalize_favorability_profile(
                key,
                profile,
                plugin_config=self.plugin_config,
                now_ts=now_ts,
            )
            data[key] = self._commit_profile(profile, latest, now_ts=now_ts)
            return data

        get_data_store().mutate_sync(_STORE_NAME, _mutate)
        self._mirror_latest(key)

    def load_data(self) -> dict[str, dict[str, Any]]:
        now_ts = int(time.time())
        now_date = self.current_date(now_ts)
        document = self._load_document()
        meta = self._store_meta(document)
        bulk_attempted = False
        external_profiles: dict[str, dict[str, Any]] = {}
        if self.external.available and _safe_int(
            meta.get("external_load_migration_version", 0),
            0,
        ) < _EXTERNAL_MIGRATION_VERSION:
            bulk_attempted, external_profiles = self._external_all_data()

        local_needs_migration = False
        for raw_key, raw_profile in document.items():
            key = str(raw_key or "").strip()
            if raw_key == _STORE_META_KEY:
                continue
            if not key or not isinstance(raw_profile, dict):
                local_needs_migration = True
                break
            prepared = self._prepare_profile(
                key,
                raw_profile,
                now_ts=now_ts,
                now_date=now_date,
                store_meta=meta,
            )
            if prepared != raw_profile:
                local_needs_migration = True
                break
        if not bulk_attempted and not local_needs_migration:
            return copy.deepcopy(self._snapshot_from_document(document))

        result: dict[str, dict[str, Any]] = {}

        def _mutate(current: Any) -> dict[str, Any]:
            nonlocal result
            data = dict(current) if isinstance(current, dict) else {}
            current_meta = self._store_meta(data)
            profile_keys = {
                str(raw_key or "").strip()
                for raw_key in data
                if raw_key != _STORE_META_KEY and str(raw_key or "").strip()
            }
            if bulk_attempted:
                profile_keys.update(external_profiles)
            normalized: dict[str, dict[str, Any]] = {}
            for key in profile_keys:
                latest = data.get(key)
                if latest is not None and not isinstance(latest, dict):
                    latest = None
                attempted_for_profile = bulk_attempted and (
                    not isinstance(latest, dict)
                    or _safe_int(latest.get("external_migration_version", 0), 0)
                    < _EXTERNAL_MIGRATION_VERSION
                )
                profile = self._prepare_profile(
                    key,
                    latest,
                    now_ts=now_ts,
                    now_date=now_date,
                    store_meta=current_meta,
                    external_attempted=attempted_for_profile,
                    external_data=external_profiles.get(key, {}),
                    external_migration_at=now_ts if attempted_for_profile else 0,
                )
                if not isinstance(latest, dict) or profile != latest:
                    profile = self._commit_profile(profile, latest, now_ts=now_ts)
                normalized[key] = profile
            next_document: dict[str, Any] = dict(normalized)
            if bulk_attempted:
                next_meta = dict(current_meta)
                next_meta["schema_version"] = _SCHEMA_VERSION
                next_meta["external_load_migration_version"] = _EXTERNAL_MIGRATION_VERSION
                next_meta["external_load_migration_at"] = now_ts
                next_document[_STORE_META_KEY] = next_meta
            elif current_meta:
                next_document[_STORE_META_KEY] = current_meta
            result = copy.deepcopy(normalized)
            return next_document

        get_data_store().mutate_sync(_STORE_NAME, _mutate)
        return result

    def _mirror_latest(self, user_id: str) -> None:
        if not self.external.available:
            return
        with self._mirror_lock:
            try:
                profile = self.peek_user_data(user_id)
                if profile is None:
                    return
                projection = {
                    key: copy.deepcopy(profile[key])
                    for key in _EXTERNAL_MIGRATION_FIELDS
                    if key in profile
                }
                self.external.update_user_data(user_id, **projection)
            except Exception as exc:
                self._log_external_failure(f"拟人插件：同步外部签到好感度失败 user={user_id}: {exc}")

    def get_level_name(self, value: float) -> str:
        levels = getattr(self.plugin_config, "personification_favorability_levels", None)
        return level_name_for_score(value, levels)

    def apply_group_good_atmosphere(
        self,
        group_id: str,
        *,
        now: Any = None,
        reason: str = "模型输出氛围好控制标记",
        event_id: str = "",
    ) -> dict[str, Any]:
        gid = str(group_id or "").strip()
        return self.apply_event(
            f"group_{gid}",
            "group_good_atmosphere",
            reason=reason,
            group_id=gid,
            now=now,
            event_id=event_id,
        )

    def apply_user_interesting_chat(
        self,
        user_id: str,
        *,
        now: Any = None,
        group_id: str = "",
        reason: str = "模型输出有趣控制标记",
        event_id: str = "",
    ) -> dict[str, Any]:
        return self.apply_event(
            str(user_id or "").strip(),
            "user_interesting_chat",
            reason=reason,
            group_id=str(group_id or "").strip(),
            now=now,
            event_id=event_id,
        )

    def apply_user_reply_interaction(
        self,
        user_id: str,
        *,
        now: Any = None,
        group_id: str = "",
        is_direct: bool = False,
        is_random_chat: bool = False,
        reason: str = "成功完成一轮可见回复互动",
        event_id: str = "",
    ) -> dict[str, Any]:
        return self.apply_event(
            str(user_id or "").strip(),
            "user_reply_interaction",
            reason=reason,
            group_id=str(group_id or "").strip(),
            now=now,
            metadata={
                "is_direct": bool(is_direct),
                "is_random_chat": bool(is_random_chat),
            },
            event_id=event_id,
        )

    def apply_perm_blacklist(
        self,
        user_id: str,
        *,
        set_blacklisted: bool,
        actor: str = "",
        now: Any = None,
    ) -> dict[str, Any]:
        key = str(user_id or "").strip()
        if not key:
            return {"applied": False, "status": "invalid", "old": 0.0, "new": 0.0, "delta": 0.0}
        if set_blacklisted:
            return self._apply_event_atomic(
                key,
                "user_perm_blacklist",
                reason="管理员加入永久黑名单",
                actor=actor,
                now=now,
                blacklist_state=True,
            )
        return self._apply_event_atomic(
            key,
            "user_perm_blacklist_removed",
            reason="管理员移出永久黑名单",
            actor=actor,
            now=now,
            blacklist_state=False,
        )

    def set_score(
        self,
        user_id: str,
        score: float,
        *,
        actor: str = "",
        reason: str = "管理员手动设置好感度",
        now: Any = None,
    ) -> dict[str, Any]:
        key = str(user_id or "").strip()
        return self._apply_event_atomic(
            key,
            "manual_adjust",
            reason=reason,
            actor=actor,
            now=now,
            target_score=score,
        )

    def run_decay_once(self, *, now: Any = None, limit: int | None = None) -> dict[str, Any]:
        if not self.enabled or not bool(
            getattr(self.plugin_config, "personification_favorability_decay_enabled", False)
        ):
            return {"enabled": False, "checked": 0, "decayed": 0, "events": []}
        idle_days = max(
            1,
            _safe_int(
                getattr(self.plugin_config, "personification_favorability_decay_idle_days", 14),
                14,
            ),
        )
        delta = _safe_float(
            getattr(self.plugin_config, "personification_favorability_decay_delta", -0.20),
            -0.20,
        )
        if delta >= 0:
            return {"enabled": True, "checked": 0, "decayed": 0, "events": []}
        now_ts = _timestamp(now)
        now_date = self.current_date(now)
        max_items = max(0, _safe_int(limit, 0)) if limit is not None else 0
        checked = 0
        decayed = 0
        events: list[dict[str, Any]] = []
        decayed_keys: list[str] = []

        def _mutate(current: Any) -> dict[str, Any]:
            nonlocal checked, decayed, events
            data = dict(current) if isinstance(current, dict) else {}
            store_meta = self._store_meta(data)
            for raw_key, raw_profile in list(data.items()):
                key = str(raw_key or "").strip()
                if raw_key == _STORE_META_KEY or not key or _is_group_key(key):
                    continue
                latest = raw_profile if isinstance(raw_profile, dict) else None
                profile = self._prepare_profile(
                    key,
                    latest,
                    now_ts=now_ts,
                    now_date=now_date,
                    store_meta=store_meta,
                )
                prepared_changed = not isinstance(latest, dict) or profile != latest
                checked += 1
                event_changed = False
                if not (max_items and decayed >= max_items) and not profile.get("is_perm_blacklisted"):
                    already_decayed = str(profile.get("last_favorability_decay_date", "") or "") == now_date
                    activity_at = max(0, _safe_int(profile.get("last_relationship_activity_at", 0), 0))
                    idle_elapsed = not activity_at or now_ts - activity_at >= idle_days * 86400
                    floor = default_favorability_for_id(key, self.plugin_config)
                    current_score = _clamp_score(profile.get("favorability", floor), floor)
                    if not already_decayed and idle_elapsed and current_score > floor:
                        applied_delta = max(delta, floor - current_score)
                        profile, result, event_changed = self._apply_event_to_profile(
                            profile,
                            user_id=key,
                            event_name="daily_decay",
                            delta=applied_delta,
                            reason=f"超过 {idle_days} 天无关系互动后的每日维护衰减",
                            actor="",
                            group_id="",
                            now_ts=now_ts,
                            now_date=now_date,
                            daily_cap=None,
                            patch={
                                "last_favorability_decay_date": now_date,
                                "last_favorability_decay_at": now_ts,
                            },
                            metadata={"idle_days": idle_days},
                            event_id="",
                        )
                        if result.get("delta"):
                            decayed += 1
                            events.append(result)
                            decayed_keys.append(key)
                if prepared_changed or event_changed:
                    data[key] = self._commit_profile(profile, latest, now_ts=now_ts)
            return data

        get_data_store().mutate_sync(_STORE_NAME, _mutate)
        for key in decayed_keys:
            self._mirror_latest(key)
        return {"enabled": True, "checked": checked, "decayed": decayed, "events": events}


def build_external_sign_in_adapter() -> ExternalFavorabilityAdapter:
    try:
        try:
            from plugin.sign_in.utils import get_user_data, load_data, update_user_data  # type: ignore
            from plugin.sign_in.config import get_level_name  # type: ignore
        except ImportError:
            from ...sign_in.utils import get_user_data, load_data, update_user_data  # type: ignore
            from ...sign_in.config import get_level_name  # type: ignore
        return ExternalFavorabilityAdapter(
            available=True,
            get_user_data=get_user_data,
            update_user_data=update_user_data,
            load_data=load_data,
            get_level_name=get_level_name,
        )
    except Exception:
        return ExternalFavorabilityAdapter(
            available=False,
            get_user_data=lambda _uid: {},
            update_user_data=lambda *_a, **_k: None,
            load_data=lambda: {},
            get_level_name=lambda value: level_name_for_score(value),
        )


__all__ = [
    "DEFAULT_FAVORABILITY_ATTITUDES",
    "DEFAULT_FAVORABILITY_EVENT_DELTAS",
    "DEFAULT_FAVORABILITY_LEVELS",
    "ExternalFavorabilityAdapter",
    "FavorabilityService",
    "build_external_sign_in_adapter",
    "default_favorability_for_id",
    "normalize_favorability_event_deltas",
    "level_name_for_score",
    "normalize_favorability_levels",
    "normalize_favorability_profile",
]
