from ._loader import load_personification_module


def test_plugin_homepage_points_to_release_repository() -> None:
    plugin_meta = load_personification_module("plugin.personification.core.plugin_meta")

    metadata = plugin_meta.build_plugin_metadata(object)

    assert metadata.homepage == "https://github.com/luojisama/nonebot-plugin-shiro-personification"
