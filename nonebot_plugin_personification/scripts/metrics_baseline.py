#!/usr/bin/env python
"""拟人插件性能基线对照工具。

从 metrics 持久化文件读取计数与延迟数据，按 turn_planner_enabled 开关分组，
输出 p50/p95、LLM 调用数、工具调用数对比表。

用法：
    python -m plugin.personification.scripts.metrics_baseline \\
        --metrics data/metrics.jsonl --output baseline_report.md
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


def _parse_metrics_file(path: str) -> list[dict[str, Any]]:
    file_path = Path(path)
    if not file_path.exists():
        return []
    records: list[dict[str, Any]] = []
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                records.append(data)
    return records


def _group_key(record: dict[str, Any]) -> str:
    tags = record.get("tags", {}) or {}
    enabled = bool(tags.get("turn_planner_enabled", False))
    return "new (planner_enabled)" if enabled else "old (planner_disabled)"


def aggregate_latency(records: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    groups: dict[str, list[float]] = defaultdict(list)
    for r in records:
        if r.get("metric") != "agent.turn_latency_ms":
            continue
        value = r.get("value")
        if not isinstance(value, (int, float)):
            continue
        groups[_group_key(r)].append(float(value))

    result: dict[str, dict[str, float]] = {}
    for group, values in groups.items():
        if not values:
            continue
        values.sort()
        result[group] = {
            "count": len(values),
            "p50": statistics.median(values),
            "p95": values[int(len(values) * 0.95)] if len(values) >= 20 else values[-1],
            "avg": statistics.mean(values),
            "max": max(values),
        }
    return result


def aggregate_llm_calls(records: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    groups: dict[str, list[int]] = defaultdict(list)
    for r in records:
        if r.get("metric") != "agent.llm_calls_per_turn":
            continue
        value = r.get("value")
        if not isinstance(value, (int, float)):
            continue
        groups[_group_key(r)].append(int(value))

    result: dict[str, dict[str, float]] = {}
    for group, values in groups.items():
        if not values:
            continue
        result[group] = {
            "count": len(values),
            "avg_calls": round(statistics.mean(values), 2),
            "max_calls": max(values),
            "over_budget": sum(1 for v in values if v > 4),
        }
    return result


def aggregate_tool_calls(records: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for r in records:
        metric = r.get("metric", "")
        if metric == "agent.tool_ok_total":
            tags = r.get("tags", {}) or {}
            tool = str(tags.get("tool", "") or "unknown")
            counts[tool] += int(r.get("value", 1) or 1)
    return dict(counts)


def render_report(
    latency: dict[str, dict[str, float]],
    llm_calls: dict[str, dict[str, float]],
    tool_calls: dict[str, int],
) -> str:
    lines: list[str] = [
        "# 拟人插件性能基线对照",
        "",
        "## 端到端延迟（agent.turn_latency_ms）",
        "",
        "| 组 | 样本数 | p50 (ms) | p95 (ms) | avg (ms) | max (ms) |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for group, stats in sorted(latency.items()):
        lines.append(
            f"| {group} | {stats['count']} | {stats['p50']:.0f} | {stats['p95']:.0f} | "
            f"{stats['avg']:.0f} | {stats['max']:.0f} |"
        )
    if not latency:
        lines.append("| _无数据_ | | | | | |")

    lines.extend(["", "## 每回合 LLM 调用数（agent.llm_calls_per_turn）", ""])
    lines.append("| 组 | 样本数 | 平均调用 | 最大调用 | 超预算 (>4) 次数 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for group, stats in sorted(llm_calls.items()):
        lines.append(
            f"| {group} | {stats['count']} | {stats['avg_calls']} | "
            f"{stats['max_calls']} | {stats['over_budget']} |"
        )
    if not llm_calls:
        lines.append("| _无数据_ | | | | |")

    lines.extend(["", "## 工具调用统计（agent.tool_ok_total）", "", "| 工具 | 调用次数 |", "| --- | --- |"])
    if tool_calls:
        for tool, count in sorted(tool_calls.items(), key=lambda x: -x[1])[:20]:
            lines.append(f"| {tool} | {count} |")
    else:
        lines.append("| _无数据_ | |")

    lines.extend([
        "",
        "## 验收门槛",
        "",
        "- 端到端 p95：planner_enabled 不应超过 planner_disabled 的 115%，绝对增量 ≤ 1500ms",
        "- 每回合 LLM 调用：banter ≤ 3，lookup/research ≤ 4",
        "- 超预算样本占比 < 5%",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="拟人插件性能基线对照")
    parser.add_argument("--metrics", default="data/metrics.jsonl", help="metrics jsonl 文件路径")
    parser.add_argument("--output", default="-", help="输出报表路径，'-' 表示打印到 stdout")
    args = parser.parse_args()

    records = _parse_metrics_file(args.metrics)
    if not records:
        print(f"[warn] 未在 {args.metrics} 找到 metrics 记录", file=sys.stderr)

    latency = aggregate_latency(records)
    llm_calls = aggregate_llm_calls(records)
    tool_calls = aggregate_tool_calls(records)
    report = render_report(latency, llm_calls, tool_calls)

    if args.output == "-" or args.output == "":
        print(report)
    else:
        Path(args.output).write_text(report, encoding="utf-8")
        print(f"报表已写入 {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
