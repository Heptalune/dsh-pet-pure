# -*- coding: utf-8 -*-
"""设置窗「外框」三件事：侧栏宽度跟着内容走、导航可达、窗口挂正式桌宠图标。

为什么单独一个文件：这三条都是**壳层几何/窗口属性**（侧栏、标题栏），与设置项、
持久化、搜索候选集无关；本仓库的设置测试按主题分文件（``test_settings_page_guide``
管页内布局、``test_settings_reset_defaults`` 管重置链路），壳层单独立一份便于定位。

口径（都落在真实 ``ModernSettingsDialog`` 这个公开 seam 上）：

1. **侧栏宽度 = ``max(200, 内容所需)``**：200px 是设计令牌，但放大字体下按钮文案会
   长过它——本机实测 1.5 倍字体（20px）时侧栏按钮（「重置本宠设置…」）的 ``sizeHint``
   要 186px，而 200px 的侧栏内宽只有 175px，文字会被省略号吃掉。宽度按**最宽的
   非撑满子控件**算，默认字体下恒为 200（逐像素不变），放大字体下跟着长；**构造之后
   字体才变大也要跟上**（系统放大字体 / 无障碍放大 / 截图脚本的 ``--font-scale`` 都是
   这条路，所以不能只在构造时算一次）。
2. **导航可达**：默认窗口 800×560 下 11 个能力域必须全部落在视口里（默认尺寸就把
   「更新」截掉一半＝用户看不到还有这一页）；720×500 下允许滚动，但每一行都要能选中
   并切到对应页。
3. **窗口图标**：``setWindowTitle`` 之后挂正式桌宠图标（``load_application_icon``），
   原生标题栏不再摆系统给的那一个；图标拿不到时照旧能建窗（空图标，不抛异常）。
"""
from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QFrame,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

import pet.application_icon as application_icon  # noqa: E402
import pet.modern_settings_dialog as settings_mod  # noqa: E402
from pet.config import Config  # noqa: E402
from pet.settings_widgets import (  # noqa: E402
    CONTENT_READING_MAX_WIDTH,
    SIDEBAR_MIN_WIDTH,
    SettingsSection,
    SettingsTabContainer,
    sidebar_content_width,
)

app = QApplication.instance() or QApplication([])

#: 1.5 倍字体（与 ``scripts/capture_settings_pages.py --font-scale 1.5`` 同一口径）：
#: 无障碍/系统放大字体下按钮文案最容易撑破侧栏。
FONT_SCALE = 1.5


def _scale_fonts(root: QWidget, scale: float) -> None:
    """按截图脚本同一做法逐控件放大像素字号（构造之后再放大）。"""
    for widget in [root, *root.findChildren(QWidget)]:
        font = widget.font()
        pixels = font.pixelSize()
        if pixels > 0:
            font.setPixelSize(max(1, round(pixels * scale)))
            widget.setFont(font)


def _dialog(tmp_path: Path, monkeypatch, *, width: int = 800, height: int = 560,
            font_scale: float = 1.0):
    monkeypatch.setattr(settings_mod.autostart_mod, "is_enabled", lambda: False)
    dialog = settings_mod.ModernSettingsDialog(Config(base=tmp_path), include_ai=True)
    dialog.resize(width, height)
    dialog.show()
    app.processEvents()
    if font_scale != 1.0:
        _scale_fonts(dialog, font_scale)
        # 与截图脚本同一收尾：改完字体各 resize 一拍，让布局按新字号落定。
        dialog.resize(width + 1, height)
        app.processEvents()
        dialog.resize(width, height)
        app.processEvents()
    return dialog


def _pane(dialog):
    pane = dialog.findChild(QFrame, "sidebarPane")
    assert pane is not None, "侧栏容器不见了（sidebarPane）"
    return pane


# ------------------------------------------------------------------ 1. 侧栏宽度
def test_sidebar_keeps_the_200px_token_at_the_default_font(tmp_path, monkeypatch):
    """默认字体下侧栏宽度仍是设计令牌 200px（这条防"顺手改宽"）。"""
    dialog = _dialog(tmp_path, monkeypatch)
    try:
        assert _pane(dialog).width() == SIDEBAR_MIN_WIDTH
    finally:
        dialog.close()


def test_sidebar_width_ignores_widgets_that_fill_the_column():
    """撑满列的控件不参与宽度需求——判据是**策略**，不是宽度。

    这条钉的是"为什么不能直接用 ``layout.minimumSize()``"：搜索框这类控件的
    ``sizeHint`` 是 Qt 默认的「十七个字符宽」（本机默认字体下实测 210px），照它算会把
    侧栏顶到 290px 这种与内容无关的宽度。

    **前提不许写成绝对阈值**：注册过应用字体之后 Qt 对默认家族的解析会变（``tests/
    test_screen_capture_ocr.py`` 的 ``addApplicationFont`` 一跑，'x' 的 advance 实测
    12 → 6，同一个 ``QLineEdit.sizeHint`` 从 210 掉到 108），于是"更宽"这个前提会随字体库
    状态漂。这里改成自己造一个**确定很宽**的撑满控件来立前提。
    """
    host = QWidget()
    layout = QVBoxLayout(host)
    try:
        filler = QLineEdit(host)
        assert filler.sizePolicy().horizontalPolicy() == QSizePolicy.Policy.Expanding
        layout.addWidget(filler)
        assert sidebar_content_width(layout) == 0, "撑满列的控件不该产生宽度需求"

        # 再宽也一样：宽度超过令牌、策略是撑满 → 仍然不参与（判据是策略）。
        wide = QLabel("跨平台同步与自动恢复策略的超长本地化标题示例", host)
        wide.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout.addWidget(wide)
        assert wide.sizeHint().width() > SIDEBAR_MIN_WIDTH, "前提：这个撑满控件自己确实很宽"
        assert sidebar_content_width(layout) == 0, "再宽也不参与：判据是策略不是宽度"

        button = QPushButton("保存并退出", host)
        layout.addWidget(button)
        assert sidebar_content_width(layout) == button.sizeHint().width()
    finally:
        host.deleteLater()
        app.processEvents()


def test_sidebar_grows_so_button_copy_is_not_elided_at_150_percent_font(
        tmp_path, monkeypatch):
    """放大字体后侧栏按钮不许被压到小于自己的 ``sizeHint``（文字被省略号吃掉）。

    判据是**按钮自己的宽度 ≥ sizeHint 宽度**：``QPushButton`` 里文字比控件宽时 Qt
    直接省略，用户看到的是「重置本宠设…」这种半截文案。
    """
    dialog = _dialog(tmp_path, monkeypatch, font_scale=FONT_SCALE)
    try:
        pane = _pane(dialog)
        assert pane.width() > SIDEBAR_MIN_WIDTH, (
            "放大字体后侧栏没有跟着变宽：按钮文案会被省略号吃掉")
        buttons = [
            ("保存并退出", dialog.save_exit_button),
            ("本宠重置", dialog.reset_defaults_btn),
            ("全局重置", getattr(dialog, "reset_global_defaults_btn", None)),
        ]
        for name, button in buttons:
            if button is None:
                continue
            assert button.width() >= button.sizeHint().width(), (
                f"{name} 按钮被压到 {button.width()}px（需要 {button.sizeHint().width()}px）"
                f"：文案「{button.text()}」会被省略")
    finally:
        dialog.close()


def test_sidebar_width_tracks_a_font_change_after_construction(tmp_path, monkeypatch):
    """构造之后再放大字体也要跟上（尺寸只在构造时算是同一条缺陷的另一种表现）。"""
    dialog = _dialog(tmp_path, monkeypatch)
    try:
        pane = _pane(dialog)
        assert pane.width() == SIDEBAR_MIN_WIDTH
        _scale_fonts(dialog, FONT_SCALE)
        app.processEvents()
        assert pane.width() > SIDEBAR_MIN_WIDTH, (
            "字体是在构造之后变大的：侧栏宽度没跟上，按钮文案会被省略")
    finally:
        dialog.close()


# ------------------------------------------------------------------ 2. 导航可达
def test_default_window_shows_every_domain_without_scrolling(tmp_path, monkeypatch):
    """默认窗口 800×560：11 个能力域全部落在侧栏视口里（不必滚动才发现「更新」）。

    判据是 ``scrollBar.maximum() == 0``：内容只要比视口高 1px，Qt 就把它抬到 1（实测
    556 → 555 的分界），没有"差一点也算过"的余地。余量只来自侧栏纵向间距——导航行高是
    显式 ``setSizeHint``（与字体无关），其余子控件的高度由 13px 字号与 QSS
    ``min-height`` 决定。CI 280 上 6px 间距留的 4px 余量被 Ubuntu runner 的字体/风格吃光，
    第 10 行落到视口外（ubuntu-latest 红、windows/macOS 绿）；间距因此收到 3px，
    本机余量 4 → 19px。2026-10-21 能力域 10 → 11（新增「功能」域）：行高按同一判据
    从 34 校准到 31（实测 11 × 31 余量 17px，与之前相当），间距与行距不动。
    别的平台再把这份余量吃掉，说明"默认尺寸真的装不下"，
    修法是继续收间距或按字体度量重算预算，不是放宽这条断言。
    """
    dialog = _dialog(tmp_path, monkeypatch)
    try:
        sidebar = dialog.sidebar
        assert sidebar.count() >= 11, "能力域少了一个（本用例会假绿）"
        assert sidebar.verticalScrollBar().maximum() == 0, (
            "默认尺寸下侧栏仍需滚动：最后一个能力域被截在视口外")
        viewport = sidebar.viewport().height()
        for index in range(sidebar.count()):
            rect = sidebar.visualItemRect(sidebar.item(index))
            assert rect.bottom() <= viewport, (
                f"「{sidebar.item(index).text()}」落在视口外（{rect.bottom()} > {viewport}）")
    finally:
        dialog.close()


@pytest.mark.parametrize("width,height", [(800, 560), (720, 500)])
def test_every_domain_is_reachable_at_both_window_sizes(tmp_path, monkeypatch,
                                                        width, height):
    """两个尺寸下每个能力域都能滚进视口并选中切页（键盘/滚轮可达的机器判据）。

    720×500 下侧栏装不下 11 行，需要滚动——这里量的是"滚得到、选得中、切得过去"：
    只断言 ``setCurrentRow`` 会漏掉"选中了但在视口外"这种不可达。
    """
    dialog = _dialog(tmp_path, monkeypatch, width=width, height=height)
    try:
        sidebar = dialog.sidebar
        viewport = sidebar.viewport().height()
        for index in range(sidebar.count()):
            item = sidebar.item(index)
            sidebar.scrollToItem(item)
            app.processEvents()
            rect = sidebar.visualItemRect(item)
            assert rect.top() >= 0 and rect.bottom() <= viewport, (
                f"「{item.text()}」滚不进视口（{rect.top()}..{rect.bottom()} / {viewport}）")
            sidebar.setCurrentRow(index)
            app.processEvents()
            assert dialog.pages.currentIndex() == index, f"「{item.text()}」切不过去"
    finally:
        dialog.close()


@pytest.mark.parametrize("width,height", [(800, 560), (720, 500)])
def test_sidebar_children_are_not_clipped_by_the_pane(tmp_path, monkeypatch,
                                                      width, height):
    """侧栏子控件（保存/搜索/两个重置按钮/版本号）都完整落在侧栏里，不被底边切掉。"""
    dialog = _dialog(tmp_path, monkeypatch, width=width, height=height,
                     font_scale=FONT_SCALE)
    try:
        pane = _pane(dialog)
        children = [
            ("保存并退出", dialog.save_exit_button),
            ("搜索框", dialog.search_edit),
            ("导航", dialog.sidebar),
            ("本宠重置", dialog.reset_defaults_btn),
            ("全局重置", getattr(dialog, "reset_global_defaults_btn", None)),
            ("版本号", dialog.version_footer),
        ]
        for name, widget in children:
            if widget is None:
                continue
            bottom = widget.geometry().y() + widget.geometry().height()
            assert bottom <= pane.height(), (
                f"{name} 被侧栏底边裁掉（{bottom} > {pane.height()}）")
    finally:
        dialog.close()


# ------------------------------------------------------------------ 3. 窗口图标
def test_settings_window_carries_the_application_icon(tmp_path, monkeypatch):
    """窗口图标 = 正式桌宠图标（``load_application_icon`` 的同一份）。"""
    dialog = _dialog(tmp_path, monkeypatch)
    try:
        icon = dialog.windowIcon()
        assert not icon.isNull(), "设置窗没拿到正式图标（原生标题栏会摆系统通用图标）"
        assert not application_icon.load_application_icon().isNull(), (
            "前提：仓库里应当有 assets/icon.ico")
        assert icon.cacheKey() == application_icon.load_application_icon().cacheKey()
    finally:
        dialog.close()


def test_settings_window_still_opens_when_the_icon_is_unavailable(tmp_path, monkeypatch):
    """图标缺失/读不出时照旧建窗（空图标，不抛）：图标是装饰，不该挡住设置页。"""
    monkeypatch.setattr(application_icon, "_ICON_CACHE", None)
    monkeypatch.setattr(
        application_icon, "application_icon_candidates",
        lambda: (tmp_path / "missing.ico",),
    )
    monkeypatch.setattr(
        settings_mod, "load_application_icon", application_icon.load_application_icon)
    dialog = _dialog(tmp_path, monkeypatch)
    try:
        assert dialog.windowIcon().isNull(), "找不到图标时应当就是空图标"
        assert dialog.windowTitle() == "桌宠设置"
    finally:
        dialog.close()


# ------------------------------------------------------------------ 4. 表单阅读宽度
def _settle(turns: int = 3) -> None:
    """把布局推平：切页/视口尺寸变化都会再排一拍（页头宽度跟着视口走，见
    ``_SettingsPageShell._sync_heading_width``）。"""
    for _turn in range(turns):
        app.processEvents()


def _content_of(dialog, domain: str):
    """域页的可滚动内容与视口（页内标签域取当前标签页）。"""
    index = next(i for i in range(dialog.sidebar.count())
                 if dialog.sidebar.item(i).text() == domain)
    page = dialog.pages.widget(index)
    scroll = page.findChild(QScrollArea, "settingsScroll")
    content = scroll.widget()
    tabs = content.findChild(SettingsTabContainer, "settingsTaskTabs")
    root = tabs.stack.currentWidget() if tabs is not None else content
    return page, scroll, root


#: 表单最大阅读宽度的**定稿值**：宽过它的一律按这一列排（设计令牌，见
#: ``settings_widgets.CONTENT_READING_MAX_WIDTH``）。写死在这里是有意的——改这个数
#: 必须同时改这条用例，等于把"表单看着多宽"变成一次显式决定。
READING_WIDTH = 800


def test_wide_window_keeps_the_form_at_a_reading_width(tmp_path, monkeypatch):
    """宽窗下表单不拉满整屏：内容宽封顶 **800px**（页头跟着同一个契约）。

    「菜单」域例外：布局编辑器要"编辑区 + 预览"两栏并排，自己在内容上声明了更宽的值。
    """
    assert CONTENT_READING_MAX_WIDTH == READING_WIDTH, (
        "阅读宽度令牌被改动：这是设计决定，改它要连这条用例一起改")
    dialog = _dialog(tmp_path, monkeypatch, width=1600, height=900)
    try:
        checked = 0
        for index in range(dialog.sidebar.count()):
            domain = dialog.sidebar.item(index).text()
            dialog.sidebar.setCurrentRow(index)
            _settle()
            page, scroll, root = _content_of(dialog, domain)
            if scroll is None:
                continue  # 「更新」是命令面板，不是设置表单
            if domain == "菜单":
                assert root.maximumWidth() > READING_WIDTH, (
                    "布局编辑器需要更宽的两栏，自己声明的上限应当大于默认值")
                continue
            assert root.width() == READING_WIDTH, (
                f"「{domain}」在 1600px 宽的窗口里内容宽 {root.width()}px，"
                f"没有收到阅读宽度 {READING_WIDTH}px")
            checked += 1
        assert checked >= 8, f"只量到 {checked} 个设置域，本用例会假绿"
    finally:
        dialog.close()


@pytest.mark.parametrize("width,height", [(720, 500), (800, 560), (1100, 760), (1600, 900)])
def test_heading_and_content_share_one_width_contract(tmp_path, monkeypatch, width, height):
    """页头与滚动内容同宽；两条居中链最多差半个滚动条（有滚动条的页上）。

    宽度必须**逐像素相等**：页头走的是"滚动视口宽"上限，内容走的是同一个视口（
    ``_SettingsPageShell.resizeEvent`` 取视口而不是页宽，否则有滚动条的页头会宽出一条）。

    左边缘允许一个**有界**的小差（≤ 滚动条宽）：页头由页布局居中、内容由滚动视口居中，
    两条链差半个滚动条。这是既有几何（不是本刀引入），所以这里钉的是"差得有界"而不是
    绝对相等——真要绝对相等得让页头跟着内容走，那是另一件事。
    """
    dialog = _dialog(tmp_path, monkeypatch, width=width, height=height)
    try:
        for index in range(dialog.sidebar.count()):
            domain = dialog.sidebar.item(index).text()
            dialog.sidebar.setCurrentRow(index)
            _settle()
            page, scroll, root = _content_of(dialog, domain)
            header = page.findChild(QWidget, "pageHeader")
            if scroll is None or header is None:
                continue
            content_left = root.mapTo(page, root.rect().topLeft()).x()
            assert header.width() == root.width(), (
                f"{width}×{height} 「{domain}」：页头 {header.width()}px / 内容 "
                f"{root.width()}px，宽度契约不一致")
            extent = max(0, scroll.width() - scroll.viewport().width())
            if extent == 0:
                assert header.geometry().x() == content_left, (
                    f"{width}×{height} 「{domain}」：没有滚动条时页头与内容必须同左边缘"
                    f"（{header.geometry().x()} / {content_left}）")
            else:
                drift = abs(header.geometry().x() - content_left)
                assert drift <= extent, (
                    f"{width}×{height} 「{domain}」：页头与内容错开 {drift}px，"
                    f"超过滚动条宽 {extent}px")
    finally:
        dialog.close()


def test_compact_window_never_clips_the_form(tmp_path, monkeypatch):
    """最小窗口（720×500）下没有横向裁切：内容不宽过视口、横向滚动条无需出现。

    页面用的是 ``ScrollBarAlwaysOff``：真超宽就是**切掉右边**，不是能滚过去。
    """
    dialog = _dialog(tmp_path, monkeypatch, width=720, height=500)
    try:
        for index in range(dialog.sidebar.count()):
            domain = dialog.sidebar.item(index).text()
            dialog.sidebar.setCurrentRow(index)
            _settle()
            page, scroll, root = _content_of(dialog, domain)
            if scroll is None:
                continue
            assert root.width() <= scroll.viewport().width() + 1, (
                f"「{domain}」内容 {root.width()}px 宽过视口 "
                f"{scroll.viewport().width()}px（右边被切掉）")
            assert scroll.horizontalScrollBar().maximum() == 0, domain
    finally:
        dialog.close()


# ------------------------------------------------------------------ 5. 保存并退出
def test_save_and_exit_button_asks_for_the_save_icon(tmp_path, monkeypatch):
    """「保存并退出」按 **save** 这个名字渲图标（此前要的是 back 的返回箭头，与动作相反）。

    **为什么断言的是"要了哪个名字"而不是渲染结果**：名字是"这一枚字形画的是什么"的唯一
    判据（``save``/``back`` 历史上一度同形），而按钮的图标与 tooltip 现在都由
    ``pet/settings_widgets.settings_save_exit_button`` 落——所以拦的是**控件库**里的渲染入口。
    """
    import pet.context_menus.icons as icons_mod
    from pet.modern_settings_dialog import vector_widget_icon as real_renderer
    from pet.settings_widgets import SAVE_EXIT_TOOLTIP

    requested: list[str] = []

    def _recording_renderer(widget, name, size=18):
        requested.append(name)
        return real_renderer(widget, name, size)

    monkeypatch.setattr("pet.settings_widgets.vector_widget_icon", _recording_renderer)
    dialog = _dialog(tmp_path, monkeypatch)
    try:
        assert "save" in requested, f"保存并退出没按 save 渲染图标（实得 {requested}）"
        assert dialog.save_exit_button.icon().isNull() is False, "图标不该是空的"
        # 没有「未保存」信号（表单草稿只存在于控件里，Config 读不到），保存时机就写在这条
        # tooltip 上：关窗也会保存、两个音效开关即时写盘、失败时的出口。
        assert dialog.save_exit_button.toolTip() == SAVE_EXIT_TOOLTIP
        assert "关窗" in SAVE_EXIT_TOOLTIP and "立刻写盘" in SAVE_EXIT_TOOLTIP
    finally:
        dialog.close()
    assert icons_mod.vector_widget_icon is not None  # 渲染入口本身没被动过


#: 一级能力域的准入判据（`docs/SETTINGS-CHANGE-GATES.md`「准入记录」末段）：新增一级域须
#: "代表长期独立用户目标、预计包含**至少两个**稳定设置组"。下面这两个域是该判据落地之前
#: 就在的既成事实，作为快照豁免；它们现在是 1 组就写 1 组——将来变多，这条断言会逼你更新
#: 快照，免得豁免名单悄悄扩大成垃圾桶。
DOMAINS_WITH_ONE_GROUP = {"文件识别", "更新"}


def test_new_top_level_domain_must_own_at_least_two_setting_groups(tmp_path, monkeypatch):
    """新增一级域必须自带 ≥2 个稳定设置组，否则应当做页内标签而不是开域。

    2026-10-17 的【趣味】域就是反例：只有一组「抽签」，为把它塞进默认 800×560 还要把
    侧栏布局间距与导航行距压到 0（**所有域**的点击目标一起缩水），最终按准入被改回
    「互动」域的页内标签。判据原本只是文件里的一句话，本用例把它变成机器门——同一类设计
    错误下次会在提交前就撞墙，而不是等评审来发现。
    """
    dialog = _dialog(tmp_path, monkeypatch)
    try:
        groups = {}
        for index in range(dialog.sidebar.count()):
            name = dialog.sidebar.item(index).text()
            page = dialog.pages.widget(index)
            assert page is not None, f"「{name}」没有对应页"
            groups[name] = len(page.findChildren(SettingsSection))

        thin = {name: count for name, count in groups.items()
                if count < 2 and name not in DOMAINS_WITH_ONE_GROUP}
        assert not thin, (
            f"这些一级域只有 {thin} 个设置组：新增一级域须 ≥2 个稳定设置组，"
            f"否则改做同域页内标签（判据见 docs/SETTINGS-CHANGE-GATES.md「准入记录」）")

        for name in sorted(DOMAINS_WITH_ONE_GROUP & set(groups)):
            assert groups[name] == 1, (
                f"「{name}」已不是单组域（{groups[name]} 组）：请更新 DOMAINS_WITH_ONE_GROUP 快照")
        stale = DOMAINS_WITH_ONE_GROUP - set(groups)
        assert not stale, f"快照里的 {sorted(stale)} 已不在侧栏：请更新 DOMAINS_WITH_ONE_GROUP"
    finally:
        dialog.close()
