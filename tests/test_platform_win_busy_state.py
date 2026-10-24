# -*- coding: utf-8 -*-
"""_fs_user_busy_state 的 QUNS 口径回归测试（2026-09-23 实机频闪案）。

实机证据：overlay 合成窗（全屏置顶分层窗）可见时，Windows
SHQueryUserNotificationState 报 QUNS_BUSY(2)——若兜底把 BUSY 当全屏，
overlay 每显每隐形成 1Hz 自激频闪（soak 日志 64 次翻转）。只有
QUNS_RUNNING_D3D_FULL_SCREEN(3) / QUNS_PRESENTATION_MODE(4) 才是真全屏信号。
"""
from __future__ import annotations

import ctypes
import sys

import pytest

from pet import platform_win


@pytest.mark.skipif(sys.platform != "win32", reason="QUNS 是 Windows API")
@pytest.mark.parametrize("state,expected", [
    (1, False),   # QUNS_NOT_PRESENT
    (2, False),   # QUNS_BUSY——不收（自身 overlay 会触发， noisy）
    (3, True),    # QUNS_RUNNING_D3D_FULL_SCREEN
    (4, True),    # QUNS_PRESENTATION_MODE
    (5, False),   # QUNS_ACCEPTS_NOTIFICATIONS
    (6, False),   # QUNS_QUIET_TIME
])
def test_busy_state_quns_values(monkeypatch, state, expected):
    def fake_quns(buf):
        ctypes.cast(buf, ctypes.POINTER(ctypes.c_int)).contents.value = state
        return 0  # S_OK
    monkeypatch.setattr(ctypes.windll.shell32,
                        "SHQueryUserNotificationState", fake_quns)
    busy, raw = platform_win._fs_user_busy_state()
    assert busy is expected
    assert raw == state
