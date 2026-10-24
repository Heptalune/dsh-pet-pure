# -*- coding: utf-8 -*-
"""纯桌宠构建变体的进程级旗标。

「纯桌宠」变体（``scripts/build_pure.ps1`` → ``packaging/pet_entry_pure.py``）
在 import pet.app 之前把环境变量 ``DSH_PET_PURE`` 置为 ``1``。这个变体的产品
语义是：**只保留桌宠本体**（动画播放、点击/拖拽/物理/多开、自言自语气泡、
点击音效、托盘与菜单、设置、角色切换与素材页），一切可选功能模块
（AI 对话 / 识屏 OCR / 主动识屏 / 文件解读 / Agent 联动 / 灵动岛 / 音乐 /
语音 TTS / 天气 / 抽签 / 待办 / 节日 / 快捷启动 / 直播捕获 / 系统工具）与
在线更新、DeepSeek 相关入口一概不出场。

设计约束：

- 只读环境变量，不依赖配置/Qt/任何第三方库——它会被启动链最早的模块
  （``feature_activation``）import，依赖面必须与那个模块一样薄；
- 环境变量由入口脚本在进程启动时设置并被子进程（设置页 ``--settings``、
  多开子宠）天然继承，不需要落盘——它是**构建形态**的属性，不是用户偏好：
  用户在纯桌宠版里没有「把砍掉的功能再开回来」的入口，也不该有。
"""

from __future__ import annotations

import os

#: 纯桌宠变体的环境变量名（入口脚本设置，子进程经继承拿到同一取值）。
ENV_PURE = "DSH_PET_PURE"


def is_pure_build() -> bool:
    """当前进程是否运行在纯桌宠变体里。"""
    return os.environ.get(ENV_PURE, "") == "1"
