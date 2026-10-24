# -*- coding: utf-8 -*-
"""带「代理不通就直连」兜底的 urlopen（Windows 系统代理常被加速器打开后进程不在）。

背景（2026-09-17 实机）：Windows 的系统代理（IE/WinINET 设置，`ProxyEnable=1`,
`ProxyServer=127.0.0.1:33210`）被加速器/VPN 打开后如果那个进程没在跑，
`urllib.request.urlopen` 仍会照着它走 ⇒ 所有请求 `ConnectionRefused`。歌词取词因此
全军覆没（气泡只剩歌名，用户报"歌词显示一半就剩歌名"）；余额查询、识屏、AI 对话、
更新检查同样会废掉。

用法：把 `urllib.request.urlopen(req, timeout=…)` 换成
`http_util.urlopen(req, timeout=…)` —— 先按系统代理试一次（尊重用户的加速器/VPN），
只在**传输失败**时改直连重试，并把结论固化在进程内，后续请求不再撞一遍死代理。
服务器明确答复（`HTTPError` 4xx/5xx）不重试也不改传输方式：链路本来就是通的。
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

_force_direct = False
_direct_opener = None


def _opener(use_proxy: bool, context=None):
    """取 opener：``use_proxy=False`` 强制直连；``context`` 为 SSL 上下文（如 providers）。"""
    handlers = []
    if not use_proxy:
        handlers.append(urllib.request.ProxyHandler({}))
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    if not handlers:
        return urllib.request.build_opener()
    if not use_proxy and context is None:
        global _direct_opener
        if _direct_opener is None:
            _direct_opener = urllib.request.build_opener(*handlers)
        return _direct_opener
    return urllib.request.build_opener(*handlers)


def force_direct() -> bool:
    """本进程是否已确定"系统代理不可用、固定直连"（诊断/测试用）。"""
    return _force_direct


def reset_transport() -> None:
    """复位传输层结论（测试隔离；网络环境变化后也可显式重试代理）。"""
    global _force_direct, _direct_opener
    _force_direct = False
    _direct_opener = None


def urlopen(request, *, timeout: float, context=None, **kwargs):
    """同 :func:`urllib.request.urlopen`，但代理连不上时自动改直连重试一次。

    返回的就是 response（可直接 ``with`` / ``.read()``）。`HTTPError` 原样抛出；
    两种方式都失败时抛最后一次的传输异常，保持与原生 `urlopen` 一致的失败语义。
    """
    global _force_direct
    attempts = (False,) if _force_direct else (True, False)
    last_exc: Exception | None = None
    for use_proxy in attempts:
        try:
            response = _opener(use_proxy, context).open(
                request, timeout=timeout, **kwargs
            )
        except urllib.error.HTTPError:
            raise  # 服务器有答复：链路通，重试无意义
        except Exception as exc:  # noqa: BLE001 - 传输失败：换下一种方式再试
            last_exc = exc
            continue
        if not use_proxy and not _force_direct:
            _force_direct = True
            log.info("网络请求改走直连：系统代理不可用（%s）", type(last_exc).__name__)
        return response
    assert last_exc is not None
    raise last_exc
