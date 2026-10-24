# -*- coding: utf-8 -*-
"""「互动」域页内分标签（点击与音效 / 自言自语 / 天气 / 趣味）的聚焦回归。

背景：主人反馈「设置界面有点乱，单页面太多东西」。互动域原先 18 行 / 3 组**平铺、
零折叠**，是本仓库最长的"无折叠"域之一；「菜单」域已用 ``SettingsTabContainer``
把同域的多个同级任务分成页内标签（见 test_menu_layout 里的菜单域用例）。

本文件钉住四件事：
1. 互动域确实用页内标签组织，且**侧栏域清单不变**（域数量/顺序是外部契约，
   2026-10-21 新增「功能」域后 11 个）；
2. 每个设置行都能通过它的标签**到达**（切到对应标签后可见）——"整理后功能仍可用"；
3. 设置搜索命中非默认标签里的行时，**自动切到那个标签**（否则结果不可见）；
4. 全局不变量：所有 ``settingRow_*`` 仍落在某个域页里，不出现「待分类（开发期）」。
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

EXPECTED_SIDEBAR = [
    "常规", "桌宠", "互动", "菜单", "桌面组件", "功能", "AI 与对话", "自动化与联动", "语音", "文件识别", "更新",
]
# 标签键/名：键给代码（稳定），名给用户（可读）。顺序 = 使用顺序。
# 「天气」（issue #209，2026-10-07）与「点击显示余额」同族——都是"桌宠替你看一眼
# 外部数据"的互动面，故挂在同一个域里当第三个同级任务，而不是新开侧栏域；
# 「抽签」（每日一签，2026-10-15）同族（用户主动拉一次、桌宠给一个结果），是第四个。
# 「快捷键」（2026-10-18）是第五个：用户怎么唤出这三个动作，同属"人宠交互"面。
EXPECTED_TABS = (("click", "点击与音效"), ("self_talk", "自言自语"), ("weather", "天气"),
                 ("divination", "趣味"), ("hotkeys", "快捷键"))

# 每组行各自应该落在哪个标签
ROWS_BY_TAB = {
    "hotkeys": ("hotkey_open_chat", "hotkey_look_screen", "hotkey_screen_capture"),
    "click": (
        "mouse_through", "click_sound", "click_sound_pack", "click_sound_volume",
        "click_sound_preview", "click_balance", "click_self_talk", "click_self_talk_speak",
        "click_self_talk_precache", "click_talk_bindings", "golden_spin_click",
        "golden_spin_direct",
    ),
    "self_talk": (
        "self_talk_bubble_style", "self_talk", "self_talk_duration", "self_talk_min",
        "self_talk_max", "self_talk_texts", "self_talk_images", "self_talk_image_scale",
        "self_talk_image_chance",
    ),
    "weather": ("weather_enabled", "weather_city", "weather_city_list"),
    "divination": ("divination_enabled", "divination_speak", "divination_preview",
                   "divination_hint"),
}


def _qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def dialog(tmp_path, monkeypatch):
    import pet.modern_settings_dialog as settings_mod
    from pet.config import Config

    app = _qapp()
    monkeypatch.setattr(settings_mod.autostart_mod, "is_enabled", lambda: False)
    dlg = settings_mod.ModernSettingsDialog(Config(tmp_path), include_ai=True)
    yield dlg
    dlg.close()
    app.processEvents()


def _interaction_page(dialog):
    index = next(
        i for i in range(dialog.sidebar.count()) if dialog.sidebar.item(i).text() == "互动"
    )
    return dialog.pages.widget(index)


def _row(dialog, key: str):
    from pet.modern_settings_dialog import SettingRow

    found = dialog.findChild(SettingRow, f"settingRow_{key}")
    assert found is not None, f"缺少设置行 {key}"
    return found


def test_interaction_domain_uses_in_page_tabs_without_changing_sidebar(dialog):
    """页内标签组织互动域，侧栏域清单不变（域契约是外部可见的）。"""
    from pet.settings_widgets import SettingsTabContainer

    assert [dialog.sidebar.item(i).text() for i in range(dialog.sidebar.count())] == EXPECTED_SIDEBAR

    page = _interaction_page(dialog)
    tabs = page.findChild(SettingsTabContainer, "settingsTaskTabs")
    assert tabs is not None, "互动域没有用页内标签组织"
    assert tabs.keys() == tuple(key for key, _ in EXPECTED_TABS)
    assert tabs.labels() == tuple(label for _, label in EXPECTED_TABS)

    # 默认落在第一个标签，且标签容器本身在互动域页内
    assert tabs.currentKey() == EXPECTED_TABS[0][0]
    assert page.isAncestorOf(tabs)


def test_every_interaction_row_routes_to_its_tab_and_is_reachable(dialog):
    """每行都能由标签键路由到，且父开关打开后在所属标签里可见（分页没丢功能）。

    半数行另有**父开关显隐联动**（既有契约，见 test_desktop_pet_features 的依赖显隐
    用例与 test_click_self_talk_speech）：``self_talk_*`` 细项随「气泡自言自语」、
    ``click_self_talk_*`` / ``click_talk_bindings`` 随「点击触发自言自语」、
    ``click_sound_*`` 细项随「点击音效」、``golden_spin_direct`` 随「点击回旋」。
    所以先把这四个父开关全打开，再断言"激活标签后确实看得到"。
    """
    from pet.settings_widgets import SettingsTabContainer

    for parent in ("self_talk_check", "click_self_talk_check", "click_sound_check", "golden_spin_click_check"):
        getattr(dialog, parent).setChecked(True)
    tabs = _interaction_page(dialog).findChild(SettingsTabContainer, "settingsTaskTabs")
    for tab_key, keys in ROWS_BY_TAB.items():
        tabs.setCurrentKey(tab_key)
        for key in keys:
            row = _row(dialog, key)
            assert tabs.key_for_descendant(row) == tab_key, (
                f"{key} 期望落在 {tab_key} 标签，实际 {tabs.key_for_descendant(row)}"
            )
            assert row.isVisibleTo(tabs) is True, f"{key} 在 {tab_key} 标签里不可见"


def test_rows_of_the_other_tab_are_out_of_view_but_not_destroyed(dialog):
    """另一标签的行只是不可见（isVisibleTo False），对象与取值都还在——整理没有删功能。"""
    from pet.settings_widgets import SettingsTabContainer

    tabs = _interaction_page(dialog).findChild(SettingsTabContainer, "settingsTaskTabs")
    tabs.setCurrentKey("click")

    hidden_row = _row(dialog, "self_talk_bubble_style")
    assert hidden_row.isVisibleTo(tabs) is False
    assert hidden_row.isHidden() is False, "行只是不在当前标签页，不是被隐藏/删除"
    assert hidden_row.control is not None, "控件对象必须仍在（保存链依赖它）"

    tabs.setCurrentKey("self_talk")
    assert hidden_row.isVisibleTo(tabs) is True


def test_search_jumps_to_the_owning_tab(dialog):
    """搜索命中非默认标签的行 → 自动切标签（否则结果在视野外，等于搜不到）。"""
    from pet.settings_widgets import SettingsTabContainer

    app = _qapp()
    dialog.self_talk_check.setChecked(True)  # 让「配图概率」按父开关联动显现
    tabs = _interaction_page(dialog).findChild(SettingsTabContainer, "settingsTaskTabs")
    assert tabs.currentKey() == "click"

    dialog.search_edit.setText("配图概率")
    for _ in range(3):
        app.processEvents()
    assert dialog.sidebar.currentItem().text() == "互动"
    assert tabs.currentKey() == "self_talk"
    assert _row(dialog, "self_talk_image_chance").isVisibleTo(tabs) is True

    dialog.search_edit.setText("点击音效")
    for _ in range(3):
        app.processEvents()
    assert tabs.currentKey() == "click"
    assert _row(dialog, "click_sound").isVisibleTo(tabs) is True


def test_sections_keep_their_titles_inside_the_tabs(dialog):
    """组名不变（外部契约/既有用例硬编码）：输入 / 点击反馈 在第一标签，自言自语 在第二。"""
    from PySide6.QtWidgets import QLabel

    from pet.settings_widgets import SettingsSection, SettingsTabContainer

    tabs = _interaction_page(dialog).findChild(SettingsTabContainer, "settingsTaskTabs")

    def section_titles(tab_key: str) -> list[str]:
        tabs.setCurrentKey(tab_key)
        return [
            section.findChild(QLabel, "sectionTitle").text()
            for section in tabs.stack.currentWidget().findChildren(SettingsSection)
        ]

    assert section_titles("click") == ["输入", "点击反馈"]
    assert section_titles("self_talk") == ["自言自语"]
    assert section_titles("weather") == ["天气"]
    assert section_titles("divination") == ["抽签"]


def test_click_speak_toggle_starts_unchecked_like_the_config_default(dialog):
    """点击台词朗读默认关闭：设置页控件初始未勾选，与 Config 默认值同口径。

    「配置默认值 / 规范化默认值 / 运行时缺省」三处必须同一个口径，否则会出现
    「配置说关、界面却显示开」这种自相矛盾的默认状态。
    """
    assert dialog.config.get("self_talk_speak_enabled") is False
    assert dialog.click_self_talk_speak_check.isChecked() is False


def test_every_setting_row_still_lives_in_a_domain_page(dialog):
    """全局不变量：没有孤儿行、没有掉进「待分类（开发期）」。"""
    from PySide6.QtWidgets import QLabel, QWidget

    from pet.modern_settings_dialog import SettingRow

    pages = [dialog.pages.widget(i) for i in range(dialog.pages.count())]
    orphans = [
        row.objectName()
        for row in dialog.findChildren(SettingRow)
        if not any(page.isAncestorOf(row) for page in pages)
    ]
    assert orphans == [], f"这些行没有落在任何域页里：{orphans}"

    labels = [label.text() for label in dialog.findChildren(QLabel)]
    assert "待分类（开发期）" not in labels
    assert all(page is not None for page in pages)
    assert isinstance(_interaction_page(dialog), QWidget)


# —— 每日一签（「互动 · 抽签」）归位后的四条聚焦口径 ——————————————————
# 它 2026-10-15 曾以一个只有一组「抽签」的**一级域【趣味】**进设置页，靠把侧栏
# 布局间距 3→0 / 导航行距 1→0 挤进 800×560。2026-10-17 按
# docs/SETTINGS-CHANGE-GATES.md 的准入（新一级域须"预计包含至少两个稳定设置组"）
# 判不合格：域撤回、行归位到「互动」页内第四个标签，侧栏间距与行高一个都不动。

def test_divination_rows_hang_off_the_interaction_domain_not_a_new_sidebar_domain(dialog):
    """侧栏没有叫「趣味」的一级域——抽签的行在「互动」域里。"""
    from pet.settings_widgets import SettingsTabContainer

    labels = [dialog.sidebar.item(i).text() for i in range(dialog.sidebar.count())]
    assert labels == EXPECTED_SIDEBAR
    assert "趣味" not in labels, "只有一组的「趣味」一级域不得存在（设置门准入不合格）"

    page = _interaction_page(dialog)
    tabs = page.findChild(SettingsTabContainer, "settingsTaskTabs")
    tabs.setCurrentKey("divination")
    for key in ROWS_BY_TAB["divination"]:
        assert page.isAncestorOf(_row(dialog, key)), f"{key} 必须落在「互动」域页里"


def test_sidebar_spacing_and_row_height_are_not_squeezed(dialog):
    """侧栏间距 3 / 导航行距 1 不许压（压它们 = 拿所有域的呼吸空间换一个域）。

    行高是另一条预算：2026-10-21 新增「功能」域（10 → 11 域，拍板决策，见
    docs/SETTINGS-CHANGE-GATES.md）后按「默认 800×560 装下全部域」重新校准为 31——
    11 × 31 在本机余量 17px，与 10 × 34 时的 19px 相当（间距与行距一个都没动）。
    再压行高（< 31）或动间距/行距来多塞域，都不许。
    """
    from PySide6.QtWidgets import QFrame

    from pet.settings_widgets import SIDEBAR_ROW_HEIGHT, SettingsSidebarPane

    pane = dialog.findChild(SettingsSidebarPane) or dialog.findChild(QFrame, "sidebarPane")
    assert pane is not None, "侧栏容器没找到"
    assert pane.layout().spacing() == 3, "侧栏布局间距被压过（原值 3）"
    assert dialog.sidebar.spacing() == 1, "侧栏导航行距被压过（原值 1）"
    assert SIDEBAR_ROW_HEIGHT == 31
    assert dialog.sidebar.sizeHintForRow(0) >= SIDEBAR_ROW_HEIGHT


def test_divination_rows_are_searchable_and_search_switches_to_their_tab(dialog):
    """搜「抽签」/「每日一签」都能到行，且自动切到「抽签」标签（否则结果在视野外）。"""
    from pet.settings_widgets import SettingsTabContainer

    app = _qapp()
    tabs = _interaction_page(dialog).findChild(SettingsTabContainer, "settingsTaskTabs")
    for query, key in (("抽签", "divination_enabled"), ("每日一签", "divination_enabled"),
                       ("试听今天的签", "divination_preview")):
        tabs.setCurrentKey("click")
        dialog.search_edit.setText(query)
        for _ in range(3):
            app.processEvents()
        assert dialog.sidebar.currentItem().text() == "互动", f"搜「{query}」没跳到互动域"
        assert tabs.currentKey() == "divination", f"搜「{query}」没切到抽签标签"
        assert _row(dialog, key).isVisibleTo(tabs) is True, f"搜「{query}」的行不可见"
    dialog.search_edit.setText("")
    app.processEvents()


def test_divination_toggles_take_part_in_the_default_marks_but_the_preview_button_does_not(dialog):
    """三个开关可比较默认值（进「已修改」标记）；「试听」是按钮行，不参与。

    口径见 ``pet/settings_default_marks.py``：只认四类控件，行键与顶层配置键同名时
    不必显式登记（先查表再回落）。抽签三行正好落在"同名回落"这条路径上，所以这里
    钉的既是标记覆盖面，也是"没有第二张默认值表"。
    """
    registered = {row.objectName()[len("settingRow_"):] for row, _c, _d in dialog._default_mark_rows}
    for key in ("divination_enabled", "divination_speak", "divination_hint"):
        assert key in registered, f"{key} 应有可比默认值（进标记覆盖面）"
    assert "divination_preview" not in registered, "按钮行不参与默认值标记"

    from pet.settings_widgets import SettingsTabContainer

    tabs = _interaction_page(dialog).findChild(SettingsTabContainer, "settingsTaskTabs")
    tabs.setCurrentKey("divination")
    speak = _row(dialog, "divination_speak")
    assert speak.control.isChecked() is False
    # 用 isHidden（显式 hide/show 的意图）而不是 isVisible：本 fixture 没有 show() 窗口，
    # isVisible 在整棵树上恒为 False，量不到"这一行自己出没出标记"。
    assert speak.modified_mark.isHidden() is True, "默认态不该有标记"
    speak.control.setChecked(True)
    _qapp().processEvents()
    assert speak.modified_mark.isHidden() is False, "改成非默认值就该出标记"
