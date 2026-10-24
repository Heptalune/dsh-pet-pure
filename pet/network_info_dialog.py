# -*- coding: utf-8 -*-
"""右键菜单「网络信息」弹窗：公网 IP / IP 属地 / 外网连通性。

形态与观感沿用现有小窗（气泡 / 快速对话 / 待办面板）的既有视觉语言：卡片 12px
圆角 1px 边框、13px 正文 / 12px 提示、字段与按钮 7px 圆角、强调色 #0a84ff、明暗
跟随系统调色板亮度（令牌口径见 ``.agents/skills/desktop-pet-ui-style/``）。
不发明新视觉语言。

数据口径（与 ``pet/network_info.py`` 一致）：

- **打开时取一次**（约 1KB），不轮询；只有用户点「重新获取」才再取一次。
- 取数在后台线程，失败如实显示「获取失败」＋原因，绝不静默显示空值。
- 属地按**当前出口 IP** 判定：开着系统代理 / VPN 时显示的是出口节点位置
  （见 ``docs/NETWORK-PROXY-AND-VPN-2026-09-22.md``），文案里明说。
"""
from __future__ import annotations

import logging
import threading

import shiboken6
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import network_info as info_mod

logger = logging.getLogger(__name__)

_LOADING = "获取中…"


def _stylesheet(widget: QWidget) -> str:
    """令牌：与待办面板同一套（卡片/字段/按钮/提示），随明暗主题切换。"""
    dark = widget.palette().color(QPalette.ColorRole.Window).lightness() < 128
    if dark:
        window, card, border, divider = "#202024", "#2a2a30", "#3a3a42", "#33333a"
        text, hint, hover = "#e0e0e6", "#9a9aa3", "#303036"
    else:
        window, card, border, divider = "#fcfcfd", "#ffffff", "#e2e4e8", "#eceef1"
        text, hint, hover = "#252525", "#777777", "#f4f5f6"
    accent = "#0a84ff"
    return f"""
    QDialog, QWidget#networkInfoBody {{
        background: {window};
        color: {text};
        font-size: 13px;
    }}
    QLabel#networkInfoTitle {{
        font-size: 15px;
        font-weight: 600;
        color: {text};
        background: transparent;
    }}
    QLabel[role="label"] {{
        color: {hint};
        font-size: 12px;
    }}
    QLabel[role="value"] {{
        color: {text};
        font-size: 13px;
    }}
    QLabel#networkInfoHint {{
        color: {hint};
        font-size: 12px;
    }}
    QFrame#networkInfoCard {{
        background: {card};
        border: 1px solid {border};
        border-radius: 12px;
    }}
    QFrame[divider="true"] {{
        background: {divider};
        border: none;
        max-height: 1px;
    }}
    QPushButton {{
        background: {card};
        color: {text};
        border: 1px solid {border};
        border-radius: 7px;
        min-height: 26px;
        padding: 3px 14px;
    }}
    QPushButton:hover {{ background: {hover}; }}
    QPushButton:focus {{ border: 1px solid {accent}; }}
    QPushButton:disabled {{ color: {hint}; border-color: {divider}; }}
    QPushButton[accent="true"] {{
        background: {accent};
        color: #ffffff;
        border: 1px solid {accent};
    }}
    QPushButton[accent="true"]:hover {{ background: {accent}; }}
    QPushButton[accent="true"]:disabled {{
        background: {divider};
        color: {hint};
        border-color: {divider};
    }}
    """


class _FetchResult(QObject):
    """后台取数的落点：结果回 GUI 线程（跨线程只走这一条 queued 信号）。"""

    finished = Signal(object)


class NetworkInfoDialog(QDialog):
    """非模态小窗；打开时取一次数据，失败如实展示。"""

    def __init__(self, parent: QWidget | None = None, *, probe_factory=None) -> None:
        super().__init__(parent)
        self._probe_factory = probe_factory or info_mod.NetworkInfoProbe
        self._bridge = _FetchResult()
        self._bridge.finished.connect(self._apply_snapshot, Qt.ConnectionType.QueuedConnection)
        self._fetching = False
        self._snapshot: info_mod.NetworkInfoSnapshot | None = None

        self.setWindowTitle("网络信息")
        self.setObjectName("networkInfoDialog")
        self.setMinimumWidth(340)
        self.setStyleSheet(_stylesheet(self))
        self._build_ui()

    # ------------------------------------------------------------ 构建
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        title = QLabel("网络信息", self)
        title.setObjectName("networkInfoTitle")
        layout.addWidget(title)

        self.card = QFrame(self)
        self.card.setObjectName("networkInfoCard")
        card_layout = QGridLayout(self.card)
        card_layout.setContentsMargins(14, 12, 14, 12)
        card_layout.setHorizontalSpacing(14)
        card_layout.setVerticalSpacing(8)
        card_layout.setColumnStretch(1, 1)

        self.value_labels: dict[str, QLabel] = {}
        self._labels = (
            ("ip", "公网 IP"),
            ("place", "IP 属地"),
            ("china", "在中国大陆"),
            ("reachability", "外网连通性"),
        )
        row = 0
        for key, text in self._labels:
            label = QLabel(text, self.card)
            label.setProperty("role", "label")
            label.setWordWrap(True)
            value = QLabel(_LOADING, self.card)
            value.setProperty("role", "value")
            value.setWordWrap(True)
            value.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
                | Qt.TextInteractionFlag.TextSelectableByKeyboard)
            value.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
            card_layout.addWidget(label, row, 0, Qt.AlignmentFlag.AlignTop)
            card_layout.addWidget(value, row, 1)
            self.value_labels[key] = value
            row += 1
        layout.addWidget(self.card)

        self.hint_label = QLabel("", self)
        self.hint_label.setObjectName("networkInfoHint")
        self.hint_label.setWordWrap(True)
        layout.addWidget(self.hint_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.refresh_btn = QPushButton("重新获取", self)
        self.refresh_btn.setProperty("accent", True)
        self.refresh_btn.clicked.connect(self.refresh)
        close_btn = QPushButton("关闭", self)
        close_btn.clicked.connect(self.close)
        buttons.addWidget(self.refresh_btn)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

    # ------------------------------------------------------------ 取数
    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().showEvent(event)
        # 每次打开取一次（关掉再开＝新环境，例如刚切了代理/关掉 VPN）
        if self._snapshot is None and not self._fetching:
            self.refresh()

    def refresh(self) -> None:
        """后台线程取一次数据；取数中不重复发请求（按钮置灰）。"""
        if self._fetching:
            return
        self._fetching = True
        self.refresh_btn.setEnabled(False)
        for value in self.value_labels.values():
            value.setText(_LOADING)
        self.hint_label.setText("正在获取（每次打开只取一次，约 1KB）…")

        def worker() -> None:
            try:
                snapshot = self._probe_factory().collect()
            except Exception as exc:  # noqa: BLE001 - 后台异常必须回 GUI，不能静默
                logger.debug("网络信息：取数失败", exc_info=True)
                try:
                    self._bridge.finished.emit(exc)
                except RuntimeError:  # pragma: no cover - 窗口已销毁
                    pass
                return
            try:
                self._bridge.finished.emit(snapshot)
            except RuntimeError:  # pragma: no cover - 窗口已销毁
                pass

        threading.Thread(target=worker, daemon=True, name="pet-network-info").start()

    def _apply_snapshot(self, payload) -> None:
        """后台结果落回界面：失败如实显示原因，不静默、不留「获取中」。"""
        # 异步回调可能晚于窗口销毁（关窗/退出）：先探活再碰 Qt 对象
        if not shiboken6.isValid(self):
            return
        self._fetching = False
        self.refresh_btn.setEnabled(True)
        if not isinstance(payload, info_mod.NetworkInfoSnapshot):
            self._snapshot = None
            reason = info_mod.describe_error(payload) if isinstance(payload, BaseException) \
                else "未知错误"
            for value in self.value_labels.values():
                value.setText("获取失败")
            self.hint_label.setText(f"获取失败：{reason}")
            return

        self._snapshot = payload
        self.value_labels["ip"].setText(info_mod.ip_text(payload))
        self.value_labels["place"].setText(info_mod.place_text(payload))
        self.value_labels["china"].setText(info_mod.china_text(payload))
        detail = info_mod.reachability_detail(payload)
        summary = info_mod.reachability_text(payload)
        self.value_labels["reachability"].setText(
            f"{summary}\n{detail}" if detail else summary
        )
        lines = [
            f"数据源：{info_mod.source_text(payload)}{info_mod.source_note(payload)}",
            f"获取时间：{payload.fetched_at_text}（耗时 {payload.elapsed_s:.1f}s）",
            "属地按当前出口 IP 判定：开着代理 / VPN 时显示的是出口节点的位置。",
        ]
        failure = info_mod.error_text(payload)
        if failure:
            lines.insert(0, failure)
        self.hint_label.setText("\n".join(lines))
        self.adjustSize()

    # ------------------------------------------------------------ 测试/排查面
    @property
    def snapshot(self):
        return self._snapshot

    @property
    def fetching(self) -> bool:
        return self._fetching
