from __future__ import annotations

from pathlib import Path


_PLUGIN_ROOT = Path(__file__).resolve().parent.parent


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
