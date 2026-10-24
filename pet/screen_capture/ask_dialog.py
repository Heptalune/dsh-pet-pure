# -*- coding: utf-8 -*-
"""截图问答浮窗：带图的迷你对话窗（图在输入栏上方，打字发问，可多轮）。

为什么是一个对话窗而不是"框选 → 直接发图 → 弹答案"（2026-10-14 实机反馈后
重做）：

- 一次性问答把"问什么"的权利没收了——用户框的是图，想问的却是自己的事
  （"这段报错什么意思"/"这页说的是哪天截止"），答案面不该替用户出题；
- 一层系统 Yes/No 确认框既突兀又与操作条语义重复。**浮窗本身就是确认面**：
  图躺在输入栏上方，发不发、发什么文字，都是用户亲手点「发送」那一刻决定的；
  打开窗口一个字节都不出网。

隐私不变量（与 ``vision_bridge`` 同一约定）：选区图只在 :meth:`start_with_image`
时编码成内存 JPEG（不落盘），唯一的网络出口是用户配置的视觉/聊天 API，且只在
点「发送」时发生。消息列表如实呈现每一次发出去的内容（首轮 = 图 + 问题）。

线程纪律：发送在后台线程（``VisionConversation.ask`` 同步阻塞、超时 60s 起），
结果经 Qt 信号回 GUI 线程；窗口对象只在 GUI 线程碰。
"""

from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QPoint, QSize, Qt, Signal
from PySide6.QtGui import QImage, QKeyEvent, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizeGrip,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .brand_icons import load_ds_whale
from .result_panel import _STYLES, resolve_style
from .vision_bridge import VisionConversation

log = logging.getLogger("dsh-pet-standalone")

__all__ = ["ScreenAskDialog"]

_MARGIN = 12          # 缺省落点贴屏幕右下的留白
_MIN_W, _MIN_H = 320, 360
_BUBBLE_RADIUS = 10

#: 标题栏品牌标识：固定 DS logo 直接顶掉原来的「截图问答」四字（不随机、旁边
#: 不加第二个图标位）；这四个字仍是窗口标题与无障碍名，供识别与自动化使用。
_TITLE_TEXT = "截图问答"
_TITLE_LOGO_BOX = 24   # logo 的边长（逻辑像素；实际出图按屏 DPR 放大）


class _Bubble(QLabel):
    """一条消息泡（用户靠右、AI 靠左；可选中可复制——问答窗里的文本不许只读死）。"""

    def __init__(self, text: str, role: str, colors: dict, parent=None) -> None:
        super().__init__(text, parent)
        self.setObjectName(f"screen-ask-{role}")
        # 模型输出/OCR 文本里可能带 <b>/<img> 这类 HTML 残片：一律纯文本渲染
        # （AutoText 会把本地路径图片加载出来——那是隐私面，不是样式面）
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(True)
        self.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        bg = "#1677e8" if role == "user" else colors["button"]
        fg = "#ffffff" if role == "user" else colors["text"]
        side = "right" if role == "user" else "left"
        self.setStyleSheet(
            f"QLabel {{ background: {bg}; color: {fg}; border-radius: {_BUBBLE_RADIUS}px;"
            f"  padding: 7px 10px; font-size: 13px; }}"
        )
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self.setProperty("_side", side)


class ScreenAskDialog(QFrame):
    """截图问答浮窗（每次 ``start_with_image`` 开新会话，窗口本体复用）。

    ``config`` 是宿主配置面（与 ``ChatVisionChannel`` 同一个 duck-type）；
    ``conversation_factory`` 是测试替身入口（默认 :class:`VisionConversation`）。
    """

    _reply_ready = Signal(int, str)    # 后台线程 → GUI：（会话代号, 答复）
    _reply_failed = Signal(int, str)   # 后台线程 → GUI：（会话代号, 失败理由）

    def __init__(self, config, parent: QWidget | None = None, *,
                 style: str | None = None, conversation_factory=None) -> None:
        super().__init__(parent)
        self._config = config
        self._conversation_factory = (
            conversation_factory if conversation_factory is not None else VisionConversation
        )
        self._conversation = None
        self._sending = False
        # 会话代号：换图开新会话时 +1。旧会话还在路上的回复凭代号丢弃——否则旧答案
        # 会落进新会话的消息列表、还会把新会话的发送锁解开（盲审 M2）。
        self._gen = 0
        self._context_note = ""   # 首轮随图发出的 OCR 参考文字（chip 文案要带上字数）
        self._drag_offset: QPoint | None = None
        self._user_pos: QPoint | None = None   # 用户拖过的位置（复用窗体时沿用）
        self._user_size: QSize | None = None
        self._pending_below = None             # start_with_image 带入的选区矩形

        self._style = resolve_style(style, self.palette())
        self._palette = _STYLES[self._style]
        self.setObjectName("screen-ask-dialog")
        self.setWindowTitle(_TITLE_TEXT)
        self.setFrameShape(QFrame.Shape.NoFrame)  # 卡片由 paintEvent 手画（同结果面板）
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMinimumSize(_MIN_W, _MIN_H)
        self._build()
        self._grip = QSizeGrip(self)
        self._grip.resize(16, 16)
        self._reply_ready.connect(self._on_reply)
        self._reply_failed.connect(self._on_error)

    # ---------------------------------------------------------------- 构建

    def _build(self) -> None:
        colors = self._palette
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        # 标题栏（拖拽把手：标签对鼠标透明，按下落在面板上）
        header = QHBoxLayout()
        header.setSpacing(8)
        # 标题栏就是品牌标识本身：「截图问答」四字由固定 DS logo 直接顶掉
        # （标志性文字不重复出现在 logo 旁边）；这四个字留在窗口标题与
        # 无障碍名里，供识别与自动化取用。素材缺席时退回这四个字。
        self._title = QLabel(_TITLE_TEXT, self)
        self._title.setObjectName("screen-ask-title")
        self._title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._title.setAccessibleName(_TITLE_TEXT)
        self._title_src = QPixmap()       # logo 原图（DPR 变化时按它重出）
        self._title_scaled = None         # 最近一次出图（测试可核对 DPR/物理尺寸）
        self._title_screen_wired = False  # screenChanged 一次性接线标记
        header.addWidget(self._title)
        note = QLabel("点「发送」才会把图发给你配置的 AI 服务", self)
        note.setObjectName("screen-ask-note")
        note.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        header.addWidget(note)
        header.addStretch(1)
        close_btn = QPushButton("×", self)
        close_btn.setObjectName("screen-ask-close")
        close_btn.setFixedSize(24, 24)
        close_btn.clicked.connect(self.close)
        header.addWidget(close_btn)
        layout.addLayout(header)

        # 缩略图 chip（输入栏上方一小条——Kimi Code 输入区附件同款形态；
        # 大图横幅会把对话区挤没，实机反馈后改成 chip）
        self._thumb_chip = QFrame(self)
        self._thumb_chip.setObjectName("screen-ask-chip")
        chip_row = QHBoxLayout(self._thumb_chip)
        chip_row.setContentsMargins(6, 4, 8, 4)
        chip_row.setSpacing(8)
        self._thumb = QLabel(self._thumb_chip)
        self._thumb.setObjectName("screen-ask-thumb")
        self._thumb.setFixedSize(40, 40)
        self._thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        chip_row.addWidget(self._thumb)
        self._chip_text = QLabel("截图已就位 · 发送时随问题一起给 AI", self._thumb_chip)
        self._chip_text.setObjectName("screen-ask-chip-text")
        chip_row.addWidget(self._chip_text, 1)
        layout.addWidget(self._thumb_chip)

        # 消息列表（视口与宿主都不透明：透明窗上"透明滚动区"会透出桌面，
        # 消息少的时候整片内容像浮在壁纸上——实机反馈的"重叠看不清"就是这条）
        self._scroll = QScrollArea(self)
        self._scroll.setObjectName("screen-ask-scroll")
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._messages_host = QWidget()
        self._messages_host.setObjectName("screen-ask-messages")
        self._messages = QVBoxLayout(self._messages_host)
        self._messages.setContentsMargins(2, 2, 2, 2)
        self._messages.setSpacing(6)
        self._messages.addStretch(1)
        self._scroll.setWidget(self._messages_host)
        layout.addWidget(self._scroll, 1)

        # 状态行（正在看图 / 失败理由）
        self._status = QLabel("", self)
        self._status.setObjectName("screen-ask-status")
        self._status.setVisible(False)
        layout.addWidget(self._status)

        # 输入行
        row = QHBoxLayout()
        row.setSpacing(8)
        self._input = QLineEdit(self)
        self._input.setObjectName("screen-ask-input")
        self._input.setPlaceholderText("问问这张截图…（回车发送）")
        self._input.returnPressed.connect(self.send_current)
        row.addWidget(self._input, 1)
        self._send_btn = QPushButton("发送", self)
        self._send_btn.setObjectName("screen-ask-send")
        self._send_btn.clicked.connect(self.send_current)
        row.addWidget(self._send_btn)
        layout.addLayout(row)

        self.setStyleSheet(f"""
            QLabel#screen-ask-title {{
                color: {colors["text"]}; font-size: 13px; font-weight: 600;
            }}
            QLabel#screen-ask-note, QLabel#screen-ask-status {{
                color: {colors["hint"]}; font-size: 11px;
            }}
            QFrame#screen-ask-chip {{
                background: {colors["button"]}; border-radius: 8px;
                border: 1px solid {colors["border"]};
            }}
            QLabel#screen-ask-thumb {{
                background: {colors["divider"]}; border-radius: 6px;
            }}
            QLabel#screen-ask-chip-text {{
                color: {colors["hint"]}; font-size: 11px;
            }}
            QScrollArea#screen-ask-scroll,
            QScrollArea#screen-ask-scroll QWidget#qt_scrollarea_viewport,
            QWidget#screen-ask-messages {{
                background: {colors["surface"]};
            }}
            QScrollArea#screen-ask-scroll QScrollBar:vertical {{
                background: transparent; width: 8px; margin: 2px;
            }}
            QScrollArea#screen-ask-scroll QScrollBar::handle:vertical {{
                background: {colors["border"]}; border-radius: 4px; min-height: 24px;
            }}
            QScrollArea#screen-ask-scroll QScrollBar::add-line:vertical,
            QScrollArea#screen-ask-scroll QScrollBar::sub-line:vertical {{ height: 0; }}
            QScrollArea#screen-ask-scroll QScrollBar::add-page:vertical,
            QScrollArea#screen-ask-scroll QScrollBar::sub-page:vertical {{
                background: transparent;
            }}
            QLineEdit#screen-ask-input {{
                background: {colors["button"]}; color: {colors["text"]};
                border: 1px solid {colors["border"]}; border-radius: 8px;
                padding: 6px 10px; font-size: 13px;
            }}
            QLineEdit#screen-ask-input:focus {{ border-color: #1677e8; }}
            QPushButton#screen-ask-send {{
                background: #1677e8; color: #ffffff; border: none;
                border-radius: 8px; padding: 6px 16px; font-size: 13px;
            }}
            QPushButton#screen-ask-send:hover {{ background: #3d8ef0; }}
            QPushButton#screen-ask-send:disabled {{
                background: {colors["button"]}; color: {colors["hint"]};
            }}
            QPushButton#screen-ask-close {{
                border: none; background: transparent;
                color: {colors["hint"]}; font-size: 14px; padding: 0;
            }}
            QPushButton#screen-ask-close:hover {{ color: {colors["text"]}; }}
        """)
        # 标题栏 logo 先按构建时的 DPR 出一张（真实落点由 showEvent 按最终屏重出）
        self._refresh_title_icon()

    # ---------------------------------------------------------------- 对外

    def start_with_image(self, image: QImage, context_text: str = "", below=None) -> None:
        """开新会话：换缩略图、清消息、重置会话上下文，然后显示并聚焦输入框。

        ``context_text``：随首轮发出的参考资料（现在是结果面板带来的 OCR 文字）。
        ``below``：采集选区——窗口落在选区正下方（用户拖过位置后沿用用户的位置）。
        """
        if image is None or image.isNull():
            raise ValueError("截图问答收到空图")
        try:
            conversation = self._conversation_factory(self._config)
            conversation.start(image, context_text=context_text)
        except Exception:
            # 通道建不起来（没配置/配置坏了）：如实报错，窗口照样不开。
            log.exception("截图问答：会话通道创建失败")
            raise
        self._pending_below = below
        # 窗口已经可见时 show() 不会再触发 showEvent：用户没拖过位置就跟新选区走
        # （拖过位置的窗口不动——"开着就别乱动"优先于"跟选区"）。
        if self.isVisible() and self._user_pos is None and below is not None:
            from .result_panel import position_below_rect

            position_below_rect(self, below)
            self._pending_below = None
        self._conversation = conversation
        self._gen += 1   # 新会话：旧会话在途的回复一律作废（见 _on_reply 的代号核对）
        self._context_note = str(context_text or "").strip()
        thumb = QPixmap.fromImage(image).scaled(
            40, 40, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        self._thumb.setPixmap(thumb)
        # OCR 参考文字会随首轮一起发出（VisionConversation）：必须在界面上看得见，
        # 不许"发了什么用户不知道"（盲审 m2）
        base = "截图已就位 · 发送时随问题一起给 AI"
        if self._context_note:
            base += f"（附识别文字 {len(self._context_note)} 字）"
        self._chip_text.setText(base)
        self._clear_messages()
        self._set_status("")
        self._set_sending(False)
        self.show()
        self.raise_()
        self.activateWindow()
        self._input.setFocus(Qt.FocusReason.ActiveWindowFocusReason)

    def _on_title_screen_changed(self, _screen) -> None:
        """拖到不同缩放比的屏：按新屏 DPR 重出同一张 logo（只重出，不换素材）。"""
        self._refresh_title_icon()

    def _refresh_title_icon(self, dpr: float | None = None) -> None:
        """按当前屏 DPR 出标题栏 logo（固定 DS logo，见 ``brand_icons``）。

        窗口落在哪块屏由 showEvent/选区定位决定，所以真实落点会再出一次；
        拖到不同缩放屏时经 screenChanged 重出——只重出图，不换素材。
        素材缺席时退回那四个字（标识不许变成空白洞）。``dpr`` 是测试注入缝
        （offscreen 恒 1.0），生产缺省读窗口真实 DPR。
        """
        src = load_ds_whale()
        if src.isNull():
            self._title_src = QPixmap()
            self._title_scaled = None
            self._title.setText(_TITLE_TEXT)
            return
        dpr = self.devicePixelRatioF() if dpr is None else float(dpr)
        scaled = src.scaled(
            int(_TITLE_LOGO_BOX * dpr), int(_TITLE_LOGO_BOX * dpr),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        scaled.setDevicePixelRatio(dpr)
        self._title.setPixmap(scaled)   # setPixmap 会清掉文字：两态不并存
        self._title_src = src
        self._title_scaled = scaled     # 出图留档：测试可直接核对 DPR/物理尺寸
        #（QLabel.pixmap() 在 offscreen 下会归一化 DPR，读它验不了 HiDPI）

    def send_current(self) -> bool:
        """发送当前输入（空输入走默认问法）。发送中/无会话时拒收，返回是否受理。"""
        if self._sending or self._conversation is None:
            return False
        text = self._input.text().strip()
        conversation = self._conversation
        gen = self._gen   # 闭包捕获：回来时对不上代号就说明会话已经换了
        question = text or "看看这块区域里是什么，说清内容，能给出判断或建议时就直接给。"
        self._append_bubble(question, "user")
        self._input.clear()
        self._set_sending(True)
        self._set_status("正在看图…")
        threading.Thread(
            target=self._ask_worker, args=(conversation, question, gen),
            daemon=True, name="pet-screen-ask",
        ).start()
        return True

    # ---------------------------------------------------------------- 会话收口

    def _ask_worker(self, conversation, question: str, gen: int) -> None:
        try:
            reply = conversation.ask(question)
        except Exception as exc:
            log.exception("截图问答：发送失败")
            self._reply_failed.emit(gen, str(exc) or type(exc).__name__)
            return
        self._reply_ready.emit(gen, reply)

    def _on_reply(self, gen: int, reply: str) -> None:
        if gen != self._gen:
            log.debug("截图问答：丢弃上一会话迟到的回复 (gen=%s 当前=%s)", gen, self._gen)
            return
        self._set_sending(False)
        self._set_status("")
        text = str(reply or "").strip()
        self._append_bubble(text or "（模型没说话，再试一次？）", "assistant")

    def _on_error(self, gen: int, reason: str) -> None:
        if gen != self._gen:
            return
        self._set_sending(False)
        self._set_status(f"发送失败：{reason}")

    # ---------------------------------------------------------------- 界面小件

    def _append_bubble(self, text: str, role: str) -> None:
        bubble = _Bubble(text, role, self._palette, self._messages_host)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        if role == "user":
            row.addStretch(1)
            row.addWidget(bubble, 4)
        else:
            row.addWidget(bubble, 4)
            row.addStretch(1)
        self._messages.insertLayout(self._messages.count() - 1, row)
        bar = self._scroll.verticalScrollBar()
        bar.setValue(bar.maximum())   # 新消息滚到底

    def _clear_messages(self) -> None:
        while self._messages.count() > 1:    # 留住底部 stretch
            item = self._messages.takeAt(0)
            layout = item.layout()
            if layout is not None:
                while layout.count():
                    child = layout.takeAt(0)
                    widget = child.widget()
                    if widget is not None:
                        widget.deleteLater()
            else:
                widget = item.widget()
                if widget is not None:
                    widget.deleteLater()

    def _set_sending(self, sending: bool) -> None:
        self._sending = sending
        self._send_btn.setEnabled(not sending)
        self._input.setEnabled(not sending)

    def _set_status(self, text: str) -> None:
        self._status.setText(text)
        self._status.setVisible(bool(text))

    # ------------------------------------------------------------ 展示与几何

    def showEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        super().showEvent(event)
        # 标题栏 logo 按最终落点的屏 DPR 重出（构建时不知道会落哪块屏）；
        # 并一次性接上换屏信号——拖到不同缩放比的屏只重出图，不换素材。
        self._refresh_title_icon()
        if not self._title_screen_wired:
            handle = self.windowHandle()
            if handle is not None:
                handle.screenChanged.connect(self._on_title_screen_changed)
                self._title_screen_wired = True
        if self._user_size is not None:
            self.resize(self._user_size)
        if self._user_pos is not None:
            self.move(self._user_pos)
            return
        below, self._pending_below = self._pending_below, None
        if below is not None:
            from .result_panel import position_below_rect

            position_below_rect(self, below)
            return
        screen = self.screen()
        if screen is None:
            return
        available = screen.availableGeometry()
        x = available.right() - self.width() - _MARGIN
        y = available.bottom() - self.height() - _MARGIN
        self.move(max(available.left(), x), max(available.top(), y))

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        super().resizeEvent(event)
        self._grip.move(self.width() - 16, self.height() - 16)
        if self.isVisible():
            self._user_size = event.size()

    def moveEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        super().moveEvent(event)
        if self._drag_offset is not None:
            self._user_pos = event.pos()

    # ------------------------------------------------------------ 拖拽与键盘

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        # 透明背景窗口上 QSS 不画自身背景（同结果面板的实测结论）——卡片手画。
        from PySide6.QtGui import QColor, QPainter, QPainterPath

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.rect().adjusted(0, 0, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(float(rect.x()), float(rect.y()),
                            float(rect.width()), float(rect.height()), 14.0, 14.0)
        painter.fillPath(path, QColor(self._palette["surface"]))
        painter.setPen(QColor(self._palette["border"]))
        painter.drawPath(path)
        painter.end()
        super().paintEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.pos()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        if event.button() == Qt.MouseButton.LeftButton and self._drag_offset is not None:
            self._drag_offset = None
            self._user_pos = self.pos()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 (Qt 命名)
        # 本窗是普通焦点窗（要打字），Esc 直接关；输入框里的 Esc 留给输入框自己。
        if event.key() == Qt.Key.Key_Escape and not self._input.hasFocus():
            self.close()
            event.accept()
            return
        super().keyPressEvent(event)
