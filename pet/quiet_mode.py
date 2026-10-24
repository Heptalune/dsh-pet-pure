# -*- coding: utf-8 -*-
"""免打扰（issue #208）：进程级状态 + 暂存队列 + 到期汇报。

设计要点（对应 issue #208 的功能规格）：

- **进程级单一状态**：所有 sprite / 所有窗口共享同一个 ``QuietState``（本模块的
  单例 ``shared_quiet_state()``）。多宠时"免打扰"是用户对整个应用的意图，不是
  逐只偏好——所以既不挂 PetWindow/sprite，也不落盘（运行态）。
- **两种进入方式、一个判据**：手动（右键菜单四档时长）与"全屏应用等效免打扰"
  （``quiet_also_on_fullscreen``）。是否处于免打扰只由 ``suppression_active()``
  一处回答，消费方（``window_alerts.quiet_blocked``）不再各自读标志——这就是
  issue 要求的"单点收口"。
- **状态是纯逻辑、模块是 Qt 层**：倒计时 / 档位 / 暂存队列都在 ``QuietState`` 上，
  它自身不碰 Qt，注入时钟即可在无 Qt 环境独立测试；但**本模块整体不是零 Qt 模块**
  （顶层 import ``PySide6.QtCore``，:class:`QuietTicker` 就是 QObject + QTimer），
  所以它**不在** ``tests/test_architecture.py`` 的「纯逻辑模块零 Qt」名单里。Qt 侧只有
  :class:`QuietTicker` 这一个 1Hz 轮询表，且**只在免打扰生效期间才跑**（不新增常驻
  高频表）。

与既有"全屏避让"的关系：全屏避让（``auto_hide_fullscreen`` → 隐藏桌宠）继续管
**呈现**，本模块管**提醒**——两者共用同一份全屏信号（不另起探测），语义不重叠：
全屏期间普通提醒不再被丢弃/改道，而是进暂存队列，退出全屏后统一汇报。
"""

from __future__ import annotations

import logging
import time

from PySide6.QtCore import QObject, QTimer, Signal

from .config import (
    DEFAULT_QUIET_ALSO_ON_FULLSCREEN,
    DEFAULT_QUIET_MINUTES,
    QUIET_QUEUE_LIMIT,
    normalize_quiet_minutes,
)

log = logging.getLogger("dsh-pet-standalone")

#: 汇报气泡里摘录单条提醒文案时的截断长度（过长会撑爆气泡）。
QUIET_TEXT_LIMIT = 60

#: 免打扰结束汇报气泡的展示时长（毫秒）。
QUIET_SUMMARY_MS = 9000

#: Qt 轮询表的节拍（毫秒）。1Hz 足够——到期判定是"秒级"语义，汇报晚一拍无感。
TICK_INTERVAL_MS = 1000


def format_remaining(seconds: float) -> str:
    """剩余时长的菜单文案（``12:34`` / ``1:02:03``）。"""
    total = int(max(0.0, float(seconds)))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def quiet_duration_label(minutes: int) -> str:
    """档位文案（分钟 / 整小时）——右键菜单与设置页共用一份，避免两处漂移。"""
    value = int(minutes)
    if value % 60 == 0:
        return f"{value // 60} 小时"
    return f"{value} 分钟"


class QuietState:
    """免打扰的进程级纯状态：倒计时 + 全屏等效标志 + 暂存队列。

    ``clock`` 可注入（测试用假钟推进到期，不 sleep、不赌真实时序）。
    """

    def __init__(self, *, clock=time.monotonic, queue_limit: int = QUIET_QUEUE_LIMIT,
                 text_limit: int = QUIET_TEXT_LIMIT) -> None:
        self._clock = clock
        self._queue_limit = max(1, int(queue_limit))
        self._text_limit = max(1, int(text_limit))
        self._until: float | None = None
        self._minutes = 0
        self._fullscreen = False
        self._queue: list[str] = []
        # 「自上次结算以来进过免打扰」——到期/退出全屏的**边沿**由它判定。
        # 入场路径（start / set_fullscreen(True)）就置位，不靠轮询表第一拍去补：
        # 首个 1Hz tick 之前就退出全屏时，边沿必须是已经锁好的（否则暂存留着
        # 永远不汇报）。
        self._entered = False
        # 已发出待交付汇报、尚未确认交付。交付确认前不排空、不停表。
        self._awaiting_ack = False
        self._listeners: list = []

    # ---------------------------------------------------------------- 监听
    def add_listener(self, callback) -> None:
        """订阅状态边沿（进入/退出免打扰、全屏翻转）。Qt 侧据此起停轮询表。"""
        if callback not in self._listeners:
            self._listeners.append(callback)

    def remove_listener(self, callback) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    def _notify(self) -> None:
        for callback in list(self._listeners):
            try:
                callback()
            except Exception:
                # 订阅方（Qt 轮询表）自毁不该把状态机带走：免打扰本身照常生效，
                # 只是到期后的汇报少了那一拍。
                log.debug("免打扰状态订阅者失败", exc_info=True)

    # ---------------------------------------------------------------- 手动免打扰
    def start(self, minutes=None) -> float:
        """开启（或重新计时）免打扰，返回本次时长（秒）。

        已在免打扰中再次选择档位 = **重新计时**（用户改主意要再战一小时），
        不叠加、不复用剩余。

        入场即锁定边沿（``_entered``）：不能等轮询表第一拍——首拍之前就结束
        （提前结束 / 退出全屏）时，那一轮暂存提醒必须仍然汇报得出去。
        """
        span = normalize_quiet_minutes(
            DEFAULT_QUIET_MINUTES if minutes is None else minutes
        ) * 60.0
        self._minutes = int(span // 60)
        self._until = self._clock() + span
        self._entered = True
        self._notify()
        return span

    def manual_active(self) -> bool:
        """手动免打扰是否生效（到点即自我结束，不需要外部推它）。"""
        if self._until is None:
            return False
        if self._clock() >= self._until:
            self._until = None
            return False
        return True

    def started_minutes(self) -> int:
        """当前（或刚结束的）这一轮手动免打扰的档位；未开启过为 0。"""
        return self._minutes if self._until is not None else 0

    def remaining_seconds(self) -> float:
        if self._until is None:
            return 0.0
        return max(0.0, self._until - self._clock())

    def remaining_label(self) -> str:
        return format_remaining(self.remaining_seconds())

    def set_fullscreen(self, on: bool) -> None:
        """全屏状态翻转（唯一来源 = 既有全屏 watcher，不另起探测）。

        只记标志：是否**等效免打扰**由 ``suppression_active`` 结合配置键判断，
        本方法不替调用方决定（``quiet_also_on_fullscreen`` 关掉时全屏不该抑制）。

        进入全屏时立刻锁定边沿（同 ``start``）：首个轮询拍之前就退出全屏，
        暂存的提醒也必须汇报得出去。
        """
        on = bool(on)
        if on == self._fullscreen:
            return
        self._fullscreen = on
        if on:
            self._entered = True
        self._notify()

    def fullscreen(self) -> bool:
        return self._fullscreen

    # ---------------------------------------------------------------- 判据
    def suppression_active(self, *, also_on_fullscreen: bool = DEFAULT_QUIET_ALSO_ON_FULLSCREEN) -> bool:
        """当前是否处于免打扰（手动 **或** 全屏等效）——全应用唯一的判据。"""
        if self.manual_active():
            return True
        return bool(also_on_fullscreen) and self._fullscreen

    def take_report(self, *, also_on_fullscreen: bool = DEFAULT_QUIET_ALSO_ON_FULLSCREEN) -> list[str] | None:
        """取出待汇报的暂存提醒**快照**（不排空）；无待汇报返回 ``None``。

        **交付确认制**：排空只发生在 :meth:`ack_report`，调用方必须先确认这条
        汇报真的交付出去了。先排空再交付是不允许的——退出全屏的那一拍里窗口
        往往还没恢复显示，呈现代理筛不到可见目标，先排空就等于把提醒静默丢掉
        且不可恢复。

        幂等：在 :meth:`ack_report` 之前反复调用返回同一份快照，轮询表据此重试
        直到有可交付目标。
        """
        if self.suppression_active(also_on_fullscreen=also_on_fullscreen):
            self._entered = True
            return None
        if not self._entered:
            return None
        self._awaiting_ack = True
        return list(self._queue)

    def ack_report(self) -> None:
        """汇报已交付（或确认这一轮本就无事可报）：结束这一轮并排空暂存。"""
        self._entered = False
        self._awaiting_ack = False
        self._queue = []

    def report_pending(self) -> bool:
        """是否已结算但尚未确认交付（轮询表据此保持重试，不得提前停表）。"""
        return self._awaiting_ack

    def end_now(self) -> bool:
        """提前结束手动免打扰；返回是否本来在免打扰中。

        **不动暂存队列、不排空、不发汇报**：结束计时只是让"免打扰不再生效"，
        汇报统一走 ``take_report`` → 交付确认 → ``ack_report``，与到期、退出全屏
        是同一条链（否则两条路径会各丢一次或各报一次）。
        """
        was = self.manual_active()
        self._until = None
        if was:
            self._entered = True
        return was

    # ---------------------------------------------------------------- 暂存队列
    def remember(self, text: str) -> None:
        """把一条被抑制的普通提醒存入暂存队列；溢出丢**最旧**（规格）。"""
        value = str(text or "").strip()[: self._text_limit]
        if not value:
            return
        self._queue.append(value)
        if len(self._queue) > self._queue_limit:
            del self._queue[: len(self._queue) - self._queue_limit]

    def pending_count(self) -> int:
        return len(self._queue)

    def drain(self) -> list[str]:
        """直接排空暂存（低层原语：测试与确认交付后的收口用）。"""
        items = self._queue
        self._queue = []
        return items

    # ---------------------------------------------------------------- 测试收口
    def reset(self) -> None:
        """恢复出厂（测试收口 / 进程级重来）：不动监听表。"""
        self._until = None
        self._minutes = 0
        self._fullscreen = False
        self._entered = False
        self._awaiting_ack = False
        self._queue = []


_SHARED_STATE: QuietState | None = None


def shared_quiet_state() -> QuietState:
    """进程级唯一免打扰状态（多宠共享一份，见模块 docstring）。"""
    global _SHARED_STATE
    if _SHARED_STATE is None:
        _SHARED_STATE = QuietState()
    return _SHARED_STATE


def reset_shared_quiet_state() -> None:
    """测试收口：把进程级状态恢复到未开启免打扰。"""
    shared_quiet_state().reset()


def note_fullscreen(hit: bool) -> None:
    """全屏状态翻转的进程级入口（GUI 线程调用）。

    唯一调用点 = 既有全屏避让的两条消费路径（``AppShell._on_shared_fullscreen`` /
    ``OverlayShell._on_fullscreen_changed``）——刻意不另起探测，也不在这里判定
    ``quiet_also_on_fullscreen``（那是读判据时的事，见 ``suppression_active``）。
    """
    shared_quiet_state().set_fullscreen(hit)


class QuietTicker(QObject):
    """免打扰的 1Hz 轮询表：只在免打扰生效期间跑，结束时汇报暂存提醒。

    为什么需要它：到期自动结束与"退出全屏即结束"都是**时间/外部输入的边沿**，
    没有事件可挂——但它也只在免打扰期间存在（``_on_state_changed`` 里按需起停），
    空闲时零开销。

    线程纪律：``ended`` 由 GUI 线程的 QTimer 发射，订阅方（AppShell）在此直接
    做气泡汇报；``set_fullscreen`` 也必须在 GUI 线程调用（调用点见
    ``AppShell._on_shared_fullscreen`` / ``OverlayShell._on_fullscreen_changed``）。
    """

    ended = Signal(list)

    def __init__(self, *, also_on_fullscreen=None, state: QuietState | None = None,
                 interval_ms: int = TICK_INTERVAL_MS, parent=None) -> None:
        super().__init__(parent)
        self._state = state if state is not None else shared_quiet_state()
        self._also_on_fullscreen = also_on_fullscreen or (lambda: DEFAULT_QUIET_ALSO_ON_FULLSCREEN)
        self._timer = QTimer(self)
        self._timer.setInterval(int(interval_ms))
        self._timer.timeout.connect(self.poll)
        self._state.add_listener(self._on_state_changed)

    def _also(self) -> bool:
        try:
            return bool(self._also_on_fullscreen())
        except Exception:
            return DEFAULT_QUIET_ALSO_ON_FULLSCREEN

    def _on_state_changed(self) -> None:
        """状态边沿：进入免打扰 → 起表；退出（全屏解除）→ 立刻结算一次。"""
        if self._state.suppression_active(also_on_fullscreen=self._also()):
            if not self._timer.isActive():
                self._timer.start()
            return
        # 刚退出免打扰（退出全屏；到点由轮询表自己发现）：立刻结算一次，不等
        # 下一拍。两个 note_fullscreen 调用点都把本通知放在「恢复可见性之后」
        # （见 ``AppShell._on_shared_fullscreen`` / ``OverlayShell._on_fullscreen_changed``
        # 的落点）——代理只投向可见窗，窗口还没回来就结算等于汇报无处可送。
        self.poll()

    def poll(self) -> None:
        """结算一次：仍在免打扰 → 什么都不做；刚结束 → 发射 ``ended``。

        汇报**未确认交付**（订阅方没 ``ack_report``）时保持重试：暂存不清空、
        表不停。这样即使某一拍交付失败（此刻没有可见目标），提醒也只是晚一点
        到，而不是消失。
        """
        items = self._state.take_report(also_on_fullscreen=self._also())
        if not self._state.suppression_active(also_on_fullscreen=self._also()) \
                and not self._state.report_pending():
            self._timer.stop()
        if items is not None:
            self.ended.emit(list(items))

    def active(self) -> bool:
        return self._timer.isActive()

    def stop(self) -> None:
        """进程退出收口：停表并摘掉监听（幂等）。"""
        self._timer.stop()
        self._state.remove_listener(self._on_state_changed)
