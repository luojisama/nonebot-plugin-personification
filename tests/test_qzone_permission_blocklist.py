"""回归测试：QZone 访问拒绝缓存 + 周期重检。"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_flow_module():
    """直接按文件加载 qzone_social_flow.py，绕开 personification __init__.py 的 NoneBot 依赖。

    qzone_social_flow.py 自身又依赖一堆 core 模块，那些会触发 NoneBot import 链。
    所以这里测试范围仅限纯函数（访问拒绝工具函数），用最小化 stub 替代依赖。
    """
    # 测试纯工具函数，直接构造一个 minimal module 复用源码片段
    # （避免完整 import 链）
    source_path = Path(__file__).parent.parent / "flows" / "qzone_social_flow.py"
    source = source_path.read_text(encoding="utf-8")
    # 提取访问拒绝相关函数（从 _PERMISSION_BLOCK_RECHECK_SECONDS 到 outcome handler 结束）
    start_marker = "# 空间访问拒绝缓存"
    end_marker = "async def recheck_qzone_access_denied_users"
    start = source.find(start_marker)
    end = source.find(end_marker)
    assert start >= 0 and end > start, f"无法定位访问拒绝函数源码: start={start} end={end}"
    snippet = source[start:end]
    # 准备最小 namespace
    import time as _time
    namespace = {
        "time": _time,
        "Any": object,
    }
    # 类型注解里用了 dict[str, Any] 等，需要 __future__ annotations 才能纯字符串解析
    exec("from __future__ import annotations\n" + snippet, namespace)
    return namespace


_FLOW = _load_flow_module()


def test_is_permission_denied_message_recognizes_chinese_hints() -> None:
    fn = _FLOW["_is_permission_denied_message"]
    assert fn("对不起,主人设置了保密,您没有权限查看") is True
    assert fn("没有权限查看") is True
    assert fn("Access Denied") is True
    assert fn("the space is private space") is True
    assert fn("") is False
    assert fn("Network error") is False
    assert fn("ok") is False


def test_access_denied_lifecycle_mark_check_clear() -> None:
    state: dict = {}
    is_blocked = _FLOW["_is_qzone_access_denied"]
    mark = _FLOW["_mark_qzone_access_denied"]
    clear = _FLOW["_clear_qzone_access_denied"]

    # 起始：未阻塞
    assert is_blocked(state, "100001") is False
    # 标记后立即被识别为阻塞
    mark(state, "100001", "对不起,主人设置了保密")
    assert is_blocked(state, "100001") is True
    bucket = state["qzone_access_denied"]
    assert "100001" in bucket
    assert bucket["100001"]["blocked_count"] == 1
    # 再次标记，blocked_count 累加
    mark(state, "100001", "仍然无权限")
    assert bucket["100001"]["blocked_count"] == 2
    # 清除后恢复
    clear(state, "100001")
    assert is_blocked(state, "100001") is False
    assert "100001" not in bucket


def test_access_denied_expires_after_recheck_interval() -> None:
    state: dict = {}
    mark = _FLOW["_mark_qzone_access_denied"]
    is_blocked = _FLOW["_is_qzone_access_denied"]
    recheck_seconds = _FLOW["_PERMISSION_BLOCK_RECHECK_SECONDS"]

    mark(state, "200002", "保密")
    entry = state["qzone_access_denied"]["200002"]
    # 把 last_checked_ts 倒拨到一周前 + 1 秒
    entry["last_checked_ts"] -= recheck_seconds + 1
    # 此时应允许重检（is_blocked=False，调用方会触发新一次 fetch）
    assert is_blocked(state, "200002") is False


def test_handle_outcome_clears_block_on_success() -> None:
    state: dict = {}
    mark = _FLOW["_mark_qzone_access_denied"]
    handle = _FLOW["_handle_qzone_fetch_outcome"]
    is_blocked = _FLOW["_is_qzone_access_denied"]

    class StubLogger:
        def info(self, *_a, **_k): pass
        def debug(self, *_a, **_k): pass
        def warning(self, *_a, **_k): pass

    mark(state, "300003", "保密")
    assert is_blocked(state, "300003") is True
    # 模拟成功 fetch，应清掉访问拒绝缓存
    triggered = handle(state=state, user_id="300003", ok=True, msg="ok", logger=StubLogger())
    assert triggered is False
    assert is_blocked(state, "300003") is False


def test_handle_outcome_marks_block_on_permission_denied_message() -> None:
    state: dict = {}
    handle = _FLOW["_handle_qzone_fetch_outcome"]
    is_blocked = _FLOW["_is_qzone_access_denied"]

    class StubLogger:
        msgs: list = []
        def info(self, msg, *a, **k): self.msgs.append(("info", msg))
        def debug(self, *_a, **_k): pass
        def warning(self, *_a, **_k): pass

    logger = StubLogger()
    triggered = handle(state=state, user_id="400004", ok=False, msg="对不起,主人设置了保密", logger=logger)
    assert triggered is True
    assert is_blocked(state, "400004") is True


def test_handle_outcome_keeps_state_for_non_permission_errors() -> None:
    state: dict = {}
    handle = _FLOW["_handle_qzone_fetch_outcome"]
    is_blocked = _FLOW["_is_qzone_access_denied"]

    class StubLogger:
        def info(self, *_a, **_k): pass
        def debug(self, *_a, **_k): pass
        def warning(self, *_a, **_k): pass

    # 网络错误不应触发访问拒绝缓存
    triggered = handle(state=state, user_id="500005", ok=False, msg="Network timeout", logger=StubLogger())
    assert triggered is False
    assert is_blocked(state, "500005") is False


def test_legacy_permission_bucket_migrates_once_to_access_denied() -> None:
    get_bucket = _FLOW["_get_access_denied_bucket"]
    state = {
        "qzone_permission_blocked": {
            "600006": {"last_checked_ts": 1, "blocked_count": 2}
        },
        "qzone_access_denied": {
            "700007": {"last_checked_ts": 2, "blocked_count": 1}
        },
    }

    bucket = get_bucket(state)

    assert set(bucket) == {"600006", "700007"}
    assert "qzone_permission_blocked" not in state
