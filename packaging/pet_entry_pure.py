# -*- coding: utf-8 -*-
"""PyInstaller entry for the pure desktop-pet build.

纯桌宠变体 = no-chat 变体（排除 ``pet.chat``）再叠加「全部可选功能模块停用」：
``DSH_PET_PURE=1`` 必须在 import pet.app / pet.__main__ 之前设置——
``pet.feature_activation.decide_and_freeze`` 与设置页进程都按它冻结空启用集
（见 pet/pure_build.py 的模块说明）。
"""
import os
import sys

# 纯桌宠旗标先于一切 pet 导入：feature_activation 在启动链早期读取它。
os.environ["DSH_PET_PURE"] = "1"

from pet.runtime_cleanup import mark_frozen_runtime_dir

# 同 pet_entry_no_chat.py：onefile 运行目录归属标记 + 卸载清理分流都必须在
# import pet.app 之前完成（否则清理参数会当成普通启动，清理不执行）。
mark_frozen_runtime_dir()

if "--uninstall-cleanup" in sys.argv:
    from pet.uninstall_cleanup import main as uninstall_cleanup_main

    sys.exit(uninstall_cleanup_main())

if "--settings" in sys.argv:
    # 同 pet_entry_no_chat.py：--settings 必须在 import pet.app 之前分流到独立
    # 设置进程。pure 变体里 pet.chat 被 excludes 剔除，_run_settings 会经
    # find_spec 探测到并传 include_ai=False（不构造 AI 设置页）；功能页/更新页
    # 由 DSH_PET_PURE 旗标（经环境继承）在 modern_settings_dialog 里隐藏。
    from pet.__main__ import _run_settings

    sys.exit(_run_settings())

if "--ocr-worker" in sys.argv:
    # 同 pet_entry_no_chat.py：本地 OCR 的识别子进程分流（纯桌宠变体不装
    # rapidocr/onnxruntime，该分支实际不会被触发，保留只为与兄弟入口同构）。
    from pet.__main__ import _run_ocr_worker

    sys.exit(_run_ocr_worker())

from pet.app import main

if __name__ == "__main__":
    sys.exit(main(enable_chat=False))
