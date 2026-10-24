# -*- coding: utf-8 -*-
"""原生事件过滤器的消息读取开销（O2）：先窄读 message 字段，只在命中会话消息时才整结构解析。

背景：``SessionWatcher.nativeEventFilter`` 对**每一条** Windows 消息都会被调用，
旧实现每条都 ``string_at(48B)`` + ``from_buffer_copy(48B)`` 才拿到 ``message``
字段。大多数消息（WM_PAINT/WM_MOUSEMOVE…）与关机无关，这次拷贝纯属浪费。

要求：偏移由 ctypes 结构体（``_WinMsg.message.offset``）算出而不是硬编码；
非会话消息只读 4 字节即返回；确认是会话消息后才做完整解析。

纪律：真 ``wintypes.MSG`` 结构 + ``ctypes.addressof``（与
``tests/test_session_end_ffmpeg_guard.py`` 同款，结构体保活到断言结束）。
"""
from __future__ import annotations

import ctypes
import timeit
from ctypes import wintypes

from pet import session_watcher as session_watcher_mod

WM_PAINT = 0x000F


def _msg_pointer(message: int):
    """构造真实 MSG 结构并返回 (地址, 结构体)；结构体需由调用方保活。"""
    msg = wintypes.MSG()
    msg.hwnd = 0
    msg.message = message
    msg.wParam = 0
    msg.lParam = 0
    msg.time = 0
    msg.pt.x = 0
    msg.pt.y = 0
    return ctypes.addressof(msg), msg


def _count_string_at(monkeypatch):
    """记账 ctypes.string_at 调用（整结构拷贝的唯一入口）。"""
    calls: list = []
    real = ctypes.string_at

    def _recording(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(session_watcher_mod.ctypes, "string_at", _recording)
    return calls


def test_message_offset_comes_from_struct_layout():
    """偏移必须由 ctypes 结构体算出：与真实 MSG 布局一致，不许硬编码。"""
    offset = session_watcher_mod._MSG_MESSAGE_OFFSET
    assert offset == session_watcher_mod._WinMsg.message.offset
    assert offset == wintypes.MSG.message.offset


def test_non_session_message_skips_full_struct_copy(monkeypatch):
    """非会话消息（绝大多数）：只读 message 字段，不做 48 字节拷贝。"""
    calls = _count_string_at(monkeypatch)
    addr, keepalive = _msg_pointer(WM_PAINT)
    assert session_watcher_mod.session_end_reason(addr) is None
    assert calls == [], "非会话消息不得整结构拷贝（每条消息都会走这条路）"
    assert keepalive is not None


def test_session_message_still_parses_full_struct(monkeypatch):
    """会话消息：仍做完整解析（wParam/lParam 留痕），判定不受解析影响。"""
    calls = _count_string_at(monkeypatch)
    addr, keepalive = _msg_pointer(session_watcher_mod.WM_QUERYENDSESSION)
    assert session_watcher_mod.session_end_reason(addr) == "native_query_end_session"
    assert len(calls) == 1, "确认是会话消息后才整结构解析一次"
    assert calls[0][1] == ctypes.sizeof(session_watcher_mod._WinMsg)
    assert keepalive is not None


def test_narrow_read_matches_full_parse_for_all_session_messages():
    for message, reason in ((session_watcher_mod.WM_QUERYENDSESSION,
                             "native_query_end_session"),
                            (session_watcher_mod.WM_ENDSESSION,
                             "native_end_session")):
        addr, keepalive = _msg_pointer(message)
        assert session_watcher_mod.session_end_reason(addr) == reason
        assert keepalive is not None


def test_narrow_read_is_cheaper_than_full_parse():
    """微基准（同进程交替测 5 轮取最小值）：窄读必须快于整结构解析。

    实测比值 ≈ 0.3（本机），min-of-5 对负载噪声不敏感；绝对数字见
    ``.scratch/windows-parity-20260926-a/fix-20260927-O2/bench_visibility.py``。
    """
    addr, keepalive = _msg_pointer(WM_PAINT)
    size = ctypes.sizeof(session_watcher_mod._WinMsg)
    offset = session_watcher_mod._MSG_MESSAGE_OFFSET

    def narrow():
        return int(ctypes.c_uint.from_address(addr + offset).value)

    def full():
        return session_watcher_mod._WinMsg.from_buffer_copy(
            ctypes.string_at(addr, size)).message_id()

    iterations = 2000
    narrow_min = min(timeit.repeat(narrow, number=iterations, repeat=5))
    full_min = min(timeit.repeat(full, number=iterations, repeat=5))
    assert narrow_min < full_min, (
        f"窄读必须快于整结构解析：narrow={narrow_min:.6f}s full={full_min:.6f}s")
    assert keepalive is not None
