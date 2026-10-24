# -*- coding: utf-8 -*-
"""全局快捷键的**纯文本口径**（零 Qt、零 ctypes，便于跨平台测试）。

设置页里存的是人能读的一行字符串（``Ctrl+Alt+P``），这个模块负责：

- :func:`parse` —— 字符串 → ``(Win32 修饰位, 虚拟键码)``；不认识就 ``None``；
- :func:`normalize` —— 把用户/外部写进来的字符串收成**规范形式**（顺序固定、
  大小写统一、不认识的一律 ``""``），配置文件里不存脏值；
- :func:`from_win32` —— 反向：``(修饰位, 虚拟键码)`` → 规范字符串（测试与日志用）。

为什么不用 ``QKeySequence``：它的文本是**本地化**的（中文系统上可能给出
``"Ctrl+Alt+P"`` 以外的形态），而 Windows 的 ``RegisterHotKey`` 只认虚拟键码。
自己维护一张白名单表，好处是"存进去什么、注册的就是什么"，坏处是只支持常见键
——够用，且不认识时能明确告诉用户而不是静默不生效。

纪律：**必须有至少一个修饰键**。无修饰的全局热键（比如单个 ``P``）会抢走整个
系统的按键输入，那不是快捷键，是灾难。
"""

from __future__ import annotations

# ---------------------------------------------------------------- Win32 常量
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

#: 文本长度上限：快捷键写法最长也就 ``Ctrl+Alt+Shift+Win+PrintScreen``，
#: 超长的只可能是脏数据/容器 stringify，没必要解析。
_MAX_TEXT = 64

#: 修饰键的书写形式 → Win32 位（别名收全：用户可能手写成 Control / Meta）
_MODIFIERS = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "meta": MOD_WIN,
    "super": MOD_WIN,
}
_MODIFIER_ORDER = (("Ctrl", MOD_CONTROL), ("Alt", MOD_ALT),
                   ("Shift", MOD_SHIFT), ("Win", MOD_WIN))

#: 支持的非字符键（小写写法 → 虚拟键码）
_NAMED_KEYS = {
    "space": 0x20,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "esc": 0x1B,
    "escape": 0x1B,
    "backspace": 0x08,
    "insert": 0x2D,
    "delete": 0x2E,
    "del": 0x2E,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "pgup": 0x21,
    "pgdn": 0x22,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "printscreen": 0x2C,
    "prtsc": 0x2C,
    "pause": 0x13,
    "capslock": 0x14,
    "numlock": 0x90,
    "scrolllock": 0x91,
    "num0": 0x60, "num1": 0x61, "num2": 0x62, "num3": 0x63, "num4": 0x64,
    "num5": 0x65, "num6": 0x66, "num7": 0x67, "num8": 0x68, "num9": 0x69,
    # 小键盘运算符：**不用 "+" 做名字**（本模块用 "+" 分隔组合键，"num+" 会被切坏），
    # 统一写成 numplus。这些键与数字键一样，Qt 侧靠 KeypadModifier 表达。
    "num*": 0x6A, "numplus": 0x6B, "num-": 0x6D, "num.": 0x6E, "num/": 0x6F,
}
for _n in range(1, 25):
    _NAMED_KEYS[f"f{_n}"] = 0x70 + (_n - 1)

#: 虚拟键码 → 规范写法（``from_win32`` 用；未收录的键返回 None）
_VK_NAMES: dict[int, str] = {}
for _name, _vk in _NAMED_KEYS.items():
    # 同一 vk 有多个别名时取最短的规范写法（enter/return → Enter 等）
    preferred = {
        0x0D: "Enter", 0x1B: "Esc", 0x2E: "Delete", 0x21: "PageUp",
        0x22: "PageDown", 0x2C: "PrintScreen",
        # 三个锁键按用户看得懂的原名回显（否则 canonical 会给出 "Capslock" 这种
        # 大小写不对的写法，跟解析表里的写法不一致，往返就对不上）。
        0x14: "CapsLock", 0x90: "NumLock", 0x91: "ScrollLock",
    }.get(_vk)
    if preferred is not None:
        _VK_NAMES[_vk] = preferred
    elif _vk not in _VK_NAMES or len(_name) < len(_VK_NAMES[_vk]):
        _VK_NAMES[_vk] = _name.capitalize() if not _name.startswith("num") else _name


#: 允许的字符键：**只认 ASCII**。不能用 ``str.isalpha()``——``"ß".isalpha()`` 为真
#: 而 ``"ß".upper() == "SS"``，``ord("SS")`` 直接抛 TypeError；那个异常发生在
#: ``Config.__init__`` 的归一化里，等于一条手改配置让主程序和设置页都起不来。
_ASCII_LETTERS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
_ASCII_DIGITS = frozenset("0123456789")


def _key_code(token: str) -> int | None:
    """单个按键写法 → 虚拟键码（ASCII 字母/数字 + 白名单命名键）。"""
    token = token.strip()
    if not token:
        return None
    low = token.lower()
    if len(token) == 1:
        # 先判**本字符**是不是 ASCII：只看 ``upper()`` 不够——``"ı".upper() == "I"``、
        # ``"ſ".upper() == "S"``，那会把 dotless-i / long-s 悄悄注册成 I / S。
        if not token.isascii():
            return None  # 非 ASCII 单字符（é / ² / ß / ı / ſ …）一律不认
        upper = token.upper()
        if upper in _ASCII_LETTERS or upper in _ASCII_DIGITS:
            return ord(upper)  # 字母/数字的 vk 就是大写 ASCII
        return None
    # 判**原始 token**（不是小写化结果）：Kelvin 符号 K 的 ``lower()`` 就是 "k"，
    # 所以 "CapsLocK" 这种小写化后全 ASCII、会命中 CapsLock —— 必须看原串。
    if not token.isascii():
        return None
    return _NAMED_KEYS.get(low)


def parse(text) -> tuple[int, int] | None:
    """``"Ctrl+Alt+P"`` → ``(mods, vk)``；不合法/不完整返回 ``None``。

    合法性：至少一个修饰键 + **恰好一个**普通键；普通键必须在白名单里。
    """
    raw = str(text or "").strip()
    if not raw or len(raw) > _MAX_TEXT:
        return None
    # 旧写法 "num+" 与分隔符 "+" 冲突：入口先归一化，老值仍能解析成 numplus。
    raw = raw.replace("num+", "numplus").replace("NUM+", "numplus")
    parts = [p.strip() for p in raw.replace(" ", "").split("+")]
    parts = [p for p in parts if p]
    if len(parts) < 2:
        return None
    mods = 0
    vk = None
    for part in parts:
        low = part.lower()
        if low in _MODIFIERS:
            mods |= _MODIFIERS[low]
            continue
        code = _key_code(part)
        if code is None or vk is not None:
            return None  # 不认识，或出现两个普通键
        vk = code
    if not mods or vk is None:
        return None
    return mods, vk


def canonical(mods: int, vk: int) -> str | None:
    """``(mods, vk)`` → 规范字符串（按键不在白名单时返回 ``None``）。"""
    name = _VK_NAMES.get(int(vk))
    if name is None:
        if 0x41 <= int(vk) <= 0x5A:
            name = chr(int(vk))
        elif 0x30 <= int(vk) <= 0x39:
            name = chr(int(vk))
        else:
            return None
    order = [label for label, bit in _MODIFIER_ORDER if int(mods) & bit]
    if not order:
        return None
    return "+".join(order + [name])


def normalize(text) -> str:
    """任意输入 → 规范字符串；不认识/不完整 → ``""``（= 未绑定）。"""
    parsed = parse(text)
    if parsed is None:
        return ""
    return canonical(parsed[0], parsed[1]) or ""


def from_win32(mods: int, vk: int) -> str | None:
    """反解 ``RegisterHotKey`` 的修饰位 + 键码（日志/诊断用）。"""
    return canonical(mods & ~MOD_NOREPEAT, vk)


def display(text) -> str:
    """给界面看的一行：未绑定就是「未设置」，其余用规范写法。"""
    normalized = normalize(text)
    return normalized or "未设置"
