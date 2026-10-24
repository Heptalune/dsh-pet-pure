# -*- coding: utf-8 -*-
"""「快速识屏」的不可用说明——两份消费面共用的**单一真相源**。

同一句话有多个出口，措辞必须一致：

- **动作条 / 结果浮层**：引擎缺席或配置缺失时，overlay 与 ``actions.py`` 写出的
  是同一句——各写一份字面量就会漂成"按钮说一套、点下去说另一套"；文案收进本模块。

本模块**零 import**（不拖 Qt、不拖截图栈）：菜单层在启动期就装载，它可以把这里的
import 放在模块级而不付实质代价（本机实测：暖缓存下 import 0.6–0.8 ms，首次含字节码
编译约 3 ms；要避开的整条截图栈是另一个量级）。
"""

from __future__ import annotations

__all__ = ["ASK_UNAVAILABLE_REASON", "OCR_PENDING_REASON", "TRANSLATION_UNAVAILABLE_REASON"]

#: 本地 OCR 引擎缺席时那条如实说明（不是"暂时没有数据"，也不摆假结果）。
OCR_PENDING_REASON = "本地文字识别引擎尚未接入"

#: 翻译走用户配置的聊天模型（纯文本调用）：没配置/已关闭时的如实说明。
TRANSLATION_UNAVAILABLE_REASON = "截图翻译要用你在设置里配置的 AI 服务（未配置或已关闭）"

#: 问答同理（多轮对话浮窗要视觉/聊天通道）。
ASK_UNAVAILABLE_REASON = "问问肥鱼要你在设置里配置的 AI 服务（未配置或已关闭）"
