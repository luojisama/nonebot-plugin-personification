import importlib.util
from pathlib import Path


_PLUGIN_META_PATH = (
    Path(__file__).resolve().parents[1]
    / "nonebot_plugin_personification"
    / "core"
    / "plugin_meta.py"
)


def _load_plugin_meta_module():
    spec = importlib.util.spec_from_file_location(
        "personification_source_plugin_meta",
        _PLUGIN_META_PATH,
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {_PLUGIN_META_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_plugin_metadata_keeps_store_identity() -> None:
    metadata = _load_plugin_meta_module().build_plugin_metadata(object)

    assert metadata.name == "拟人化聊天"
    assert metadata.homepage == "https://github.com/luojisama/nonebot-plugin-personification"
