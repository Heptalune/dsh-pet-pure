# -*- coding: utf-8 -*-
"""脱敏诊断的分类表：错误分类成用户能懂的人话，细节进日志且不带凭据。

provider 化之后，这套诊断由 edge 后端自己消费（``pet/tts/edge.py`` 的 ``synth()``）；
这里只验分类表本身，端到端（错误串 + 日志 + 半截产物清理）在 ``tests/test_tts_edge.py``。
"""
from __future__ import annotations

import sys

import pytest

from pet.tts_diagnostics import describe_failure, missing_dependency_hint


class HandshakeRejected(Exception):
    status = 403


@pytest.mark.parametrize("error,expected", [
    (HandshakeRejected("wss://host/?token=PRIVATE_TOKEN http://user:PRIVATE_PASSWORD@proxy"), "403"),
    (TimeoutError("request included PRIVATE_TOKEN"), "超时"),
    (type("NoAudioReceived", (Exception,), {})("PRIVATE_TOKEN"), "未返回音频"),
    (type("ClientProxyConnectionError", (Exception,), {})("PRIVATE_PASSWORD"), "代理"),
])
def test_describe_failure_classifies_and_never_echoes_credentials(error, expected):
    message, diagnostic = describe_failure(error, proxy_used=True)

    assert expected in message, f"分类结果里要能看出是什么错：{message}"
    assert "PRIVATE_TOKEN" not in message + diagnostic
    assert "PRIVATE_PASSWORD" not in message + diagnostic
    assert "version=" in diagnostic and "route=" in diagnostic


def test_describe_failure_route_states_proxy_use_without_the_proxy_value():
    """route 只说「走没走代理」，绝不带代理地址或凭据。"""
    error = RuntimeError("http://user:PRIVATE_PASSWORD@127.0.0.1:7890 连接失败")

    assert "route=system_proxy" in describe_failure(error, proxy_used=True)[1]
    assert "route=direct" in describe_failure(error, proxy_used=False)[1]
    assert "route=unknown" in describe_failure(error, proxy_used=None)[1]
    assert "PRIVATE_PASSWORD" not in "".join(describe_failure(error, proxy_used=True))


@pytest.mark.parametrize("frozen,expected", [
    (False, "pip install edge-tts"),
    (True, "更新"),
])
def test_missing_dependency_hint_knows_packaged_builds(monkeypatch, frozen, expected):
    """打包版用户没地方 pip install：冻结时只能说「更新或重装」。"""
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)

    hint = missing_dependency_hint()

    assert expected in hint
    if frozen:
        assert "pip install" not in hint
