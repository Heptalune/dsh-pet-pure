# -*- coding: utf-8 -*-
"""快速对话气泡（Quick Chat）。

点击桌宠弹出的头顶小气泡输入框；回车发送，AI 回复在气泡内流式显示，
与完整 AI 对话窗口共用同一会话历史（SessionStore / ChatService）。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, QTimer, Qt
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .speech_bubble import BUBBLE_STYLE_PRESETS
from .speech_bubble_text import truncate_bubble_text

from .chat.models import ChatMessage
from .chat.markdown_view import markdown_to_html
from .chat.memory_extract import schedule_extraction
from .chat.prompt import PromptBuilder
from .chat.service import ChatService
from .chat.session_store import SessionStore

_PAGE_SIZE = 500

# 气泡内 AI 回答的展示上限：超长回答不进气泡（头顶气泡只有几百像素），
# 截断到 _REPLY_PREVIEW_LIMIT 字并提示全文在聊天窗（气泡内提供直达按钮）。
_REPLY_PREVIEW_LIMIT = 150
_REPLY_PREVIEW_SUFFIX = "…（全文见聊天窗）"


def _surface_radius(preset: dict) -> float:
    """气泡主体圆角。

    breath_bubble 是有机水滴形（speech_bubble 专属几何，preset 里 radius=0）：
    quick_chat 不支持该形状，按 0 渲染会变直角方框——同一
    self_talk_bubble_style 设置在桌宠气泡与快速对话两套观感。这里用大圆角
    近似，与 speech_bubble 同设置不破解；普通预设用自己的 radius。
    """
    if preset.get("shape") == "breath_bubble":
        return 22.0
    return float(preset.get("radius", 14))


class QuickChatBubble(QFrame):
    def __init__(self, config, pet_window=None, parent=None):
        super().__init__(parent)
        self.config = config
        self.pet_window = pet_window
        self.setObjectName("quick-chat-bubble")
        flags = (
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._capture_compat = False
        self._capture_host: QWidget | None = None
        self.setMinimumWidth(320)
        self.setMaximumWidth(460)

        style_id = str(config.get("self_talk_bubble_style", "classic_top") or "classic_top")
        self._preset = BUBBLE_STYLE_PRESETS.get(style_id, BUBBLE_STYLE_PRESETS["classic_top"])
        self._tail_up = False

        self.character_id = str(config.get("character", "shenshen"))
        self.settings = config.chat_settings()
        self.prompt_builder = PromptBuilder(Path(__file__).resolve().parent.parent / "assets" / "characters")
        self.store = SessionStore(config.dir, "" if str(getattr(config, "instance_id", "")) == "slot-0" else str(getattr(config, "instance_id", "")))
        self.session = self._get_session()
        self.service = ChatService(parent=self)
        self.service.memory_extract_hook = self._schedule_memory_extract
        self._active_request_id: str | None = None
        self._reply_full = ""       # AI 回答全文（会话历史/聊天窗用，不截断）
        self._reply_text = ""       # 气泡内展示文本（超上限截断 + 提示看聊天窗）
        self._reply_truncated = False
        self._page = 0
        self._pages: list[str] = []
        self._deactivate_check_pending = False

        self._build()
        self._connect()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("快速对话")
        title.setObjectName("quick-chat-title")
        self.title_label = title  # 子类（灵动岛气泡）改标题用
        header.addWidget(title)
        header.addStretch(1)
        self.hint_label = QLabel("")
        self.hint_label.setObjectName("quick-chat-hint")
        header.addWidget(self.hint_label)
        self.close_btn = QPushButton("×")
        self.close_btn.setObjectName("quick-chat-close")
        self.close_btn.setFixedSize(24, 24)
        header.addWidget(self.close_btn)
        layout.addLayout(header)

        self.output = QLabel("")
        self.output.setObjectName("quick-chat-output")
        self.output.setWordWrap(True)
        self.output.setMinimumHeight(60)
        self.output.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.output_scroll = QScrollArea()
        self.output_scroll.setObjectName("quick-chat-output-scroll")
        self.output_scroll.setWidgetResizable(True)
        self.output_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.output_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.output_scroll.setMinimumHeight(60)
        self.output_scroll.setMaximumHeight(220)
        self.output_scroll.setWidget(self.output)
        layout.addWidget(self.output_scroll)

        self.page_widget = QWidget()
        self.page_row = QHBoxLayout(self.page_widget)
        self.page_row.setContentsMargins(0, 0, 0, 0)
        self.prev_btn = QPushButton("←")
        self.page_label = QLabel("")
        self.next_btn = QPushButton("→")
        for btn in (self.prev_btn, self.next_btn):
            btn.setObjectName("quick-chat-page")
            btn.setFixedSize(28, 24)
        self.open_chat_btn = QPushButton("去聊天窗")
        self.open_chat_btn.setObjectName("quick-chat-open")
        self.page_row.addWidget(self.prev_btn)
        self.page_row.addWidget(self.page_label)
        self.page_row.addWidget(self.next_btn)
        self.page_row.addStretch(1)
        self.page_row.addWidget(self.open_chat_btn)
        layout.addWidget(self.page_widget)
        self.page_widget.setVisible(False)

        input_row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("输入消息，回车发送…")
        self.send_btn = QPushButton("发送")
        self.send_btn.setObjectName("quick-chat-send")
        input_row.addWidget(self.input, 1)
        input_row.addWidget(self.send_btn)
        layout.addLayout(input_row)

        bg = self._preset["background"]
        fg = self._preset["foreground"]
        border = self._preset["border"]
        self.setStyleSheet(f"""
            QLabel#quick-chat-title, QLabel#quick-chat-hint, QLabel#quick-chat-output,
            QLabel#quick-chat-page {{ background: transparent; color: {fg}; border: none; }}
            QScrollArea#quick-chat-output-scroll {{ background: transparent; border: none; }}
            QPushButton {{
                background: {border}; color: {fg}; border: none; border-radius: 8px;
                padding: 3px 8px;
            }}
            QPushButton:hover {{ background: {fg}; color: {bg}; }}
            QLineEdit {{
                background: {bg}; color: {fg}; border: 1px solid {border};
                border-radius: 8px; padding: 5px 8px;
            }}
        """)

    def _connect(self) -> None:
        self.close_btn.clicked.connect(self.close)
        self.send_btn.clicked.connect(self._send)
        self.input.returnPressed.connect(self._send)
        self.prev_btn.clicked.connect(lambda: self._show_page(self._page - 1))
        self.next_btn.clicked.connect(lambda: self._show_page(self._page + 1))
        self.open_chat_btn.clicked.connect(self._open_full_chat)
        # started 在 send() 内**同步**发出，而 _active_request_id 是 send() 的
        # 返回值——直连会让 _started 的 id 过滤恒命中（气泡提示永远停在
        # 「思考中…」）。排队一拍：emit 先于 worker.start() 投递，事件按
        # FIFO 处理，本槽必在首个 delta 之前跑到，此刻 id 已落位。
        self.service.started.connect(self._started, Qt.ConnectionType.QueuedConnection)
        self.service.delta.connect(self._delta)
        self.service.activity.connect(self._activity)
        self.service.finished.connect(self._finished)
        self.service.error.connect(self._error)
        self.service.stopped.connect(self._stopped)

    # ------------------------------------------------------------ 绘制
    def paintEvent(self, event) -> None:  # noqa: N802
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(6, 8, -6, -8)
        radius = _surface_radius(self._preset)
        body = QPainterPath()
        body.addRoundedRect(rect, radius, radius)
        tail = QPainterPath()
        tip_x = rect.center().x()
        if self._tail_up:
            tip = QPointF(tip_x, rect.top() - 6)
            tail.moveTo(QPointF(tip_x - 8, rect.top() + 2))
            tail.lineTo(tip)
            tail.lineTo(QPointF(tip_x + 8, rect.top() + 2))
        else:
            tip = QPointF(tip_x, rect.bottom() + 6)
            tail.moveTo(QPointF(tip_x - 8, rect.bottom() - 2))
            tail.lineTo(tip)
            tail.lineTo(QPointF(tip_x + 8, rect.bottom() - 2))
        tail.closeSubpath()
        surface = body.united(tail).simplified()

        # 柔和阴影
        shadow = QPainterPath(surface)
        shadow.translate(0, 2)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(self._preset.get("shadow", "#6b542b")))
        painter.drawPath(shadow)

        # 气泡主体
        painter.setBrush(QColor(self._preset["background"]))
        painter.setPen(QPen(QColor(self._preset["border"]), 1))
        painter.drawPath(surface)
        painter.end()

    # ------------------------------------------------------------ 会话
    def _get_session(self):
        sessions = self.store.list(self.character_id)
        return sessions[0] if sessions else self._new_session()

    def refresh_session(self) -> None:
        """从磁盘重取最近会话（公开 seam；角色切换后外部刷新用，替代私访 _get_session）。"""
        self.session = self._get_session()

    def switch_character(self, character_id: str) -> None:
        """切角色同步 seam（B07）：先作废在途请求，再换身份 + 按新角色重取会话。

        气泡的角色是构造期一次性快照（``character_id`` 决定会话目录与系统提示，
        见 ``_get_session`` / ``_send`` 的 ``build_messages``）；主宠切角色后不同步
        这里，气泡会一直用旧角色的会话与人格提示。空身份/同角色不动作（避免无谓
        重建会话，丢掉正在续聊的那一条）。

        作废在途请求是先决条件：``_finished`` / ``_delta`` / ``_error`` / ``_stopped``
        一律按 ``_active_request_id`` 认领信号，换会话时不清它，旧角色那次请求的完成
        信号到达时 ID 仍然匹配，回答就被 append 进**新**角色的 session（对照完整聊天窗
        ``chat/widgets.py::switch_character`` 的同款闸门）。
        """
        new_id = str(character_id or "")
        if not new_id or new_id == self.character_id:
            return
        self._invalidate_in_flight_request()
        self.character_id = new_id
        self.refresh_session()

    def _invalidate_in_flight_request(self) -> None:
        """作废在途请求：清 ID 闸 + 停掉 worker + 撤掉它的显示残留。

        清 ID 后旧请求的完成/增量/错误/停止信号全部按 ID 失配静默丢弃（既不落库也不
        上屏）；停 worker 只是别让它在后台白跑完再被丢弃。已上屏的半截回答属于**旧角色**
        的回答，换身份后不该留在新角色的气泡里，故与完整聊天窗换角色一致地复位显示。
        """
        if self._active_request_id is None:
            return
        self._active_request_id = None
        self.service.stop()
        self._set_reply_text("")
        self._page = 0
        self._pages = []
        self.page_widget.setVisible(False)
        self._render_reply()
        self.hint_label.setText("")

    def _new_session(self):
        session = self.store.create(
            self.character_id,
            self.settings.active_provider,
            self.prompt_builder.effective_system_prompt(self.settings, self.character_id),
        )
        self.store.save(session)
        return session

    def position_near_pet(self) -> None:
        pet = self.pet_window
        if pet is None or not hasattr(pet, "visible_content_rect"):
            return
        anchor = pet.visible_content_rect()
        host = self._capture_host if self._capture_compat else None
        if host is not None and not host.geometry().isEmpty():
            available = host.geometry()
        else:
            screen = QGuiApplication.screenAt(anchor.center())
            available = screen.availableGeometry() if screen else QGuiApplication.primaryScreen().availableGeometry()
        self.adjustSize()
        w = self.width()
        h = self.height()
        x = anchor.center().x() - w // 2
        y = anchor.top() - h - 8
        self._tail_up = False
        if host is not None and y < available.top():
            # 子模式默认只允许落在主窗内，会把气泡挤到下方/遮挡宠物。
            # 先向主窗申请透明头顶空间，尽量恢复“向上生成”的原生位置。
            screen = QGuiApplication.screenAt(anchor.center()) or QGuiApplication.primaryScreen()
            if screen is not None:
                screen_top = screen.availableGeometry().top()
                desired_top = y - 4
                if desired_top >= screen_top:
                    desired_h = available.top() - desired_top
                    setter = getattr(host, "set_capture_headroom", None)
                    if desired_h > 0 and setter is not None and setter(desired_h):
                        return self.position_near_pet()
        if y < available.top():
            y = anchor.bottom() + 8
            self._tail_up = True
        x = max(available.left() + 4, min(x, available.right() - w - 4))
        if host is not None:
            # 直播捕获子模式下只允许落在主窗矩形内；空间不足时夹到窗内，
            # 避免子控件被主窗边界裁掉。
            if h <= available.height():
                y = max(available.top() + 4, min(y, available.bottom() - h - 4))
            else:
                y = available.top() + 4
            self.move(host.mapFromGlobal(QPoint(x, y)))
        else:
            self.move(x, y)
        self.update()

    def set_capture_compat(self, on: bool, host: QWidget | None = None) -> None:
        """直播捕获兼容：把快速对话气泡作为桌宠主窗的子内容渲染（issue #62）。

        开启后快速对话不再是独立 Tool 窗口，而成为主窗子控件，捕获主窗时即可
        看到并操作它；关闭后恢复独立置顶 Tool 窗口形态。
        """
        on = bool(on)
        if on == self._capture_compat:
            return
        if on and host is None:
            return
        was_visible = self.isVisible()
        self._capture_compat = on
        self._capture_host = host if on else None
        if on:
            self.setWindowFlags(Qt.WindowType.Widget)
            self.setParent(host)
        else:
            self.setParent(None)
            flags = (
                Qt.WindowType.Tool
                | Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
            )
            self.setWindowFlags(flags)
        if was_visible:
            self.position_near_pet()
            self.show()
            if not on:
                self.raise_()
                self.activateWindow()
                self.input.setFocus()
        else:
            # 子模式重挂主窗后不能随父窗显示而自动弹出空白窗。
            self.hide()

    def show_for_pet(self, pet_window=None) -> None:
        if pet_window is not None:
            self.pet_window = pet_window
        self.position_near_pet()
        self.show()
        self.raise_()
        self.activateWindow()
        self.input.setFocus()

    # ------------------------------------------------------------ 发送
    def _send(self) -> None:
        if self.service.busy:
            self.service.stop()
            return
        text = self.input.text().strip()
        if not text:
            return
        self.input.clear()
        # 陈旧快照防护（DS-M7 → R3 P1 硬修）：原子「读-追加-提交」
        synced, _absorbed = self.store.append_message(
            self.session, ChatMessage("user", text)
        )
        if synced is None:
            self.session.messages.append(ChatMessage("user", text))
            self.store.save(self.session)
        else:
            self.session = synced
        self.output.setText("")
        self._set_reply_text("")
        self._page = 0
        self._pages = []
        self.page_widget.setVisible(False)
        self.hint_label.setText("思考中…")
        config = self.settings.active_config
        config.api_key = self.config.resolve_api_key(config)
        # 气泡快速对话刻意不注入记忆：一次性问答，注入只会拖慢首字；它仍然
        # 通过 service.memory_extract_hook 参与后台提炼（见 _schedule_memory_extract）。
        messages = self.prompt_builder.build_messages(
            self.settings, self.character_id, self.session.messages[:-1], text,
            # 每宠键：config 可能是 slot 路由视图，必须从 get 逐键读（chat_settings()
            # 转发主配置，读不到这只宠的场景卡）。与 widgets.py / legacy_widgets.py 同口径。
            roleplay_enabled=bool(self.config.get("roleplay_enabled", False)),
            roleplay_scene=str(self.config.get("roleplay_scene", "") or ""),
            roleplay_cast=str(self.config.get("roleplay_cast", "") or ""),
        )
        self._request_pet_window = self.pet_window
        self._active_request_id = self.service.send(messages, config)

    def _started(self, request_id: str) -> None:
        if request_id != self._active_request_id:
            return
        self.hint_label.setText("生成中…")

    def _activity(self, request_id: str, kind: str, text: str) -> None:
        if request_id != self._active_request_id or self._reply_full:
            return
        self.hint_label.setText("深度思考中…" if kind == "reasoning" else str(text).strip()[:30])

    def _request_presentation_valid(self) -> bool:
        target = getattr(self, "_request_pet_window", self.pet_window)
        valid = getattr(target, "presentation_valid", None)
        return target is self.pet_window and (not callable(valid) or bool(valid()))

    def _delta(self, request_id: str, text: str) -> None:
        if request_id != self._active_request_id or not self._request_presentation_valid():
            return
        self._set_reply_text(self._reply_full + str(text))
        self._render_reply()

    def _schedule_memory_extract(self, user_text: str, reply_text: str, provider, provider_config) -> None:
        """档 B：回复完成后的静默记忆提炼（护栏/后台线程都在 memory_extract 内）。"""
        schedule_extraction(
            config=self.config,
            memory_path=self.store.root / self.character_id / "memory.json",
            user_text=user_text, reply_text=reply_text,
            provider=provider, provider_config=provider_config)

    def _finished(self, request_id: str, text: str) -> None:
        if request_id != self._active_request_id:
            return
        # 落库/聊天窗用全文；气泡展示文本另行截断（见 _set_reply_text）。
        self._set_reply_text(text)
        synced, _absorbed = self.store.append_message(self.session, ChatMessage("assistant", self._reply_full))
        if synced is None:
            self.session.messages.append(ChatMessage("assistant", self._reply_full))
            self.store.save(self.session)
        else:
            self.session = synced
        self._active_request_id = None
        self.hint_label.setText("")
        if self._request_presentation_valid():
            self._render_reply()
        else:
            self.hide()

    def _error(self, request_id: str, text: str) -> None:
        if request_id != self._active_request_id:
            return
        self._active_request_id = None
        self.hint_label.setText("")
        self._set_reply_text(f"请求失败：{text}")
        self._render_reply()

    def _stopped(self, request_id: str) -> None:
        if request_id != self._active_request_id:
            return
        self._active_request_id = None
        self.hint_label.setText("已停止")

    def request_in_flight(self) -> bool:
        """是否有自己发出的、尚未到终态的请求在途。

        预览式改写（``show_reply``/``show_feedback``）会把 ``_active_request_id``
        清成 None，此后真实完成回调按 request_id 匹配全部**静默返回**（回答既不
        上屏也不落库）。所以任何"接管气泡显示"的外部入口都必须先问这一句。
        """
        return self._active_request_id is not None or self.service.busy

    # ------------------------------------------------------------ 显示
    def _set_reply_text(self, full_text: str) -> None:
        """记录 AI 回答全文，并算出气泡内展示文本（超限截断 + 提示全文位置）。

        全文与展示文本分开：会话历史/聊天窗始终拿全文，气泡只展示开头一段，
        免得长回答把头顶气泡撑成一堵墙。
        """
        self._reply_full = str(full_text or "")
        self._reply_text = truncate_bubble_text(
            self._reply_full, _REPLY_PREVIEW_LIMIT, _REPLY_PREVIEW_SUFFIX
        )
        self._reply_truncated = self._reply_text != self._reply_full

    def _set_page_controls_visible(self, on: bool) -> None:
        """显示/隐藏分页箭头与页码（截断预览时只保留「去聊天窗」按钮）。"""
        for widget in (self.prev_btn, self.page_label, self.next_btn):
            widget.setVisible(on)

    def _render_reply(self) -> None:
        text = self._reply_text
        if len(text) > _PAGE_SIZE:
            self._pages = [text[i:i + _PAGE_SIZE] for i in range(0, len(text), _PAGE_SIZE)]
            self._show_page(0)
            self._set_page_controls_visible(True)
            self.page_widget.setVisible(True)
            return
        self._pages = []
        # 截断预览：文案末尾已提示「全文见聊天窗」，这里只露出直达按钮；
        # 分页箭头/页码对一段预览没有意义，隐藏。
        self._set_page_controls_visible(not self._reply_truncated)
        self.page_widget.setVisible(self._reply_truncated)
        self._set_output_markdown(text)

    def _show_page(self, index: int) -> None:
        if not self._pages:
            return
        self._page = max(0, min(index, len(self._pages) - 1))
        self._set_output_markdown(self._pages[self._page])
        self.page_label.setText(f"{self._page + 1}/{len(self._pages)}")

    def _set_output_markdown(self, text: str) -> None:
        """助手回复按 Markdown 渲染；所有 chat surface 统一渲染，落盘原文不变。

        空文本清空即可；非空走 RichText。分页是对纯文本切的，跨页围栏代码块
        可能渲染不完整——气泡本来就是预览面，全文在聊天窗（分页为既有行为，
        不在这里改）。
        """
        if not text:
            self.output.setText("")
            return
        self.output.setTextFormat(Qt.TextFormat.RichText)
        self.output.setText(markdown_to_html(text))
        self.prev_btn.setEnabled(self._page > 0)
        self.next_btn.setEnabled(self._page < len(self._pages) - 1)

    def _open_full_chat(self) -> None:
        pet = self.pet_window
        if pet is not None and callable(getattr(pet, "on_open_chat", None)):
            pet.on_open_chat()
        elif hasattr(self, "open_chat_callback") and callable(self.open_chat_callback):
            self.open_chat_callback()

    def event(self, event) -> bool:
        if event.type() == QEvent.Type.WindowDeactivate and self.isVisible():
            # Cocoa 在另一个应用内窗口仍活动时首次 show/activate Tool 窗口，
            # 会产生一次过渡性的 WindowDeactivate；此时同步 close 会留下只
            # 有原生灰色底板的空白窗口。下一事件轮的 activeWindow 已稳定，
            # 可以区分这次过渡和用户真正切走焦点。
            if not self._deactivate_check_pending:
                self._deactivate_check_pending = True
                QTimer.singleShot(0, self, self._close_if_still_inactive)
        return super().event(event)

    def _close_if_still_inactive(self) -> None:
        self._deactivate_check_pending = False
        if self.isVisible() and QApplication.activeWindow() is not self:
            self.close()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.service.busy:
            self.service.stop()
        if self._capture_compat and self._capture_host is not None:
            setter = getattr(self._capture_host, "set_capture_headroom", None)
            if setter is not None:
                try:
                    setter(0)
                except RuntimeError:
                    pass  # 宿主窗口已在销毁中
        super().closeEvent(event)
