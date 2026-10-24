# -*- coding: utf-8 -*-
"""「快速识屏」会话状态机与选区几何（纯逻辑，无 Qt）。

形态（设计定稿）：真截屏体验 = 触发 → 隐藏自家所有窗口 → 冻结一帧底图 →
手动框选（Esc 取消 / Enter 确认）→ 只对选区抓图。本模块只管两件事：
「走到哪一步了」与「选区是哪个矩形」，**不碰 Qt**——Qt 侧分别是
``host.py``（隐藏/恢复自家顶层窗口）、``pixels.py``（抓帧/裁剪/sRGB）、
``overlay.py``（框选覆盖层），宿主编排在 ``controller.py``。

状态图（每一步都不可跳）：

    idle --start--> hiding --hidden--> frozen --begin_selection--> selecting
                                                                      |
                                            +-------------------------+
                                            v                         v
                                        captured                  cancelled
                                            +------ restore ---------+
                                                      |
                                                 restoring --settle--> idle

为什么状态机要这么严：隐藏与恢复分处两端，中间任何一步静默失败都会留下
「窗口藏了没还」的终态（用户的桌宠凭空消失）。因此非法迁移一律抛
``IllegalTransition`` 且**不改动当前状态**——错了就要在调用点被看见，而不是
被吞掉后表现在"桌宠不见了"上。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "CapturePhase",
    "CaptureSession",
    "IllegalTransition",
    "SelectionRect",
    "normalize_drag",
]


class CapturePhase(str, Enum):
    """采集会话的单一状态源。"""

    IDLE = "idle"
    HIDING = "hiding"
    FROZEN = "frozen"
    SELECTING = "selecting"
    CAPTURED = "captured"
    CANCELLED = "cancelled"
    RESTORING = "restoring"


class IllegalTransition(RuntimeError):
    """非法迁移：当前阶段不接受该动作。

    ``phase`` 是拒绝时的阶段、``action`` 是被拒的动作名；会话状态保持不变，
    调用方要么修调用点，要么先走合法路径。
    """

    def __init__(self, phase: CapturePhase, action: str) -> None:
        self.phase = phase
        self.action = action
        super().__init__(f"capture session cannot {action} in phase {phase.value}")


@dataclass(frozen=True)
class SelectionRect:
    """逻辑像素（DPR 前的屏幕坐标）下的选区，``x/y`` 为左上角、宽高非负才算有效。"""

    x: int
    y: int
    width: int
    height: int

    @property
    def is_empty(self) -> bool:
        return self.width <= 0 or self.height <= 0

    def device_rect(self, dpr: float) -> tuple[int, int, int, int]:
        """换算成设备像素 ``(left, top, width, height)``。

        左/上取 ``floor``、右/下取 ``ceil``：宁可多覆盖一像素，也不漏掉用户
        框进来的边缘（分数 DPR 下四舍五入会切掉半像素宽的一条）。
        """
        left = math.floor(self.x * dpr)
        top = math.floor(self.y * dpr)
        right = math.ceil((self.x + self.width) * dpr)
        bottom = math.ceil((self.y + self.height) * dpr)
        return left, top, right - left, bottom - top


def normalize_drag(x0: int, y0: int, x1: int, y1: int,
                   bounds: tuple[int, int] | None = None) -> SelectionRect:
    """把一次拖拽的起止点归一成选区。

    支持反向拖拽（从右下往左上拉）；``bounds`` 给定时按屏宽高钳制，越界部分
    直接裁掉（全在外面则得到空选区）。坐标为**边**语义：从 (10,10) 拖到
    (20,20) 得到 10×10，原地点击得到空选区（空选区不许进下游）。
    """
    left, right = min(x0, x1), max(x0, x1)
    top, bottom = min(y0, y1), max(y0, y1)
    if bounds is not None:
        width, height = int(bounds[0]), int(bounds[1])
        left = max(0, left)
        top = max(0, top)
        right = min(width, right)
        bottom = min(height, bottom)
    return SelectionRect(int(left), int(top),
                         max(0, int(right) - int(left)), max(0, int(bottom) - int(top)))


class CaptureSession:
    """采集会话状态机（无 Qt、无定时器、无 IO）。

    调用方法与状态的对应关系：

    - ``start()``：idle → hiding（进不去说明上一次没走完 ``settle()``）
    - ``hidden()``：hiding → frozen（自家窗口已藏好，可以冻底图了）
    - ``begin_selection()``：frozen → selecting（底图到手，覆盖层可以出来了）
    - ``confirm(selection)``：selecting → captured（非空选区才允许）
    - ``cancel(reason)``：hiding/frozen/selecting → cancelled（用户 Esc、
      抓帧失败、隐藏失败等一切"没截成"的出口，理由进 ``cancel_reason``）
    - ``restore()``：captured/cancelled → restoring
    - ``settle()``：restoring → idle（清空上一轮的选择与理由）

    并发：本类不是线程安全的，也不打算是——采集闭环全程在 GUI 线程（覆盖层
    与窗口显隐都只能在 GUI 线程做）。
    """

    def __init__(self) -> None:
        self._phase = CapturePhase.IDLE
        self._selection: SelectionRect | None = None
        self._cancel_reason: str | None = None

    # ------------------------------------------------------------ 只读视图

    @property
    def phase(self) -> CapturePhase:
        return self._phase

    @property
    def selection(self) -> SelectionRect | None:
        """已确认的选区（仅 captured 阶段非空）。"""
        return self._selection

    @property
    def cancel_reason(self) -> str | None:
        """取消理由（仅 cancelled 阶段非空）。"""
        return self._cancel_reason

    # -------------------------------------------------------------- 迁移

    def start(self) -> CapturePhase:
        self._expect(CapturePhase.IDLE, "start")
        self._selection = None
        self._cancel_reason = None
        return self._switch(CapturePhase.HIDING)

    def hidden(self) -> CapturePhase:
        self._expect(CapturePhase.HIDING, "hidden")
        return self._switch(CapturePhase.FROZEN)

    def begin_selection(self) -> CapturePhase:
        self._expect(CapturePhase.FROZEN, "begin_selection")
        return self._switch(CapturePhase.SELECTING)

    def confirm(self, selection: SelectionRect) -> CapturePhase:
        self._expect(CapturePhase.SELECTING, "confirm")
        if not isinstance(selection, SelectionRect):
            raise TypeError(f"confirm() 需要 SelectionRect，收到 {type(selection).__name__}")
        if selection.is_empty:
            raise IllegalTransition(self._phase, "confirm")
        self._selection = selection
        return self._switch(CapturePhase.CAPTURED)

    def cancel(self, reason: str) -> CapturePhase:
        allowed = (CapturePhase.HIDING, CapturePhase.FROZEN, CapturePhase.SELECTING)
        self._expect(allowed, "cancel")
        self._cancel_reason = str(reason)
        self._selection = None
        return self._switch(CapturePhase.CANCELLED)

    def restore(self) -> CapturePhase:
        self._expect((CapturePhase.CAPTURED, CapturePhase.CANCELLED), "restore")
        return self._switch(CapturePhase.RESTORING)

    def settle(self) -> CapturePhase:
        self._expect(CapturePhase.RESTORING, "settle")
        self._selection = None
        self._cancel_reason = None
        return self._switch(CapturePhase.IDLE)

    # -------------------------------------------------------------- 内部

    def _expect(self, expected: CapturePhase | tuple[CapturePhase, ...], action: str) -> None:
        if isinstance(expected, CapturePhase):
            expected = (expected,)
        if self._phase not in expected:
            raise IllegalTransition(self._phase, action)

    def _switch(self, phase: CapturePhase) -> CapturePhase:
        self._phase = phase
        return phase
