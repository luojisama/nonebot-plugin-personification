from pathlib import Path


def test_tool_creator_view_is_lazy_loaded_and_stops_polling() -> None:
    root = Path(__file__).resolve().parents[1]
    core = (root / "webui" / "static" / "app-core.js").read_text(encoding="utf-8")
    app = (root / "webui" / "static" / "app-tool-creator.js").read_text(encoding="utf-8")

    assert 'tool_creator:"app-tool-creator.js"' in core
    assert "navItem('tool_creator','创建工具'" in core
    assert 'state.view === "tool_creator"' in core
    assert "stopToolCreatorPolling" in core
    assert "TOOL_CREATOR_ACTIVE" in app
    assert "data-tool-creator-answer" in app
    assert "artifact_digest:task.artifact_digest" in app
    assert "__personificationToolCreatorEvents" in app
