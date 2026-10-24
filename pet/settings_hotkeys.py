# -*- coding: utf-8 -*-
"""设置页「互动 · 快捷键」：三个动作的**系统级**热键录入。

三个动作与右键菜单同名同实现（不复制行为，只换触发方式）：

- AI 对话 → ``PetInstance.open_chat``
- 看看屏幕 → ``OverlayShell.look_at_screen``
- 截图提问 → ``OverlayShell.on_screen_capture``

三条纪律：

1. **默认全空**：不绑任何键，行为与加这个功能之前逐位相同（``docs/SETTINGS-CHANGE-GATES.md``
   的"有安全默认"）。
2. **必须有修饰键**：单个字母/数字的全局热键会抢走系统级打字输入，录入时就拒掉
   （判据在 ``pet/hotkey_spec.py``，不是在这里自定义）。
3. **被占用要说**：Windows 的全局热键表是独占的，注册失败（通常是被别的程序占了）
   会写日志并让主进程侧的那一项不生效；这里不假装成功。
"""

from __future__ import annotations

from PySide6.QtCore import QKeyCombination, Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QHBoxLayout, QKeySequenceEdit, QPushButton, QWidget

from . import hotkey_spec
from .hotkeys import ACTIONS, ACTION_LABELS, is_supported
from .settings_widgets import SettingRow

#: 动作 → 给用户的一句说明（行 hint）
_ACTION_HINTS = {
    "open_chat": "在任意程序前台时按这个组合键，直接打开 AI 对话窗口。",
    "look_screen": "按一下就让它看一眼当前屏幕并说说看到了什么。",
    "screen_capture": "按一下进入截图提问（框选区域后发给模型）。",
}

#: 占用说明（只在支持全局热键的平台上拼进 hint）。
#: 注册发生在**主进程**里，而设置页是独立进程（`--settings`），拿不到注册结果——
#: 所以界面能做到的是**提前把后果说清**，而不是显示一个它无法确认的"已生效"。
_OCCUPIED_HINT = " 若这个组合键已被其他程序占用，它不会生效（日志里会记一条）。"

#: Qt 修饰位 → Win32 修饰位
_QT_MODS = (
    (Qt.KeyboardModifier.ControlModifier, hotkey_spec.MOD_CONTROL),
    (Qt.KeyboardModifier.AltModifier, hotkey_spec.MOD_ALT),
    (Qt.KeyboardModifier.ShiftModifier, hotkey_spec.MOD_SHIFT),
    (Qt.KeyboardModifier.MetaModifier, hotkey_spec.MOD_WIN),
)

#: Qt 键 → 虚拟键码（只收白名单里的，其余一律"不支持"）
_QT_KEYS = {
    Qt.Key.Key_Space: 0x20,
    Qt.Key.Key_Tab: 0x09,
    Qt.Key.Key_Return: 0x0D,
    Qt.Key.Key_Enter: 0x0D,
    Qt.Key.Key_Escape: 0x1B,
    Qt.Key.Key_Backspace: 0x08,
    Qt.Key.Key_Insert: 0x2D,
    Qt.Key.Key_Delete: 0x2E,
    Qt.Key.Key_Home: 0x24,
    Qt.Key.Key_End: 0x23,
    Qt.Key.Key_PageUp: 0x21,
    Qt.Key.Key_PageDown: 0x22,
    Qt.Key.Key_Left: 0x25,
    Qt.Key.Key_Up: 0x26,
    Qt.Key.Key_Right: 0x27,
    Qt.Key.Key_Down: 0x28,
    Qt.Key.Key_Print: 0x2C,
    Qt.Key.Key_Pause: 0x13,
    # `hotkey_spec` 认这三个锁键，回显表也得认——否则手写进来的 Ctrl+CapsLock 在
    # 录入框里显示成空（看着像没设），而它其实已经注册了。
    Qt.Key.Key_CapsLock: 0x14,
    Qt.Key.Key_NumLock: 0x90,
    Qt.Key.Key_ScrollLock: 0x91,
}


#: 小键盘运算符：Qt 侧是"普通运算符键 + KeypadModifier"，Win32 侧是独立虚拟键码。
_VK_NUMPAD_OPS = {
    0x6A: "Key_multiply",
    0x6B: "Key_Plus",
    0x6D: "Key_Minus",
    0x6E: "Key_Period",
    0x6F: "Key_Slash",
}


def chord_to_text(sequence) -> str:
    """``QKeySequence`` → 规范字符串；不支持/无修饰键 → ``""``（= 未设置）。

    只取第一个和弦（录入控件已限制 ``setMaximumSequenceLength(1)``）：Windows 的
    ``RegisterHotKey`` 没有"序列"概念，多和弦的尾部会被静默丢掉——不如一开始就不收。
    """
    if sequence is None or sequence.isEmpty():
        return ""
    combination = sequence[0]
    mods = 0
    for qt_mod, win_mod in _QT_MODS:
        if combination.keyboardModifiers() & qt_mod:
            mods |= win_mod
    if not mods:
        return ""
    key = int(combination.key())
    keypad = bool(combination.keyboardModifiers() & Qt.KeyboardModifier.KeypadModifier)
    if keypad and int(Qt.Key.Key_0) <= key <= int(Qt.Key.Key_9):
        # 小键盘数字与主键盘数字是不同的虚拟键码：Ctrl+小键盘5 存成 Ctrl+5 的话，
        # 用户按小键盘不会触发（绑的是另一颗键）。
        return hotkey_spec.canonical(mods, 0x60 + (key - int(Qt.Key.Key_0))) or ""
    if keypad:
        # 乘号在不同 Qt/键盘上可能报 Key_multiply(×) 或 Key_Asterisk(*)，两个都认。
        if key in (int(Qt.Key.Key_multiply), int(Qt.Key.Key_Asterisk)):
            return hotkey_spec.canonical(mods, 0x6A) or ""
        for vk, qt_name in _VK_NUMPAD_OPS.items():
            if key == int(getattr(Qt.Key, qt_name)):
                return hotkey_spec.canonical(mods, vk) or ""
    vk = _QT_KEYS.get(key)
    if vk is None:
        if int(Qt.Key.Key_A) <= key <= int(Qt.Key.Key_Z):
            vk = key  # Qt 的 A..Z 就等于 ASCII 大写
        elif int(Qt.Key.Key_0) <= key <= int(Qt.Key.Key_9):
            vk = key
        elif int(Qt.Key.Key_F1) <= key <= int(Qt.Key.Key_F24):
            vk = 0x70 + (key - int(Qt.Key.Key_F1))
    if vk is None:
        return ""
    return hotkey_spec.canonical(mods, vk) or ""


def _vk_to_qt_key(vk: int) -> int | None:
    """虚拟键码 → Qt 键码（``_QT_KEYS`` 的反向表 + 字母/数字/F 键）。"""
    for qt_key, mapped in _QT_KEYS.items():
        if mapped == vk:
            return int(qt_key)
    if 0x41 <= vk <= 0x5A or 0x30 <= vk <= 0x39:
        return vk
    if 0x70 <= vk <= 0x87:                     # F1..F24
        return int(Qt.Key.Key_F1) + (vk - 0x70)
    return None


def text_to_sequence(text) -> QKeySequence:
    """规范字符串 → ``QKeySequence``（录入框回显用）。

    不能用 ``QKeySequence("Win+F1")``：我们自己的写法（``Win`` / ``num5``）不是 Qt
    保证能解析的文本格式，实测那些写法解析出来是空序列——控件会显示"未设置"，
    而配置里明明有值。所以按 ``(mods, vk)`` 反构造键组合。
    """
    parsed = hotkey_spec.parse(text)
    if parsed is None:
        return QKeySequence()
    mods, vk = parsed
    # 小键盘数字：Qt 没有「Keypad5」这种独立键码，它用小键盘修饰位 + 主键码表示。
    # 反构造时要把修饰位补回去，否则 Ctrl+小键盘5 会显示成 Ctrl+5（而绑的其实是
    # 小键盘那颗键）。
    keypad = False
    if 0x60 <= vk <= 0x69:
        keypad = True
        vk = 0x30 + (vk - 0x60)
    elif vk in _VK_NUMPAD_OPS:
        keypad = True
        qt_key = int(getattr(Qt.Key, _VK_NUMPAD_OPS[vk]))
        mods_and = {qt_mod for qt_mod, win_mod in _QT_MODS if mods & win_mod}
        qt_mods = Qt.KeyboardModifier.NoModifier
        for qt_mod in mods_and:
            qt_mods |= qt_mod
        qt_mods |= Qt.KeyboardModifier.KeypadModifier
        return QKeySequence(QKeyCombination(qt_mods, Qt.Key(qt_key)))
    qt_key = _vk_to_qt_key(vk)
    if qt_key is None:
        return QKeySequence()
    qt_mods = Qt.KeyboardModifier.NoModifier
    for qt_mod, win_mod in _QT_MODS:
        if mods & win_mod:
            qt_mods |= qt_mod
    if keypad:
        qt_mods |= Qt.KeyboardModifier.KeypadModifier
    return QKeySequence(QKeyCombination(qt_mods, Qt.Key(qt_key)))


def create_hotkey_controls(dialog) -> None:
    """幂等建立三个录入控件（初值取配置；空 = 未设置）。"""
    if getattr(dialog, "_hotkey_edits", None):
        return
    dialog._hotkey_edits = {}
    dialog._hotkey_values = {}
    for action in ACTIONS:
        edit = QKeySequenceEdit(dialog)
        edit.setObjectName(f"hotkeyEdit_{action}")
        edit.setMaximumSequenceLength(1)
        edit.setClearButtonEnabled(True)
        stored = hotkey_spec.normalize(dialog.config.get(ACTIONS[action]))
        dialog._hotkey_values[action] = stored
        if stored:
            edit.setKeySequence(text_to_sequence(stored))
        dialog._hotkey_edits[action] = edit


def _row_control(dialog, action: str) -> QWidget:
    """一行里的控件：录入框 + 「清除」。"""
    holder = QWidget(dialog)
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    edit = dialog._hotkey_edits[action]
    layout.addWidget(edit)

    clear = QPushButton("清除", holder)
    clear.setObjectName(f"hotkeyClear_{action}")
    clear.setToolTip("取消这个动作的快捷键（不绑也能从右键菜单触发）")
    layout.addWidget(clear)

    def _remember(_sequence=None) -> None:
        text = chord_to_text(edit.keySequence())
        if not text and not edit.keySequence().isEmpty():
            # 收到一个不支持的组合（例如只按了 P、或 F13 之外的怪键）：清空并提示，
            # 绝不留一个"看着绑上了、其实永远不触发"的值。
            edit.setKeySequence(QKeySequence())
            edit.setToolTip("需要至少一个修饰键（Ctrl/Alt/Shift/Win）+ 一个常见按键")
        else:
            edit.setToolTip("")
        dialog._hotkey_values[action] = text

    def _clear() -> None:
        edit.setKeySequence(QKeySequence())
        dialog._hotkey_values[action] = ""

    edit.keySequenceChanged.connect(_remember)
    clear.clicked.connect(_clear)
    return holder


#: 非 Windows 平台下追加在行说明后面的一句（否则"设了却没反应"且毫无提示）
_UNSUPPORTED_HINT = "（当前系统不支持系统级快捷键：这里可以保存，但不会生效。）"


def build_hotkey_rows(dialog) -> list[SettingRow]:
    """「互动 · 快捷键」一节的行装配（三项）。"""
    create_hotkey_controls(dialog)
    supported = is_supported()
    rows = []
    for action in ACTIONS:
        hint = _ACTION_HINTS[action] + (
            _OCCUPIED_HINT if supported else _UNSUPPORTED_HINT)
        row = SettingRow(ACTIONS[action], ACTION_LABELS[action], hint,
                         _row_control(dialog, action))
        # 热键是进程级（全局键），与"影响所有桌宠"的其他全局项同一口径标注
        row.set_scope_note("影响所有桌宠")
        if not supported:
            dialog._hotkey_edits[action].setEnabled(False)
        rows.append(row)
    return rows


def save_hotkey_settings(dialog) -> None:
    """写回三个配置键（空串 = 不绑）。真正注册由主进程侧的热键服务负责。"""
    values = getattr(dialog, "_hotkey_values", None) or {}
    for action, key in ACTIONS.items():
        text = hotkey_spec.normalize(values.get(action, ""))
        if text != hotkey_spec.normalize(dialog.config.get(key)):
            dialog.config.set(key, text)
