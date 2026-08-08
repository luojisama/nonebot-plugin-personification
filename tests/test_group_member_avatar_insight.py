from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from ._loader import load_personification_module


member_avatar = load_personification_module(
    "plugin.personification.core.group_member_avatar_insight"
)


def _runtime() -> SimpleNamespace:
    return SimpleNamespace(
        plugin_config=SimpleNamespace(personification_thinking_mode="none"),
        logger=SimpleNamespace(warning=lambda *_a, **_k: None),
        get_configured_api_providers=lambda: [],
        profile_service=None,
    )


def _vision_json() -> str:
    return json.dumps(
        {
            "asset_kind": "acg_character",
            "subject_count": 1,
            "neutral_summary": "金发红眼、比着剪刀手的动漫角色",
            "acg_candidates": ["某虚构角色"],
            "contains_text": False,
            "confidence": 0.91,
        },
        ensure_ascii=False,
    )


def test_group_member_avatar_tool_requires_policy_membership_and_returns_envelope(monkeypatch) -> None:  # noqa: ANN001
    calls: list[str] = []

    class _Adapter:
        async def get_group_member_info(self, *, group_id, user_id):  # noqa: ANN001
            calls.append(f"member:{group_id}:{user_id}")
            return SimpleNamespace(ok=True, data={"group_id": str(group_id), "user_id": str(user_id)})

    async def _download(url: str, **_kwargs):  # noqa: ANN202
        calls.append(f"download:{url}")
        return SimpleNamespace(content=b"avatar", content_type="image/png")

    async def _analyze(**kwargs):  # noqa: ANN003, ANN202
        calls.append("vision")
        assert len(kwargs["image_refs"]) == 1
        return _vision_json(), "route_direct"

    monkeypatch.setattr(member_avatar, "get_protocol_adapter", lambda *_a, **_k: _Adapter())
    monkeypatch.setattr(member_avatar, "download_public_image", _download)
    monkeypatch.setattr(
        member_avatar,
        "sanitize_image",
        lambda content, _mime: (content, "image/png", 128, 128, ".png"),
    )
    monkeypatch.setattr(member_avatar, "analyze_images_with_route_or_fallback", _analyze)

    async def _authorize(_user_id: str):
        return SimpleNamespace(blocked=False, allow_context_read=True)

    tool = member_avatar.build_group_member_avatar_insight_tool(
        runtime=_runtime(),
        bot=SimpleNamespace(self_id="90001"),
        event=SimpleNamespace(group_id=20001, user_id=10001),
        candidates=[{"user_id": "10002", "label": "群友乙"}],
        policy_authorizer=_authorize,
    )
    assert tool is not None
    payload = json.loads(asyncio.run(tool.handler(member_label="群友乙")))

    assert payload["type"] == "personification_evidence_envelope"
    assert payload["available"] is True
    assert any("金发红眼" in claim for claim in payload["allowed_claims"])
    assert any("用户本人" in item for item in payload["forbidden_inferences"])
    assert "10002" not in json.dumps(payload, ensure_ascii=False)
    assert tool.metadata["final_behavior"] == "constrained_persona_output"
    assert tool.per_session_quota == 1
    assert calls[0] == "member:20001:10002"
    assert calls[-1] == "vision"


def test_group_member_avatar_tool_blocks_before_membership_or_download(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        member_avatar,
        "get_protocol_adapter",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("membership must not run")),
    )

    async def _authorize(_user_id: str):
        return SimpleNamespace(blocked=True, allow_context_read=False)

    tool = member_avatar.build_group_member_avatar_insight_tool(
        runtime=_runtime(),
        bot=SimpleNamespace(self_id="90001"),
        event=SimpleNamespace(group_id=20001, user_id=10001),
        candidates=[{"user_id": "10002", "label": "群友乙"}],
        policy_authorizer=_authorize,
    )
    payload = json.loads(asyncio.run(tool.handler(member_label="群友乙")))

    assert payload["available"] is False
    assert payload["allowed_claims"] == []


def test_group_member_avatar_tool_rejects_private_or_invalid_candidates() -> None:
    assert member_avatar.build_group_member_avatar_insight_tool(
        runtime=_runtime(),
        bot=SimpleNamespace(self_id="90001"),
        event=SimpleNamespace(user_id=10001),
        candidates=[{"user_id": "10002", "label": "群友乙"}],
    ) is None


def test_normal_and_yaml_paths_register_group_member_avatar_tool() -> None:
    root = Path(__file__).resolve().parents[1]
    normal = (root / "handlers" / "reply_pipeline" / "pipeline_context.py").read_text(
        encoding="utf-8"
    )
    yaml = (root / "handlers" / "yaml_pipeline" / "processor.py").read_text(
        encoding="utf-8"
    )

    assert "register_group_member_avatar_insight_tool(" in normal
    assert "register_group_member_avatar_insight_tool(" in yaml
    assert "policy_authorizer=" in normal
    assert "policy_authorizer=" in yaml
