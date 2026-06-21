"""群消息分享卡片标题抽取：让转发的视频/链接/小程序卡片标题进入文本与上下文。"""
from __future__ import annotations

import json

from ._loader import load_personification_module

event_rules = load_personification_module("plugin.personification.handlers.event_rules")


def test_json_share_card_extracts_title_and_strips_tag() -> None:
    token = event_rules._extract_share_card_token(
        "json", {"data": json.dumps({"prompt": "[QQ小程序]100h岛上生存"})}
    )
    assert token == "[分享:100h岛上生存]"


def test_json_share_card_falls_back_to_meta_title() -> None:
    payload = {"meta": {"detail": {"title": "软脚虾合集", "desc": "x"}}}
    token = event_rules._extract_share_card_token("json", {"data": json.dumps(payload)})
    assert token == "[分享:软脚虾合集]"


def test_xml_share_card_extracts_brief() -> None:
    token = event_rules._extract_share_card_token("xml", {"data": '<msg brief="软脚虾" serviceID="1"/>'})
    assert token == "[分享:软脚虾]"


def test_share_card_empty_or_bad_payload() -> None:
    assert event_rules._extract_share_card_token("json", {"data": ""}) == "[分享]"
    assert event_rules._extract_share_card_token("json", {"data": "not-json"}) == "[分享]"
