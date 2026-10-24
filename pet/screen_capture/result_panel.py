# -*- coding: utf-8 -*-
"""「快速识屏」结果浮层：岛旁的临时结果面板（可复制 / Esc 可关 / 不抢焦点）。

为什么要有这个面板，而不是全部塞进灵动岛或气泡：

- 岛是"一眼"面（定宽胶囊 + 卡片里两行消息），OCR 出来的一整段文字放不下；
- 气泡（``show_bubble``）是短反馈面，不能选中、不能复制，长文本会在屏上摊开一大片；
- 所以长结果落在一个**跟在岛旁**的临时浮层里：查完就关，不常驻、不进任何列表。

三个硬约束（都在 ``tests/test_screen_capture_result_panel.py`` 里钉死）：

1. **显示时不抢焦点**：窗口带 ``WA_ShowWithoutActivating``——主人正在别的窗口打字
   时弹出结果，输入焦点不许被抢走（与灵动岛、桌面通知同口径）。
   **不再带** ``WindowDoesNotAcceptFocus``（``WS_EX_NOACTIVATE``）：那个标志连
   "用户点击后激活"也一并禁掉，于是键盘事件永远进不来——选中一段后 Ctrl+C 复制不了，
   只能右键菜单（2026-10-19 实机反馈）。现在保留"打开不激活"、放开"点击可激活"。
2. **鼠标交互照常**：鼠标事件本来就有（岛上的卡片按钮是先例）；文本视图是
   ``ClickFocus``，选中后 Ctrl+C 能复制，右键菜单与工具栏的「复制」按钮照旧。
3. **Esc 可关**：同上，面板收不到键盘事件，Esc 只能走应用级事件过滤器；过滤器
   **永不吞事件**（恒返回 ``False``）——主人的 Esc 语义（弹弓取消、关设置页）
   一个都不能被结果面板挡住。

隐私：本模块只做展示，不碰像素、不落盘。
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFontMetrics, QGuiApplication, QPainter, QPainterPath, QPalette, QPen, QPixmap, QTextOption
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSizeGrip,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .brand_icons import pick_loading_icon

__all__ = ["SHORT_RESULT_LIMIT", "SYSTEM_STYLE", "ScreenResult", "ScreenResultPanel",
           "resolve_style"]

#: 岛上"短结果"的字数上限：超过就只在岛上留个摘要，全文去面板。
SHORT_RESULT_LIMIT = 40

_GAP = 8          # 面板与锚（岛）之间的间距
_MARGIN = 12      # 无锚时贴屏幕边的留白
_MIN_WIDTH = 300
_MAX_WIDTH = 520
_PRIMARY_MIN_H = 56
_PRIMARY_MAX_H = 240
_SECONDARY_MIN_H = 48
_SECONDARY_MAX_H = 160
#: 文本槽定高的安全余量（px）：QPlainTextEdit 的文档行高比 QFontMetrics.lineSpacing
#: 大一点点（中文字形实测约 0.5px/行），不留这点余量末行会被切掉半行。
_TEXT_SAFETY_PX = 6

#: 这些控件里 Esc 有自己的语义（撤销编辑 / 关闭输入），面板不许抢。
_ESC_OWNING_WIDGETS = (QLineEdit, QAbstractSpinBox, QTextEdit, QPlainTextEdit)

_STYLES = {
    "dark": {
        "surface": "#1c1e26", "text": "#ebedf3", "hint": "#a0aabe",
        "border": "#3a3a42", "divider": "#33333a", "button": "#2f3138",
    },
    "light": {
        "surface": "#ffffff", "text": "#1f2328", "hint": "#6b7280",
        "border": "#e2e4e8", "divider": "#eceef1", "button": "#f0f1f3",
    },
}

#: 宿主没给明确主题时的判定阈值：窗口底色亮度低于它算暗（与菜单层
#: ``context_menus/menu_styles/modern.py`` 的 ``theme == "system"`` 同一口径）。
_DARK_LIGHTNESS = 128


def position_below_rect(widget: QWidget, rect, gap: int = _GAP) -> None:
    """把窗口摆到选区矩形正下方（放不下翻上缘，再不行贴屏幕边）。

    实机反馈（2026-10-14）：结果面板/问答窗落在屏幕角落 = 视线要从选区跳到角落
    再跳回来；跟在选区旁边才是"这块图的下文"。``rect`` 是逻辑像素下的屏幕坐标
    （采集选区就是它）。
    """
    from PySide6.QtCore import QRect

    if rect is None:
        return
    bounds = QRect(int(rect.x), int(rect.y), int(rect.width), int(rect.height))
    screen = QGuiApplication.screenAt(bounds.center()) or QGuiApplication.primaryScreen()
    available = screen.availableGeometry() if screen is not None else None
    if available is None:
        return
    width, height = widget.width(), widget.height()
    x = bounds.center().x() - width // 2
    y = bounds.bottom() + gap
    if y + height > available.bottom():
        y = bounds.top() - height - gap           # 下方放不下 → 翻上缘
    if y < available.top():
        y = available.bottom() - height - _MARGIN  # 选区贴顶 → 贴屏幕下边
    x = max(available.left(), min(x, available.right() - width))
    y = max(available.top(), min(y, available.bottom() - height))
    widget.move(x, y)

#: ``style`` 参数里代表"跟着宿主走"的取值（含缺省 ``None``）。
SYSTEM_STYLE = "system"


def resolve_style(style: str | None, palette=None) -> str:
    """把 ``style`` 归一成 ``"dark"`` / ``"light"``。

    三档输入：

    - ``"dark"`` / ``"light"``：照用（用户/宿主明确选了）；
    - ``None`` / 空 / ``"system"``：**跟着宿主走**——按宿主调色板判（``palette``
      缺省用 ``QGuiApplication.palette()``），判据与菜单层同一条；
    - 其它不认识的值：同上（不抛异常——面板样式不该成为一次截图失败的理由）。
    """
    key = "" if style is None else str(style).strip().lower()
    if key in _STYLES:
        return key
    if palette is None:
        app = QApplication.instance()
        palette = app.palette() if app is not None else None
    if palette is not None:
        window = palette.color(QPalette.ColorRole.Window)
        return "dark" if window.lightness() < _DARK_LIGHTNESS else "light"
    return "dark"   # 连调色板都问不到（无 QApplication）：退回本片原有的暗色


#: 加载动画里图标的方槽边长（逻辑像素）：素材按原始宽高比等比塞进方槽并居中。
_ICON_BOX = 44


def _fitted_rect(icon: QPixmap, box: int) -> QRectF:
    """图标等比居中塞进 ``box×box`` 方槽的目标矩形（非方形素材不许被拉变形）。

    鲸蛙素材是 847×682，硬塞进方槽会被横向压扁；这里按窄边定比例、长边贴槽。
    尺寸按**设备无关像素**算（``deviceIndependentSize``）：2x 素材的逻辑边长是物理
    边长的一半，按物理像素算会把 HiDPI 素材画成两倍大。返回的矩形以槽心为原点，
    正好配 ``paintEvent`` 里已经 ``translate`` 过的坐标系。
    （别把这条口径套到 ``drawPixmap`` 的 source 上——那个参数是**物理像素**，
    取整图要用 ``icon.rect()``。）
    """
    source = icon.deviceIndependentSize()
    ratio = min(box / max(1.0, source.width()), box / max(1.0, source.height()))
    width, height = source.width() * ratio, source.height() * ratio
    return QRectF(-width / 2, -height / 2, width, height)


class _LoadingFish(QWidget):
    """加载动画：品牌图标轻轻浮动 + 喷泡泡（素材 ``assets/chat/`` 随包分发）。

    **每一轮加载抽一次图标**（:meth:`start`，由 :meth:`ScreenResultPanel.show_result`
    的过程态调用，四条链路 OCR/翻译/问答/重翻都走这里）：``random.random() < 0.1``
    出鲸蛙，其余 DS logo（:func:`brand_icons.pick_loading_icon`）。整轮保持所选的那
    一只——相位只驱动浮动与泡泡，不轮播也不每帧重抽。

    **轮次身份（``round_key``）由调用方给**，一次用户操作一个身份：

    - 换身份＝换了件事，哪怕上一轮还没收口也**重抽**（失败/取消只有一个"失败理由"、
      没有终态结果可摆到面板上，收口漏一处也不许把下一轮的脸留住）；
    - 同身份＝同一次操作的过程更新（``actions._run_translate`` 先"正在识别"再"正在
      翻译"），不重抽——换脸等于告诉用户"换了件事"；
    - 收口只收**自己的轮**（:meth:`stop` 的归属判据）：两条链路共用这一块面板，重翻
      的收口不许把采集链路还在跑的那一轮连动画一起收掉。

    ``hideEvent`` 只停表、**留着轮次身份**：卡片被收起（用户关掉 / 被别的链路的终态
    顶下去）是共用面板的覆盖行为，不是"换了件事"；真正结束一轮的是本轮终态结果或
    显式收口（``ScreenResultPanel.end_loading``）。素材全缺席时回退为三点跳动
    （绝不让"加载动画"自己成为故障点）。

    面板是防抢焦点的复用件，动画用自己的 QTimer：``hideEvent``/非 loading 结果
    都会停掉它，绝不会在面板收起后还占着定时器空转。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("screen-result-loading")
        self.setFixedHeight(64)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._phase = 0.0
        self._round_key: object = None   # 当前轮的身份（换身份＝换了件事）
        self._icon = QPixmap()           # 本轮的主角：start() 抽定，整轮不变
        self._timer = QTimer(self)
        self._timer.setInterval(50)   # 20fps 足够顺滑又省电
        self._timer.timeout.connect(self._tick)

    def _current_icon(self) -> QPixmap:
        """本轮加载选中的那一只（``start`` 抽定之后整轮不变）。"""
        return self._icon

    def start(self, round_key: object = None) -> None:
        """开始（或继续）一轮加载：**某一张卡片的图标只在"新的一轮"里抽一次**。

        "新的一轮"＝身份变了（``round_key`` 与当前轮不同）或本轮还没有图标
        （上一轮已收口）。同身份的过程更新只重启计时器，不重抽（见类文档）。
        """
        if round_key != self._round_key or self._icon.isNull():
            self._round_key = round_key
            self._icon = pick_loading_icon()
        if not self._timer.isActive():
            self._timer.start()

    def stop(self, round_key: object) -> bool:
        """收口一轮加载；返回是否**真的**收了口（键不合＝别人的轮，什么都没动）。

        ``round_key`` 是轮次身份（没有缺省值：收口必须说清收的是谁的轮）：只有当前轮的
        主人能收它——重翻链路的收口不许把采集链路还在跑的那一轮连动画一起收掉。键不合
        时连可见性都不碰。
        """
        if round_key != self._round_key:
            return False
        self._round_key = None
        self._icon = QPixmap()
        self._timer.stop()
        self.setVisible(False)   # 轮收了口就不该还挂着"在跑"的卡片
        return True

    def _tick(self) -> None:
        self._phase += 0.13
        self.update()

    def hideEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        # 只停表：轮次身份留着（见类文档——被收起/被顶下去不是换了件事）
        self._timer.stop()
        super().hideEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        import math

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        phase = self._phase
        cx, cy = self.width() / 2, self.height() / 2 + 10

        icon = self._current_icon()
        if icon.isNull():
            # 素材缺席的回退：三点跳动（形状简单但绝不能不画）
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#1677e8"))
            spacing = 22
            for i in range(3):
                lift = max(0.0, math.sin((phase * 2.2 - i * 0.9))) * 8
                painter.drawEllipse(QPointF(cx + (i - 1) * spacing, cy - 6 - lift), 5, 5)
            painter.end()
            return

        # 泡泡：从图标头顶（喷水孔位置）往上升、越来越淡
        for i in range(3):
            t = (phase * 0.5 + i / 3.0) % 1.0          # 0..1 循环
            bubble_x = cx - 6 + math.sin(t * math.pi * 2 + i) * 4
            bubble_y = cy - 24 - t * 30
            r = 2.2 + t * 1.8
            if t < 0.2:
                # 最新生的泡泡：实心 accent 蓝（空心描边在小尺寸下会全被抗锯齿
                # 糊掉，实心才有纯 accent 像素——这一点有测试守着）
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor("#1677e8"))
            else:
                alpha = int(220 * (1.0 - t * 0.75))
                painter.setPen(QPen(QColor(22, 119, 232, alpha), 1.4))
                painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QPointF(bubble_x, bubble_y), r, r)
            painter.drawEllipse(QPointF(bubble_x, bubble_y), r, r)

        # 本轮加载的主角：随相位轻轻上下浮动 + 极轻微摇摆（像浮在水里）——
        # 相位只推动画，图标是 start() 抽定的那一只（不轮播、不重抽）；
        # 出图等比居中（鲸蛙 847×682 硬塞方槽会被压扁）：目标矩形是等比后的
        # **逻辑**尺寸，源矩形取 ``icon.rect()``（**物理**像素的整图）——三参重载的
        # source 就是物理像素，按设备无关尺寸给会让 2x 素材只画出左上那 1/4。
        bob = math.sin(phase) * 2.4
        tilt = math.sin(phase * 0.7) * 0.06
        painter.save()
        painter.translate(cx, cy + bob)
        painter.rotate(math.degrees(tilt))
        painter.drawPixmap(_fitted_rect(icon, _ICON_BOX), icon, QRectF(icon.rect()))
        painter.restore()
        painter.end()


@dataclass(frozen=True)
class ScreenResult:
    """一次截图动作的结果（展示层的唯一输入模型）。

    ``primary`` = 主文本（OCR 文字 / 问答答复 / 翻译原文），``secondary`` =
    双语对照的第二槽（译文——翻译已接通：聊天模型文本互译），``status`` =
    一句话口径（"本地识别 · 12 字" / "已翻译 · N 字"），写在标题右侧。

    ``image`` = 这次截下的选区图（纯内存，不落盘）：非空时面板底部出现
    「继续提问…」入口——把图（与已识别文字）带进问答浮窗接着聊（豆包式
    "结果卡底部继续问"的等价物：我们的面板是防抢焦点窗，输入落在问答浮窗里）。
    """

    title: str
    primary: str = ""
    secondary: str = ""
    status: str = ""
    primary_label: str = ""
    secondary_label: str = ""
    force_panel: bool = False
    image: object = None
    #: 翻译结果专用：面板底部出「译为」语言行（切换即重翻，回调由编排层注入）。
    language_selector: bool = False
    #: 过程态占位卡片（"正在识别/翻译"）：True 时正文区摆跳动三点动画，
    #: 不摆文字（实机反馈：等待文字说明看着像卡死，动画才像"在跑"）。
    loading: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.primary.strip() and not self.secondary.strip()

    @property
    def summary(self) -> str:
        """岛的短文案：压平成一行、限长（岛是定宽胶囊，多行会撑坏几何）。"""
        text = " ".join(self.primary.split())
        if not text:
            return " ".join(self.status.split())
        if len(text) <= SHORT_RESULT_LIMIT:
            return text
        return text[:SHORT_RESULT_LIMIT] + "…"


class ScreenResultPanel(QFrame):
    """岛旁的结果浮层（复用一件：每次展示只换内容与位置）。

    ``style`` 三档同 :func:`resolve_style`：``"dark"`` / ``"light"`` 明写，
    ``None``（缺省）/ ``"system"`` 跟着宿主调色板走。**缺省不再是写死的暗色**：
    写死的那一版让 ``_STYLES["light"]`` 成了死代码（浅色宿主上永远弹一块深色
    浮层），现在宿主主题由调用方（``ScreenCapturePresenter``）从配置里读出来传进来。

    可调性（2026-10-14 实机反馈"太丑、不能调"后加的）：

    - **真圆角**：``WA_TranslucentBackground`` 打开 + ``paintEvent`` 手画卡片
      （实测：透明背景窗口上 QSS 的 ``background`` 不落像素，只能靠画）；
      （之前不透明背景把 ``border-radius`` 吃成了直角白盒）；
    - **可拖动**：按住标题栏区域拖（标题/状态标签对鼠标透明，事件落到面板上）；
    - **可缩放**：右下角 ``QSizeGrip``；用户拉过的尺寸在本实例上记住
      （面板是复用件，下一张结果沿用你调好的大小——只记进程内，不碰配置）。

    **共用一件，但同一时刻只认一轮**（跨截图返修 P1）：四条链路都会往这一块面板上摆
    卡片，所以"谁有权改这块面板"必须是明确的一处口径——最新到过的那一轮。这个判据
    **不在面板里**，而在落点 ``ScreenCapturePresenter``（``actions._claims_display``，
    唯一一份水位）：它必须先判再通知/再开面板，短结果不上面板也得判。面板因此只做
    "照着摆"：被顶下去的旧卡片根本到不了这里，一个字段都不会被改、连还在跑的加载
    动画都不碰；面板自己留的轮次状态只有加载动画那一轮的**身份相等**
    （:class:`_LoadingFish`）。
    """

    def __init__(self, parent: QWidget | None = None, *, style: str | None = None) -> None:
        super().__init__(parent)
        self._style = resolve_style(style, self.palette())
        self._palette = _STYLES[self._style]
        self.setObjectName("screen-result-panel")
        self.setWindowTitle("")
        self.setFrameShape(QFrame.Shape.NoFrame)  # 卡片由 paintEvent 手画（见下）
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        # 真圆角靠透明背景 + paintEvent 手画卡片；不透明会把圆角吃成直角
        # （实机"白盒"观感之源），而透明窗上 QSS 又不画自身背景（A/B 实测）。
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # 显示不激活：主人正在打字时不许把焦点抢过来
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        if hasattr(Qt.WidgetAttribute, "WA_MacAlwaysShowToolWindow"):
            self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow, True)
        self.setMinimumSize(_MIN_WIDTH, 140)
        self._user_size = None       # 用户用 grip 拉过的尺寸（QSize；进程内记忆）
        self._drag_offset: QPoint | None = None
        self._applying_size = False  # 预留：程序化排版的同步标记
        # 首次 show() 前的 resize 事件会被 Qt 挂起到显示那一刻才投递（那时
        # _applying_size 早复位了），所以"程序化尺寸"要靠值比对而不是旗标——
        # 否则第一张结果的排版尺寸会被误记成用户尺寸，之后每张结果都被钉死在
        # 第一张的高度上（2026-10-14 测试适配期发现的真实缺陷）。
        self._last_programmatic_size = None
        self._escape_installed = False
        self._on_continue_ask = None  # show_result 注入的"继续提问"回调
        self._continue_image = None
        self._continue_below = None   # 这张卡片自己的选区（「继续提问」随卡片带走）
        self._on_language_change = None  # show_result 注入的语言行回调
        # 展示归属**不在这里**：共用面的归属由落点 ``ScreenCapturePresenter`` 一份
        # 水位判（见 ``actions._claims_display``）——面板只负责"照着摆"，不再自己
        # 记身份历史（旧的 ``_round_epoch`` 字典每个身份留一条 int、进程内永不清理）。
        # 面板对 ``round_key`` 的用法只剩一处：加载动画那一轮的**身份相等**（抽不抽 /
        # 收不收自己的口，见 :class:`_LoadingFish`）。
        self._build()
        self._grip = QSizeGrip(self)
        self._grip.setObjectName("screen-result-grip")
        self._grip.resize(16, 16)

    # ---------------------------------------------------------------- 构建
    def _build(self) -> None:
        colors = self._palette
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 13, 16, 14)
        layout.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(8)
        self._title = QLabel("")
        self._title.setObjectName("screen-result-title")
        self._title.setWordWrap(False)
        # 标题栏同时是拖拽把手：标签对鼠标透明，按下/拖动落到面板本体上。
        self._title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        header.addWidget(self._title)
        self._status = QLabel("")
        self._status.setObjectName("screen-result-status")
        self._status.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        header.addWidget(self._status)
        header.addStretch(1)
        self._copy_btn = QPushButton("复制")
        self._copy_btn.setObjectName("screen-result-copy")
        self._copy_btn.clicked.connect(self.copy_primary)
        header.addWidget(self._copy_btn)
        self._copy_secondary_btn = QPushButton("复制译文")
        self._copy_secondary_btn.setObjectName("screen-result-copy-secondary")
        self._copy_secondary_btn.clicked.connect(self.copy_secondary)
        self._copy_secondary_btn.setVisible(False)
        header.addWidget(self._copy_secondary_btn)
        self._close_btn = QPushButton("×")
        self._close_btn.setObjectName("screen-result-close")
        self._close_btn.setFixedSize(24, 24)
        self._close_btn.clicked.connect(self.close_panel)
        header.addWidget(self._close_btn)
        layout.addLayout(header)

        self._primary_label = QLabel("")
        self._primary_label.setObjectName("screen-result-slot")
        self._primary_label.setVisible(False)
        layout.addWidget(self._primary_label)
        self._primary_view = self._make_view(_PRIMARY_MIN_H, _PRIMARY_MAX_H)
        layout.addWidget(self._primary_view)

        # 加载动画（过程态占位卡片的正文区；出结果即收起停表）
        self._loading = _LoadingFish(self)
        self._loading.setVisible(False)
        layout.addWidget(self._loading)

        self._divider = QFrame()
        self._divider.setObjectName("screen-result-divider")
        self._divider.setFrameShape(QFrame.Shape.HLine)
        self._divider.setFixedHeight(1)
        self._divider.setVisible(False)
        layout.addWidget(self._divider)
        self._secondary_label = QLabel("")
        self._secondary_label.setObjectName("screen-result-slot")
        self._secondary_label.setVisible(False)
        layout.addWidget(self._secondary_label)
        self._secondary_view = self._make_view(_SECONDARY_MIN_H, _SECONDARY_MAX_H)
        self._secondary_view.setVisible(False)
        layout.addWidget(self._secondary_view)

        # 翻译语言行（仅翻译结果出现）：自动检测 / 中文 / English / 日本語
        self._lang_row = QFrame()
        self._lang_row.setObjectName("screen-result-langrow")
        lang_layout = QHBoxLayout(self._lang_row)
        lang_layout.setContentsMargins(0, 0, 0, 0)
        lang_layout.setSpacing(6)
        lang_label = QLabel("译为")
        lang_label.setObjectName("screen-result-slot")
        lang_layout.addWidget(lang_label)
        self._lang_combo = QComboBox()
        self._lang_combo.setObjectName("screen-result-lang")
        for key, label in (("auto", "自动检测"), ("中文", "中文"),
                           ("English", "English"), ("日本語", "日本語")):
            self._lang_combo.addItem(label, key)
        self._lang_combo.currentIndexChanged.connect(self._language_changed)
        lang_layout.addWidget(self._lang_combo)
        lang_layout.addStretch(1)
        self._lang_row.setVisible(False)
        layout.addWidget(self._lang_row)

        # 底部「继续提问」入口（有图可带时才出现；豆包式结果卡续聊的等价物）
        self._continue_btn = QPushButton("继续提问…")
        self._continue_btn.setObjectName("screen-result-continue")
        self._continue_btn.setVisible(False)
        self._continue_btn.clicked.connect(self._continue_ask)
        layout.addWidget(self._continue_btn)

        self.setStyleSheet(f"""
            QLabel#screen-result-title {{
                color: {colors["text"]}; font-size: 13px; font-weight: 600;
            }}
            QLabel#screen-result-status, QLabel#screen-result-slot {{
                color: {colors["hint"]}; font-size: 11px;
            }}
            QPlainTextEdit {{
                background: transparent; color: {colors["text"]};
                border: none; font-size: 13px;
                selection-background-color: #1677e8;
            }}
            QPlainTextEdit QScrollBar:vertical {{
                background: transparent; width: 8px; margin: 2px;
            }}
            QPlainTextEdit QScrollBar::handle:vertical {{
                background: {colors["border"]}; border-radius: 4px; min-height: 24px;
            }}
            QPlainTextEdit QScrollBar::handle:vertical:hover {{
                background: {colors["hint"]};
            }}
            QPlainTextEdit QScrollBar::add-line:vertical,
            QPlainTextEdit QScrollBar::sub-line:vertical {{ height: 0; }}
            QPlainTextEdit QScrollBar::add-page:vertical,
            QPlainTextEdit QScrollBar::sub-page:vertical {{ background: transparent; }}
            QFrame#screen-result-divider {{ background: {colors["divider"]}; border: none; }}
            QPushButton {{
                background: {colors["button"]}; color: {colors["text"]};
                border: 1px solid {colors["border"]}; border-radius: 7px;
                padding: 3px 12px; font-size: 12px;
            }}
            QPushButton:hover {{ border-color: {colors["hint"]}; }}
            QPushButton#screen-result-copy {{
                background: #1677e8; border-color: #1677e8; color: #ffffff;
            }}
            QPushButton#screen-result-copy:hover {{ background: #3d8ef0; }}
            QPushButton#screen-result-close {{
                padding: 0; font-size: 14px; border: none; background: transparent;
                color: {colors["hint"]};
            }}
            QPushButton#screen-result-close:hover {{ color: {colors["text"]}; }}
            QPushButton#screen-result-continue {{
                background: transparent; color: {colors["hint"]};
                border: 1px dashed {colors["border"]}; border-radius: 8px;
                padding: 5px 10px; font-size: 12px; text-align: center;
            }}
            QPushButton#screen-result-continue:hover {{
                color: #1677e8; border-color: #1677e8;
            }}
            QComboBox#screen-result-lang {{
                background: {colors["button"]}; color: {colors["text"]};
                border: 1px solid {colors["border"]}; border-radius: 6px;
                padding: 2px 8px; font-size: 12px;
            }}
            QComboBox#screen-result-lang QAbstractItemView {{
                background: {colors["surface"]}; color: {colors["text"]};
                border: 1px solid {colors["border"]};
            }}
            QSizeGrip#screen-result-grip {{ background: transparent; }}
        """)

    def _make_view(self, min_h: int, max_h: int) -> QPlainTextEdit:
        view = QPlainTextEdit()
        view.setObjectName("screen-result-text")
        view.setReadOnly(True)
        view.setFrameShape(QFrame.Shape.NoFrame)
        view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        view.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        view.setMinimumHeight(min_h)
        view.setMaximumHeight(max_h)
        # 点击才聚焦（打开时仍不抢焦点，见 WA_ShowWithoutActivating）：
        # 不给任何焦点的话 Ctrl+C 永远没有落点，用户只能右键菜单复制。
        view.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        return view

    # ---------------------------------------------------------------- 只读视图
    @property
    def style(self) -> str:
        """实际生效的样式（``"dark"`` / ``"light"``，主题跟着宿主走之后的结论）。"""
        return self._style

    def title_text(self) -> str:
        return self._title.text()

    def status_text(self) -> str:
        return self._status.text()

    def primary_text(self) -> str:
        return self._primary_view.toPlainText()

    def secondary_text(self) -> str:
        return self._secondary_view.toPlainText()

    def has_secondary(self) -> bool:
        return self._secondary_view.isVisibleTo(self)

    def copy_button(self) -> QPushButton:
        return self._copy_btn

    def copy_secondary_button(self) -> QPushButton:
        return self._copy_secondary_btn

    def close_button(self) -> QPushButton:
        return self._close_btn

    def continue_button(self) -> QPushButton:
        """「继续提问…」入口（测试断言用；产品代码不碰）。"""
        return self._continue_btn

    def _continue_ask(self) -> None:
        callback = self._on_continue_ask
        if callback is None or self._continue_image is None:
            return
        # 选区随卡片一起交出去（``(图, 正文, 本卡选区)``）：回调方不许回头去问"最近一次
        # 采集的选区"——那张卡的图与文字可能来自更早的一次操作（跨截图串用）。
        callback(self._continue_image, self._primary_view.toPlainText(), self._continue_below)

    def _language_changed(self, _index: int) -> None:
        callback = self._on_language_change
        if callback is None:
            return
        callback(self._lang_combo.currentData())

    def language_row(self) -> QWidget:
        """「译为」语言行（测试断言用；产品代码不碰）。"""
        return self._lang_row

    def language_combo(self) -> QComboBox:
        return self._lang_combo

    # ---------------------------------------------------------------- 展示
    def show_result(self, result: ScreenResult, anchor: QWidget | None = None,
                    on_continue_ask=None, below=None, on_language_change=None,
                    round_key: object = None) -> bool:
        """换内容 + 定位 + 显示（复用同一件面板，不重复造窗）；返回是否摆了这张卡。

        ``on_continue_ask``：签名 ``(image, recognized_text, below) -> None``——面板底部
        「继续提问…」的回调（由编排层注入，把图带进问答浮窗）。``result.image``
        为空时入口不出现（没有上下文的"继续问"是空转）。``below`` 是**这张卡自己的**
        选区，随卡片一起交出去（见 :meth:`_continue_ask`）。

        ``on_language_change``：签名 ``(target) -> None``——翻译语言行的回调。
        只在 ``result.language_selector`` 为真且回调存在时出现该行。

        ``below``：采集选区（SelectionRect 或矩形）——给定时面板落在选区正下方
        （实机反馈：落在屏幕角落视线要来回跳），缺省维持锚/屏幕边的旧定位。

        ``round_key``：这张卡片所属的轮次身份（编排层给的，一次用户操作一个）——面板
        只用它管**加载动画那一轮**：过程态靠它决定"抽不抽"、终态靠它决定"收的是不是
        自己那一轮"（见 ``_LoadingFish``）。

        **展示归属不在这里**（跨截图返修 P1 + 二轮返修）：这份面板是共用件，但"谁有权
        改它"的判据只有一份，在落点 ``ScreenCapturePresenter``（``actions._claims_display``）
        ——只有它能先判再通知、并且连不上面板的短结果也一起判（短结果照样覆写岛/气泡）。
        被顶下去的旧卡片因此**根本到不了这一行**：正文、状态行、译文槽、语言行一个字
        都不改，连还在跑的加载动画都不碰（它属于新一轮），面板的可见性/位置也不动。

        真实故障：A 的翻译结果摆着 → 用户切语言触发重翻 → 重翻还在路上时用户又截了
        第二张图，第二张的过程态卡片已经占住面板 → A 的旧终态回来。旧实现先写正文
        （把第二张的卡片写成 A 的原文/译文）**再**判轮次归属，连第二张还在转的动画一起
        收掉：用户看到的是一张静止的旧结果，第二张"正在跑"没有留下任何痕迹。

        面板这一层**不留任何按身份的登记**（``ScreenResult`` → 控件是本方法唯一的
        副作用）：谁能改这块屏由上游那**一份**水位说了算，面板再记一份就是两套口径
        分叉的起点。返回 ``True`` 表示"照单摆了"——归属判在落点，那里才是"这张卡该不该
        出现"的唯一结论（``ScreenCapturePresenter.show`` 判不过就根本不调用本方法）。
        """
        self._title.setText(str(result.title))
        self._status.setText(str(result.status))
        self._primary_view.setPlainText(str(result.primary))
        self._secondary_view.setPlainText(str(result.secondary))

        self._primary_label.setText(str(result.primary_label))
        self._primary_label.setVisible(bool(result.primary_label))
        # 加载态：复制/语言行/继续提问收起；正文槽有内容就留着（翻译第二段：
        # 原文已就位，动画摆在下面表示"还在跑"），空正文就只有动画。
        loading = bool(result.loading)
        self._loading.setVisible(loading)
        if loading:
            self._loading.start(round_key)
        else:
            self._loading.stop(round_key)
        self._copy_btn.setVisible(not loading)
        # 空正文（引擎缺席/没认到字）不占地方：面板只剩标题栏与状态说明
        self._primary_view.setVisible(bool(result.primary.strip()))
        # 双语对照位只在真有译文时出现（翻译引擎接入前不占地方）；译文槽的
        # 标题可由调用方覆盖，缺省「译文」。
        bilingual = bool(result.secondary.strip())
        self._secondary_label.setText(str(result.secondary_label or "译文"))
        self._secondary_label.setVisible(bilingual)
        self._divider.setVisible(bilingual)
        self._secondary_view.setVisible(bilingual)
        self._copy_secondary_btn.setVisible(bilingual)
        self._copy_btn.setText("复制原文" if bilingual else "复制")

        # 「继续提问」只在有图可带且非加载态时出现（复制/纯状态/加载中都没有下文可续）。
        self._on_continue_ask = (
            on_continue_ask if (result.image is not None and not loading) else None
        )
        self._continue_btn.setVisible(self._on_continue_ask is not None)
        self._continue_image = result.image
        self._continue_below = below
        # 加载态不摆"正在识别…"这类文字（实机反馈：动画即可，文字像卡死说明）
        self._status.setVisible(bool(result.status) and not loading)

        # 翻译语言行：翻译结果且编排层给了重翻回调才出现；过程态/其它结果不出现。
        self._on_language_change = (
            on_language_change if (result.language_selector and not loading) else None
        )
        self._lang_row.setVisible(self._on_language_change is not None)
        if result.language_selector:
            # 新一轮翻译复位到"自动检测"（面板是复用件：上一轮选了日本語，
            # 这一轮走自动判定却显示日本語 = 控件和内容各说各话）
            self._lang_combo.blockSignals(True)
            self._lang_combo.setCurrentIndex(0)
            self._lang_combo.blockSignals(False)

        self.show_near(anchor, below=below)
        self.show()
        self.raise_()
        return True

    def end_loading(self, round_key: object = None) -> bool:
        """显式收口一轮加载；返回是否真的收了口（键不合＝别人的轮，什么都没动）。

        给**没有终态内容可摆**的出口用（失败 / 取消）：终态结果走 ``show_result``
        的 ``loading=False``。不收口的后果是加载卡继续转、下一独立操作还接着上一轮
        的脸（抽签失灵），所以这类出口必须显式结束这一轮。

        ``round_key`` 是本轮的身份：别人的轮收不得（重翻链路的收口不许把采集链路
        还在跑的那一轮收掉——两条链路共用这一块面板，见 ``_LoadingFish``）。
        """
        return self._loading.stop(round_key)

    def show_near(self, anchor: QWidget | None = None, below=None) -> None:
        """贴边定位：有选区矩形优先跟选区；否则优先锚（岛）左边，再贴屏幕边。

        用户用 grip 拉过尺寸后（``_user_size`` 非 None）沿用用户的大小——面板是
        复用件，"每张结果都重置回默认大小"会把用户刚调好的布局没收掉。
        """
        available = self._available_geometry(anchor)
        width = min(self.sizeHint().width(), available.width())
        width = max(width, min(_MIN_WIDTH, available.width()))
        self._applying_size = True
        try:
            self.resize(width, self.height())
            # 文本槽按内容实测高度（上下限之间）：一行结果不该撑出满高面板，
            # 一行也放不下时由各自的滚动条接住。
            self._fit_view_height(self._primary_view, _PRIMARY_MIN_H, _PRIMARY_MAX_H, width)
            self._fit_view_height(self._secondary_view, _SECONDARY_MIN_H, _SECONDARY_MAX_H,
                                  width)
            self.adjustSize()
            height = min(self.sizeHint().height(), available.height())
            if self._user_size is not None:
                width = max(_MIN_WIDTH, min(self._user_size.width(), available.width()))
                height = max(140, min(self._user_size.height(), available.height()))
            # 记录的必须是**实际会交付的尺寸**——最小高度地板会把 resize(300,115)
            # 钳成 (300,140)，记 115 的话延迟投递的事件就被误当成"用户拉的"
            # （短结果首发永久锁高度的缺陷就是这么来的）。
            width = max(self.minimumWidth(), width)
            height = max(self.minimumHeight(), height)
            self._last_programmatic_size = QSize(width, height)
            self.resize(width, height)
        finally:
            self._applying_size = False

        if anchor is not None:
            geo = anchor.frameGeometry()
            if not geo.isValid() or geo.isNull():
                geo = anchor.geometry()
            x = geo.left() - width - _GAP          # 左侧（岛常贴右/上边）
            if x < available.left():
                x = geo.right() + _GAP             # 右边
            y = geo.center().y() - height // 2
            if y + height > available.bottom():
                y = geo.top() - height - _GAP      # 上方
        else:
            x = available.right() - width - _MARGIN
            y = available.bottom() - height - _MARGIN
        x = max(available.left(), min(x, available.right() - width))
        y = max(available.top(), min(y, available.bottom() - height))
        if below is not None:
            position_below_rect(self, below)       # 选区优先于角落定位
        else:
            self.move(QPoint(x, y))

    def _fit_view_height(self, view: QPlainTextEdit, min_h: int, max_h: int,
                         panel_width: int) -> None:
        """按内容实测给文本槽定高（一行结果不撑满高面板，长文由滚动条接住）。

        用字体度量算折行高度（``view.font()`` 就是 QSS 生效后的那份字体，实测
        pixelSize=13 与渲染一致）：**不能**问 ``document().size()``——QPlainTextEdit
        的专用 layout 那份尺寸不是折行后的高度（900 字实测报 48px）。宽度一律
        预留竖向滚动条的位置，让"要不要滚动条"和"要不要换行"不互相追着改。
        """
        if not view.isVisibleTo(self):
            return
        margin = int(view.document().documentMargin())
        inner = max(80, int(panel_width) - 2 * 14 - 2 * margin - 15)
        metrics = QFontMetrics(view.font())
        bounds = metrics.boundingRect(
            0, 0, inner, 100000,
            int(Qt.TextFlag.TextWordWrap | Qt.TextFlag.TextExpandTabs),
            view.toPlainText(),
        )
        needed = (int(bounds.height()) + 2 * margin
                  + int(view.frameWidth()) * 2 + _TEXT_SAFETY_PX)
        view.setFixedHeight(max(int(min_h), min(int(max_h), needed)))

    def _available_geometry(self, anchor: QWidget | None):
        screen = None
        if anchor is not None:
            screen = QGuiApplication.screenAt(anchor.frameGeometry().center())
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        if screen is None:
            return self.geometry()
        return screen.availableGeometry()

    # ---------------------------------------------------------------- 交互
    def copy_primary(self) -> bool:
        """复制主文本；空文本返回 False（不冒充成功）。"""
        return self._copy_text(self.primary_text())

    def copy_secondary(self) -> bool:
        return self._copy_text(self.secondary_text())

    @staticmethod
    def _copy_text(text: str) -> bool:
        if not str(text).strip():
            return False
        clipboard = QApplication.clipboard()
        if clipboard is None:
            return False
        clipboard.setText(str(text))
        return True

    def close_panel(self) -> None:
        """收口：藏起来 + 摘掉 Esc 过滤器（复用件，下一次展示重新装）。"""
        self._detach_escape_filter()
        self.hide()

    # ------------------------------------------------------------ 拖动与缩放
    def paintEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        # 透明背景窗口上 QSS 不画自身背景（实测 A/B：WA_TranslucentBackground 一开，
        # background/background-color 都不落像素）——卡片圆角矩形只能手画。
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

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        super().resizeEvent(event)
        self._grip.move(self.width() - 16, self.height() - 16)
        # 程序化排版（show_near）不记；只有 grip/WM 拉出来的才算用户尺寸。
        # 首次显示前的尺寸事件是延迟投递的，单靠 _applying_size 旗标挡不住，
        # 必须再比对一次值（延迟事件被 Qt 合并成最后一次排版的尺寸）。
        if self._applying_size:
            return
        if self._last_programmatic_size is not None and event.size() == self._last_programmatic_size:
            return
        self._user_size = event.size()

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        # 面板本体（标题栏/留白）上的按下 = 拖动起点；子控件（按钮/文本槽/grip）
        # 的事件根本到不了这里，不用排除。
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
            event.accept()
            return
        super().mouseReleaseEvent(event)

    # ------------------------------------------------------------ Esc 过滤器
    def showEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        self._attach_escape_filter()
        super().showEvent(event)

    def hideEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        self._detach_escape_filter()
        super().hideEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        self._detach_escape_filter()
        super().closeEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        """兜底：万一某平台给了键盘焦点，Esc 同样可关。"""
        if event.key() == Qt.Key.Key_Escape:
            self.close_panel()
            event.accept()
            return
        super().keyPressEvent(event)

    def _attach_escape_filter(self) -> None:
        app = QApplication.instance()
        if app is None or self._escape_installed:
            return
        app.installEventFilter(self)
        self._escape_installed = True

    def _detach_escape_filter(self) -> None:
        app = QApplication.instance()
        if app is None or not self._escape_installed:
            return
        app.removeEventFilter(self)
        self._escape_installed = False

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 (Qt 命名)
        """应用级 Esc 兜底——**永不吞事件**。

        面板是防抢焦点窗，拿不到键盘焦点，Esc 只能从应用级过滤器进来；而应用级
        过滤器看得到**所有**窗口的 Esc，一旦返回 True 就等于把主人的弹弓取消 /
        关设置页一起吃掉。这里只做"顺手把面板关掉"，事件照旧传下去（返回
        False）；真正的 Esc 归属（弹窗、菜单、输入框里的撤销编辑）一律让路。
        """
        if event.type() != QEvent.Type.KeyPress:
            return False
        if event.key() != Qt.Key.Key_Escape:
            return False
        if not self.isVisible():
            return False
        app = QApplication.instance()
        if app is not None:
            if app.activeModalWidget() is not None:
                return False
            if app.activePopupWidget() is not None:
                return False
        focus = app.focusWidget() if app is not None else None
        if (
            focus is not None
            and not self.isAncestorOf(focus)
            and isinstance(focus, _ESC_OWNING_WIDGETS)
        ):
            return False
        self.close_panel()
        return False
