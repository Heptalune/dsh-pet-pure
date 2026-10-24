# -*- coding: utf-8 -*-
"""「快速识屏」P0 会话状态机与选区几何（纯逻辑，无 Qt）。

锁定三条契约：

1. **合法流转**：idle → hiding → frozen → selecting → captured/cancelled
   → restoring → idle，顺序不可跳步（跳步会让"窗口已藏但底图没冻"这类
   中间态对覆盖层可见）。
2. **非法迁移一律抛 IllegalTransition 且状态不变**——静默容忍会让隐藏侧
   与恢复侧失去同步（本片最贵的 bug 形态是"窗口藏了没还"）。
3. **选区几何**：反向拖拽归一、越界钳制、逻辑 → 设备像素换算（分数 DPR
   用 floor/ceil 包住选区，宁可多覆盖一像素也不漏）。
"""

from __future__ import annotations

import pytest

from pet.screen_capture.session import (
    CapturePhase,
    CaptureSession,
    IllegalTransition,
    SelectionRect,
    normalize_drag,
)


# ------------------------------------------------------------------ 合法流转


def test_happy_path_visits_every_phase_in_order():
    session = CaptureSession()
    assert session.phase is CapturePhase.IDLE

    assert session.start() is CapturePhase.HIDING
    assert session.hidden() is CapturePhase.FROZEN
    assert session.begin_selection() is CapturePhase.SELECTING

    selection = SelectionRect(10, 20, 30, 40)
    assert session.confirm(selection) is CapturePhase.CAPTURED
    assert session.selection == selection

    assert session.restore() is CapturePhase.RESTORING
    assert session.settle() is CapturePhase.IDLE
    assert session.selection is None
    assert session.cancel_reason is None


def test_cancel_path_goes_through_restoring():
    session = CaptureSession()
    session.start()
    session.hidden()
    session.begin_selection()

    assert session.cancel("esc") is CapturePhase.CANCELLED
    assert session.cancel_reason == "esc"
    assert session.selection is None

    assert session.restore() is CapturePhase.RESTORING
    assert session.settle() is CapturePhase.IDLE
    assert session.cancel_reason is None


def test_cancel_is_allowed_from_hiding_and_frozen():
    """隐藏或冻结阶段出故障也必须能收口（否则窗口永久藏住）。"""
    for steps in ((), ("hidden",), ("hidden", "begin_selection")):
        session = CaptureSession()
        session.start()
        for step in steps:
            getattr(session, step)()
        assert session.cancel("grab-failed") is CapturePhase.CANCELLED
        session.restore()
        assert session.settle() is CapturePhase.IDLE


def test_restart_after_settle_clears_previous_result():
    session = CaptureSession()
    session.start()
    session.hidden()
    session.begin_selection()
    session.confirm(SelectionRect(1, 2, 3, 4))
    session.restore()
    session.settle()

    session.start()
    assert session.phase is CapturePhase.HIDING
    assert session.selection is None
    assert session.cancel_reason is None


# ---------------------------------------------------------------- 非法迁移


def _drive_to(phase: CapturePhase) -> CaptureSession:
    """把会话推到指定阶段（只走公开 API）。"""
    session = CaptureSession()
    if phase is CapturePhase.IDLE:
        return session
    session.start()
    if phase is CapturePhase.HIDING:
        return session
    session.hidden()
    if phase is CapturePhase.FROZEN:
        return session
    session.begin_selection()
    if phase is CapturePhase.SELECTING:
        return session
    if phase is CapturePhase.CAPTURED:
        session.confirm(SelectionRect(0, 0, 5, 5))
        return session
    if phase is CapturePhase.CANCELLED:
        session.cancel("esc")
        return session
    if phase is CapturePhase.RESTORING:
        session.cancel("esc")
        session.restore()
        return session
    raise AssertionError(f"不可达阶段 {phase}")


# (阶段, 该阶段非法的动作) —— 全组合穷举，不留"没测到"的缝
_ILLEGAL_ACTIONS = [
    (CapturePhase.IDLE, "hidden"),
    (CapturePhase.IDLE, "begin_selection"),
    (CapturePhase.IDLE, "confirm"),
    (CapturePhase.IDLE, "cancel"),
    (CapturePhase.IDLE, "restore"),
    (CapturePhase.IDLE, "settle"),
    (CapturePhase.HIDING, "start"),
    (CapturePhase.HIDING, "begin_selection"),
    (CapturePhase.HIDING, "confirm"),
    (CapturePhase.HIDING, "restore"),
    (CapturePhase.HIDING, "settle"),
    (CapturePhase.FROZEN, "start"),
    (CapturePhase.FROZEN, "hidden"),
    (CapturePhase.FROZEN, "confirm"),
    (CapturePhase.FROZEN, "restore"),
    (CapturePhase.FROZEN, "settle"),
    (CapturePhase.SELECTING, "start"),
    (CapturePhase.SELECTING, "hidden"),
    (CapturePhase.SELECTING, "begin_selection"),
    (CapturePhase.SELECTING, "restore"),
    (CapturePhase.SELECTING, "settle"),
    (CapturePhase.CAPTURED, "start"),
    (CapturePhase.CAPTURED, "hidden"),
    (CapturePhase.CAPTURED, "begin_selection"),
    (CapturePhase.CAPTURED, "confirm"),
    (CapturePhase.CAPTURED, "cancel"),
    (CapturePhase.CAPTURED, "settle"),
    (CapturePhase.CANCELLED, "start"),
    (CapturePhase.CANCELLED, "hidden"),
    (CapturePhase.CANCELLED, "begin_selection"),
    (CapturePhase.CANCELLED, "confirm"),
    (CapturePhase.CANCELLED, "cancel"),
    (CapturePhase.CANCELLED, "settle"),
    (CapturePhase.RESTORING, "start"),
    (CapturePhase.RESTORING, "hidden"),
    (CapturePhase.RESTORING, "begin_selection"),
    (CapturePhase.RESTORING, "confirm"),
    (CapturePhase.RESTORING, "cancel"),
    (CapturePhase.RESTORING, "restore"),
]


@pytest.mark.parametrize("phase,action", _ILLEGAL_ACTIONS)
def test_illegal_transition_is_rejected_and_leaves_phase_unchanged(phase, action):
    session = _drive_to(phase)
    args = {"confirm": (SelectionRect(0, 0, 4, 4),), "cancel": ("esc",)}.get(action, ())

    with pytest.raises(IllegalTransition) as err:
        getattr(session, action)(*args)

    assert err.value.phase is phase
    assert err.value.action == action
    assert session.phase is phase  # 拒绝后状态不许被改动


def test_illegal_action_names_the_phase_and_action():
    session = CaptureSession()
    with pytest.raises(IllegalTransition) as err:
        session.confirm(SelectionRect(0, 0, 1, 1))
    message = str(err.value)
    assert "idle" in message and "confirm" in message


def test_confirm_rejects_empty_selection():
    session = _drive_to(CapturePhase.SELECTING)
    with pytest.raises(IllegalTransition):
        session.confirm(SelectionRect(10, 10, 0, 0))
    assert session.phase is CapturePhase.SELECTING


def test_cancel_reason_is_required():
    session = _drive_to(CapturePhase.SELECTING)
    with pytest.raises(TypeError):
        session.cancel()


# -------------------------------------------------------------------- 选区


def test_normalize_drag_orders_reverse_drag():
    assert normalize_drag(300, 200, 100, 50) == SelectionRect(100, 50, 200, 150)
    assert normalize_drag(100, 50, 300, 200) == SelectionRect(100, 50, 200, 150)


def test_normalize_drag_clamps_to_bounds():
    assert normalize_drag(-50, -50, 30, 30, bounds=(800, 600)) == SelectionRect(0, 0, 30, 30)
    assert normalize_drag(790, 590, 1000, 1000, bounds=(800, 600)) == SelectionRect(790, 590, 10, 10)


def test_normalize_drag_without_bounds_keeps_negative_edges():
    assert normalize_drag(-10, -20, 10, 20) == SelectionRect(-10, -20, 20, 40)


def test_normalize_drag_degenerate_drag_is_empty():
    rect = normalize_drag(10, 10, 10, 10)
    assert rect == SelectionRect(10, 10, 0, 0)
    assert rect.is_empty


def test_normalize_drag_fully_outside_bounds_is_empty():
    rect = normalize_drag(900, 700, 1000, 1000, bounds=(800, 600))
    assert rect.is_empty


def test_negative_size_is_empty():
    assert SelectionRect(0, 0, -1, 5).is_empty
    assert SelectionRect(0, 0, 5, -1).is_empty
    assert not SelectionRect(0, 0, 1, 1).is_empty


@pytest.mark.parametrize(
    "rect,dpr,expected",
    [
        (SelectionRect(10, 20, 30, 40), 1.0, (10, 20, 30, 40)),
        (SelectionRect(10, 20, 30, 40), 2.0, (20, 40, 60, 80)),
        # 1.5 倍：左/上取 floor，右/下取 ceil ⇒ 设备矩形包住逻辑选区
        (SelectionRect(1, 1, 3, 1), 1.5, (1, 1, 5, 2)),
        (SelectionRect(10, 10, 10, 10), 1.25, (12, 12, 13, 13)),
        (SelectionRect(0, 0, 1, 1), 1.25, (0, 0, 2, 2)),
    ],
)
def test_device_rect_floor_ceil(rect, dpr, expected):
    assert rect.device_rect(dpr) == expected


@pytest.mark.parametrize("dpr", [1.0, 1.25, 1.5, 1.75, 2.0, 2.25])
@pytest.mark.parametrize(
    "rect",
    [
        SelectionRect(0, 0, 1, 1),
        SelectionRect(3, 7, 11, 13),
        SelectionRect(199, 199, 1, 1),
        SelectionRect(0, 0, 800, 600),
    ],
)
def test_device_rect_always_covers_logical_selection(rect, dpr):
    left, top, width, height = rect.device_rect(dpr)
    assert left <= rect.x * dpr
    assert top <= rect.y * dpr
    assert left + width >= (rect.x + rect.width) * dpr
    assert top + height >= (rect.y + rect.height) * dpr
    assert width > 0 and height > 0
