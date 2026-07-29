from __future__ import annotations

import time
from typing import Any


_MAX_TEXT = 160


def _norm_id(value: Any) -> str:
    return str(value or "").strip()


def _text(value: Any, *, max_chars: int = _MAX_TEXT) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text[: max(1, int(max_chars))]


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def qq_avatar_url(user_id: str) -> str:
    uid = _norm_id(user_id)
    if not uid:
        return ""
    return f"https://q.qlogo.cn/headimg_dl?dst_uin={uid}&spec=640"


def qq_homepage_url(user_id: str) -> str:
    uid = _norm_id(user_id)
    if not uid:
        return ""
    return f"https://user.qzone.qq.com/{uid}"


def _read_attr_or_key(obj: Any, *names: str) -> Any:
    for name in names:
        if isinstance(obj, dict) and name in obj:
            value = obj.get(name)
        else:
            value = getattr(obj, name, None)
        if value not in (None, ""):
            return value
    return ""


def _merge_text_field(target: dict[str, Any], key: str, *values: Any, max_chars: int = _MAX_TEXT) -> None:
    for value in values:
        normalized = _text(value, max_chars=max_chars)
        if normalized:
            target[key] = normalized
            return


def _merge_int_field(target: dict[str, Any], key: str, *values: Any) -> None:
    for value in values:
        normalized = _int_or_none(value)
        if normalized is not None:
            target[key] = normalized
            return


def build_user_profile_meta(
    user_id: str,
    *,
    sender: Any = None,
    stranger_info: dict[str, Any] | None = None,
    existing: dict[str, Any] | None = None,
    source: str = "",
    now_ts: float | None = None,
) -> dict[str, Any]:
    """Build a protocol/profile metadata snapshot for prompt and WebUI use.

    The fields are protocol facts and deterministic URLs. They are not used for
    dialogue routing; the LLM receives them as context if available.
    """
    uid = _norm_id(user_id)
    if not uid:
        return {}
    raw = stranger_info if isinstance(stranger_info, dict) else {}
    old = existing if isinstance(existing, dict) else {}
    out: dict[str, Any] = {
        "user_id": uid,
        "avatar_url": _text(old.get("avatar_url") or qq_avatar_url(uid), max_chars=260),
        "homepage_url": _text(old.get("homepage_url") or qq_homepage_url(uid), max_chars=260),
        "updated_at": float(now_ts if now_ts is not None else time.time()),
    }
    if source:
        out["source"] = _text(source, max_chars=80)

    _merge_text_field(
        out,
        "nickname",
        _read_attr_or_key(sender, "nickname"),
        raw.get("nickname"),
        old.get("nickname"),
    )
    _merge_text_field(out, "remark", raw.get("remark"), raw.get("remark_name"), old.get("remark"))
    _merge_text_field(out, "card", _read_attr_or_key(sender, "card"), raw.get("card"), old.get("card"))
    _merge_text_field(out, "sex", _read_attr_or_key(sender, "sex", "gender"), raw.get("sex"), raw.get("gender"), old.get("sex"), max_chars=24)
    _merge_int_field(out, "age", _read_attr_or_key(sender, "age"), raw.get("age"), old.get("age"))
    _merge_text_field(out, "qid", raw.get("qid"), raw.get("QID"), old.get("qid"), max_chars=48)
    _merge_text_field(out, "level", raw.get("level"), _read_attr_or_key(sender, "level"), old.get("level"), max_chars=48)
    _merge_int_field(out, "login_days", raw.get("login_days"), raw.get("loginDays"), old.get("login_days"))
    _merge_text_field(
        out,
        "signature",
        raw.get("signature"),
        raw.get("sign"),
        raw.get("long_nick"),
        raw.get("longNick"),
        raw.get("longnick"),
        raw.get("personal_signature"),
        old.get("signature"),
        max_chars=220,
    )
    _merge_text_field(out, "area", _read_attr_or_key(sender, "area"), raw.get("area"), old.get("area"), max_chars=80)
    _merge_text_field(out, "role", _read_attr_or_key(sender, "role"), old.get("role"), max_chars=32)
    _merge_text_field(out, "title", _read_attr_or_key(sender, "title"), raw.get("title"), old.get("title"), max_chars=80)
    _merge_text_field(out, "title_expire_time", raw.get("title_expire_time"), old.get("title_expire_time"), max_chars=32)

    extra_keys = []
    for key in ("vip_level", "qq_level", "constellation", "company", "school", "email"):
        value = raw.get(key)
        if value not in (None, ""):
            out[key] = _text(value, max_chars=80)
            extra_keys.append(key)
    if extra_keys:
        out["extra_fields"] = extra_keys[:12]
    for key in ("avatar_analysis", "avatar_insight"):
        value = old.get(key)
        if isinstance(value, dict):
            out[key] = dict(value)
    return out


def merge_profile_json_with_meta(profile_json: dict[str, Any] | None, meta: dict[str, Any]) -> dict[str, Any]:
    base = dict(profile_json or {})
    old_meta = base.get("qq_profile")
    if not isinstance(old_meta, dict):
        old_meta = {}
    merged = build_user_profile_meta(
        str(meta.get("user_id") or old_meta.get("user_id") or ""),
        stranger_info=meta,
        existing=old_meta,
        source=str(meta.get("source") or old_meta.get("source") or "profile_meta"),
        now_ts=float(meta.get("updated_at", 0) or time.time()),
    )
    if merged:
        base["qq_profile"] = merged
    return base


def render_user_profile_meta(meta: dict[str, Any] | None) -> str:
    data = meta if isinstance(meta, dict) else {}
    if not data:
        return ""
    lines: list[str] = []
    basic_parts: list[str] = []
    for key, label in (("nickname", "昵称"), ("card", "群名片"), ("remark", "备注")):
        if data.get(key):
            basic_parts.append(f"{label}：{data[key]}")
    if data.get("sex"):
        basic_parts.append(f"性别：{data['sex']}")
    if data.get("age") not in (None, ""):
        basic_parts.append(f"年龄：{data['age']}")
    if basic_parts:
        lines.append("[QQ资料] " + "；".join(str(x) for x in basic_parts))
    social_parts: list[str] = []
    for key, label in (("qid", "QID"), ("level", "等级"), ("login_days", "登录天数"), ("area", "地区")):
        value = data.get(key)
        if value not in (None, ""):
            social_parts.append(f"{label}：{value}")
    if social_parts:
        lines.append("[账号资料] " + "；".join(str(x) for x in social_parts))
    if data.get("title") or data.get("role"):
        parts = []
        if data.get("role"):
            parts.append(f"群角色：{data['role']}")
        if data.get("title"):
            parts.append(f"专属头衔：{data['title']}")
        lines.append("[群内身份] " + "；".join(parts))
    if data.get("signature"):
        lines.append(f"[个性签名] {data['signature']}")
    avatar_insight = data.get("avatar_insight") if isinstance(data.get("avatar_insight"), dict) else {}
    if avatar_insight:
        avatar_parts = []
        if avatar_insight.get("asset_kind"):
            avatar_parts.append(f"类型：{avatar_insight['asset_kind']}")
        if avatar_insight.get("neutral_summary"):
            avatar_parts.append(f"中性摘要：{avatar_insight['neutral_summary']}")
        candidates = avatar_insight.get("acg_candidates")
        if isinstance(candidates, list) and candidates:
            avatar_parts.append("ACG候选：" + "、".join(str(item) for item in candidates[:5]))
        if avatar_parts:
            lines.append("[头像视觉（长期弱证据）] " + "；".join(avatar_parts))
            lines.append("[头像视觉边界] 不用于推断真实身份、性别、年龄、性格、精神状态、职业或现实关系。")
    if data.get("homepage_url"):
        lines.append(f"[主页] {data['homepage_url']}")
    return "\n".join(lines)


def compact_user_profile_meta_summary(meta: dict[str, Any] | None) -> str:
    data = meta if isinstance(meta, dict) else {}
    if not data:
        return ""
    parts = []
    for key in ("nickname", "card", "signature"):
        value = _text(data.get(key), max_chars=48)
        if value:
            parts.append(value)
    return " / ".join(parts[:3])


__all__ = [
    "build_user_profile_meta",
    "compact_user_profile_meta_summary",
    "merge_profile_json_with_meta",
    "qq_avatar_url",
    "qq_homepage_url",
    "render_user_profile_meta",
]
