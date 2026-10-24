# -*- coding: utf-8 -*-
"""语音报时「合成后端」参数区的折叠编排（纯呈现层，不碰任何配置键）。

机主原话（2026-10-13）：「现在语音界面的各种信息太复杂了，需要整理，写得更人性化和简洁一点」。
四个后端一共 12 个参数字段全铺开，等于让所有人都先读三套自己用不上的参数。现在：

1. **当前后端**的字段平铺在最上面（它的凭据行进「要你自己去拿的凭据」组）；
2. **其余后端**各占一个折叠头行（标题＝后端短名＋「（未启用）」），成员行默认收起，
   点「展开」才看得见——是折起来，不是藏起来；
3. 换后端时其余后端一律回到收起态，当前后端永远在最上面。

**为什么折叠做成「行 + 行显隐」，而不是 ``CollapsibleGroup``**：设置页的行会被
``modern_settings_dialog._rebuild_domain_navigation`` 收进「语音」域的一张平卡，那一步只搬
``SettingRow``、不搬容器——任何 QWidget 嵌套到那里都会散架（容器留下，行被搬走）。
所以折叠只能落在行上：一个折叠头行 + ``setVisible`` 管成员行。

由此还带来一个约束：**行序必须直接改 ``SettingsCard`` 的布局**（平卡是域构建期现造的，
本模块没有构建权）。``SettingsCard.refresh_separators`` 只认 ``rows`` 的先后，所以布局与
``rows`` 必须一起改，否则分隔线会挂到别的行上。
"""

from __future__ import annotations

import re
from functools import partial
from typing import NamedTuple

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QPushButton, QWidget

from .settings_widgets import SettingRow, SettingsCard

#: 凭据组的标题与说明（机主口径：组标题直说用途，组内讲清存在哪）
CREDENTIAL_TITLE = "要你自己去拿的凭据"
CREDENTIAL_HINT = "只存在你自己电脑的系统钥匙串上，不会写进配置文件。"
#: 折叠头的动作文案
EXPAND_TEXT = "展开"
COLLAPSE_TEXT = "收起"
#: 非当前后端的标题后缀
DISABLED_SUFFIX = "（未启用）"

#: provider.label 形如「小米 MiMo（需 API Key）」：括号里那段是下拉用的补充说明。
_LABEL_HEAD = re.compile(r"[（(]")
_LABEL_DETAIL = re.compile(r"[（(]([^（()）]*)[）)]\s*$")


def short_label(provider) -> str:
    """后端短名：折叠标题只要名字，不要标签里那句补充说明。"""
    head = _LABEL_HEAD.split(str(getattr(provider, "label", "") or ""))[0].strip()
    return head or str(getattr(provider, "id", "") or "")


def label_detail(provider) -> str:
    """折叠头的一句话说明：直接复用 ``provider.label`` 括号里那段，不另造文案。"""
    match = _LABEL_DETAIL.search(str(getattr(provider, "label", "") or ""))
    return match.group(1).strip() if match else ""


def notice_row(key: str, title: str, hint: str) -> SettingRow:
    """纯文案行（组标题 + 一句说明）：控件位留零尺寸占位，视觉上只有标题与说明。"""
    stub = QWidget()
    stub.setFixedSize(0, 0)
    return SettingRow(key, title, hint, stub)


class _FieldRow(NamedTuple):
    """一个 provider 字段行：行本身 + 它的内部短名（判依赖显隐用）+ 是否凭据。"""

    row: SettingRow
    name: str
    secret: bool


class TtsBackendPanel:
    """按后端折叠「合成后端」参数行，并把当前后端排到最上面。

    设置页侧的接线只有三句：``add_provider()`` / ``add_field_row()`` /
    ``set_credential_header()``，然后每次后端变化调一次 ``apply()``。
    """

    def __init__(self) -> None:
        self._order: list[str] = []
        self._fields: dict[str, list[_FieldRow]] = {}
        self._headers: dict[str, SettingRow] = {}
        self._buttons: dict[str, QPushButton] = {}
        self._expanded: dict[str, bool] = {}
        self._credential_header: SettingRow | None = None
        self._current = ""
        self._field_visible: dict[str, dict[str, bool]] = {}

    # ------------------------------------------------------------ 构建
    def add_provider(self, provider) -> SettingRow:
        """登记一个后端，返回它的折叠头行（调用方须把它放进行清单，保持块连续）。"""
        provider_id = str(provider.id)
        button = QPushButton(EXPAND_TEXT)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(lambda _checked=False, pid=provider_id: self.toggle(pid))
        header = SettingRow(
            f"tts_backend_fold_{provider_id}",
            f"{short_label(provider)}{DISABLED_SUFFIX}",
            label_detail(provider),
            button,
        )
        header.setVisible(False)  # 显隐由 apply() 按「是不是当前后端」定
        self._order.append(provider_id)
        self._fields[provider_id] = []
        self._headers[provider_id] = header
        self._buttons[provider_id] = button
        self._expanded[provider_id] = False
        return header

    def add_field_row(self, provider_id: str, row: SettingRow, *, name: str, secret: bool) -> None:
        """登记一个参数字行；``secret=True`` 的行进凭据组（只跟着当前后端）。"""
        self._fields.setdefault(provider_id, []).append(_FieldRow(row, name, secret))
        # 搜索命中折叠里的行时要把这一行露出来（见 settings_widgets.reveal_settings_row）
        row._reveal_fold = partial(self.expand, provider_id)

    def set_credential_header(self, row: SettingRow) -> None:
        """凭据组标题行：只在当前后端有凭据字段时显示。"""
        self._credential_header = row

    # ------------------------------------------------------------ 编排
    def apply(self, current: str, field_visible: dict | None = None) -> None:
        """按当前后端重排行序与显隐；``field_visible`` 是 {后端: {字段短名: 是否可见}}。"""
        if field_visible is not None:
            self._field_visible = field_visible
        if current != self._current:
            # 换后端 = 其余后端回到默认收起（非当前后端一律折起来）
            for provider_id in self._order:
                self._expanded[provider_id] = False
            self._current = current
        card = self._resolve_card()
        if card is not None:
            self._reorder(card, self._ordered_rows())
        self._sync_visibility()
        if card is not None:
            card.refresh_separators()

    def toggle(self, provider_id: str) -> None:
        """展开/收起一个后端（当前后端的头行本就隐藏，点不到它）。"""
        self._expanded[provider_id] = not self._expanded.get(provider_id, False)
        self.apply(self._current)

    def expand(self, provider_id: str) -> None:
        """把一个后端展开（搜索命中折叠内的行时由 reveal_settings_row 调用）。"""
        if self._expanded.get(provider_id):
            return
        self._expanded[provider_id] = True
        self.apply(self._current)

    def is_expanded(self, provider_id: str) -> bool:
        return bool(self._expanded.get(provider_id, False))

    # ------------------------------------------------------------ 内部
    def _resolve_card(self) -> SettingsCard | None:
        """行当前的宿主平卡：域构建时行会被搬到另一张卡上，所以每次现找。"""
        for row in self._all_rows():
            card = row.parentWidget()
            if isinstance(card, SettingsCard):
                return card
        return None

    def _all_rows(self) -> list[SettingRow]:
        rows: list[SettingRow] = []
        for provider_id in self._order:
            header = self._headers.get(provider_id)
            if header is not None:
                rows.append(header)
            rows.extend(entry.row for entry in self._fields.get(provider_id, ()))
        if self._credential_header is not None:
            rows.append(self._credential_header)
        return rows

    def _ordered_rows(self) -> list[SettingRow]:
        """当前后端最前（凭据组跟在它的普通字段后），其余后端按注册顺序。

        当前后端的折叠头也要排进去（它只是隐藏着）：一换后端它就变回可见的那一行，
        必须已经紧挨着自己的字段。
        """
        order = [self._current] + [pid for pid in self._order if pid != self._current]
        rows: list[SettingRow] = []
        for provider_id in order:
            entries = self._fields.get(provider_id, ())
            plain = [entry.row for entry in entries if not entry.secret]
            secret = [entry.row for entry in entries if entry.secret]
            header = self._headers.get(provider_id)
            if header is not None:
                rows.append(header)
            rows.extend(plain)
            if provider_id == self._current and self._credential_header is not None:
                rows.append(self._credential_header)
            rows.extend(secret)
        return rows

    def _sync_visibility(self) -> None:
        current = self._current
        for provider_id in self._order:
            is_current = provider_id == current
            expanded = is_current or self.is_expanded(provider_id)
            header = self._headers.get(provider_id)
            if header is not None:
                header.setVisible(not is_current)
            button = self._buttons.get(provider_id)
            if button is not None:
                button.setText(COLLAPSE_TEXT if expanded else EXPAND_TEXT)
            visible = self._field_visible.get(provider_id, {})
            for entry in self._fields.get(provider_id, ()):
                # hidden_when 这类字段间依赖只在「看得见这个后端」时才有意义
                entry.row.setVisible(expanded and bool(visible.get(entry.name, True)))
        if self._credential_header is not None:
            self._credential_header.setVisible(
                any(entry.secret for entry in self._fields.get(current, ()))
            )

    def _reorder(self, card: SettingsCard, rows: list[SettingRow]) -> None:
        """把 ``rows`` 按新次序放回它们在平卡里原本占的位置（块内重排，不挪别的设置）。"""
        existing = list(getattr(card, "rows", ()))
        block = set(self._all_rows())
        positions = [index for index, row in enumerate(existing) if row in block]
        if len(positions) != len(rows):
            # 平卡里还没搬齐这些行（构建中途）：原样不动，绝不按猜的索引搬家
            return
        first = positions[0]
        if positions != list(range(first, first + len(positions))):
            # 行不连续：插空重排会打乱别的设置，宁可不动
            return
        layout = card.layout()
        for row in rows:
            layout.removeWidget(row)
        # 行在布局里的下标是它在 rows 里的两倍（中间夹着分隔线）；先全摘掉再按新序插回，
        # 间隔的分隔线正好各自归位（第 k 条分隔线始终在第 k-1 与第 k 行之间）。
        for slot, row in zip(positions, rows):
            layout.insertWidget(2 * slot, row)
        reordered = list(existing)
        for slot, row in zip(positions, rows):
            reordered[slot] = row
        card.rows = reordered
