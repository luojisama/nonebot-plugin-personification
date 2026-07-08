from __future__ import annotations

import copy
import time
from dataclasses import dataclass
from typing import Any, Callable


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

_STORE_NAME = "favorability_profiles"


def get_data_store() -> Any:
    from .data_store import get_data_store as _get_data_store

    return _get_data_store()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamp_score(value: Any, default: float = 0.0) -> float:
    return round(max(0.0, min(100.0, _safe_float(value, default))), 2)


def _date_string(now: Any = None) -> str:
    if now is not None and hasattr(now, "strftime"):
        try:
            return str(now.strftime("%Y-%m-%d"))
        except Exception:
            pass
    if now is not None and not isinstance(now, str):
        ts = _safe_float(now, time.time())
    else:
        ts = time.time()
    return time.strftime("%Y-%m-%d", time.localtime(ts))


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
            getattr(plugin_config, "personification_favorability_group_default_score", 100.0),
            100.0,
        )
    return _clamp_score(
        getattr(plugin_config, "personification_favorability_default_score", 0.0),
        0.0,
    )


def normalize_favorability_profile(
    user_id: str,
    value: Any,
    *,
    plugin_config: Any = None,
) -> dict[str, Any]:
    profile = dict(value) if isinstance(value, dict) else {}
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
    for key in ("last_favorability_event_at", "last_favorability_decay_at"):
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
            event["old"] = _clamp_score(event.get("old", 0.0), 0.0)
            event["new"] = _clamp_score(event.get("new", event["old"]), event["old"])
            event["timestamp"] = max(0, _safe_int(event.get("timestamp", 0) or 0, 0))
            for text_key in ("reason", "actor", "group_id", "status"):
                if text_key in event:
                    event[text_key] = str(event.get(text_key, "") or "").strip()
            events.append(event)
    limit = _event_log_limit(plugin_config)
    profile["favorability_events"] = events[-limit:] if limit else []
    profile["is_perm_blacklisted"] = bool(profile.get("is_perm_blacklisted", False))
    profile.setdefault("created_at", int(time.time()))
    profile["updated_at"] = int(_safe_float(profile.get("updated_at", time.time()), time.time()))
    profile["schema_version"] = 2
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

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.plugin_config, "personification_favorability_enabled", True))

    @property
    def external_available(self) -> bool:
        return bool(self.external.available)

    def _load_store(self) -> dict[str, dict[str, Any]]:
        raw = get_data_store().load_sync(_STORE_NAME)
        data = raw if isinstance(raw, dict) else {}
        changed = False
        normalized: dict[str, dict[str, Any]] = {}
        for user_id, profile in data.items():
            key = str(user_id or "").strip()
            if not key:
                changed = True
                continue
            normalized_profile = normalize_favorability_profile(
                key,
                profile,
                plugin_config=self.plugin_config,
            )
            normalized[key] = normalized_profile
            if normalized_profile != profile:
                changed = True
        if changed:
            get_data_store().save_sync(_STORE_NAME, normalized)
        return normalized

    def _save_store(self, data: dict[str, dict[str, Any]]) -> None:
        normalized = {
            str(user_id): normalize_favorability_profile(
                str(user_id),
                profile,
                plugin_config=self.plugin_config,
            )
            for user_id, profile in (data or {}).items()
            if str(user_id or "").strip()
        }
        get_data_store().save_sync(_STORE_NAME, normalized)

    def _external_user_data(self, user_id: str) -> dict[str, Any]:
        if not self.external.available:
            return {}
        try:
            data = self.external.get_user_data(str(user_id))
        except Exception as exc:
            if self.logger is not None:
                try:
                    self.logger.debug(f"拟人插件：读取外部签到好感度失败 user={user_id}: {exc}")
                except Exception:
                    pass
            return {}
        return dict(data) if isinstance(data, dict) else {}

    def _merge_external_fields(self, local: dict[str, Any], external: dict[str, Any]) -> dict[str, Any]:
        if not external:
            return dict(local)
        merged = dict(local)
        for key in (
            "custom_title",
            "nickname",
            "is_perm_blacklisted",
            "blacklist_count",
            "last_update",
            "daily_fav_count",
            "last_interesting_date",
            "daily_interesting_count",
        ):
            if key not in merged or merged.get(key) in ("", None, 0, 0.0, False):
                if key in external:
                    merged[key] = external.get(key)
        return merged

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
    ) -> dict[str, Any]:
        key = str(user_id or "").strip()
        event_name = str(event_type or "").strip()
        if not key or not event_name or not self.enabled:
            return {
                "applied": False,
                "status": "disabled" if not self.enabled else "invalid",
                "old": 0.0,
                "new": 0.0,
                "delta": 0.0,
                "requested_delta": 0.0,
                "capped": False,
            }

        requested_delta = self._event_delta(event_name, delta)
        now_ts = _timestamp(now)
        now_date = _date_string(now)
        data = self._load_store()
        current = data.get(key)
        if current is None:
            current = self.get_user_data(key)
            data = self._load_store()
        profile = normalize_favorability_profile(key, current or {}, plugin_config=self.plugin_config)

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
            user_id=key,
            event_type=event_name,
            delta=requested_delta,
            now_date=now_date,
            daily_cap=daily_cap,
        )
        old_score = _clamp_score(profile.get("favorability", default_favorability_for_id(key, self.plugin_config)))
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
            profile["daily_fav_count"] = round(
                max(0.0, legacy_daily) + applied_delta,
                2,
            )
        elif event_name == "user_interesting_chat" and applied_delta > 0:
            legacy_daily = (
                0.0
                if str(profile.get("last_interesting_date", "") or "") != now_date
                else _safe_float(profile.get("daily_interesting_count", 0.0), 0.0)
            )
            profile["last_interesting_date"] = now_date
            profile["daily_interesting_count"] = round(
                max(0.0, legacy_daily) + applied_delta,
                2,
            )

        if patch:
            profile.update(dict(patch))
        profile["favorability"] = new_score
        profile["last_favorability_event_at"] = now_ts
        profile["last_favorability_event_date"] = now_date
        profile["updated_at"] = now_ts
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
        )
        normalized = normalize_favorability_profile(key, profile, plugin_config=self.plugin_config)
        data[key] = normalized
        self._save_store(data)
        if self.external.available:
            mirror_patch = dict(patch or {})
            mirror_patch.update(
                {
                    "favorability": normalized.get("favorability", new_score),
                    "daily_fav_count": normalized.get("daily_fav_count", 0.0),
                    "last_update": normalized.get("last_update", ""),
                    "daily_interesting_count": normalized.get("daily_interesting_count", 0.0),
                    "last_interesting_date": normalized.get("last_interesting_date", ""),
                    "is_perm_blacklisted": normalized.get("is_perm_blacklisted", False),
                    "blacklist_count": normalized.get("blacklist_count", 0),
                }
            )
            try:
                self.external.update_user_data(key, **mirror_patch)
            except Exception as exc:
                if self.logger is not None:
                    try:
                        self.logger.debug(f"拟人插件：同步外部签到好感度事件失败 user={key}: {exc}")
                    except Exception:
                        pass

        return {
            "applied": bool(applied_delta or patch),
            "status": status,
            "old": old_score,
            "new": new_score,
            "delta": applied_delta,
            "requested_delta": requested_delta,
            "capped": capped,
            "daily_used": round(used_before + abs(applied_delta), 2) if bucket else 0.0,
            "daily_cap": cap,
            "event_type": event_name,
        }

    def get_user_data(self, user_id: str) -> dict[str, Any]:
        key = str(user_id or "").strip()
        if not key:
            return {}
        data = self._load_store()
        existing = data.get(key)
        external = self._external_user_data(key)
        if existing is None:
            seed = external or {"favorability": default_favorability_for_id(key, self.plugin_config)}
            profile = normalize_favorability_profile(key, seed, plugin_config=self.plugin_config)
            profile["source"] = "external_sign_in" if external else "personification"
            data[key] = profile
            self._save_store(data)
            return copy.deepcopy(profile)
        merged = self._merge_external_fields(existing, external)
        profile = normalize_favorability_profile(key, merged, plugin_config=self.plugin_config)
        if profile != existing:
            data[key] = profile
            self._save_store(data)
        return copy.deepcopy(profile)

    def update_user_data(self, user_id: str, **patch: Any) -> None:
        key = str(user_id or "").strip()
        if not key:
            return
        data = self._load_store()
        current = data.get(key)
        if current is None:
            current = self.get_user_data(key)
            data = self._load_store()
        updated = dict(current or {})
        updated.update(dict(patch or {}))
        updated["updated_at"] = int(time.time())
        data[key] = normalize_favorability_profile(key, updated, plugin_config=self.plugin_config)
        self._save_store(data)
        if self.external.available:
            try:
                self.external.update_user_data(key, **patch)
            except Exception as exc:
                if self.logger is not None:
                    try:
                        self.logger.debug(f"拟人插件：同步外部签到好感度失败 user={key}: {exc}")
                    except Exception:
                        pass

    def load_data(self) -> dict[str, dict[str, Any]]:
        local = self._load_store()
        merged: dict[str, dict[str, Any]] = {}
        if self.external.available:
            try:
                external = self.external.load_data() or {}
            except Exception:
                external = {}
            if isinstance(external, dict):
                for user_id, profile in external.items():
                    key = str(user_id or "").strip()
                    if key:
                        merged[key] = normalize_favorability_profile(
                            key,
                            profile,
                            plugin_config=self.plugin_config,
                        )
        for user_id, profile in local.items():
            merged[str(user_id)] = normalize_favorability_profile(
                str(user_id),
                profile,
                plugin_config=self.plugin_config,
            )
        if merged != local:
            self._save_store(merged)
        return copy.deepcopy(merged)

    def get_level_name(self, value: float) -> str:
        levels = getattr(self.plugin_config, "personification_favorability_levels", None)
        return level_name_for_score(value, levels)

    def apply_group_good_atmosphere(
        self,
        group_id: str,
        *,
        now: Any = None,
        reason: str = "模型输出氛围好控制标记",
    ) -> dict[str, Any]:
        gid = str(group_id or "").strip()
        return self.apply_event(
            f"group_{gid}",
            "group_good_atmosphere",
            reason=reason,
            group_id=gid,
            now=now,
        )

    def apply_user_interesting_chat(
        self,
        user_id: str,
        *,
        now: Any = None,
        group_id: str = "",
        reason: str = "模型输出有趣控制标记",
    ) -> dict[str, Any]:
        return self.apply_event(
            str(user_id or "").strip(),
            "user_interesting_chat",
            reason=reason,
            group_id=str(group_id or "").strip(),
            now=now,
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
        current = self.get_user_data(key)
        already = bool(current.get("is_perm_blacklisted", False))
        blacklist_count = max(0, _safe_int(current.get("blacklist_count", 0), 0))
        if set_blacklisted:
            patch = {
                "is_perm_blacklisted": True,
                "blacklist_count": blacklist_count if already else blacklist_count + 1,
            }
            return self.apply_event(
                key,
                "user_perm_blacklist",
                delta=0.0 if already else None,
                reason="管理员加入永久黑名单",
                actor=actor,
                now=now,
                patch=patch,
            )
        return self.apply_event(
            key,
            "user_perm_blacklist_removed",
            reason="管理员移出永久黑名单",
            actor=actor,
            now=now,
            patch={"is_perm_blacklisted": False},
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
        current = self.get_user_data(key)
        old_score = _clamp_score(current.get("favorability", default_favorability_for_id(key, self.plugin_config)))
        target = _clamp_score(score, old_score)
        return self.apply_event(
            key,
            "manual_adjust",
            delta=round(target - old_score, 2),
            reason=reason,
            actor=actor,
            now=now,
            daily_cap=-1.0,
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
        now_date = _date_string(now)
        max_items = max(0, _safe_int(limit, 0)) if limit is not None else 0
        data = self._load_store()
        checked = 0
        decayed = 0
        events: list[dict[str, Any]] = []
        for key, raw_profile in list(data.items()):
            if max_items and decayed >= max_items:
                break
            key = str(key or "").strip()
            if not key or _is_group_key(key):
                continue
            profile = normalize_favorability_profile(key, raw_profile, plugin_config=self.plugin_config)
            checked += 1
            if profile.get("is_perm_blacklisted"):
                continue
            if str(profile.get("last_favorability_decay_date", "") or "") == now_date:
                continue
            last_event_at = max(
                _safe_int(profile.get("last_favorability_event_at", 0), 0),
                _safe_int(profile.get("updated_at", 0), 0),
                _safe_int(profile.get("created_at", 0), 0),
            )
            if last_event_at and now_ts - last_event_at < idle_days * 86400:
                continue
            floor = default_favorability_for_id(key, self.plugin_config)
            current_score = _clamp_score(profile.get("favorability", floor), floor)
            if current_score <= floor:
                continue
            applied_delta = max(delta, floor - current_score)
            result = self.apply_event(
                key,
                "daily_decay",
                delta=applied_delta,
                reason=f"超过 {idle_days} 天无好感事件的每日维护衰减",
                now=now,
                patch={
                    "last_favorability_decay_date": now_date,
                    "last_favorability_decay_at": now_ts,
                },
                metadata={"idle_days": idle_days},
            )
            if result.get("delta"):
                decayed += 1
                events.append(result)
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
    except ImportError:
        return ExternalFavorabilityAdapter(
            available=False,
            get_user_data=lambda _uid: {},
            update_user_data=lambda *_a, **_k: None,
            load_data=lambda: {},
            get_level_name=lambda value: level_name_for_score(value),
        )


__all__ = [
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
