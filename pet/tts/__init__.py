# -*- coding: utf-8 -*-
"""TTS 提供者包：注册表 + 内置 provider。

接入新 TTS 的流程见 ``pet/tts/base.py`` 的模块文档。内置 provider 在这里 import
即完成注册（模块顶层无重量级依赖，import 成本可忽略）。
"""

from __future__ import annotations

from .base import (
    FIELD_KINDS,
    TtsAttempt,
    TtsField,
    TtsProvider,
    TtsUnavailable,
    fallback_ids,
    get,
    labels,
    provider_ids,
    providers,
    register,
    secret_fields,
    unregister,
)
from . import doubao, edge, mimo, sapi  # noqa: F401 —— import 即注册（edge 在前 = 默认后备；sapi 为离线本地后端）

__all__ = [
    "FIELD_KINDS",
    "TtsAttempt",
    "TtsField",
    "TtsProvider",
    "TtsUnavailable",
    "doubao",
    "edge",
    "fallback_ids",
    "get",
    "labels",
    "mimo",
    "provider_ids",
    "providers",
    "register",
    "sapi",
    "secret_fields",
    "unregister",
]
