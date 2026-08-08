from __future__ import annotations

import asyncio
import json

from ._loader import load_personification_module


compat = load_personification_module("plugin.personification.skill_runtime.compat_adapters")


def test_multi_search_engine_compat_handler_runs_all_selected_engines(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    skill_dir = tmp_path / "multi-search-engine"
    skill_dir.mkdir()
    (skill_dir / "config.json").write_text(
        json.dumps(
            {
                "engines": [
                    {"name": "Engine A", "url": "https://a.example/search?q={keyword}", "region": "global"},
                    {"name": "Engine B", "url": "https://b.example/search?q={keyword}", "region": "global"},
                ]
            }
        ),
        encoding="utf-8",
    )
    calls: list[str] = []

    async def fake_fetch(*, engine_name, query, **_kwargs):  # noqa: ANN001, ANN003, ANN202
        calls.append(engine_name)
        await asyncio.sleep(0)
        return [
            {
                "title": f"{engine_name}:{query}",
                "url": f"https://result.example/{engine_name[-1].lower()}",
                "snippet": "ok",
                "source": engine_name,
                "engine": engine_name,
                "type": "web_result",
            }
        ]

    monkeypatch.setattr(compat, "_fetch_search_results", fake_fetch)
    tools = compat.build_compat_tools(
        skill_dir=skill_dir,
        frontmatter={"name": "multi_search_engine", "description": "test"},
        runtime=None,
    )

    payload = json.loads(asyncio.run(tools[0].handler(query="agent runtime", region="global", limit=5)))

    assert payload["ok"] is True
    assert payload["skill"] == "multi_search_engine"
    assert {item["engine"] for item in payload["results"]} == {"Engine A", "Engine B"}
    assert calls == ["Engine A", "Engine B"]
