from __future__ import annotations

from pathlib import Path

from tools.personification_semantic_scan import default_target_paths, scan_paths


def test_personification_semantic_scan_current_core_modules() -> None:
    violations = scan_paths(default_target_paths(Path.cwd()))

    assert violations == []


def test_personification_semantic_scan_detects_keyword_any(tmp_path: Path) -> None:
    target = tmp_path / "bad_semantics.py"
    target.write_text(
        "\n".join(
            [
                "_MOOD_HINTS = ('开心', '难过')",
                "def decide(message_text):",
                "    if any(token in message_text for token in _MOOD_HINTS):",
                "        return 'matched'",
                "    return 'none'",
            ]
        ),
        encoding="utf-8",
    )

    violations = scan_paths([target])

    assert {item.code for item in violations} == {
        "semantic-keyword-table",
        "semantic-keyword-any",
    }
