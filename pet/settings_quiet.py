# -*- coding: utf-8 -*-
"""设置页「免打扰」：控件创建 / 行装配 / 保存。

**为什么单独一个模块**：``modern_settings_dialog.py`` 的行数预算已无余量，
本组控件与行全部在此构建（与 ``settings_file_interpret`` / ``settings_music`` 同口径）。

**为什么这两项进设置页**（``docs/SETTINGS-CHANGE-GATES.md`` 准入 1~6 全中）：
它们是**偏好**（"以后默认开多久""全屏时要不要一起静音"），不是一次性命令；
低频、跨任务；默认值安全（30 分钟 / 全屏开）；归属唯一（「自动化与联动」域）；
产品无法替用户推断"你打游戏时想不想收提醒"；契约完整（无平台差异、无依赖、
立即保存）。**开启/提前结束本身是命令**，所以留在右键菜单，不在设置页放按钮。

设置页写的是**默认档**与**全屏联动开关**；当前是否在免打扰是运行态，不落盘。
"""

from __future__ import annotations

from PySide6.QtWidgets import QComboBox

from .config import (
    DEFAULT_QUIET_ALSO_ON_FULLSCREEN,
    DEFAULT_QUIET_MINUTES,
    QUIET_MINUTE_CHOICES,
)
from .quiet_mode import quiet_duration_label
from .settings_widgets import SettingRow, ToggleSwitch

#: 设置行 objectName 键（``settingRow_<key>``），测试与搜索按它定位。
ROW_KEYS = ("quiet_minutes_default", "quiet_also_on_fullscreen")


def create_quiet_controls(dialog) -> None:
    """创建默认档下拉与全屏联动开关（幂等：域重建会再调一次）。"""
    if getattr(dialog, "quiet_minutes_select", None) is not None:
        return
    selector = QComboBox(dialog)
    selector.setObjectName("quietMinutesSelect")
    # 档位与右键菜单**同一份列表**（config.QUIET_MINUTE_CHOICES），不另抄一份；
    # 文案共用 quiet_mode.quiet_duration_label，菜单/设置页永远说的是同一句话。
    for minutes in QUIET_MINUTE_CHOICES:
        selector.addItem(quiet_duration_label(minutes), int(minutes))
    current = int(dialog.config.get("quiet_minutes_default", DEFAULT_QUIET_MINUTES) or DEFAULT_QUIET_MINUTES)
    index = selector.findData(current)
    selector.setCurrentIndex(index if index >= 0 else 0)
    dialog.quiet_minutes_select = selector

    toggle = ToggleSwitch(dialog)
    toggle.setObjectName("quietAlsoOnFullscreenToggle")
    toggle.setChecked(
        bool(
            dialog.config.get(
                "quiet_also_on_fullscreen", DEFAULT_QUIET_ALSO_ON_FULLSCREEN
            )
        )
    )
    dialog.quiet_also_on_fullscreen_check = toggle
    # 两个键是**进程级默认值**（不在 PER_SLOT_SETTING_KEYS，运行时读主配置）：子宠页
    # 保存由设置进程的配置路由直写主配置（pet/settings_slot_config.py），改了真生效
    # ——此前这里按"写了 slot 文件、运行期读主配置"的死开关口径置灰，路由接通后摘除。


def build_quiet_rows(dialog) -> list[SettingRow]:
    """「免打扰」小节的两行（由「自动化与联动」域直接取用，不走 claim）。

    与 ``settings_music.build_music_player_rows`` 同口径：本模块自己建控件与行，
    ``modern_settings_dialog`` 只接线，行不进该文件的 ``all_rows`` 快照。
    """
    create_quiet_controls(dialog)
    return [
        SettingRow(
            ROW_KEYS[0],
            "默认时长",
            "右键「免打扰」里四档时长的默认档（菜单里标「默认」的那一项）。"
            "免打扰期间再选一档 = 重新计时，不叠加。",
            dialog.quiet_minutes_select,
        ),
        SettingRow(
            ROW_KEYS[1],
            "全屏应用时自动免打扰",
            "全屏应用（游戏 / 电影）期间，普通提醒照旧不往外冒，但会攒进暂存队列，"
            "退出全屏后用一条气泡汇报攒了几条——比直接丢掉更不容易漏事。"
            "审批、提问、错误这类要你回应的提示不受影响，任何时候都会冒出来。"
            "全屏判定复用「全屏时自动隐藏」那套探针（不另跑一套检测）："
            "那一项关掉后全屏探测不跑，本项也就无从生效。",
            dialog.quiet_also_on_fullscreen_check,
        ),
    ]


def save_quiet_settings(dialog) -> None:
    """把本组控件写回 config（未创建控件时无操作，兼容 include_ai=False 之外的宿主）。"""
    selector = getattr(dialog, "quiet_minutes_select", None)
    toggle = getattr(dialog, "quiet_also_on_fullscreen_check", None)
    if selector is None or toggle is None:
        return
    dialog.config.set("quiet_minutes_default", int(selector.currentData() or DEFAULT_QUIET_MINUTES))
    dialog.config.set("quiet_also_on_fullscreen", bool(toggle.isChecked()))
