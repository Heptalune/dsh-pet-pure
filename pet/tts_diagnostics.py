# -*- coding: utf-8 -*-
"""Bounded TTS diagnostics without request URLs, tokens or proxy credentials."""
from __future__ import annotations

from functools import lru_cache
import sys


def missing_dependency_hint() -> str:
    if getattr(sys, "frozen", False):
        return "语音组件缺失或不完整，请更新或重新安装桌宠。"
    return "语音报时需要 edge-tts 库：请运行 pip install edge-tts 后重试"


@lru_cache(maxsize=1)
def _version() -> str:
    from importlib import metadata

    try:
        return metadata.version("edge-tts")
    except metadata.PackageNotFoundError:
        return "missing"


def describe_failure(error: Exception, *, proxy_used: bool | None) -> tuple[str, str]:
    name = type(error).__name__
    raw_status = getattr(error, "status", None)
    status = raw_status if isinstance(raw_status, int) else None
    if status == 403:
        message = "在线语音握手被拒绝（HTTP 403）"
    elif isinstance(error, TimeoutError):
        message = "在线语音连接超时"
    elif "Proxy" in name:
        message = "在线语音代理连接失败"
    elif name == "NoAudioReceived":
        message = "在线语音未返回音频"
    elif isinstance(status, int):
        message = f"在线语音连接失败（HTTP {status}）"
    else:
        message = f"在线语音处理失败（{name}）"
    route = "unknown" if proxy_used is None else "system_proxy" if proxy_used else "direct"
    diagnostic = f"{message}; type={name} version={_version()} http={status} route={route}"
    return message, diagnostic
