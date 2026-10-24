# -*- coding: utf-8 -*-
"""设置窗导航图标的主题/状态取色、保存图标语义与侧栏选中态。

为什么单独一个文件：这三条都属于**设置窗导航外观**（侧栏图标、保存并退出按钮、侧栏
选中态），与设置项、持久化、搜索候选集无关；本仓库设置测试按主题分文件
（``test_settings_sidebar_and_chrome`` 管壳层几何、``test_settings_page_guide`` 管页内
布局），导航外观单独立一份便于定位。

口径（都落在真实渲染像素或公开 QSS 上，不做「非空」式断言）：

1. **主题取色**：设置窗内的图标跟随**设置窗自己的主题**（``settingsDark`` 真值 +
   「跟随系统」兜底），深色侧栏上必须是浅灰 ``#d6d6d6``——改前是恒定的 ``#595959``，
   在深色侧栏（``#26262b``）上只有约 2.2:1 对比度，用户看不清。
2. **三态**：静息灰 / 选中强调蓝 ``#0a84ff`` / 禁用淡灰，三态互不相同。项视图按
   ``QIcon::Mode`` 取图标，所以侧栏选中行不需要额外接线就能换色。
3. **不波及别的消费者**：聊天窗与右键菜单只打 ``menuStyle=modern``（没有设置窗标记），
   颜色必须与改动前逐像素一致——那正是白底白图回归的修复点。
4. **保存图标语义**：``保存并退出`` 不能再用返回箭头（改前 ``back`` 与 ``save`` 同一
   分支）。软盘图标是一圈闭合外框，返回箭头不是。
5. **侧栏选中态**：选中行带 3px 强调色左条（左内边距同步减 3，文字位置不跳），且悬停态
   与侧栏底色的亮度差必须明显小于选中态——否则「悬停哪一行」看起来就像「选中哪一行」。
   注：``::item:selected`` 上的 ``font-weight`` 实测不被 Qt 渲染，所以文字加粗不可用，
   状态只能靠强调蓝（左条 + 图标）与灰度层次表达。
"""
from __future__ import annotations

import os
import re
from collections import Counter
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import QSize  # noqa: E402
from PySide6.QtGui import QIcon  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

import pet.context_menus.icons as icons  # noqa: E402
from pet.config import Config  # noqa: E402
from pet.settings_theme_qss import _settings_stylesheet, settings_selection_accent  # noqa: E402

app = QApplication.instance() or QApplication([])

SIZE = 16
REST_LIGHT = "#595959"
REST_DARK = "#d6d6d6"
#: 规范强调色：焦点环、菜单语义色、深色档的选中态都是它。
ACCENT = "#0a84ff"
LIGHT_PANE = "#f7f7f8"
DARK_PANE = "#26262b"


# ----------------------------------------------------------------- 像素工具
def _visible_colors(pixmap) -> Counter:
    image = pixmap.toImage()
    counts: Counter = Counter()
    for y in range(image.height()):
        for x in range(image.width()):
            color = image.pixelColor(x, y)
            if color.alpha() > 200:
                counts[color.name()] += 1
    return counts


def _dominant(pixmap) -> str:
    counts = _visible_colors(pixmap)
    assert counts, "图标没有画出任何可见像素"
    return counts.most_common(1)[0][0]


def _signature(pixmap) -> tuple:
    image = pixmap.toImage()
    return tuple(
        (x, y, image.pixelColor(x, y).name())
        for y in range(image.height())
        for x in range(image.width())
        if image.pixelColor(x, y).alpha() > 200
    )


def _quadrant_ink(pixmap) -> dict[str, int]:
    """四象限可见像素数；中缝各留 2px，避免中线笔画同时落进相邻两象限。"""
    image = pixmap.toImage()
    mid_x, mid_y, band = image.width() // 2, image.height() // 2, 2
    zones = {"tl": 0, "tr": 0, "bl": 0, "br": 0}
    for y in range(image.height()):
        for x in range(image.width()):
            if image.pixelColor(x, y).alpha() <= 200:
                continue
            if abs(x - mid_x) < band or abs(y - mid_y) < band:
                continue
            zones[("t" if y < mid_y else "b") + ("l" if x < mid_x else "r")] += 1
    return zones


def _relative_luminance(color: str) -> float:
    channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(one: str, other: str) -> float:
    """WCAG 对比度（非文本图形对象按 1.4.11 要 ≥3:1）。"""
    first, second = _relative_luminance(one), _relative_luminance(other)
    return (max(first, second) + 0.05) / (min(first, second) + 0.05)


def _ink(icon: QIcon, mode: QIcon.Mode = QIcon.Mode.Normal) -> str:
    """一枚图标在某个模式下的主墨色。"""
    return _dominant(icon.pixmap(QSize(SIZE, SIZE), mode))


# ----------------------------------------------------------------- 承载控件
def _settings_host(dark: bool | None = None) -> QWidget:
    """设置窗表面：``ModernSettingsDialog.__init__`` 打的同一组标记。

    ``dark=None`` 复现构造期那一刻——导航图标在 ``_apply_selected_theme`` 之前就渲染了，
    所以 ``settingsDark`` 还没挂上。
    """
    host = QWidget()
    host.setProperty("menuStyle", "modern")
    host.setProperty("modernStyle", True)
    if dark is not None:
        host.setProperty("settingsDark", dark)
    return host


def _menu_host() -> QWidget:
    """聊天窗/新版右键菜单：只有 ``menuStyle=modern``，没有设置窗标记。"""
    host = QWidget()
    host.setProperty("menuStyle", "modern")
    return host


def _pixmap(host, name: str, mode: QIcon.Mode = QIcon.Mode.Normal):
    return icons.vector_widget_icon(host, name, SIZE).pixmap(QSize(SIZE, SIZE), mode)


# ----------------------------------------------------------------- 1. 主题取色
@pytest.mark.parametrize("dark,expected", [(False, REST_LIGHT), (True, REST_DARK)])
def test_settings_icon_rest_colour_follows_the_settings_theme(dark, expected):
    host = _settings_host(dark)
    assert _dominant(_pixmap(host, "settings")) == expected


def test_settings_icon_falls_back_to_the_system_theme_before_it_is_published(monkeypatch):
    """构造期没有 ``settingsDark`` 时按「跟随系统」兜底（设置窗主题的默认档）。

    若这里退回恒定 ``#595959``，深色系统下侧栏图标在 ``#26262b`` 上只有约 2.2:1 对比度。
    """
    monkeypatch.setattr("pet.settings_theme_qss._system_dark", lambda: True)
    assert _dominant(_pixmap(_settings_host(), "settings")) == REST_DARK
    monkeypatch.setattr("pet.settings_theme_qss._system_dark", lambda: False)
    assert _dominant(_pixmap(_settings_host(), "settings")) == REST_LIGHT


def test_chat_and_context_menu_icons_keep_the_light_surface_ink(monkeypatch):
    """深色系统下聊天窗图标仍是深灰（白底白图回归的修复点），强调底仍取浅灰。"""
    monkeypatch.setattr("pet.settings_theme_qss._system_dark", lambda: True)
    assert _dominant(_pixmap(_menu_host(), "minimize")) == REST_LIGHT
    accent_host = _menu_host()
    accent_host.setProperty("modernDark", True)
    assert _dominant(_pixmap(accent_host, "minimize")) == REST_DARK


def test_context_menu_icons_never_take_the_selected_accent():
    """右键菜单不分设置窗的状态色：三种模式下都不出现选中强调色（规范色或压深档）。"""
    host = _menu_host()
    accents = {
        settings_selection_accent(False), settings_selection_accent(True), ACCENT,
    }
    for name in ("settings", "save"):
        icon = icons.vector_menu_icon(host, name, SIZE)
        for mode in (QIcon.Mode.Normal, QIcon.Mode.Selected, QIcon.Mode.Disabled):
            drawn = set(_visible_colors(icon.pixmap(QSize(SIZE, SIZE), mode)))
            assert not (drawn & accents), (
                f"「{name}」在 {mode.name} 模式染上了设置窗的强调色：{drawn & accents}")


# ----------------------------------------------------------------- 2. 三态
@pytest.mark.parametrize("dark,rest", [(False, REST_LIGHT), (True, REST_DARK)])
def test_settings_icon_has_three_distinct_states(dark, rest):
    host = _settings_host(dark)
    icon = icons.vector_widget_icon(host, "appearance", SIZE)
    rest_ink = _dominant(icon.pixmap(QSize(SIZE, SIZE), QIcon.Mode.Normal))
    selected_ink = _dominant(icon.pixmap(QSize(SIZE, SIZE), QIcon.Mode.Selected))
    disabled_ink = _dominant(icon.pixmap(QSize(SIZE, SIZE), QIcon.Mode.Disabled))
    assert rest_ink == rest
    assert selected_ink == settings_selection_accent(dark), (
        "选中态应当用设置窗的选中强调色（与侧栏左条同一个真值）")
    assert disabled_ink not in (rest_ink, selected_ink)
    assert _relative_luminance(disabled_ink) != _relative_luminance(rest_ink)


@pytest.mark.parametrize("dark", [False, True])
def test_disabled_state_is_weaker_than_rest_on_its_own_theme(dark, monkeypatch):
    """禁用态是「降对比仍可读」：与主题底色更近，但没淡到不可见。"""
    pane = DARK_PANE if dark else LIGHT_PANE
    host = _settings_host(dark)
    icon = icons.vector_widget_icon(host, "appearance", SIZE)
    rest_ink = _dominant(icon.pixmap(QSize(SIZE, SIZE), QIcon.Mode.Normal))
    disabled_ink = _dominant(icon.pixmap(QSize(SIZE, SIZE), QIcon.Mode.Disabled))
    pane_luminance = _relative_luminance(pane)
    gap_rest = abs(_relative_luminance(rest_ink) - pane_luminance)
    gap_disabled = abs(_relative_luminance(disabled_ink) - pane_luminance)
    assert 0 < gap_disabled < gap_rest


# ----------------------------------------------------------------- 3. 保存图标
def test_save_glyph_is_not_the_back_arrow():
    """现状 ``back`` 与 ``save`` 共用左箭头分支：保存按钮画的是「返回」。"""
    host = _menu_host()
    save = _pixmap(host, "save")
    back = _pixmap(host, "back")
    assert _signature(save) != _signature(back), "保存图标仍然是返回箭头"


def test_save_glyph_is_a_closed_disk_frame_while_back_is_an_arrow():
    host = _menu_host()
    save_zones = _quadrant_ink(_pixmap(host, "save"))
    back_zones = _quadrant_ink(_pixmap(host, "back"))
    assert all(save_zones[zone] > 0 for zone in "tl tr bl br".split()), (
        f"保存图标应当是一圈闭合外框（软盘），四象限都有笔画，实测 {save_zones}")
    assert back_zones["br"] == 0, (
        f"返回箭头不该有右下角笔画，实测 {back_zones}（那是软盘外框才有的形状）")


@pytest.mark.parametrize("dark,expected", [(False, REST_LIGHT), (True, REST_DARK)])
def test_save_glyph_is_theme_aware_too(dark, expected):
    assert _dominant(_pixmap(_settings_host(dark), "save")) == expected


# ----------------------------------------------------------------- 4. 侧栏选中态 QSS
def _rules(stylesheet: str, selector: str) -> list[str]:
    # 先去掉 /* … */ 注释：注释里也含有 ':' 与 ';'，会让声明解析错位。
    cleaned = re.sub(r"/\*.*?\*/", "", stylesheet, flags=re.DOTALL)
    found = re.findall(re.escape(selector) + r"\s*\{([^}]*)\}", cleaned)
    assert found, f"QSS 里没有 {selector} 规则"
    return found


def _last_rule(stylesheet: str, selector: str) -> str:
    """同一份样式表里后声明的规则覆盖先声明的（同特异性），取最后一条。"""
    return _rules(stylesheet, selector)[-1]


def _declaration(rule: str, prop: str) -> str:
    match = re.search(rf"^\s*{re.escape(prop)}\s*:\s*([^;}}]+)", rule, re.MULTILINE)
    assert match, f"规则里没有 {prop}：{rule!r}"
    return match.group(1).strip()


SIDEBAR_ITEM = "QListWidget#settingsSidebar::item"
SIDEBAR_HOVER = SIDEBAR_ITEM + ":hover"
SIDEBAR_SELECTED = SIDEBAR_ITEM + ":selected"


@pytest.mark.parametrize("theme,pane,dark", [("light", LIGHT_PANE, False), ("dark", DARK_PANE, True)])
def test_selected_sidebar_row_carries_the_accent_bar_without_moving_its_text(theme, pane, dark):
    stylesheet = _settings_stylesheet(theme)
    selected = _last_rule(stylesheet, SIDEBAR_SELECTED)
    assert _declaration(selected, "border-left") == f"3px solid {settings_selection_accent(dark)}"
    # 左内边距同步减 3，被选中的那一行文字位置与其它行一致（不会左右跳）。
    # 静息态的左内边距来自基础 ::item 规则（深色覆盖段只重述 color，不重述 padding）。
    rest_padding = next(
        (
            _declaration(rule, "padding")
            for rule in _rules(stylesheet, SIDEBAR_ITEM)
            if re.search(r"^\s*padding\s*:", rule, re.MULTILINE)
        ),
        "",
    )
    assert rest_padding, "静息态侧栏行没有 padding，无法核对边框与内边距之和"
    rest_padding_left = int(re.match(r"(\d+)px", rest_padding.split()[1]).group(1))
    selected_padding_left = int(re.match(r"(\d+)px", _declaration(selected, "padding-left")).group(1))
    assert selected_padding_left + 3 == rest_padding_left, (
        f"选中态左内边距 {selected_padding_left}px + 边框 3px 应当等于静息态 {rest_padding_left}px")


@pytest.mark.parametrize("theme,dark", [("light", False), ("dark", True)])
def test_selected_ink_and_bar_share_one_accent_clear_of_three_to_one(theme, dark):
    """选中态的**两处**指示（图标 ink 与侧栏左条）取同一个强调色，且对选中行底色 ≥3:1。

    改前的取舍：图标沿用 Qt 自动染色，浅色下 4.67:1 看着够，但那是把强调蓝与底色混成的
    浑浊色，深色下只剩 1.90:1；换成规范蓝后深色 3.07:1 达标、浅色反而掉到 2.89:1——低于
    WCAG 1.4.11 对图形对象要求的 3:1。所以浅色档压深一档，并让 QSS 与图标共用同一真值
    （``settings_selection_accent``）：两处各写一个色号，迟早只改一处。
    """
    stylesheet = _settings_stylesheet(theme)
    selected = _last_rule(stylesheet, SIDEBAR_SELECTED)
    bar = _declaration(selected, "border-left").split()[-1]
    ink = _ink(icons.vector_widget_icon(_settings_host(dark), "appearance", SIZE),
               QIcon.Mode.Selected)
    assert ink == bar == settings_selection_accent(dark), (
        f"选中图标 ink {ink} 与侧栏左条 {bar} 必须是同一个强调色")
    row_background = _declaration(selected, "background")
    assert _contrast(ink, row_background) >= 3.0, (
        f"{theme} 档选中态对选中行底色 {row_background} 只有 "
        f"{_contrast(ink, row_background):.2f}:1")


@pytest.mark.parametrize("theme,pane", [("light", LIGHT_PANE), ("dark", DARK_PANE)])
def test_selected_rule_wins_over_hover_on_the_same_row(theme, pane):
    """悬停已选中行时仍按选中态画：选中规则必须声明在悬停规则之后。"""
    stylesheet = _settings_stylesheet(theme)
    assert stylesheet.rindex(SIDEBAR_SELECTED) > stylesheet.rindex(SIDEBAR_HOVER)


@pytest.mark.parametrize("theme,pane", [("light", LIGHT_PANE), ("dark", DARK_PANE)])
def test_hover_stays_clearly_weaker_than_the_selected_state(theme, pane):
    """悬停只是提示，不许和「已选中」抢同一档灰度。

    判据：悬停与底色的亮度差 ≤ 选中态与底色亮度差的 40%（实测现状 light 为 60%
    ——悬停行看起来就像被选中的那一行），同时悬停不能等于底色（那样就没了悬停反馈）。
    """
    stylesheet = _settings_stylesheet(theme)
    pane_luminance = _relative_luminance(pane)
    hover_gap = abs(_relative_luminance(_declaration(_last_rule(stylesheet, SIDEBAR_HOVER), "background")) - pane_luminance)
    selected_gap = abs(_relative_luminance(_declaration(_last_rule(stylesheet, SIDEBAR_SELECTED), "background")) - pane_luminance)
    assert hover_gap > 0, "悬停态与底色相同：看不到悬停反馈"
    assert hover_gap <= selected_gap * 0.4, (
        f"悬停与底色的亮度差 {hover_gap:.4f} 相对选中态 {selected_gap:.4f} 太大："
        "悬停行看起来就像被选中的那一行")


# ----------------------------------------------------------------- 5. 真实设置窗
def _dialog(tmp_path: Path, monkeypatch, *, dark: bool):
    import pet.modern_settings_dialog as settings_mod

    monkeypatch.setattr(settings_mod.autostart_mod, "is_enabled", lambda: False)
    monkeypatch.setattr(settings_mod, "_system_dark", lambda: dark)
    monkeypatch.setattr("pet.settings_theme_qss._system_dark", lambda: dark)
    dialog = settings_mod.ModernSettingsDialog(Config(base=tmp_path), include_ai=True)
    dialog.resize(800, 560)
    dialog.show()
    app.processEvents()
    return dialog


def _row_band(dialog, sidebar, index: int, x0: int, x1: int) -> Counter:
    """侧栏第 index 行在 [x0, x1) 列区间里的实际渲染像素。"""
    rect = sidebar.visualItemRect(sidebar.item(index))
    origin = sidebar.mapTo(dialog, rect.topLeft())
    image = dialog.grab().toImage()
    return Counter(
        image.pixelColor(x, y).name()
        for y in range(origin.y() + 2, origin.y() + rect.height() - 2)
        for x in range(origin.x() + x0, origin.x() + x1)
    )


@pytest.mark.parametrize("dark", [False, True])
def test_selected_sidebar_row_paints_the_accent_icon_and_bar(tmp_path, monkeypatch, dark):
    """端到端：Qt 的项视图真的按 ``QIcon::Mode.Selected`` 取到了选中强调色，选中行也有左条。"""
    dialog = _dialog(tmp_path, monkeypatch, dark=dark)
    try:
        sidebar = dialog.sidebar
        accent = settings_selection_accent(dark)
        assert dialog.property("settingsDark") is dark, "前提：设置窗已按本用例的主题落定"
        assert sidebar.count() >= 3, "能力域少了一个（本用例会假绿）"
        sidebar.setCurrentRow(1)
        app.processEvents()
        selected_icon = _row_band(dialog, sidebar, 1, 12, 34)
        plain_icon = _row_band(dialog, sidebar, 2, 12, 34)
        assert accent in selected_icon, f"选中行图标没有换成强调色：{selected_icon.most_common(4)}"
        assert accent not in plain_icon, f"未选中行图标不该有强调色：{plain_icon.most_common(4)}"
        assert accent in _row_band(dialog, sidebar, 1, 0, 4), "选中行左侧没有强调条"
        assert accent not in _row_band(dialog, sidebar, 2, 0, 4), "未选中行左侧不该有强调条"
    finally:
        dialog.close()


@pytest.mark.parametrize("dark,expected", [(False, REST_LIGHT), (True, REST_DARK)])
def test_dialog_navigation_icons_are_legible_on_their_own_sidebar(tmp_path, monkeypatch, dark, expected):
    """深色侧栏上导航图标必须是浅灰（现状恒为 ``#595959``：约 2.2:1，看不清）。"""
    dialog = _dialog(tmp_path, monkeypatch, dark=dark)
    try:
        sidebar = dialog.sidebar
        icon = sidebar.item(2).icon()
        ink = _dominant(icon.pixmap(QSize(SIZE, SIZE), QIcon.Mode.Normal))
        assert ink == expected
        pane = DARK_PANE if dark else LIGHT_PANE
        assert _contrast(ink, pane) >= 4.5, f"{ink} 在侧栏 {pane} 上只有 {_contrast(ink, pane):.2f}:1"
    finally:
        dialog.close()


# ------------------------------------------------- 6. 换主题后重画（图标不跟 QSS 自己变色）
def _switchable_dialog(tmp_path, monkeypatch, *, system_dark: bool):
    """构造一个"系统深浅可切换"的设置窗：三处兜底路径都吃同一个真值。

    ``modern_settings_dialog._system_dark`` 是构造期与 ``_apply_selected_theme`` 读的那份；
    ``settings_theme_qss._system_dark`` 是导航图标在 ``settingsDark`` 还没挂上时的兜底
    （改前"深色系统 + 强制浅色"就在这条路径上取错色）。
    """
    import pet.modern_settings_dialog as settings_mod

    monkeypatch.setattr(settings_mod.autostart_mod, "is_enabled", lambda: False)
    monkeypatch.setattr(settings_mod, "_system_dark", lambda: system_dark)
    monkeypatch.setattr("pet.settings_theme_qss._system_dark", lambda: system_dark)
    dialog = settings_mod.ModernSettingsDialog(Config(base=tmp_path), include_ai=True)
    dialog.resize(800, 560)
    dialog.show()
    app.processEvents()
    return dialog


def _select_theme(dialog, value: str) -> None:
    select = dialog.menu_theme_select
    index = select.findData(value)
    assert index >= 0, f"主题下拉里没有 {value} 档（实得 {[select.itemData(i) for i in range(select.count())]}）"
    select.setCurrentIndex(index)
    app.processEvents()


def _settings_surface_inks(dialog) -> dict[str, str]:
    """设置窗里"一定会出现在屏幕上"的几枚图标的当前墨色。"""
    inks = {"侧栏导航": _ink(dialog.sidebar.item(0).icon()),
            "保存并退出": _ink(dialog.save_exit_button.icon())}
    actions = dialog.search_edit.actions()
    if actions:
        inks["搜索"] = _ink(actions[0].icon())
    reset = getattr(dialog, "reset_defaults_btn", None)
    if reset is not None:
        inks["本宠重置"] = _ink(reset.icon())
    return inks


@pytest.mark.parametrize("system_dark,theme,expect_dark", [
    (False, "dark", True),      # 浅色系统 → 强制深色
    (False, "light", False),    # 浅色系统 → 强制浅色
    (True, "light", False),     # 深色系统 + 强制浅色（改前残留风险：图标偏亮地留在浅侧栏上）
    (True, "system", True),     # 深色系统 → 跟随系统
])
def test_theme_switch_repaints_the_settings_surface_icons(
        tmp_path, monkeypatch, system_dark, theme, expect_dark):
    """切主题后导航/保存/搜索/重置的图标必须跟着换墨色（位图不会自己变色）。

    每档都先落一个**相反**的档再切到目标档：只测构造期的颜色证明不了"切了会重画"
    （构造期本来就按兜底档画对了）。
    """
    dialog = _switchable_dialog(tmp_path, monkeypatch, system_dark=system_dark)
    try:
        _select_theme(dialog, "light" if expect_dark else "dark")
        _select_theme(dialog, theme)
        expected = REST_DARK if expect_dark else REST_LIGHT
        assert dialog.property("settingsDark") is expect_dark, (
            "前提：窗口主题真值已经落到目标档")
        inks = _settings_surface_inks(dialog)
        wrong = {name: ink for name, ink in inks.items() if ink != expected}
        assert not wrong, f"切到 {theme}（系统{'深' if system_dark else '浅'}）后这些图标没重画：{wrong}"
    finally:
        dialog.close()


def test_theme_round_trip_brings_the_icons_back(tmp_path, monkeypatch):
    """浅 → 深 → 浅：来回切两次，图标回到原墨色（重画是幂等的，不会越描越淡）。"""
    dialog = _switchable_dialog(tmp_path, monkeypatch, system_dark=False)
    try:
        original = _settings_surface_inks(dialog)
        _select_theme(dialog, "dark")
        assert set(_settings_surface_inks(dialog).values()) == {REST_DARK}
        _select_theme(dialog, "light")
        assert _settings_surface_inks(dialog) == original
    finally:
        dialog.close()


def test_recording_covers_the_icon_consumers_not_their_hosts(tmp_path, monkeypatch):
    """请求必须记在**消费者**控件上：记在别处（编辑器面板、对话框本身）就没人能兑现。

    这条是"宿主换人"那批改动的守卫：``vector_widget_icon(host, ...)`` 的 host 一旦写回
    非消费者（历史代码在菜单编辑器、试听按钮、搜索框上都这么写），重画就静默漏掉它们。
    """
    dialog = _switchable_dialog(tmp_path, monkeypatch, system_dark=False)
    try:
        from pet.settings_widgets import THEMED_ICON_REQUEST

        recorded = {
            widget.property(THEMED_ICON_REQUEST)[0]
            for widget in dialog.findChildren(QWidget)
            if widget.property(THEMED_ICON_REQUEST)
        }
        assert recorded >= {"save", "search", "retry", "edit", "multi_select", "add",
                            "remove", "more", "sound"}, f"漏记的消费者：{recorded}"
        from pet.settings_widgets import refresh_settings_icons

        assert refresh_settings_icons(dialog) >= dialog.sidebar.count() + 5, (
            "重画枚数少于「侧栏导航 + 保存/搜索/重置」这几枚：有消费者没被扫到")
    finally:
        dialog.close()


def _recorded_consumers(dialog) -> list[tuple[QWidget, tuple[str, int]]]:
    """窗内所有"记了图标请求、且现在挂着一枚图标"的消费者及其请求。"""
    from pet.settings_widgets import THEMED_ICON_REQUEST

    found = []
    for widget in dialog.findChildren(QWidget):
        request = widget.property(THEMED_ICON_REQUEST)
        if not request:
            continue
        icon = widget.icon() if callable(getattr(widget, "icon", None)) else None
        if icon is None and widget.actions():
            icon = widget.actions()[0].icon()
        if icon is not None and not icon.isNull():
            found.append((widget, request))
    assert len(found) >= 10, f"只找到 {len(found)} 个消费者（本用例会假绿）"
    return found


def _close_enough(one: str, other: str, tolerance: int = 4) -> bool:
    """两枚墨色是否同色（容差只为抗混叠：小字形的"主色"可能是仅差一两级的近似色）。"""
    return all(abs(int(one[i:i + 2], 16) - int(other[i:i + 2], 16)) <= tolerance
               for i in (1, 3, 5))


def test_every_recorded_consumer_is_drawn_with_the_settings_ink(tmp_path, monkeypatch):
    """挂载顺序决定的墨色缺口：页面内容先独立构造、图标在那一刻渲染 → 拿到的是**应用调色板**
    的墨色（实测黑色），而不是设置窗的 ``#595959``/``#d6d6d6``。

    对话框装配末尾（依赖列表里的 ``_apply_selected_theme``）会按记录重画一遍把所有图标
    拉到当前主题的墨色上；这条用例盯的就是"重画跑过、且真的覆盖到每一枚"。
    """
    for dark, expected in ((False, REST_LIGHT), (True, REST_DARK)):
        dialog = _switchable_dialog(tmp_path, monkeypatch, system_dark=dark)
        try:
            inks = {
                f"{type(widget).__name__}:{request[0]}": _ink(
                    widget.icon() if callable(getattr(widget, "icon", None))
                    else widget.actions()[0].icon())
                for widget, request in _recorded_consumers(dialog)
            }
            wrong = {name: ink for name, ink in inks.items()
                     if not _close_enough(ink, expected)}
            assert not wrong, f"这些图标还是调色板墨色（期望 {expected}）：{wrong}"
        finally:
            dialog.close()
