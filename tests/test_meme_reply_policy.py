from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from ._loader import load_personification_module


data_store = load_personification_module("plugin.personification.core.data_store")
db = load_personification_module("plugin.personification.core.db")
learning = load_personification_module("plugin.personification.core.meme_learning_store")
policy = load_personification_module("plugin.personification.core.meme_reply_policy")


class _CompatiblePipeline:
    async def compare_senses(self, left, right):  # noqa: ANN001
        return {"relation": "compatible", "confidence": 0.93, "reason": "同一游戏语义"}


def _claim(*, term: str, platform: str, content_id: str, meaning: str) -> dict:
    return {
        "term": term,
        "aliases": [],
        "meaning": meaning,
        "game_context": {"canonical_name": "三角洲行动", "aliases": ["三角洲"]},
        "version_context": "",
        "usage_context": "讨论装备时",
        "safe_usage": "仅在三角洲行动装备语境中使用",
        "risk_level": "low",
        "extractor_confidence": 0.92,
        "content_key": f"{platform}:{content_id}",
        "source_cluster_id": f"source-{platform}-{content_id}",
        "source": {
            "canonical_url": f"https://example.invalid/{content_id}",
            "content_type": "video",
            "content_fingerprint": f"fp-{content_id}",
            "quality_score": 0.9,
        },
        "evidence_refs": [
            {
                "packet_id": f"packet-{content_id}",
                "platform": platform,
                "content_id": content_id,
                "discussion_id": f"comment-{content_id}",
                "quote": f"{term}指{meaning}",
            }
        ],
    }


def _store(tmp_path) -> object:
    cfg = SimpleNamespace(personification_data_dir=str(tmp_path))
    data_store.init_data_store(cfg)
    db.init_db_sync(tmp_path)
    return learning.MemeLearningStore()


def _ingest(store, claims: list[dict]) -> None:  # noqa: ANN001
    asyncio.run(
        store.ingest_claims(
            claims,
            semantic_pipeline=_CompatiblePipeline(),
            model_route="meme-reply-policy-test",
            now=1000,
        )
    )


def test_verified_sense_uses_fixed_reply_inner_probability_and_attaches_once(tmp_path) -> None:
    store = _store(tmp_path)
    _ingest(
        store,
        [
            _claim(term="刘涛", meaning="六级防具", platform="bilibili", content_id="B1"),
            _claim(term="刘涛", meaning="六级护甲", platform="xiaoheihe", content_id="X1"),
            _claim(term="刘涛", meaning="六套", platform="douyin", content_id="D1"),
        ],
    )
    frame = SimpleNamespace(turn_plan=SimpleNamespace())

    context = policy.prepare_meme_turn_context(
        group_id="g1",
        message_text="三角洲行动这局怎么配装",
        probability=0.18,
        semantic_frame=frame,
        rng=lambda: 0.17,
    )

    assert context["active_use_allowed"] is True
    assert context["selected_active_sense"]["term"] == "刘涛"
    assert context["max_active_memes"] == 1
    assert frame.meme_turn_context is context
    assert frame.turn_plan.meme_turn_context is context
    prompt = policy.format_meme_turn_prompt(context)
    assert "最多自然带一个梗" in prompt
    assert "唯一可主动使用的 sense：刘涛=" in prompt

    boundary = policy.prepare_meme_turn_context(
        group_id="g1",
        message_text="三角洲行动这局怎么配装",
        probability=0.18,
        rng=lambda: 0.18,
    )
    assert boundary["active_use_allowed"] is False
    assert boundary["selected_active_sense"] is None


def test_understand_only_never_becomes_active_meme(tmp_path) -> None:
    store = _store(tmp_path)
    _ingest(
        store,
        [
            _claim(term="大红", meaning="高价值红色物资", platform="bilibili", content_id="B1"),
            _claim(term="大红", meaning="高价值红色物品", platform="tieba", content_id="T1"),
        ],
    )

    context = policy.prepare_meme_turn_context(
        group_id="g1",
        message_text="三角洲里的大红是什么意思",
        probability=1.0,
        rng=lambda: 0.0,
    )

    assert [item["status"] for item in context["understanding_senses"]] == ["understand_only"]
    assert context["active_use_allowed"] is False
    assert context["selected_active_sense"] is None
    prompt = policy.format_meme_turn_prompt(context)
    assert "understand_only 只可用于理解或被问时解释" in prompt
    assert "本轮未通过主动玩梗抽样" in prompt


def test_game_sense_does_not_leak_into_real_person_context(tmp_path) -> None:
    store = _store(tmp_path)
    _ingest(
        store,
        [
            _claim(term="刘涛", meaning="六级防具", platform="bilibili", content_id="B1"),
            _claim(term="刘涛", meaning="六级护甲", platform="xiaoheihe", content_id="X1"),
            _claim(term="刘涛", meaning="六套", platform="douyin", content_id="D1"),
        ],
    )

    context = policy.prepare_meme_turn_context(
        group_id="g1",
        message_text="演员刘涛最近有什么新剧",
        probability=1.0,
        rng=lambda: 0.0,
    )

    assert context["understanding_senses"] == []
    assert context["active_use_allowed"] is False
    assert context["selected_active_sense"] is None
    assert policy.format_meme_turn_prompt(context) == ""


def test_normal_and_yaml_paths_sample_only_after_reply_arbitration() -> None:
    root = Path(__file__).resolve().parents[1]
    normal = (root / "handlers" / "reply_pipeline" / "processor.py").read_text(encoding="utf-8")
    yaml = (root / "handlers" / "yaml_pipeline" / "processor.py").read_text(encoding="utf-8")
    call = "meme_turn_context = prepare_meme_turn_context"

    assert normal.index("arbitration = prepared_semantics.arbitration") < normal.index(call)
    assert yaml.index("arbitration = arbitrate_reply_mode") < yaml.index(call)
    assert "rng=random.random" in normal
    assert "rng=random.random" in yaml
