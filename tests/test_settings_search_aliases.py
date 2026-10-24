# -*- coding: utf-8 -*-
"""设置页第四刀（2026-10-08）搜索增强的行为验收：折叠组标题入索引 + 口语别名表。

背景（第一刀遗留）：搜索索引原本只有「行 label + hint + objectName」，所以

- 搜**折叠组标题**（「启动项（高级）」「碰撞参数（高级）」「网络延迟检测（高级）」…）
  0 命中——组的名字在索引之外；
- 搜**用户口语**（「网速」「开机启动」「密钥」「壁纸」…）0 命中——行文案用的是另一套词。

口径（主代理裁决）：

1. 组标题命中 ⇒ 该组**全部行**进候选（回车逐行可达），并沿用既有「自动展开折叠 /
   切页内标签」机制——不许只高亮组标题却不给行；
2. 口语别名 = 人工策展的小表（不做拼音引擎、不引入依赖），每条别名都要有「真实用户
   会这么说」的理由；
3. 搜索状态行/计数语义不变；多源命中的同一行只算一次；空查询行为不变；
4. 配置键 / 默认值 / 归属 / 依赖语义零改动。

纪律同 ``tests/test_settings_ia_first_cut.py``：断言走真实 ``ModernSettingsDialog``
这个公开 seam（真实搜索框输入 + 真实回车遍历 + 真实折叠态检查），不调私有方法拼结果。
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import pet.modern_settings_dialog as settings_mod
from pet.config import Config
from pet.modern_settings_dialog import SettingRow, SettingsSection
from pet.settings_widgets import SettingsTabContainer

app = QApplication.instance() or QApplication([])

#: 桌宠域两个页内标签（第二刀）的键；「碰撞参数（高级）」属于第 2 个标签。
_TAB_MOTION = "motion_collision"

#: 「碰撞参数（高级）」组内的 5 行（第三刀的口径：音量随物理参数进这一组）。
_COLLISION_ADVANCED_ROWS = (
    "settingRow_collision_restitution",
    "settingRow_collision_friction",
    "settingRow_collision_mass_scale",
    "settingRow_collision_impulse_cap",
    "settingRow_collision_sound_volume",
)


def _dialog(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_mod.autostart_mod, "is_enabled", lambda: False)
    return settings_mod.ModernSettingsDialog(Config(tmp_path), include_ai=True)


def _row(dialog, key) -> SettingRow:
    row = dialog.findChild(SettingRow, f"settingRow_{key}")
    assert row is not None, f"缺少设置行 {key}"
    return row


def _section_of(dialog, key) -> SettingsSection:
    """行所属的 ``SettingsSection``（走 widget 祖先链，不用陈旧的 findChildren）。"""
    parent = _row(dialog, key).parentWidget()
    while parent is not None and not isinstance(parent, SettingsSection):
        parent = parent.parentWidget()
    assert parent is not None, f"{key} 必须落在某个 SettingsSection 内"
    return parent


def _page_of(dialog, domain: str):
    index = next(i for i in range(dialog.sidebar.count())
                 if dialog.sidebar.item(i).text() == domain)
    return dialog.pages.widget(index)


def _tabs_of(page):
    return page if isinstance(page, SettingsTabContainer) else page.findChild(
        SettingsTabContainer, "settingsTaskTabs")


def _current_page(dialog):
    return dialog.pages.widget(dialog.sidebar.currentRow())


def _current_match(dialog) -> SettingRow:
    matches = dialog._search_matches
    assert matches, "搜索没有命中任何行"
    return matches[dialog._search_index]


def _matches(dialog, query) -> list:
    """真实搜索框输入（textChanged），返回**全部**候选行。"""
    dialog.search_edit.setText("")
    app.processEvents()
    dialog.search_edit.setText(query)
    app.processEvents()
    return list(dialog._search_matches)


def _search(dialog, query, *, presses: int = 0) -> SettingRow:
    """真实搜索框输入 + 可选回车遍历（eventFilter），返回当前命中行。"""
    _matches(dialog, query)
    for _ in range(presses):
        QTest.keyClick(dialog.search_edit, Qt.Key.Key_Return)
        app.processEvents()
    return _current_match(dialog)


def _keys(rows) -> list:
    return [row.objectName() for row in rows]


# --------------------------------------------------------------- ① 折叠组标题命中
def test_group_title_query_returns_every_row_of_the_group(tmp_path, monkeypatch):
    """搜「启动项」：命中「启动项（高级）」组内两行（原本 0 命中），回车逐行可达且可见。"""
    dialog = _dialog(tmp_path, monkeypatch)
    try:
        matches = _matches(dialog, "启动项")
        assert _keys(matches) == [
            "settingRow_harness_autostart",
            "settingRow_harness_launch_target",
        ], "搜组标题必须把该组全部行纳入候选"

        for presses in range(len(matches)):
            row = _search(dialog, "启动项", presses=presses)
            section = _section_of(dialog, row.objectName()[len("settingRow_"):])
            assert section.advanced is True
            assert section.toggle.isChecked() is True, "命中折叠组内的行必须自动展开"
            assert not section.card.isHidden()
            assert row.isVisibleTo(_current_page(dialog)), "命中的行必须真实可见"
            assert dialog.sidebar.currentItem().text() == "常规"
            assert dialog.search_status.text().startswith(f"{presses + 1}/2 · ")
    finally:
        dialog.close()


def test_collision_group_title_query_switches_to_the_owning_tab(tmp_path, monkeypatch):
    """搜「碰撞参数」：命中高级组内 5 行、自动切到「动作与碰撞」标签并展开、逐行可达。"""
    dialog = _dialog(tmp_path, monkeypatch)
    try:
        tabs = _tabs_of(_page_of(dialog, "桌宠"))
        assert tabs is not None and tabs.currentKey() != _TAB_MOTION

        matches = _matches(dialog, "碰撞参数")
        assert _keys(matches) == list(_COLLISION_ADVANCED_ROWS), (
            "搜「碰撞参数」= 该高级组全部行，一行不多一行不少"
        )

        seen = set()
        for _ in range(len(matches)):
            row = _current_match(dialog)
            key = row.objectName()[len("settingRow_"):]
            seen.add(row.objectName())
            assert row.objectName() in _COLLISION_ADVANCED_ROWS
            section = _section_of(dialog, key)
            assert section.advanced is True and section.toggle.isChecked() is True
            assert dialog.sidebar.currentItem().text() == "桌宠"
            assert tabs.currentKey() == _TAB_MOTION, "命中另一标签的行必须自动切标签"
            assert row.isVisibleTo(_current_page(dialog))
            QTest.keyClick(dialog.search_edit, Qt.Key.Key_Return)
            app.processEvents()
        assert seen == set(_COLLISION_ADVANCED_ROWS), "回车遍历要能走到组内每一行"
    finally:
        dialog.close()


def test_group_title_query_hits_the_row_that_has_no_matching_text(tmp_path, monkeypatch):
    """搜「网络延迟检测」（只在组名里出现）：命中探测间隔行，并给依赖说明。"""
    dialog = _dialog(tmp_path, monkeypatch)
    try:
        matches = _matches(dialog, "网络延迟检测")
        assert _keys(matches) == ["settingRow_network_probe_interval"]
        row = _current_match(dialog)
        assert row.objectName() == "settingRow_network_probe_interval"
        # 依赖（网络指标关着）不满足：只给说明，不替用户改配置（第一刀既有语义）
        assert dialog.search_status.text() == "1/1 · 延迟探测间隔（需先开启「灵动岛指标 · 网络」）"
        assert dialog.island_network_check.isChecked() is False
    finally:
        dialog.close()


# ------------------------------------------------------------------- ② 口语别名
def test_alias_wangsu_lands_on_the_island_network_row(tmp_path, monkeypatch):
    """别名「网速」：命中灵动岛网络指标行（原本 0 命中）。"""
    dialog = _dialog(tmp_path, monkeypatch)
    try:
        matches = _matches(dialog, "网速")
        assert _keys(matches) == ["settingRow_island_network"]
        row = _current_match(dialog)
        assert row.objectName() == "settingRow_island_network"
        assert dialog.sidebar.currentItem().text() == "桌面组件"
        assert dialog.search_status.text().startswith("1/1 · 灵动岛指标 · 网络")

        # 依赖满足后同一查询给出可见的行（别名命中的是真实可操作的行，不是死链接）
        dialog.island_enabled_check.setChecked(True)
        dialog.island_info_check.setChecked(True)
        app.processEvents()
        row = _search(dialog, "网速")
        assert row.isVisibleTo(_current_page(dialog))
    finally:
        dialog.close()


def test_alias_matches_inside_a_sentence_and_up_down_stream_terms(tmp_path, monkeypatch):
    """别名是查询的子串即命中（「怎么看网速」）；「上行 / 下行」指同一行。"""
    dialog = _dialog(tmp_path, monkeypatch)
    try:
        assert _keys(_matches(dialog, "怎么看网速")) == ["settingRow_island_network"]
        for query in ("上行", "下行"):
            matches = _matches(dialog, query)
            assert _keys(matches) == ["settingRow_island_network"], (
                f"「{query}」必须落在网络指标行且只算一次"
            )
    finally:
        dialog.close()


def test_old_title_information_slot_content_reaches_the_content_selector(tmp_path, monkeypatch):
    """旧标题「信息槽内容」必须命中**内容选择器**那一行，不能只落在显示开关上。

    回归（终审问题 2）：改名后「信息槽」三个字整页只剩「显示中间信息栏」这一行的旧说法
    被别名兜住，而它原本的标题「信息槽内容」对应的 ``dynamic_island_info_mode`` 反倒
    不在别名目标里——老用户搜旧标题拿到的是另一个开关，「旧说法仍搜得到」的承诺没兑现。
    """
    dialog = _dialog(tmp_path, monkeypatch)
    try:
        keys = _keys(_matches(dialog, "信息槽内容"))
        assert "settingRow_dynamic_island_info_mode" in keys, (
            f"搜旧标题「信息槽内容」必须能到内容选择器那一行，实得 {keys}"
        )

        # 旧词是查询的**子串**即命中（「信息槽内容怎么改」也算），与别名口径一致。
        assert "settingRow_dynamic_island_info_mode" in _keys(_matches(dialog, "信息槽内容怎么改"))

        # 回车遍历要真能走到那一行，并且落在「桌面组件」域（不是只出现在候选集里）。
        row = _search(dialog, "信息槽内容", presses=keys.index("settingRow_dynamic_island_info_mode"))
        assert row.objectName() == "settingRow_dynamic_island_info_mode"
        assert dialog.sidebar.currentItem().text() == "桌面组件"

        # 「信息槽」这条别名**有意不收**自定义短文本那一行（它从没叫过「信息槽…」，
        # 自己那句「自定义短文本」本来就能关键词命中）。
        assert "settingRow_dynamic_island_custom_text" not in keys
        assert "settingRow_dynamic_island_custom_text" in _keys(_matches(dialog, "自定义短文本"))
    finally:
        dialog.close()


def test_alias_reset_defaults_and_excluded_terms_are_documented(tmp_path, monkeypatch):
    """别名表里每条都能命中真实行；「重置」这类没有行落点的词不假装有命中。"""
    from pet.settings_search import ROW_ALIASES

    assert ROW_ALIASES, "别名表不能是空的"
    for token, targets in ROW_ALIASES.items():
        assert token == token.strip().lower(), f"别名「{token}」必须是已规整的小写词"
        assert targets, f"别名「{token}」没有指向任何行键"

    dialog = _dialog(tmp_path, monkeypatch)
    try:
        for token, targets in ROW_ALIASES.items():
            if all(dialog.findChild(SettingRow, f"settingRow_{target}") is None
                   for target in targets):
                # 指向的行按平台闸门不存在（如主动识屏整页只在 Windows 构造）：
                # 本平台搜不到不是别名失效，漏掉哪条由 Windows 那条 CI 腿盯住。
                continue
            matches = _matches(dialog, token)
            keys = _keys(matches)
            assert keys, f"别名「{token}」没有命中任何行（别名字典 → 行键的指向已失效）"
            # 只要求**本平台真实存在**的那几行命中：有的别名会并列指到一个平台
            # 专属行（「自动化与联动」并列指主动识屏 + 待办提醒，而主动识屏整页只在
            # Windows 构造），拿本平台不存在的行去要求命中是假要求。
            present = [
                target for target in targets
                if dialog.findChild(SettingRow, f"settingRow_{target}") is not None
            ]
            assert present, f"别名「{token}」的落点全都按平台闸门不存在"
            for target in present:
                assert f"settingRow_{target}" in keys, (
                    f"别名「{token}」必须命中 settingRow_{target}"
                )
        # 「恢复默认设置…」是侧栏按钮、不是设置行：搜索框给不出行的落点，
        # 因此这条不做（口径见 pet/settings_search 的模块 docstring）。
        assert _matches(dialog, "恢复默认") == []
    finally:
        dialog.close()


# --------------------------------------------- ②b 用户视角实测的 0 命中词（2026-10-18）
def test_hotkey_words_reach_all_three_action_rows(tmp_path, monkeypatch):
    """「快捷键 / 热键」→ 三个 `hotkey_*` 行全部进候选。

    行标题只写三个动作名、说明里写「组合键」，整份设置页没有「快捷键」「热键」
    这两个词，于是用户视角实测 0 命中。三个动作是三行，别名必须**全指**：只指一行
    等于替用户挑了一个动作。首条命中是行序第一项（AI 对话），与右键菜单的顺序一致。
    """
    expected = [
        "settingRow_hotkey_open_chat",
        "settingRow_hotkey_look_screen",
        "settingRow_hotkey_screen_capture",
    ]
    dialog = _dialog(tmp_path, monkeypatch)
    try:
        for query in ("快捷键", "热键", "全局快捷键在哪"):
            assert _keys(_matches(dialog, query)) == expected, query
            assert _search(dialog, query).objectName() == expected[0]
            assert dialog.search_status.text() == "1/3 · AI 对话"
    finally:
        dialog.close()


def test_secondary_display_words_reach_the_cross_screen_row(tmp_path, monkeypatch):
    """「副屏 / 多屏」→ 跨屏开关（「跨屏 / 显示器」本来就能命中，这里补的是口语）。"""
    dialog = _dialog(tmp_path, monkeypatch)
    try:
        for query in ("副屏", "多屏", "副屏怎么开"):
            row = _search(dialog, query)
            assert row.objectName() == "settingRow_cross_screen", query
            assert _keys(dialog._search_matches) == ["settingRow_cross_screen"]
            assert dialog.search_status.text() == "1/1 · 允许桌宠跨屏"
    finally:
        dialog.close()


def test_domain_names_land_on_their_representative_rows(tmp_path, monkeypatch):
    """照着侧栏念域名也要搜得到：10 个域名里用户最常念的 4 个各给一个落点。

    侧栏域名是导航标签（``SETTINGS_DOMAIN_NAV``），不在行文案里，因此关键词路径
    永远 0 命中。落点选该域**最有代表性**的那一行（逐条理由写在别名表里）：
    桌面组件 = 灵动岛主开关；AI 与对话 = 「API 列表」（域首行 = 模型与连接抬头）+
    对话窗口（域名里「对话」那半边）；自动化与联动 = 主动识屏主开关；
    文件识别 = 拖文件后询问解读（该域只有两行，它就是功能本体）。
    """
    expected = {
        "桌面组件": ["settingRow_dynamic_island_enabled"],
        "AI 与对话": ["settingRow_provider_list", "settingRow_chat_ui_style"],
        "ai 与对话": ["settingRow_provider_list", "settingRow_chat_ui_style"],
        # 「主动识屏」页只在 Windows 建（`sys.platform == "win32" and include_ai`
        # 闸门），macOS/Linux 上那一行不存在，只剩该域第一组的「待办提醒」。
        # 命中顺序按行序，而「待办提醒」组在域里排在前面，所以 Windows 上也先命中它。
        "自动化与联动": (
            ["settingRow_todo_reminder_enabled", "settingRow_proactive_enabled"]
            if sys.platform == "win32" else ["settingRow_todo_reminder_enabled"]),
        "文件识别": ["settingRow_file_interpret_enabled"],
    }
    dialog = _dialog(tmp_path, monkeypatch)
    try:
        for query, keys in expected.items():
            assert _keys(_matches(dialog, query)) == keys, query
            row = _search(dialog, query)
            assert row.objectName() == keys[0]
            # 命中后确实翻到了那个域（不是只在别的页里高亮）
            assert _current_page(dialog).isAncestorOf(row), query
    finally:
        dialog.close()


def test_check_update_is_a_command_and_stays_out_of_the_index(tmp_path, monkeypatch):
    """「检查更新」是按钮不是设置行：按既有口径不进索引，不为它破例。

    它是「更新」页上的 ``QPushButton``（`pet/update_settings.py`），与侧栏的
    「恢复默认设置 / 保存并退出」同类——候选集只装 ``SettingRow``，命令给不出行
    落点。要让它可搜得先给按钮建落点模型，那是另一件事。
    """
    from pet.settings_search import ROW_ALIASES

    assert not any("检查更新" in token for token in ROW_ALIASES), (
        "别名表不该为命令类控件破例")
    dialog = _dialog(tmp_path, monkeypatch)
    try:
        assert _matches(dialog, "检查更新") == []
        assert dialog.search_status.text() == "未找到匹配的设置"
    finally:
        dialog.close()


def test_new_aliases_keep_the_existing_first_hits(tmp_path, monkeypatch):
    """补别名**只能追加**候选：补之前已正确的首条命中逐条不变。

    「密钥 → API Key」是最容易被新词挤掉的一条（「AI 与对话」也指向 AI 域），
    所以这里把既有正例整表钉住，而不是抽一两条。
    """
    expected = {
        "密钥": "settingRow_api_key",
        "跨屏": "settingRow_cross_screen",
        "显示器": "settingRow_cross_screen",
        "网速": "settingRow_island_network",
        "上行": "settingRow_island_network",
        "开机启动": "settingRow_autostart",
        "壁纸": "settingRow_chat_background_file",
        "帧数": "settingRow_island_fps",
        "信息槽": "settingRow_dynamic_island_info",
    }
    dialog = _dialog(tmp_path, monkeypatch)
    try:
        for query, key in expected.items():
            assert _search(dialog, query).objectName() == key, query
    finally:
        dialog.close()


def test_collapsible_box_title_query_reaches_the_rows_inside_the_box(tmp_path, monkeypatch):
    """搜默认折叠框名（「自定义台词」）：框内行也进候选，命中后框自动展开。"""
    dialog = _dialog(tmp_path, monkeypatch)
    try:
        box = dialog.dialogue_phrases_box
        assert box.is_expanded() is False, "先确认这个折叠框默认是收起的"

        matches = _matches(dialog, "自定义台词")
        keys = _keys(matches)
        assert len(set(keys)) == len(keys), f"候选行重复：{keys}"
        assert any(box.isAncestorOf(row) for row in matches), (
            "折叠框标题命中必须把框内行纳入候选"
        )

        for _ in range(len(matches)):
            row = _current_match(dialog)
            if box.isAncestorOf(row):
                assert box.is_expanded() is True, "命中框内的行必须自动展开折叠框"
                assert row.isVisibleTo(_current_page(dialog))
                break
            QTest.keyClick(dialog.search_edit, Qt.Key.Key_Return)
            app.processEvents()
        else:
            raise AssertionError("回车遍历没能走到折叠框内的行")
    finally:
        dialog.close()


def test_group_title_walk_is_memo_independent(tmp_path, monkeypatch):
    """带缓存与不带缓存算出的组标题逐行一致（缓存只加速、不改语义）。"""
    from pet.settings_search import fold_group_titles

    dialog = _dialog(tmp_path, monkeypatch)
    try:
        memo: dict = {}
        for row in dialog._search_rows:
            assert fold_group_titles(row, memo) == fold_group_titles(row), (
                f"{row.objectName()} 的组标题在带缓存时被算错"
            )
        # 折叠组内 / 组外的行各自得到正确的组标题
        assert fold_group_titles(_row(dialog, "harness_autostart")) == ["启动项（高级）"]
        assert fold_group_titles(_row(dialog, "autostart")) == []
    finally:
        dialog.close()


def test_no_chat_build_skips_aliases_whose_rows_are_absent(tmp_path, monkeypatch):
    """打包变体（excludes=['pet.chat']）：别名目标行不存在时静默跳过，不报错。"""
    monkeypatch.setattr(settings_mod.autostart_mod, "is_enabled", lambda: False)
    dialog = settings_mod.ModernSettingsDialog(Config(tmp_path), include_ai=False)
    try:
        assert dialog.ai_page is None
        # 「密钥」「壁纸」的目标行都在 AI 页里，这个变体里没有落点 → 照常「未找到」
        assert _matches(dialog, "密钥") == []
        assert _matches(dialog, "壁纸") == []
        assert dialog.search_status.text() == "未找到匹配的设置"
        # 与 AI 页无关的别名和组标题照常命中
        assert _keys(_matches(dialog, "网速")) == ["settingRow_island_network"]
        assert _keys(_matches(dialog, "启动项")) == [
            "settingRow_harness_autostart",
            "settingRow_harness_launch_target",
        ]
    finally:
        dialog.close()


# ------------------------------------------------------------------ ③ 去重与计数
def test_candidates_from_all_sources_are_deduped(tmp_path, monkeypatch):
    """组标题命中与关键词命中重叠时同一行只算一次（状态行计数与候选数一致）。"""
    dialog = _dialog(tmp_path, monkeypatch)
    try:
        # 「延迟检测」同时命中：探测间隔行（hint 里就写着延迟检测）+ 网络组名 +
        # 岛网络指标行（hint 里也有「延迟检测每次约 0.16 KB」）
        matches = _matches(dialog, "延迟检测")
        keys = _keys(matches)
        assert len(set(keys)) == len(keys), f"候选行重复了：{keys}"
        assert keys.count("settingRow_network_probe_interval") == 1
        assert set(keys) == {"settingRow_island_network", "settingRow_network_probe_interval"}
        assert dialog.search_status.text().startswith(f"1/{len(keys)} · ")

        # 组标题单独命中（关键词 0 命中）时也只给一行
        assert _keys(_matches(dialog, "网络延迟检测")) == ["settingRow_network_probe_interval"]
    finally:
        dialog.close()


def test_alias_hit_is_not_counted_twice_when_the_row_also_matches_by_keyword(
    tmp_path, monkeypatch
):
    """别名目标行同时被关键词命中时，计数不重复（「下行」既是 alias 又是 hint 子串）。"""
    dialog = _dialog(tmp_path, monkeypatch)
    try:
        for query in ("下行", "行速", "速"):
            matches = _matches(dialog, query)
            keys = _keys(matches)
            assert len(set(keys)) == len(keys), f"「{query}」候选行重复：{keys}"
            if query == "下行":
                assert keys == ["settingRow_island_network"]
                assert dialog.search_status.text().startswith("1/1 · ")
    finally:
        dialog.close()


# ------------------------------------------------------------- ④ 空/无义查询不回归
def test_unknown_query_still_reports_no_match(tmp_path, monkeypatch):
    """真正无义的查询仍是「未找到匹配的设置」，且空查询不显示状态行。"""
    dialog = _dialog(tmp_path, monkeypatch)
    try:
        for query in ("量子纠缠织物", "zzzz-not-a-setting"):
            assert _matches(dialog, query) == []
            assert dialog.search_status.text() == "未找到匹配的设置"
            assert dialog.search_status.isHidden() is False

        dialog.search_edit.setText("   ")
        app.processEvents()
        assert dialog._search_matches == [] and dialog._search_index == -1
        assert dialog.search_status.isHidden() is True, "空查询不该留状态行"
    finally:
        dialog.close()


# ------------------------------------------------------------------ ⑤ 既有搜索回归
def test_existing_keyword_searches_still_reach_their_rows(tmp_path, monkeypatch):
    """第一刀的既有查询照旧（标题格式 / 探测间隔 / 提示音 回车遍历）。"""
    dialog = _dialog(tmp_path, monkeypatch)
    try:
        if sys.platform == "win32":
            matches = _matches(dialog, "标题格式")
            keys = _keys(matches)
            # 关键词命中的标题格式行排在第一位（既有首次命中不变），且组内行一并入候选
            assert keys[0] == "settingRow_foobar_title_format"
            assert len(set(keys)) == len(keys)
            assert "settingRow_music_player_detect" in keys
            row = _search(dialog, "标题格式")
            assert row.objectName() == "settingRow_foobar_title_format"
            assert _section_of(dialog, "foobar_title_format").toggle.isChecked() is True
            assert row.isVisibleTo(_current_page(dialog))

        row = _search(dialog, "探测间隔")
        assert row.objectName() == "settingRow_network_probe_interval"
        assert dialog._search_matches == [row], "该查询不该被组标题/别名放大"

        matches = _matches(dialog, "提示音")
        assert len(matches) == 5
        assert all(dialog.agent_link_box.isAncestorOf(item) for item in matches)
        assert dialog.agent_link_box.is_expanded() is True
        first = _current_match(dialog)
        assert first.isVisibleTo(_current_page(dialog))
        QTest.keyClick(dialog.search_edit, Qt.Key.Key_Return)
        app.processEvents()
        assert dialog.search_status.text().startswith("2/")
        assert dialog.agent_link_box.isAncestorOf(_current_match(dialog))
    finally:
        dialog.close()


# ---------------------------------------------------------------- Ctrl+F 与索引口径
def test_ctrl_f_focuses_the_search_box(tmp_path, monkeypatch):
    """设置窗里注册了 Ctrl+F，触发后光标进搜索框（浏览器/编辑器的一致习惯）。

    不模拟真实按键：offscreen 平台不把 ``QTest.keyClick`` 派发给快捷方式，那样的
    用例会在 CI 上假绿/假红。这里断言两件真事——**绑定的是 ``StandardKey.Find``
    且作用域覆盖子控件**，以及**触发后焦点确实落到搜索框**。
    """
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeySequence, QShortcut

    dialog = _dialog(tmp_path, monkeypatch)
    try:
        dialog.show()
        finders = [
            shortcut for shortcut in dialog.findChildren(QShortcut)
            if shortcut.key().matches(QKeySequence.StandardKey.Find)
            == QKeySequence.SequenceMatch.ExactMatch
        ]
        assert finders, "设置窗必须注册 Ctrl+F"
        shortcut = finders[0]
        assert shortcut.context() == Qt.ShortcutContext.WidgetWithChildrenShortcut, (
            "焦点在任意子控件上时都要能触发")
        assert shortcut.key().toString() == "Ctrl+F"

        dialog.self_talk_check.setFocus()
        assert dialog.focusWidget() is not dialog.search_edit, "前提：焦点不在搜索框"

        shortcut.activated.emit()

        assert dialog.focusWidget() is dialog.search_edit, "Ctrl+F 应该把焦点送进搜索框"
    finally:
        dialog.close()
        dialog.deleteLater()


def test_author_rows_are_searchable_and_jump_to_the_update_page(tmp_path, monkeypatch):
    """「更新」页的作者账号行也进搜索索引：搜 BiliBili / 抖音 能跳过去。

    它们不是 SettingRow（没有配置键），但按同形暴露 label/hint_label，所以同一套
    匹配函数就能覆盖——不必为"可搜"改掉那一块的排版。
    """
    from pet import settings_search
    from pet.settings_about import AuthorRow

    dialog = _dialog(tmp_path, monkeypatch)
    try:
        rows = dialog._search_rows
        assert any(isinstance(row, AuthorRow) for row in rows), "作者行必须进索引"

        matches = settings_search.find_matches(rows, "bilibili")
        assert matches, "搜平台名要有命中"
        assert all(isinstance(row, AuthorRow) for row in matches)

        handles = settings_search.find_matches(rows, "快乐小咸鱼")
        assert handles, "搜账号名也要有命中"

        dialog._search_settings("bilibili")
        page = dialog.pages.currentWidget()
        assert page is not None and page.isAncestorOf(matches[0]), (
            "命中项在「更新」页，翻页要跳过去")
    finally:
        dialog.close()
        dialog.deleteLater()


# ---------------------------------------------------------------- 方向键翻命中
def test_arrow_down_advances_and_arrow_up_goes_back(tmp_path, monkeypatch):
    """搜索框里 ↓/↑ 逐条翻命中（回车之外的第二条直觉路径）。

    通用预期：浏览器查找栏、VSCode、Windows 设置搜索都用 ↑/↓ 翻结果，按回车循环
    反而少见。单行 QLineEdit 里 ↑/↓ 本来没有默认行为，不会和光标移动抢键。
    """
    dialog = _dialog(tmp_path, monkeypatch)
    try:
        dialog.show()
        dialog.search_edit.setText("对话")
        app.processEvents()
        total = len(dialog._search_matches)
        assert total >= 3, f"前提：这个查询要多于两条命中（实际 {total}）"
        assert dialog.search_status.text().startswith("1/")

        QTest.keyClick(dialog.search_edit, Qt.Key.Key_Down)
        app.processEvents()
        assert dialog.search_status.text().startswith("2/"), "↓ 应该前进一条"

        QTest.keyClick(dialog.search_edit, Qt.Key.Key_Down)
        app.processEvents()
        assert dialog.search_status.text().startswith("3/"), "↓ 再前进一条"

        QTest.keyClick(dialog.search_edit, Qt.Key.Key_Up)
        app.processEvents()
        assert dialog.search_status.text().startswith("2/"), "↑ 应该后退一条"
        assert _current_match(dialog) is dialog._search_matches[1]
    finally:
        dialog.close()
        dialog.deleteLater()


def test_arrow_up_from_the_first_match_wraps_to_the_last(tmp_path, monkeypatch):
    """第一条上按 ↑ 回绕到最后一条（取模天然支持，不需要特判分支）。"""
    dialog = _dialog(tmp_path, monkeypatch)
    try:
        dialog.show()
        dialog.search_edit.setText("对话")
        app.processEvents()
        total = len(dialog._search_matches)
        assert total >= 3
        assert dialog.search_status.text().startswith("1/")

        QTest.keyClick(dialog.search_edit, Qt.Key.Key_Up)
        app.processEvents()
        assert dialog.search_status.text().startswith(f"{total}/"), "↑ 从第一条回绕到最后"
        assert _current_match(dialog) is dialog._search_matches[-1]
    finally:
        dialog.close()
        dialog.deleteLater()


def test_arrow_with_a_modifier_is_not_swallowed_by_search(tmp_path, monkeypatch):
    """带修饰键的 ↑/↓ 必须透传给输入框，不许被搜索翻页抢走。

    输入法候选导航（尤其中文输入法预编辑态）与将来可能加的 Ctrl+↑ 类组合键，都不该
    被搜索拦掉；只有裸键（和小键盘方向键）才是"翻命中"的语义。
    """
    dialog = _dialog(tmp_path, monkeypatch)
    try:
        dialog.show()
        dialog.search_edit.setText("对话")
        app.processEvents()
        assert len(dialog._search_matches) >= 3
        assert dialog.search_status.text().startswith("1/")

        QTest.keyClick(dialog.search_edit, Qt.Key.Key_Down, Qt.KeyboardModifier.ControlModifier)
        app.processEvents()
        assert dialog.search_status.text().startswith("1/"), "Ctrl+↓ 不该翻页"

        QTest.keyClick(dialog.search_edit, Qt.Key.Key_Up, Qt.KeyboardModifier.ShiftModifier)
        app.processEvents()
        assert dialog.search_status.text().startswith("1/"), "Shift+↑ 不该翻页"

        QTest.keyClick(dialog.search_edit, Qt.Key.Key_Down, Qt.KeyboardModifier.AltModifier)
        app.processEvents()
        assert dialog.search_status.text().startswith("1/"), "Alt+↓ 不该翻页"
    finally:
        dialog.close()
        dialog.deleteLater()
