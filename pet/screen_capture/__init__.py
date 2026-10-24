# -*- coding: utf-8 -*-
"""「快速识屏」P0：契约 + 宿主采集闭环 + 选区像素 + 三个产品动作。

分层（依赖只向下）：

- ``session``：纯逻辑状态机与选区几何，**不 import Qt**（可脱 Qt 单测）；
- ``host``：隐藏/还原自家顶层窗口；
- ``pixels``：抓冻结帧、按设备像素裁剪、统一 sRGB 8-bit；
- ``overlay``：整屏框选覆盖层（半透明遮罩 + 拖框 + Esc/Enter）；
- ``controller``：把上面四件按固定顺序串成一次采集，并保证失败也收口；
- ``ocr``：本地 OCR 的**宿主侧驱动**（切片 2；``actions`` 通过模块级 seam 惰性对接）。
  切片 5 起一次识别 = 一个识别子进程，跑完即退出（"用完就丢"里的内存真归零）；
- ``ocr_models``：随包模型的文件名/摘要/目录解析（**只 import 标准库**，宿主与
  识别子进程共用一份判据）；
- ``ocr_worker``：识别子进程本体（``python -m pet.screen_capture.ocr_worker``；
  冻结包是 exe 的 ``--ocr-worker`` 子命令）。**不 import PySide**，只认管道里的
  PNG 与 stdout 上的一行 JSON；
- ``result_panel``：岛旁的结果浮层（可复制 / Esc 可关 / 不抢焦点）；
- ``reasons``：不可用说明的单一真相源（零 import：菜单 tooltip 与结果浮层共用一份）；
- ``vision_bridge``：问答的云端通道（选区图 → 内存 JPEG → 既有 vision/chat 管线）；
- ``actions``：三个产品动作的编排（截图提取文字 / 截图翻译 / 截图问答）与展示落点。

本包刻意**不在 ``__init__`` 里再导出**：``pet.screen_capture.session`` 要能在
没有 Qt 的环境里单独 import，包级 re-export 会把 Qt 拖进纯逻辑路径。
"""

__all__: list[str] = []
