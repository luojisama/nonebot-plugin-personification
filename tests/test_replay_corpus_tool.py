"""replay_corpus 工具与样本的 smoke 测试。

- 验证至少存在 cold-start 数量的样本
- 验证 jsonl 解析正常
- 验证 metadata_fallback_turn_plan 接入路径无报错
"""
from __future__ import annotations

import json
import importlib.util
import sys
from pathlib import Path

from ._loader import load_personification_module

_CORPUS_DIR = Path(__file__).parent / "replay_corpus"
_REQUIRED_BAD_REPLY_TAGS = {
    "empty_agreement",
    "echo_rephrase",
    "observer_posture",
    "transcript_summary",
    "empty_exclamation",
    "action_after_send",
    "media_overexplaining",
    "wrong_topic_interjection",
    "vague_deferred_lookup",
    "empty_evidence_self_report",
}


def _load_planner():
    return load_personification_module("plugin.personification.agent.runtime.planner")


def _load_replay_script():
    script_path = Path(__file__).resolve().parent.parent / "scripts" / "replay_corpus.py"
    spec = importlib.util.spec_from_file_location("personification_replay_corpus_script", script_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load replay_corpus script from {script_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _iter_replay_records() -> list[dict]:
    records: list[dict] = []
    for f in _CORPUS_DIR.glob("*.jsonl"):
        with open(f, "r", encoding="utf-8") as fp:
            for raw in fp:
                raw = raw.strip()
                if raw:
                    records.append(json.loads(raw))
    return records


def test_replay_corpus_has_min_samples() -> None:
    files = list(_CORPUS_DIR.glob("*.jsonl"))
    assert len(files) >= 3, "至少需要 3 个回放文件（群聊/私聊/QZone）"

    total_lines = len(_iter_replay_records())
    assert total_lines >= 15, f"冷启动样本不少于 15 段，当前 {total_lines}"


def test_replay_corpus_jsonl_valid() -> None:
    for f in _CORPUS_DIR.glob("*.jsonl"):
        with open(f, "r", encoding="utf-8") as fp:
            for line_no, raw in enumerate(fp, start=1):
                raw = raw.strip()
                if not raw:
                    continue
                data = json.loads(raw)
                assert isinstance(data, dict), f"{f.name}:{line_no} 不是对象"
                assert "scene" in data, f"{f.name}:{line_no} 缺少 scene"
                assert "expected_frame" in data, f"{f.name}:{line_no} 缺少 expected_frame"
                assert "metadata" in data, f"{f.name}:{line_no} 缺少 metadata"
                if "quality_tags" in data:
                    assert isinstance(data["quality_tags"], list), f"{f.name}:{line_no} quality_tags 必须是列表"
                if "bad_reply_examples" in data:
                    examples = data["bad_reply_examples"]
                    assert isinstance(examples, list), f"{f.name}:{line_no} bad_reply_examples 必须是列表"
                    for example in examples:
                        assert isinstance(example, dict), f"{f.name}:{line_no} bad_reply_examples 项必须是对象"
                        assert example.get("label"), f"{f.name}:{line_no} 坏例缺少 label"
                        assert example.get("text"), f"{f.name}:{line_no} 坏例缺少 text"
                        assert example.get("why"), f"{f.name}:{line_no} 坏例缺少 why"


def test_metadata_fallback_executes() -> None:
    planner = _load_planner()
    plan = planner.metadata_fallback_turn_plan(
        is_group=True,
        is_random_chat=False,
        is_direct_mention=True,
        has_images=False,
        message_target="bot",
    )
    assert plan.reply_action in {"reply", "silence", "ask_clarify"}
    assert plan.speech_act in planner.ALLOWED_SPEECH_ACTS
    assert plan.output_mode in planner.ALLOWED_OUTPUT_MODES


def test_replay_corpus_quality_samples_cover_bad_reply_modes() -> None:
    seen_tags: set[str] = set()
    bad_example_count = 0
    for data in _iter_replay_records():
        seen_tags.update(str(tag) for tag in data.get("quality_tags", []) or [])
        bad_example_count += len(data.get("bad_reply_examples", []) or [])

    missing = _REQUIRED_BAD_REPLY_TAGS - seen_tags
    assert not missing, f"坏回复回放缺少质量标签：{sorted(missing)}"
    assert bad_example_count >= len(_REQUIRED_BAD_REPLY_TAGS)


def test_replay_report_includes_speech_act_and_quality_sections() -> None:
    replay = _load_replay_script()
    records = replay.load_records([str(_CORPUS_DIR / "bad_reply_quality.jsonl")])
    diffs = []
    for record in records:
        actual_plan = replay.compute_actual_plan(record)
        diff = replay.ReplayDiff(record=record, actual_plan=actual_plan)
        diff.diffs = replay.diff_plan_vs_expected(actual_plan, record.expected_frame)
        diffs.append(diff)

    report = replay.render_report(diffs)

    assert "speech_act" in report
    assert "质量覆盖矩阵" in report
    assert "回复边界" in report
    assert "坏回复样例" in report
    assert "fallback diff" in report


def test_replay_report_json_exposes_quality_coverage() -> None:
    replay = _load_replay_script()
    records = replay.load_records([str(_CORPUS_DIR / "bad_reply_quality.jsonl")])
    diffs = []
    for record in records:
        actual_plan = replay.compute_actual_plan(record)
        diff = replay.ReplayDiff(record=record, actual_plan=actual_plan)
        diff.diffs = replay.diff_plan_vs_expected(actual_plan, record.expected_frame)
        diffs.append(diff)

    payload = json.loads(replay.render_json_report(diffs))

    assert payload["summary"]["total"] == len(records)
    quality = {row["tag"]: row for row in payload["quality_coverage"]}
    assert quality["observer_posture"]["bad_examples"] >= 1
    assert quality["empty_agreement"]["samples"] >= 1
    boundaries = {row["boundary"]: row for row in payload["reply_boundary_coverage"]}
    assert boundaries["no_reply"]["samples"] >= 1
    assert payload["details"][0]["actual_plan"]["speech_act"]
