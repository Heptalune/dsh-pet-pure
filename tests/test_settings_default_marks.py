# -*- coding: utf-8 -*-
"""设置页第三刀「已修改标记 + 默认值小字 + 单项重置」的行为验收（2026-10-08）。

口径来源：主代理裁决（本刀 §③）+ ``docs/SETTINGS-CHANGE-GATES.md`` +
``.scratch/SETTINGS-UI-DESIGN-K3-20261007.md`` §五（第三刀：默认值呈现 + 单项重置）。

本文件钉住七件事：

1. 全默认配置下**整窗没有任何标记**（默认态与改动前一致）；
2. 改不同类型控件的值 → 恰好对应数量的标记出现，且默认值文本正确；
3. 单项重置只改控件值：延迟写回行**保存前 Config 与磁盘都没被动过**，即时生效行
   （``click_sound_enabled``）重置后**立即**写盘——落盘时机跟着该行自己的保存语义，
   重置不新增写回路径；
4. 重置后保存并退出 → 新开的 Config 读回默认值（round trip 不绕开既有写回）；
5. 子宠页的**全局**行（开机自启，写 OS 登录项）可编辑且行内写明"影响所有桌宠"——
   判据是"能不能改"，不是"是哪只宠的页面"；同页的 per-slot 行不受影响；
6. 被依赖开关隐藏的行不显示标记；前置开关重新打开后标记回来（不做可见性记账）；
7. 没有可比配置默认值的行（逐服务商字段 / 列表成员 / UI 记忆）不显示标记也不报错，
   四类之外的控件类型同样不参与。

纪律：断言全部走真实 ``ModernSettingsDialog`` 这个公开 seam；「可见」用真实的域切换 +
页内标签 + 折叠展开（``_reveal``）后取 ``isVisible()``，不假定隐藏控件上的标记算数。
"""
from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLineEdit, QSpinBox

import pet.modern_settings_dialog as settings_mod
from pet import settings_default_marks
from pet.config import Config
from pet.modern_settings_dialog import SettingRow
from pet.settings_widgets import (
    BrowserSpinBox,
    ModernSelect,
    SettingsTabContainer,
    ToggleSwitch,
)

app = QApplication.instance() or QApplication([])

#: 四类受支持控件（本刀只认这四类，其余控件类型整行不参与）；
#: ``QDoubleSpinBox`` / ``QComboBox`` / 自绘选择器 / 路径选择器都不在此列。
_SUPPORTED_TYPES = (ToggleSwitch, ModernSelect, BrowserSpinBox, QLineEdit)

#: 「四类控件 + 没有可比默认值」的行快照（逐服务商字段 / 列表成员 / UI 记忆）。
#: 现状文档化：新增这类行会让覆盖率用例变红，逼一次显式归类（不是静默漏掉）。
NO_DEFAULT_ROWS = frozenset({
    # AI 服务商逐条目字段：每个服务商一套值，没有单一默认值可比
    "provider_name", "api_url", "model", "api_key", "timeout", "max_tokens", "skip_ssl",
    "vision_same", "vision_model", "vision_url", "vision_key",
    # 深度思考开关/强度也是逐服务商字段（ProviderConfig.thinking/reasoning_effort）
    "thinking_mode", "reasoning_effort",
    # 搜索 API Key 是凭据字段（优先系统钥匙串），没有可比默认值
    "web_search_key",
    # 岛指标行是 island_metrics **列表**的成员，不是标量键
    "island_network", "island_cpu", "island_mem", "island_gpu", "island_fps",
    # 台词编辑器的「上次打开的层」是 UI 记忆，不是偏好
    "dialogue_scope",
})


def _is_supported(control) -> bool:
    return isinstance(control, _SUPPORTED_TYPES)


def _row_key(row) -> str:
    return row.objectName()[len("settingRow_"):]


def _row(dialog, key) -> SettingRow:
    row = dialog.findChild(SettingRow, f"settingRow_{key}")
    assert row is not None, f"缺少设置行 {key}"
    return row


def _reveal(dialog, row) -> None:
    """把行真正呈现出来：选中所属域 → 切到所属页内标签 → 展开折叠它的组。

    分页 / 标签 / 折叠都是既有呈现层机制，测试走它们（而不是假定行在屏幕外也算
    "可见"），才能证明标记对用户真的看得见。
    """
    page = row.parentWidget()
    while page is not None and dialog.pages.indexOf(page) < 0:
        page = page.parentWidget()
    assert page is not None, f"{row.objectName()} 不在任何设置域页里"
    dialog.sidebar.setCurrentRow(dialog.pages.indexOf(page))
    parent = row.parentWidget()
    while parent is not None:
        if isinstance(parent, SettingsTabContainer):
            parent.activate_for_descendant(row)
        toggle = getattr(parent, "toggle", None)
        if toggle is not None and not toggle.isChecked():
            toggle.setChecked(True)
        parent = parent.parentWidget()
    app.processEvents()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """真实设置窗（主宠页）+ 它的 Config 与配置目录。

    配置面按生产形态给：slot0 身份经 ``SlotRoutedConfig`` 分流，每宠键（点击音效、
    气泡方案…）写进 ``config-slot-0.json``，全局键仍进 ``config.json``。
    """
    from pet.settings_slot_config import route_settings_config

    monkeypatch.setattr(settings_mod.autostart_mod, "is_enabled", lambda: False)
    cfg = route_settings_config(Config(base=tmp_path, instance_id="slot-0"))
    dialog = settings_mod.ModernSettingsDialog(cfg, include_ai=True)
    dialog.show()
    app.processEvents()
    yield dialog, cfg, tmp_path
    dialog.close()
    app.processEvents()


def _marked(dialog) -> list[str]:
    """当前处于「已修改」态的行键（按控件自带状态，不受所属页面是否在前台影响）。"""
    return sorted(
        _row_key(row) for row, _control, _default in dialog._default_mark_rows
        if not row.modified_mark.isHidden()
    )


# —— ① 默认态整窗无标记 ——————————————————————————————————————————

def test_default_config_shows_no_mark_anywhere(env):
    dialog, _cfg, _tmp = env
    assert dialog._default_mark_rows, "没有任何行参与标记，接线没生效"

    rows = dialog.findChildren(SettingRow)
    assert rows, "设置窗没有设置行"
    for row in rows:
        assert row.modified_mark.isHidden(), f"{row.objectName()} 默认态不该有标记"
        assert row.default_reset_button.isHidden(), f"{row.objectName()} 默认态不该有重置入口"
        assert "默认：" not in row.hint_label.text(), f"{row.objectName()} 默认态不该写默认值"


# —— 布局与可访问性（GATES §3）：标记/按钮不压控件、都在行内、读屏说得清 ———

def test_mark_and_reset_fit_inside_the_row_without_overlapping_control(env):
    dialog, _cfg, _tmp = env
    for key in ("on_top", "pet_opacity", "throw_strength", "balance_tier_peak"):
        row = _row(dialog, key)
        _reveal(dialog, row)
        row.set_default_state("示例", can_reset=True)
        app.processEvents()
        for width in (720, 900, 1200):  # compact / standard / wide 三档
            dialog.resize(width, 620)
            app.processEvents()
            for widget in (row.modified_mark, row.default_reset_button):
                assert row.rect().contains(widget.geometry()), (
                    f"{key}@{width}: {widget.objectName()} 跑出行外 {widget.geometry()}"
                )
                assert not widget.geometry().intersects(row.control.geometry()), (
                    f"{key}@{width}: {widget.objectName()} 压住了控件"
                )
            assert row.control.isVisible(), f"{key}@{width}: 控件被挤没了"
        assert row.label.text() in row.default_reset_button.accessibleName(), (
            f"{key}: 重置按钮的 accessible name 没说是改哪一项"
        )
        assert row.default_reset_button.focusPolicy() != Qt.FocusPolicy.NoFocus, (
            f"{key}: 重置按钮键盘不可达"
        )


# —— ② 改过就出现，默认值文本正确 ————————————————————————————————

def test_changed_rows_light_marks_with_the_right_default_text(env):
    dialog, _cfg, _tmp = env
    toggle, select, spin = (
        _row(dialog, "system_notifications_enabled"),
        _row(dialog, "balance_tier_mode"),
        _row(dialog, "balance_refresh"),
    )
    for row in (toggle, select, spin):
        _reveal(dialog, row)

    toggle.control.setChecked(False)
    select.control.setCurrentData("liangwen")
    spin.control.setValue(30)
    app.processEvents()

    assert _marked(dialog) == [
        "balance_refresh", "balance_tier_mode", "system_notifications_enabled",
    ]
    assert toggle.hint_label.text().endswith("默认：开")
    assert select.hint_label.text().endswith("默认：空闲 / 高峰（默认）")
    assert spin.hint_label.text().endswith("默认：0 分钟")
    # 标记与重置入口都是真给用户看的（行在前台）
    for row in (toggle, select, spin):
        assert row.modified_mark.isVisible()
        assert row.default_reset_button.isVisible()


def test_line_edit_row_is_supported(env):
    """四类里的 QLineEdit：改过要出标记，重置要能改回。"""
    dialog, _cfg, _tmp = env
    row = _row(dialog, "balance_tier_peak")
    _reveal(dialog, row)
    assert row.control.text() == ""

    row.control.setText("忙")
    app.processEvents()
    assert not row.modified_mark.isHidden()
    assert row.hint_label.text().endswith("默认：（空）")

    row.default_reset_button.click()
    app.processEvents()
    assert row.control.text() == ""
    assert row.modified_mark.isHidden()
    assert "默认：" not in row.hint_label.text()


# —— ③ 单项重置：只改控件；落盘时机跟着该行自己的保存语义 ————————

def test_single_reset_keeps_config_and_disk_untouched(env):
    dialog, cfg, tmp_path = env
    row = _row(dialog, "balance_refresh")
    _reveal(dialog, row)
    original_hint = row.hint_label.text()

    # 前置：先把出厂配置落盘，磁盘断言才有可断的对象（"文件不存在就整段跳过"等于
    # 没断——条件守卫一旦因环境变化不成立，这条验收会静默失效而不是变红）。
    # 路由视图只在有 pending 时才落盘，故这里直接写主配置那份出厂整表。
    Config(base=tmp_path).save()
    config_path = cfg.dir / "config.json"
    assert config_path.exists(), "前提：磁盘上已有一份出厂配置"
    on_disk_before = config_path.read_text(encoding="utf-8")
    assert json.loads(on_disk_before)["balance_refresh_minutes"] == 0, "前提：磁盘上是出厂值"

    row.control.setValue(30)
    app.processEvents()
    assert not row.modified_mark.isHidden()

    row.default_reset_button.click()
    app.processEvents()

    assert row.control.value() == 0
    assert row.modified_mark.isHidden()
    assert row.default_reset_button.isHidden()
    assert row.hint_label.text() == original_hint  # hint 恢复原文，不残留默认值小字
    # 保存前不碰 Config：内存与磁盘都还是出厂值（磁盘按整份内容比对，不只是那一个键）
    assert cfg.get("balance_refresh_minutes") == 0
    assert config_path.read_text(encoding="utf-8") == on_disk_before, \
        "重置不得写盘（整份内容逐字比对，不是只看那一个键）"


def test_single_reset_of_an_immediate_save_row_writes_through_at_once(env):
    """重置的落盘时机 = 该行自己的保存语义，本刀不另立一套写回路径。

    ``click_sound_enabled`` 是「即时生效」开关：``settings_pet_controls`` 把
    ``toggled`` 直连到 ``_apply_click_sound_enabled_now``，控件一动就写盘。重置
    把控件拨回默认值时同样走这条路——**立即**落盘，而不是等设置窗关闭。这是该行
    本来就有的语义（重置只是把控件设回默认值），tooltip / 模块 docstring 已按此
    如实表述，不再宣称「保存并退出时才生效」。延迟写回行（上一条用例）一字未动。
    """
    dialog, cfg, _tmp = env
    row = _row(dialog, "click_sound")
    _reveal(dialog, row)
    assert row.control.isChecked() is True, "前提：这一项出厂默认是开"

    # 前置：先证明这一行真的是即时落盘型（不是本用例假设出来的）
    row.control.setChecked(False)
    app.processEvents()
    assert cfg.get("click_sound_enabled") is False, "前提：即时型开关改一下就落盘"

    row.default_reset_button.click()
    app.processEvents()

    assert row.control.isChecked() is True
    assert row.modified_mark.isHidden()
    # 重置后立即落盘（不是"保存并退出时才生效"）：每宠键落在 slot0 档案里
    assert cfg.get("click_sound_enabled") is True
    slot_path = cfg.path
    assert json.loads(slot_path.read_text(encoding="utf-8"))["click_sound_enabled"] is True


# —— ④ 保存并退出后读回默认 ————————————————————————————————————

def test_reset_then_save_round_trips_to_default(env):
    dialog, cfg, tmp_path = env
    reset_row = _row(dialog, "balance_refresh")
    kept_row = _row(dialog, "balance_tier_mode")
    _reveal(dialog, reset_row)

    reset_row.control.setValue(30)
    kept_row.control.setCurrentData("custom")
    app.processEvents()
    reset_row.default_reset_button.click()
    app.processEvents()

    assert dialog._write_config() is True

    reopened = Config(base=tmp_path)
    assert reopened.get("balance_refresh_minutes") == 0  # 重置过的键回默认
    assert reopened.get("balance_tier_labels_mode") == "custom"  # 没重置的照旧保存
    assert cfg.get("balance_refresh_minutes") == 0
    # 保存后标记对齐到刚落盘的状态：重置过的行回到干净态，没重置的仍然标着
    assert reset_row.modified_mark.isHidden()
    assert not kept_row.modified_mark.isHidden()


def test_no_chat_build_still_installs_marks(tmp_path, monkeypatch):
    """打包变体（excludes=['pet.chat']）：没有 AI 行也要照常接线、不报错。"""
    monkeypatch.setattr(settings_mod.autostart_mod, "is_enabled", lambda: False)
    dialog = settings_mod.ModernSettingsDialog(Config(base=tmp_path), include_ai=False)
    dialog.show()
    app.processEvents()
    try:
        assert dialog.ai_page is None
        assert dialog._default_mark_rows, "无 chat 变体里标记接线断了"
        row = _row(dialog, "on_top")
        _reveal(dialog, row)
        row.control.setChecked(False)
        app.processEvents()
        assert row.modified_mark.isVisible()
    finally:
        dialog.close()
        app.processEvents()


# —— ⑤ 子宠页：主宠专属行置灰且不提供重置 ————————————————————————

def test_sub_pet_page_keeps_autostart_editable_with_its_scope_stated(tmp_path, monkeypatch):
    """子宠页的开机自启行**可编辑**，且界面自己说清它的全局作用域。

    旧口径把「写 OS 登录项」的行一律按"仅主桌宠可设置"置灰；而 `autostart_wanted` 是
    **全局键**（系统登录项只有一份），规范 §五要求"任意宠可以编辑全局设置，但界面必须
    明确说明影响所有桌宠"——所以这里改的是判据与文案，不是把能力交出去。判据见
    `pet/settings_pet_controls.py`（tooltip / 行 hint）与
    `docs/SETTINGS-CHANGE-GATES.md` 的 2026-10-10 记录。

    子宠页仍有一类真正置灰的行（直播捕获兼容——重启级拓扑输入、保存写 slot 文件而拓扑
    判定只读主配置），它的契约在 `tests/test_stream_capture_copy.py` 里单独钉。
    """
    monkeypatch.setattr(settings_mod.autostart_mod, "is_enabled", lambda: False)
    cfg = Config(base=tmp_path, instance_id="slot-1")
    dialog = settings_mod.ModernSettingsDialog(cfg, include_ai=True)
    dialog.show()
    app.processEvents()
    try:
        autostart = _row(dialog, "autostart")
        _reveal(dialog, autostart)
        assert autostart.control.isEnabled() is True, "任意宠都能编辑全局的开机自启"
        assert "影响所有桌宠" in autostart.control.toolTip()
        # 作用域是行内**独立标注**（不再粘在灰说明末尾），读屏说明仍是"说明 + 标注"全文。
        assert "影响所有桌宠" in autostart.scope_note.text()
        assert autostart.scope_note.isHidden() is False, "标注要真的呈现"
        assert "影响所有桌宠" not in autostart.hint_label.text(), "不许还粘在说明末尾"
        assert "影响所有桌宠" in autostart.control.accessibleDescription()

        autostart.control.setChecked(True)
        app.processEvents()
        assert autostart.modified_mark.isVisible(), "改动照旧出标记"

        # 同一页的 per-slot 行不受影响（证明这里改的是这一行的判据，不是一刀切放开）
        per_slot = _row(dialog, "on_top")
        _reveal(dialog, per_slot)
        per_slot.control.setChecked(False)
        app.processEvents()
        assert per_slot.control.isEnabled() is True
    finally:
        dialog.close()
        app.processEvents()


# —— ⑥ 依赖隐藏行不出标记，前置开关回来标记也回来 ——————————————

def test_dependent_hidden_row_shows_no_mark(env):
    dialog, _cfg, _tmp = env
    dependent = _row(dialog, "egg_title")
    parent_toggle = _row(dialog, "egg_enabled")
    _reveal(dialog, dependent)

    dependent.control.setText("今天也很厉害")
    app.processEvents()
    assert dependent.modified_mark.isVisible() is True
    assert dependent.default_reset_button.isVisible() is True

    parent_toggle.control.setChecked(False)  # 前置开关关掉 → 依赖行被隐藏
    app.processEvents()
    assert dependent.isHidden() is True
    assert dependent.modified_mark.isVisible() is False
    assert dependent.default_reset_button.isVisible() is False
    assert not dependent.modified_mark.isHidden(), "隐藏只由父级传播，不该抹掉改过的事实"

    parent_toggle.control.setChecked(True)  # 重新打开 → 标记跟着回来
    app.processEvents()
    assert dependent.modified_mark.isVisible() is True


# —— ⑦ 没有默认值可比的键不显示标记，也不报错 ————————————————

def test_rows_without_config_default_show_no_mark_and_no_error(env):
    dialog, _cfg, _tmp = env
    registered = {_row_key(row) for row, _c, _d in dialog._default_mark_rows}
    assert registered & NO_DEFAULT_ROWS == frozenset()

    # 列表成员行：勾上（island_metrics 默认是空列表）也不出标记
    island = _row(dialog, "island_cpu")
    island.control.setChecked(True)
    app.processEvents()
    assert island.modified_mark.isHidden()

    # 四类之外的控件（QDoubleSpinBox）同样不参与：改值不出标记、不抛异常
    opacity = _row(dialog, "dynamic_island_opacity")
    opacity.control.setValue(0.5)
    app.processEvents()
    assert opacity.modified_mark.isHidden()
    assert opacity.default_reset_button.isHidden()


def test_supported_rows_without_default_are_a_frozen_set(env):
    """覆盖率快照：四类控件的行要么有可比默认值，要么在显式名单里（现状文档化）。"""
    dialog, _cfg, _tmp = env
    supported = {
        _row_key(row) for row in dialog.findChildren(SettingRow)
        if _is_supported(row.control)
    }
    registered = {_row_key(row) for row, _c, _d in dialog._default_mark_rows}
    assert supported - registered == set(NO_DEFAULT_ROWS)


def test_macos_only_rows_align_with_the_same_snapshot(tmp_path, monkeypatch):
    """平台专属行同样要对得上：darwin 下建整窗再跑一遍覆盖率快照。

    本机是 Windows，macOS 专属行（「显示 Dock 图标」）在这里根本不存在——行键
    （``dock_icon``）与配置键（``show_dock_icon``）不同名，漏进映射表只会在
    macOS 上以「快照多出一个行键」的形式变红。故按既有平台分支测试模式
    （``test_desktop_pet_features.py::test_dock_icon_visibility_defaults_on_...``）
    模拟 darwin 建窗，用同一份口径复验。
    """
    monkeypatch.setattr(settings_mod.autostart_mod, "is_enabled", lambda: False)
    monkeypatch.setattr(settings_mod.sys, "platform", "darwin")
    dialog = settings_mod.ModernSettingsDialog(Config(base=tmp_path), include_ai=True)
    dialog.show()
    app.processEvents()
    try:
        supported = {
            _row_key(row) for row in dialog.findChildren(SettingRow)
            if _is_supported(row.control)
        }
        registered = {_row_key(row) for row, _c, _d in dialog._default_mark_rows}
        assert "dock_icon" in supported, "前提：darwin 下 Dock 图标行真的建出来了"
        unregistered = supported - registered
        assert unregistered == set(NO_DEFAULT_ROWS), (
            f"darwin 专属行缺默认值映射：{sorted(unregistered - set(NO_DEFAULT_ROWS))}"
        )
        # 走完整链路：从行键查到默认值，且值就是配置里那个真默认
        row = _row(dialog, "dock_icon")
        assert settings_default_marks._row_default_value(row.control, "dock_icon") == (True, True)
    finally:
        dialog.close()
        app.processEvents()


# —— 默认值来源护栏 ——————————————————————————————————————————

def test_mapping_table_registers_paths_not_default_values(tmp_path):
    """映射表里只能有「路径（+ 量纲倍数）」，且每条路径都真能在 Config 默认数据里走通。"""
    for row_key, entry in settings_default_marks._ROW_DEFAULT_PATHS.items():
        path, factor = entry if isinstance(entry, tuple) else (entry, None)
        assert isinstance(path, str) and path, row_key
        assert factor is None or isinstance(factor, (int, float)), row_key
        assert settings_default_marks._row_default_value(None, row_key)[0], (
            f"{row_key} 登记了在 Config 默认数据里走不通的路径：{path}"
        )


def test_default_values_come_from_config_itself(tmp_path):
    """默认值只有 Config 一份：本刀的表只登记路径，不抄默认值。"""
    empty = Config(base=tmp_path / "empty")
    assert settings_default_marks._config_default_data() == empty._initial_data()
