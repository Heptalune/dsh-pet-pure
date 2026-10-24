# -*- coding: utf-8 -*-
"""设置页「功能」域：卡片页 + staged 写回的测试。

seam 是真实的 ``ModernSettingsDialog`` + 真实 ``Config``（tmp_path 落盘），
与本仓库设置测试同口径（QT_QPA_PLATFORM=offscreen）。

覆盖（对应 docs/SETTINGS-CHANGE-GATES.md 准出）：

- 页面装配：16 张功能卡、按 features 分组的 5 个分组、侧栏「功能」在「桌面组件」之后；
- staged 语义：开关只改内存 pending 集，**不落盘**；
- ``write_to_config``：有改动才 ``config.set``、未改动不碰配置（不触发无谓写盘）；
- 「全部关闭」→ 保存后盘上 ``enabled=[]``（合法空集，不回退成全激活）；
- 硬依赖提示：只勾 file_interpret 不勾 chat 时，chat 卡下出现「××需要它才能用」；
- 快捷按钮：推荐套件 / 全部开启 / 全部关闭改的是 pending 集。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

import pet.modern_settings_dialog as settings_mod  # noqa: E402
from pet.config import Config  # noqa: E402
from pet.feature_activation import CURRENT_VERSION, pending_enabled  # noqa: E402
from pet.features import CATEGORY_ORDER, FEATURES, all_features  # noqa: E402
from pet.settings_features import RECOMMENDED_SUITE, FeaturesPage  # noqa: E402
from pet.settings_widgets import SettingsSection  # noqa: E402

app = QApplication.instance() or QApplication([])


def _dialog(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings_mod.autostart_mod, "is_enabled", lambda: False)
    return settings_mod.ModernSettingsDialog(Config(base=tmp_path), include_ai=True)


def _page(dialog) -> FeaturesPage:
    page = dialog._features_page
    assert isinstance(page, FeaturesPage), "「功能」页没有接线（_features_page）"
    return page


def _read_disk(config: Config) -> dict:
    """读全局 ``config.json``（``feature_activation`` 是进程级键，设置进程的配置
    路由会把它写进主配置而不是 slot 文件——见 pet/settings_slot_config.py）。"""
    path = Path(config.dir) / "config.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return {}


@pytest.fixture
def dialog(tmp_path, monkeypatch):
    dlg = _dialog(tmp_path, monkeypatch)
    try:
        yield dlg
    finally:
        dlg.close()


# ------------------------------------------------------------------ 页面装配
def test_page_builds_all_feature_cards_grouped_by_category(dialog):
    page = _page(dialog)
    specs = all_features()
    assert len(specs) == 16, "清单变了（本用例按 16 模块钉住，变了请同步）"
    for spec in specs:
        card = page.findChild(settings_mod.QFrame, f"featureCard_{spec.id}")
        assert card is not None, f"缺卡片：{spec.id}"
        assert card.label.text() == spec.name
        assert card.hint_label.text() == spec.desc
        assert card.toggle.isChecked() is True, "老用户首次打开 pending 即全激活"
    sections = [s for s in page.findChildren(SettingsSection)]
    titles = [s.findChild(settings_mod.QLabel, "sectionTitle").text() for s in sections]
    assert titles == list(CATEGORY_ORDER), f"分组不齐或顺序不对：{titles}"
    # 组内卡片顺序 = all_features() 声明顺序
    for section, (category, group_specs) in zip(
            sections, _grouped_specs().items(), strict=True):
        assert section.rows == [page._cards[s.id] for s in group_specs], category


def _grouped_specs() -> dict[str, list]:
    grouped: dict[str, list] = {}
    for spec in all_features():
        grouped.setdefault(spec.category, []).append(spec)
    return grouped


def test_features_domain_sits_after_desktop_components_in_sidebar(dialog):
    labels = [dialog.sidebar.item(i).text() for i in range(dialog.sidebar.count())]
    assert labels.index("功能") == labels.index("桌面组件") + 1
    assert dialog.select_page("功能") is True
    assert dialog.sidebar.currentItem().text() == "功能"


def test_badges_follow_build_constraints(dialog):
    page = _page(dialog)
    chat = page._cards["chat"]
    badges = [lb.text() for lb in chat.findChildren(settings_mod.QLabel, "settingHint")]
    assert any("联网" in text for text in badges)
    assert any("可能要花钱/凭据" in text for text in badges)
    weather = page._cards["weather"]
    weather_text = " ".join(
        lb.text() for lb in weather.findChildren(settings_mod.QLabel, "settingHint"))
    assert "联网" in weather_text
    assert "会拉起外部程序" not in weather_text
    quick = page._cards["quick_launch"]
    quick_text = " ".join(
        lb.text() for lb in quick.findChildren(settings_mod.QLabel, "settingHint"))
    assert "会拉起外部程序" in quick_text


def test_info_bar_states_restart_semantics(dialog):
    page = _page(dialog)
    texts = " ".join(lb.text() for lb in page.findChildren(settings_mod.QLabel))
    assert "勾选的功能重启桌宠后生效" in texts
    assert "重启后生效" in texts


# ------------------------------------------------------------------ staged 语义
def test_toggles_only_change_pending_set_and_never_touch_disk(dialog):
    page = _page(dialog)
    before = _read_disk(dialog.config)
    page._cards["weather"].toggle.setChecked(False)
    app.processEvents()
    assert "weather" not in page.pending()
    assert _read_disk(dialog.config) == before, "开关切换落盘了（staged 语义被破坏）"
    page._cards["weather"].toggle.setChecked(True)
    app.processEvents()
    assert "weather" in page.pending()
    assert _read_disk(dialog.config) == before


def test_write_to_config_sets_only_when_pending_differs(dialog, monkeypatch):
    page = _page(dialog)
    calls: list[tuple[str, object]] = []
    real_set = dialog.config.set
    monkeypatch.setattr(
        dialog.config, "set",
        lambda key, value: (calls.append((key, value)), real_set(key, value))[1])
    # 未改动：一个字都不写（避免无谓写盘触发主进程 watcher）
    page.write_to_config(dialog.config)
    assert not [key for key, _ in calls if key == "feature_activation"]
    # 有改动：整值提交一次，版本号与内容正确
    page._cards["weather"].toggle.setChecked(False)
    page.write_to_config(dialog.config)
    writes = [value for key, value in calls if key == "feature_activation"]
    assert len(writes) == 1
    assert writes[0]["version"] == CURRENT_VERSION
    assert set(writes[0]["enabled"]) == {s.id for s in all_features()} - {"weather"}
    # 再写一次（状态未再变）：不重复 set
    page.write_to_config(dialog.config)
    assert len([key for key, _ in calls if key == "feature_activation"]) == 1


def test_write_to_config_preserves_unknown_ids(dialog, tmp_path, monkeypatch):
    raw = {"version": CURRENT_VERSION, "enabled": ["weather", "future_module_x"]}
    dialog.config.set("feature_activation", raw)
    dialog.config.save()
    # 同目录重开一个对话框：pending 来自盘上值（unknown 不进展示集）
    dialog2 = _dialog(tmp_path, monkeypatch)
    try:
        page2 = _page(dialog2)
        assert set(page2.pending()) == {"weather"}
        page2._cards["todo"].toggle.setChecked(True)
        page2.write_to_config(dialog2.config)
        saved = dialog2.config.data["feature_activation"]
        assert saved["enabled"] == ["weather", "todo", "future_module_x"], (
            "未知模块 ID 必须原样保留（降级/再升级恢复）")
    finally:
        dialog2.close()


# ------------------------------------------------------------------ 全部关闭 = 合法空集
def test_disable_all_then_save_persists_empty_enabled_list(dialog):
    page = _page(dialog)
    page.findChild(QPushButton, "featureDisableAll").click()
    app.processEvents()
    assert page.pending() == []
    assert dialog._write_config() is True
    saved = _read_disk(dialog.config)["feature_activation"]
    assert saved["enabled"] == [], "合法空集被回退成了全激活"
    assert saved["version"] == CURRENT_VERSION
    # 重读：pending_enabled 把空集当明确选择，不给迁移基线
    dialog.config.reload()
    assert pending_enabled(dialog.config) == []


def test_enable_all_and_recommended_suite_only_touch_pending(dialog):
    page = _page(dialog)
    page.findChild(QPushButton, "featureDisableAll").click()
    page.findChild(QPushButton, "featurePresetRecommended").click()
    app.processEvents()
    assert page.pending() == list(RECOMMENDED_SUITE)
    assert set(page.pending()) == {"weather", "todo", "divination", "festival", "voice", "chat"}
    page.findChild(QPushButton, "featureEnableAll").click()
    app.processEvents()
    assert page.pending() == [spec.id for spec in all_features()]
    assert all(card.toggle.isChecked() for card in page._cards.values())


# ------------------------------------------------------------------ 硬依赖提示
def test_dependency_hint_names_checked_dependents(dialog):
    page = _page(dialog)
    page.findChild(QPushButton, "featureDisableAll").click()
    page._cards["file_interpret"].toggle.setChecked(True)
    app.processEvents()
    chat_hint = page._cards["chat"].dep_hint
    assert chat_hint.isVisible() or chat_hint.text(), "chat 卡下应有依赖提示行"
    assert chat_hint.text() == "「文件解读」需要它才能用"
    # 勾回 chat：提示消失
    page._cards["chat"].toggle.setChecked(True)
    app.processEvents()
    assert page._cards["chat"].dep_hint.text() == ""
    assert page._cards["chat"].dep_hint.isVisible() is False
    # 多个依赖者：vision 也硬依赖 chat
    page._cards["chat"].toggle.setChecked(False)
    page._cards["vision"].toggle.setChecked(True)
    app.processEvents()
    assert "「文件解读」" in page._cards["chat"].dep_hint.text()
    assert "「看看屏幕」" in page._cards["chat"].dep_hint.text()


def test_dependency_hint_does_not_block_saving(dialog):
    """blocked 不强制拦保存：UI 如实显示，运行时自然不可用（任务口径）。"""
    page = _page(dialog)
    page.findChild(QPushButton, "featureDisableAll").click()
    page._cards["file_interpret"].toggle.setChecked(True)
    page.write_to_config(dialog.config)
    dialog.config.save()
    saved = _read_disk(dialog.config)["feature_activation"]
    assert saved["enabled"] == ["file_interpret"], "依赖提示不许变成保存拦截"


# ------------------------------------------------------------------ 取消不保存
def test_discard_on_close_without_save_keeps_disk(dialog):
    page = _page(dialog)
    before = _read_disk(dialog.config)
    page._cards["music"].toggle.setChecked(False)
    page._cards["chat"].toggle.setChecked(False)
    app.processEvents()
    assert set(page.pending()) == {s.id for s in all_features()} - {"music", "chat"}
    assert _read_disk(dialog.config) == before, "未走保存链就落盘（取消丢弃语义被破坏）"


def test_card_shows_entry_names_and_core_note(tmp_path, monkeypatch):
    """「对应：」行告诉用户关掉卡片会消失什么；底部说明区列出不可关的核心能力。"""
    from PySide6.QtWidgets import QLabel

    dlg = _dialog(tmp_path, monkeypatch)
    try:
        page = _page(dlg)
        weather_card = page._cards["weather"]
        texts = [lb.text() for lb in weather_card.findChildren(QLabel)]
        assert any(t.startswith("对应：") and "查看天气" in t for t in texts)
        page_texts = [lb.text() for lb in page.findChildren(QLabel)]
        assert any("始终开启的核心能力" in t and "动画播放" in t for t in page_texts)
    finally:
        dlg.close()
        app.processEvents()
