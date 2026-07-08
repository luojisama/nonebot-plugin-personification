from __future__ import annotations

from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent
_PACKAGE_ROOT = _REPO_ROOT / "nonebot_plugin_personification"
_PLUGIN_ROOT = _PACKAGE_ROOT if _PACKAGE_ROOT.exists() else _REPO_ROOT


def test_search_keyword_fallback_files_have_explicit_boundaries() -> None:
    expected = {
        "agent/query_rewriter.py": "personification-semantic-boundary: search-query-rewrite-only",
        "core/web_grounding.py": "personification-semantic-boundary: grounding-context-only",
    }

    for rel_path, marker in expected.items():
        text = (_PLUGIN_ROOT / rel_path).read_text(encoding="utf-8")
        assert marker in text
        assert "must not" in text
        assert "emotion" in text
