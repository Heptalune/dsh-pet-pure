# -*- coding: utf-8 -*-
"""Palette-aware icons shared by both context-menu themes."""
from __future__ import annotations

from math import cos, pi, sin

from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBitmap, QBrush, QColor, QIcon, QImage, QImageReader, QPainter, QPainterPath, QPen, QPixmap, QPolygonF, QRegion
from PySide6.QtWidgets import QLineEdit, QMenu, QStyle


CUSTOM_ICON_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff",
})
CUSTOM_ICON_MAX_BYTES = 5 * 1024 * 1024

#: 设置窗导航图标的**静息 / 禁用**描边色，按**设置窗自己的主题**（浅/深）取值。
#: 静息沿用聊天窗/菜单共有的那枚「静息灰」；禁用取静息色与表面的中间值——降对比但仍可读，
#: 观感与 Qt 自己给深色菜单生成的淡化版相当。
#: 选中态**不在这张表里**：它取设置窗的选中强调色（``settings_theme_qss.
#: SETTINGS_SELECTION_ACCENT``，与侧栏那条 3px 左条同一个真值，浅色档为对比度压深过一档）。
#: 只有设置窗表面（``modernStyle`` 标记）才拿三态；菜单与聊天窗仍拿单态，颜色不变。
_SETTINGS_ICON_REST: dict[bool, str] = {False: "#595959", True: "#d6d6d6"}
_SETTINGS_ICON_DISABLED: dict[bool, str] = {False: "#a6a8ac", True: "#7e7e82"}
#: 静息态是画布本身（QIcon 的 Normal 图元），只有这两态是额外覆色出来的。
_SETTINGS_ICON_MODES = (
    (QIcon.Mode.Selected, "selected"),
    (QIcon.Mode.Disabled, "disabled"),
)

#: 设置窗表面的图标请求记录：渲染时把 ``(名字, 尺寸)`` 记在**消费者控件**的这个动态属性上，
#: 换主题后由 ``settings_widgets.refresh_settings_icons`` 按同一个名字与尺寸重画。
#: 记录条件与读者见 :func:`_remember_themed_icon` 与 :func:`_icon_consumer`。
THEMED_ICON_REQUEST = "themedIconRequest"


def custom_icon_file_error(path: str | Path) -> str:
    """Return a user-facing validation error, or an empty string when valid."""
    candidate = Path(path).expanduser()
    if not candidate.is_file():
        return "请选择存在的图片文件"
    if candidate.suffix.lower() not in CUSTOM_ICON_SUFFIXES:
        return "仅支持 PNG、JPG、WebP、BMP、GIF 和 TIFF 图片"
    try:
        if candidate.stat().st_size > CUSTOM_ICON_MAX_BYTES:
            return "图片文件不能超过 5 MB"
    except OSError:
        return "无法读取图片文件"
    reader = QImageReader(str(candidate))
    if not reader.canRead() or not reader.size().isValid():
        return "图片内容无效或无法解码"
    return ""


def custom_file_menu_icon(widget, spec: dict, size: int = 18) -> QIcon:
    """Render a validated local image into the square menu icon slot."""
    path = Path(str(spec.get("path") or "")).expanduser()
    if custom_icon_file_error(path):
        return QIcon()
    reader = QImageReader(str(path))
    image = reader.read()
    if image.isNull():
        return QIcon()
    mode = "cover" if spec.get("display") == "cover" else "contain"
    dpr = widget.devicePixelRatioF() or 1.0
    pixels = max(1, round(size * dpr))
    canvas = QPixmap(pixels, pixels)
    canvas.setDevicePixelRatio(dpr)
    canvas.fill(Qt.GlobalColor.transparent)
    source = QPixmap.fromImage(image)
    target_mode = (
        Qt.AspectRatioMode.KeepAspectRatioByExpanding
        if mode == "cover"
        else Qt.AspectRatioMode.KeepAspectRatio
    )
    scaled = source.scaled(
        pixels, pixels, target_mode, Qt.TransformationMode.SmoothTransformation,
    )
    scaled.setDevicePixelRatio(dpr)
    painter = QPainter(canvas)
    scaled_width = scaled.width() / dpr
    scaled_height = scaled.height() / dpr
    painter.drawPixmap(
        round((size - scaled_width) / 2),
        round((size - scaled_height) / 2),
        scaled,
    )
    painter.end()
    return QIcon(canvas)


def _theme_signals(widget) -> tuple[str, bool, bool]:
    """Resolve (menuStyle, dark, settings_surface) by walking the widget's ancestors.

    Settings and chat windows pin ``menuStyle=modern`` on the container so every
    icon rendered inside inherits the outline colour (#595959 on light surfaces,
    #d6d6d6 on dark ones) instead of the app palette. The app palette follows
    the OS theme: on a dark system its foreground is white, which painted white
    glyphs on the QSS-light chat surfaces (invisible close/minimize/add icons).
    Individual widgets may still flip ``modernDark`` (e.g. the accent send
    button) without restating the style.

    The settings window is the one surface whose theme is **its own** rather than
    the OS's: it publishes the theme it actually applied as ``settingsDark``
    (``ModernSettingsDialog._apply_selected_theme``). That property is set *after*
    the navigation icons are built, so ``modernStyle`` (the marker only that
    window sets) additionally falls back to the settings theme's own default —
    "follow the system" — via :func:`_settings_system_dark`. Without an explicit
    signal and without the marker, the colour stays light: chat windows and
    context menus keep exactly the ink they had before.
    """
    style = ""
    dark = False
    explicit = False
    settings_surface = False
    current = widget
    while current is not None:
        value = str(current.property("menuStyle") or "")
        if value:
            style = value
        if not explicit and current.property("modernDark"):
            dark = True
            explicit = True
        themed = current.property("settingsDark")
        if not explicit and themed is not None:
            # 最内层优先：设置窗自己挂的主题真值不该被外层窗口的 modernDark 覆盖。
            dark = bool(themed)
            explicit = True
        if current.property("modernStyle"):
            settings_surface = True
        current = current.parent()
    if settings_surface and not explicit:
        dark = _settings_system_dark()
    return style, dark, settings_surface


def _icon_theme(widget) -> tuple[str, bool]:
    """(menuStyle, dark) —— 给取色/图标尺寸用的两元组视图（保留原有签名）。"""
    style, dark, _settings_surface = _theme_signals(widget)
    return style, dark


def _settings_system_dark() -> bool:
    """设置窗「跟随系统」档的深浅判定，与设置窗 QSS 同源。

    惰性导入：``settings_theme_qss`` 与本模块互相引用不成环（本模块顶层不 import 它），
    而这条分支只在设置窗内的控件上才会走到，右键菜单/聊天窗永远不触发它。
    """
    from ..settings_theme_qss import settings_system_dark

    return settings_system_dark()


def _settings_icon_states(widget) -> dict[str, str] | None:
    """设置窗内的图标要三态取色；菜单与聊天窗返回 None（保持单态、颜色不变）。"""
    style, dark, settings_surface = _theme_signals(widget)
    if style != "modern" or not settings_surface:
        return None
    dark = bool(dark)
    return {
        "rest": _SETTINGS_ICON_REST[dark],
        "selected": _settings_selection_accent(dark),
        "disabled": _SETTINGS_ICON_DISABLED[dark],
    }


def _settings_selection_accent(dark: bool) -> str:
    """设置窗选中态的强调色（与 QSS 里那条 3px 左条同一个真值）。

    惰性导入的理由同 :func:`_settings_system_dark`：本模块顶层不 import
    ``settings_theme_qss``，而这条分支只在设置窗内的控件上才会走到。
    """
    from ..settings_theme_qss import settings_selection_accent

    return settings_selection_accent(dark)


def _remember_themed_icon(host, name: str, requested_size: int | None) -> None:
    """在**能兑现的消费者**控件上记下这次图标请求（名字 + 有效尺寸）。

    图标是画出来的位图——描边色在渲染那一刻烘进像素，换主题时 QSS 立刻重排、图标不会
    自己换色，只有按同一个名字与尺寸重画一次（``settings_widgets.refresh_settings_icons``）。
    记录因此挂在**消费者**（真正被 ``setIcon`` 的那个控件）上而不是别的宿主上：重画只能
    写回消费者，记在别处等于记了一张没人能兑现的欠条。

    **不在"这里是不是设置窗"上加条件**：消费者可能在挂上窗口**之前**就渲染了图标（页面
    内容先独立构造、之后才挂进对话框），那一刻宿主链上根本没有 ``modernStyle``——按表面
    判定去记，正好把这些图标漏掉（它们同时还会拿到应用调色板的墨色，实测是黑色）。记一次
    的代价是写一个动态属性（实测 2.1 µs，不触发 polish/重排），读者只有设置窗。
    """
    size = int(requested_size) if requested_size else small_icon_size(host)
    host.setProperty(THEMED_ICON_REQUEST, (str(name), size))


def _icon_consumer(host) -> bool:
    """这个宿主能否把图标写回去——能兑现才记（见 :func:`_remember_themed_icon`）。

    控件走 ``setIcon``；搜索框这类没有 ``setIcon`` 的也算一个消费者（图标挂在它的
    ``QAction`` 上，重画分支见 ``settings_widgets.refresh_settings_icons``）。菜单与
    对话框本身不记：它们拿不到 Selected 图元、也没有 setIcon，记了没有读者。
    """
    return callable(getattr(host, "setIcon", None)) or isinstance(host, QLineEdit)


def _tinted_pixmap(source: QPixmap, color: str) -> QPixmap:
    """把单色线稿换成另一种描边色：按 alpha 整体覆色，抗锯齿边缘与 DPR 原样保留。"""
    image = source.toImage().convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    painter = QPainter(image)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    painter.fillRect(image.rect(), QColor(color))
    painter.end()
    tinted = QPixmap.fromImage(image)
    tinted.setDevicePixelRatio(source.devicePixelRatio())
    return tinted


def small_icon_size(menu: QMenu) -> int:
    style, _dark = _icon_theme(menu)
    if style == "modern":
        return 18
    return max(12, int(menu.style().pixelMetric(QStyle.PixelMetric.PM_SmallIconSize, None, menu)))


def _new_icon_canvas(widget, requested_size: int | None = None):
    size = requested_size or small_icon_size(widget)
    dpr = widget.devicePixelRatioF() or 1.0
    pixmap = QPixmap(max(1, round(size * dpr)), max(1, round(size * dpr)))
    pixmap.setDevicePixelRatio(dpr)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.scale(size / 16.0, size / 16.0)
    style, dark = _icon_theme(widget)
    color = (
        QColor("#d6d6d6" if dark else "#595959")
        if style == "modern"
        else widget.palette().color(widget.foregroundRole())
    )
    painter.setPen(QPen(color, 1.35, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    return pixmap, painter, color


def vector_menu_icon(menu: QMenu, name: str, size: int | None = None) -> QIcon:
    """Draw a crisp 16-unit semantic icon using the current menu palette."""
    pixmap, painter, color = _new_icon_canvas(menu, size)
    path = QPainterPath()

    if name == "search":
        painter.drawEllipse(QPointF(6.5, 6.5), 4.5, 4.5)
        painter.drawLine(QPointF(9.8, 9.8), QPointF(14.0, 14.0))
    elif name == "chat":
        painter.drawRoundedRect(QRectF(1.5, 2.0, 13.0, 9.5), 2.4, 2.4)
        path.moveTo(5.2, 11.2); path.lineTo(4.0, 14.0); path.lineTo(7.4, 11.5)
        painter.drawPath(path)
        painter.drawLine(QPointF(4.6, 6.7), QPointF(11.4, 6.7))
    elif name == "screen":
        painter.drawRoundedRect(QRectF(1.5, 2.5, 13.0, 9.0), 1.6, 1.6)
        painter.drawLine(QPointF(6.0, 13.5), QPointF(10.0, 13.5))
        painter.drawLine(QPointF(8.0, 11.5), QPointF(8.0, 13.5))
        painter.drawEllipse(QPointF(8.0, 7.0), 2.2, 1.5)
    elif name == "hide":
        painter.drawEllipse(QPointF(8.0, 8.0), 6.0, 3.8)
        painter.drawEllipse(QPointF(8.0, 8.0), 1.5, 1.5)
        painter.drawLine(QPointF(2.5, 13.5), QPointF(13.5, 2.5))
    elif name == "balance":
        painter.drawEllipse(QPointF(8.0, 8.0), 6.0, 6.0)
        painter.drawLine(QPointF(5.0, 5.5), QPointF(11.0, 5.5))
        painter.drawLine(QPointF(8.0, 4.0), QPointF(8.0, 12.0))
        painter.drawArc(QRectF(5.2, 5.0, 5.6, 6.0), 70 * 16, 220 * 16)
    elif name == "update":
        painter.drawArc(QRectF(2.0, 2.0, 12.0, 12.0), 35 * 16, 285 * 16)
        painter.drawPolygon(QPolygonF([QPointF(10.7, 1.8), QPointF(14.0, 2.5), QPointF(12.0, 5.3)]))
    elif name == "download":
        painter.drawLine(QPointF(8.0, 2.0), QPointF(8.0, 10.0))
        painter.drawLine(QPointF(4.8, 7.0), QPointF(8.0, 10.2))
        painter.drawLine(QPointF(11.2, 7.0), QPointF(8.0, 10.2))
        painter.drawLine(QPointF(3.0, 13.5), QPointF(13.0, 13.5))
    elif name == "file":
        painter.drawRoundedRect(QRectF(2.5, 1.5, 9.5, 13.0), 1.6, 1.6)
        painter.drawLine(QPointF(5.0, 5.5), QPointF(11.0, 5.5))
        painter.drawLine(QPointF(5.0, 8.5), QPointF(11.0, 8.5))
        painter.drawLine(QPointF(5.0, 11.5), QPointF(9.0, 11.5))
    elif name == "settings":
        gear = QPolygonF()
        for index in range(24):
            angle = -pi / 2 + index * pi / 12
            radius = 6.2 if index % 3 == 1 else 5.15
            gear.append(QPointF(8.0 + cos(angle) * radius, 8.0 + sin(angle) * radius))
        painter.drawPolygon(gear)
        painter.drawEllipse(QPointF(8.0, 8.0), 2.15, 2.15)
    elif name == "weather":
        # 晴：太阳本体 + 八道短射线（与整体单色描边语言一致）
        painter.drawEllipse(QPointF(8.0, 8.0), 3.4, 3.4)
        for index in range(8):
            angle = index * pi / 4
            inner, outer = 5.0, 7.0
            painter.drawLine(
                QPointF(8.0 + cos(angle) * inner, 8.0 + sin(angle) * inner),
                QPointF(8.0 + cos(angle) * outer, 8.0 + sin(angle) * outer),
            )
    elif name == "play":
        painter.drawPolygon(QPolygonF([QPointF(4.0, 2.5), QPointF(13.0, 8.0), QPointF(4.0, 13.5)]))
    elif name == "pet":
        painter.drawEllipse(QPointF(8.0, 9.4), 3.8, 3.2)
        for x, y in ((3.8, 5.2), (6.5, 3.6), (9.5, 3.6), (12.2, 5.2)):
            painter.drawEllipse(QPointF(x, y), 1.25, 1.45)
    elif name == "interaction":
        painter.drawEllipse(QPointF(7.0, 8.0), 3.0, 3.0)
        painter.drawLine(QPointF(10.0, 10.2), QPointF(13.7, 13.5))
        painter.drawLine(QPointF(2.0, 3.4), QPointF(3.5, 4.9))
        painter.drawLine(QPointF(6.2, 1.5), QPointF(6.2, 3.5))
        painter.drawLine(QPointF(1.5, 8.0), QPointF(3.5, 8.0))
    elif name == "speed":
        painter.drawArc(QRectF(2.0, 3.0, 12.0, 12.0), 0, 180 * 16)
        painter.drawLine(QPointF(8.0, 9.0), QPointF(11.6, 5.6))
        painter.setBrush(QBrush(color)); painter.drawEllipse(QPointF(8.0, 9.0), 1.0, 1.0); painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawLine(QPointF(3.0, 11.8), QPointF(13.0, 11.8))
    elif name == "physics":
        painter.drawEllipse(QPointF(4.2, 4.0), 2.0, 2.0)
        path.moveTo(2.0, 12.8)
        path.cubicTo(5.0, 8.7, 8.8, 14.2, 14.0, 8.5)
        painter.drawPath(path)
        painter.drawLine(QPointF(11.0, 8.7), QPointF(14.0, 8.5))
        painter.drawLine(QPointF(13.7, 8.5), QPointF(13.0, 11.4))
    elif name == "character":
        painter.drawEllipse(QPointF(8.0, 5.0), 2.8, 2.8)
        painter.drawArc(QRectF(2.8, 8.0, 10.4, 6.0), 18 * 16, 144 * 16)
    elif name == "corner":
        painter.drawLine(QPointF(13.5, 4.0), QPointF(13.5, 13.5)); painter.drawLine(QPointF(4.0, 13.5), QPointF(13.5, 13.5))
        painter.drawLine(QPointF(3.0, 3.0), QPointF(10.5, 10.5)); painter.drawLine(QPointF(6.5, 10.5), QPointF(10.5, 10.5)); painter.drawLine(QPointF(10.5, 6.5), QPointF(10.5, 10.5))
    elif name == "pin":
        painter.drawRoundedRect(QRectF(4.5, 2.0, 7.0, 5.0), 1.0, 1.0)
        painter.drawLine(QPointF(5.5, 7.0), QPointF(3.5, 10.0)); painter.drawLine(QPointF(10.5, 7.0), QPointF(12.5, 10.0))
        painter.drawLine(QPointF(3.5, 10.0), QPointF(12.5, 10.0)); painter.drawLine(QPointF(8.0, 10.0), QPointF(8.0, 14.0))
    elif name == "pause":
        painter.drawRoundedRect(QRectF(3.5, 2.5, 3.2, 11.0), 0.7, 0.7); painter.drawRoundedRect(QRectF(9.3, 2.5, 3.2, 11.0), 0.7, 0.7)
    elif name == "spawn":
        # A small fish is more semantic than a coloured avatar and keeps the
        # modern menu's monochrome outline language coherent.
        body = QPainterPath()
        body.moveTo(3.0, 8.0)
        body.cubicTo(5.2, 3.9, 10.8, 3.9, 13.0, 8.0)
        body.cubicTo(10.8, 12.1, 5.2, 12.1, 3.0, 8.0)
        painter.drawPath(body)
        painter.drawPolygon(QPolygonF([QPointF(3.2, 8.0), QPointF(1.2, 4.8), QPointF(1.2, 11.2)]))
        painter.drawPoint(QPointF(10.5, 7.0))
    elif name == "autostart":
        painter.drawArc(QRectF(2.0, 2.0, 12.0, 12.0), 38 * 16, 282 * 16)
        painter.setBrush(QBrush(color)); painter.drawPolygon(QPolygonF([QPointF(10.7, 1.9), QPointF(14.0, 2.5), QPointF(12.1, 5.3)])); painter.setBrush(Qt.BrushStyle.NoBrush)
    elif name == "size":
        painter.drawLine(QPointF(3.0, 13.0), QPointF(13.0, 3.0))
        painter.drawLine(QPointF(3.0, 8.5), QPointF(3.0, 13.0)); painter.drawLine(QPointF(7.5, 13.0), QPointF(3.0, 13.0))
        painter.drawLine(QPointF(8.5, 3.0), QPointF(13.0, 3.0)); painter.drawLine(QPointF(13.0, 3.0), QPointF(13.0, 7.5))
    elif name == "harness":
        painter.drawRoundedRect(QRectF(1.5, 2.5, 13.0, 11.0), 1.5, 1.5)
        painter.drawLine(QPointF(4.0, 6.0), QPointF(6.0, 8.0)); painter.drawLine(QPointF(6.0, 8.0), QPointF(4.0, 10.0)); painter.drawLine(QPointF(8.0, 10.0), QPointF(11.5, 10.0))
    elif name == "web":
        painter.drawEllipse(QPointF(8.0, 8.0), 6.0, 6.0)
        painter.drawEllipse(QPointF(8.0, 8.0), 2.7, 6.0)
        painter.drawLine(QPointF(2.3, 8.0), QPointF(13.7, 8.0))
    elif name == "template":
        painter.drawRoundedRect(QRectF(1.5, 2.0, 5.0, 12.0), 1.0, 1.0)
        painter.drawRoundedRect(QRectF(8.0, 2.0, 6.5, 5.0), 1.0, 1.0); painter.drawRoundedRect(QRectF(8.0, 8.5, 6.5, 5.5), 1.0, 1.0)
    elif name in {"application", "launcher"}:
        painter.drawRoundedRect(QRectF(2.0, 2.0, 12.0, 12.0), 2.0, 2.0)
        for x, y in ((5.0, 5.0), (11.0, 5.0), (5.0, 11.0), (11.0, 11.0)):
            painter.drawEllipse(QPointF(x, y), 1.15, 1.15)
    elif name == "island":
        painter.drawRoundedRect(QRectF(1.5, 4.2, 13.0, 7.6), 3.8, 3.8)
        painter.setBrush(QBrush(color))
        painter.drawEllipse(QPointF(11.2, 8.0), 1.15, 1.15)
        painter.setBrush(Qt.BrushStyle.NoBrush)
    elif name == "automation":
        painter.drawLine(QPointF(4.0, 4.0), QPointF(11.8, 7.8))
        painter.drawLine(QPointF(4.0, 12.0), QPointF(11.8, 8.2))
        painter.drawEllipse(QPointF(3.5, 3.8), 2.0, 2.0)
        painter.drawEllipse(QPointF(3.5, 12.2), 2.0, 2.0)
        painter.drawEllipse(QPointF(12.5, 8.0), 2.0, 2.0)
    elif name == "todo":
        # 清单板：圆角板面 + 两行勾选，与整体单色描边语言一致。
        painter.drawRoundedRect(QRectF(2.0, 2.0, 12.0, 12.0), 2.0, 2.0)
        painter.drawLine(QPointF(4.4, 5.6), QPointF(5.6, 6.8)); painter.drawLine(QPointF(5.6, 6.8), QPointF(7.4, 4.8))
        painter.drawLine(QPointF(9.2, 5.8), QPointF(11.8, 5.8))
        painter.drawLine(QPointF(4.4, 10.2), QPointF(5.6, 11.4)); painter.drawLine(QPointF(5.6, 11.4), QPointF(7.4, 9.4))
        painter.drawLine(QPointF(9.2, 10.4), QPointF(11.8, 10.4))
    elif name == "appearance":
        painter.drawEllipse(QPointF(8.0, 8.0), 5.8, 5.8)
        painter.drawArc(QRectF(4.0, 4.0, 8.0, 8.0), 90 * 16, 180 * 16)
        painter.drawLine(QPointF(8.0, 2.2), QPointF(8.0, 13.8))
    elif name == "back":
        painter.drawLine(QPointF(13.5, 8.0), QPointF(3.0, 8.0))
        painter.drawLine(QPointF(3.0, 8.0), QPointF(7.0, 4.0))
        painter.drawLine(QPointF(3.0, 8.0), QPointF(7.0, 12.0))
    elif name == "save":
        # 软盘：闭合外框 + 顶部写入槽 + 底部标签块。「保存并退出」原先借的是返回箭头
        # （语义相反），这枚才是保存。与其它图标同一 16 单位描边语言（细线、圆头、单色）。
        painter.drawRoundedRect(QRectF(2.7, 2.2, 10.6, 11.6), 1.3, 1.3)
        painter.drawLine(QPointF(5.9, 2.2), QPointF(5.9, 6.3))
        painter.drawLine(QPointF(5.9, 6.3), QPointF(10.1, 6.3))
        painter.drawLine(QPointF(10.1, 6.3), QPointF(10.1, 2.2))
        painter.drawRoundedRect(QRectF(5.3, 9.9, 5.4, 3.9), 0.7, 0.7)
    elif name == "minimize":
        painter.drawLine(QPointF(3.0, 11.5), QPointF(13.0, 11.5))
    elif name == "clear":
        painter.drawLine(QPointF(3.0, 12.5), QPointF(13.0, 12.5))
        painter.drawLine(QPointF(5.0, 12.5), QPointF(5.0, 5.0))
        painter.drawLine(QPointF(11.0, 12.5), QPointF(11.0, 5.0))
        painter.drawLine(QPointF(3.8, 5.0), QPointF(12.2, 5.0))
        painter.drawLine(QPointF(6.0, 2.8), QPointF(10.0, 2.8))
    elif name == "add":
        painter.drawEllipse(QPointF(8.0, 8.0), 6.0, 6.0)
        painter.drawLine(QPointF(8.0, 4.5), QPointF(8.0, 11.5)); painter.drawLine(QPointF(4.5, 8.0), QPointF(11.5, 8.0))
    elif name == "remove":
        painter.drawLine(QPointF(3.0, 3.0), QPointF(13.0, 13.0)); painter.drawLine(QPointF(13.0, 3.0), QPointF(3.0, 13.0))
    elif name == "more":
        painter.setBrush(QBrush(color))
        for x in (4.0, 8.0, 12.0): painter.drawEllipse(QPointF(x, 8.0), 0.9, 0.9)
        painter.setBrush(Qt.BrushStyle.NoBrush)
    elif name == "multi_select":
        painter.drawEllipse(QPointF(4.0, 4.5), 1.5, 1.5)
        painter.drawEllipse(QPointF(4.0, 11.5), 1.5, 1.5)
        painter.drawLine(QPointF(7.5, 4.5), QPointF(13.5, 4.5))
        painter.drawLine(QPointF(7.5, 11.5), QPointF(13.5, 11.5))
    elif name == "rename" or name == "edit":
        # Document + pencil is recognisable even at native 16 px menu size;
        # the previous three-line mark looked like an unexplained hook.
        painter.drawRoundedRect(QRectF(2.2, 2.0, 8.3, 11.5), 1.2, 1.2)
        painter.drawLine(QPointF(4.2, 5.0), QPointF(8.2, 5.0))
        painter.drawLine(QPointF(4.2, 7.4), QPointF(7.0, 7.4))
        painter.setBrush(QBrush(color))
        pencil = QPolygonF([
            QPointF(6.8, 12.8), QPointF(7.5, 10.2),
            QPointF(12.6, 5.1), QPointF(14.2, 6.7), QPointF(9.1, 11.8),
        ])
        painter.drawPolygon(pencil)
        painter.setBrush(Qt.BrushStyle.NoBrush)
    elif name == "info":
        painter.drawEllipse(QRectF(1.5, 1.5, 13.0, 13.0))
        painter.drawPoint(QPointF(8.0, 5.0))
        painter.drawLine(QPointF(8.0, 7.5), QPointF(8.0, 11.0))
    elif name == "external_link":
        painter.drawLine(QPointF(8.0, 2.0), QPointF(14.0, 2.0))
        painter.drawLine(QPointF(14.0, 2.0), QPointF(14.0, 8.0))
        painter.drawLine(QPointF(14.0, 2.0), QPointF(7.0, 9.0))
        path.moveTo(5.0, 3.0); path.lineTo(2.0, 3.0); path.lineTo(2.0, 14.0)
        path.lineTo(13.0, 14.0); path.lineTo(13.0, 11.0); painter.drawPath(path)
    elif name == "copy":
        painter.drawRoundedRect(QRectF(5.0, 3.0, 8.0, 9.0), 1.3, 1.3)
        painter.drawRoundedRect(QRectF(2.5, 5.5, 8.0, 8.0), 1.3, 1.3)
    elif name == "retry":
        painter.drawArc(QRectF(2.4, 2.4, 11.2, 11.2), 35 * 16, 285 * 16)
        painter.drawLine(QPointF(10.8, 2.8), QPointF(13.8, 3.2)); painter.drawLine(QPointF(13.8, 3.2), QPointF(12.4, 6.0))
    elif name in {"thumbs_up", "thumbs_down"}:
        if name == "thumbs_down": painter.scale(1.0, -1.0); painter.translate(0, -16)
        path.moveTo(3.0, 7.0); path.lineTo(6.0, 7.0); path.lineTo(8.2, 3.0)
        path.cubicTo(8.8, 1.9, 10.1, 2.5, 10.0, 3.8); path.lineTo(9.8, 6.0)
        path.lineTo(13.0, 6.0); path.lineTo(12.0, 13.0); path.lineTo(6.0, 13.0); path.lineTo(3.0, 11.5); path.closeSubpath(); painter.drawPath(path)
    elif name == "attach":
        path.moveTo(5.1, 8.9); path.lineTo(9.5, 4.5); path.cubicTo(12.2, 1.8, 15.1, 5.0, 12.7, 7.4)
        path.lineTo(7.0, 13.1); path.cubicTo(3.0, 17.1, -0.7, 12.3, 2.5, 9.1); path.lineTo(8.1, 3.5); painter.drawPath(path)
    elif name == "send":
        painter.drawPolygon(QPolygonF([QPointF(2.0, 8.0), QPointF(13.5, 2.5), QPointF(10.8, 13.5), QPointF(7.7, 9.0)]))
        painter.drawLine(QPointF(2.0, 8.0), QPointF(7.7, 9.0)); painter.drawLine(QPointF(7.7, 9.0), QPointF(13.5, 2.5))
    elif name == "stop":
        painter.drawRoundedRect(QRectF(4.0, 4.0, 8.0, 8.0), 1.3, 1.3)
    elif name == "sidebar":
        painter.drawRoundedRect(QRectF(2.0, 2.5, 12.0, 11.0), 2.0, 2.0)
        painter.drawLine(QPointF(6.1, 2.8), QPointF(6.1, 13.2))
        painter.drawLine(QPointF(3.8, 5.5), QPointF(4.7, 5.5))
        painter.drawLine(QPointF(3.8, 8.0), QPointF(4.7, 8.0))
    elif name in {"tools", "functions"}:
        painter.drawRoundedRect(QRectF(1.5, 2.0, 13.0, 12.0), 2.0, 2.0)
        painter.drawLine(QPointF(5.0, 5.0), QPointF(11.0, 11.0)); painter.drawEllipse(QPointF(4.5, 4.5), 1.5, 1.5); painter.drawEllipse(QPointF(11.5, 11.5), 1.5, 1.5)
    elif name in {"sound", "speaker", "music"}:
        # 扬声器图标
        path.moveTo(3.0, 6.0)
        path.lineTo(6.0, 6.0)
        path.lineTo(9.5, 3.0)
        path.lineTo(9.5, 13.0)
        path.lineTo(6.0, 10.0)
        path.lineTo(3.0, 10.0)
        path.closeSubpath()
        painter.drawPath(path)
        painter.drawArc(QRectF(10.0, 5.5, 4.0, 5.0), -60 * 16, 120 * 16)
        painter.drawArc(QRectF(9.0, 3.5, 7.0, 9.0), -60 * 16, 120 * 16)
    elif name == "exit":
        painter.drawLine(QPointF(3.0, 3.0), QPointF(13.0, 13.0)); painter.drawLine(QPointF(13.0, 3.0), QPointF(3.0, 13.0))
    else:
        painter.drawEllipse(QPointF(8.0, 8.0), 5.5, 5.5)

    painter.end()
    icon = QIcon(pixmap)
    states = _settings_icon_states(menu)
    if states is not None:
        # 设置窗内的图标：静息是画布本身，选中/禁用按同一 alpha 覆色。项视图、菜单都按
        # QIcon::Mode 取图，所以侧栏选中行与禁用项无需额外接线就换色（见视觉规范 Icons）。
        for mode, role in _SETTINGS_ICON_MODES:
            icon.addPixmap(_tinted_pixmap(pixmap, states[role]), mode)
    if _icon_consumer(menu):
        _remember_themed_icon(menu, name, size)
    return icon


def vector_widget_icon(widget, name: str, size: int = 18) -> QIcon:
    """Render the same icon language for sidebar and form widgets."""
    return vector_menu_icon(widget, name, size)


def pet_avatar_menu_icon(menu: QMenu, pet) -> QIcon:
    """Fill the native icon slot while neutralising source Retina DPR metadata."""
    size = small_icon_size(menu)
    dpr = menu.devicePixelRatioF() or 1.0
    source = pet.icon_pixmap(max(32, round(size * dpr * 2)))
    return fitted_pet_pixmap_icon(menu, source)


def fitted_pet_pixmap_icon(menu: QMenu, source: QPixmap) -> QIcon:
    size = small_icon_size(menu)
    dpr = menu.devicePixelRatioF() or 1.0
    if source is None or source.isNull():
        return QIcon()
    # A frame pixmap may retain DPR=2. Drawing it by point makes Qt interpret a
    # 32px source as only 16 logical px and was the cause of the tiny avatar.
    source.setDevicePixelRatio(1.0)
    # Animation frames use the full video canvas. Scaling that canvas directly
    # reduced the visible character to only a few pixels in the 18px menu slot.
    # Crop transparent padding first, exactly as the runtime pet icon does.
    image = source.toImage()
    bounds = QRegion(QBitmap.fromImage(image.createAlphaMask())).boundingRect()
    if bounds.isValid() and not bounds.isEmpty():
        source = QPixmap.fromImage(image.copy(bounds))
    canvas = QPixmap(max(1, round(size * dpr)), max(1, round(size * dpr)))
    canvas.setDevicePixelRatio(dpr)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    extent = size * 1.04
    ratio = min(extent / source.width(), extent / source.height())
    width = source.width() * ratio
    height = source.height() * ratio
    target = QRectF((size - width) / 2.0, (size - height) / 2.0 - 0.2, width, height)
    painter.drawPixmap(target, source, QRectF(0, 0, source.width(), source.height()))
    painter.end()
    return QIcon(canvas)
