# -*- coding: utf-8 -*-
"""「网络信息」弹窗单测（``pet/network_info_dialog.py``）。

Qt 行为从弹窗这个公开 seam 测：注入探针替身（零真实请求），断言
"打开取一次 / 失败如实显示 / 取数中不重复发请求 / 关窗后回调不炸"。
异步一律事件同步 + 宽预算（``_wait_until``），不赌固定 sleep。
"""
from __future__ import annotations

import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from pet import network_info as ni
from pet.network_info_dialog import NetworkInfoDialog

app = QApplication.instance() or QApplication([])


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    app.processEvents()
    return predicate()


_DEFAULT_GEO = object()


def _snapshot(*, ok: bool = True, geo=_DEFAULT_GEO, geo_error="") -> ni.NetworkInfoSnapshot:
    results = (
        ni.ReachabilityResult("Google", "u1", ok, "HTTP 204，0.4s" if ok else "超时，5.0s"),
        ni.ReachabilityResult("GitHub", "u2", ok, "HTTP 200，0.5s" if ok else "超时，5.0s"),
    )
    if geo is _DEFAULT_GEO:
        geo = ni.GeoInfo(
            ip="1.2.3.4", region="广东", city="深圳", isp="电信", source="太平洋 IP 库",
        )
    return ni.NetworkInfoSnapshot(
        geo=geo,
        geo_error=geo_error,
        source_errors=(),
        reachability=results,
        elapsed_s=0.8,
        fetched_at_text="12:34:56",
    )


class FakeProbe:
    """探针替身：按脚本返回快照/抛异常，并记录调用次数。"""

    def __init__(self, result):
        self.result = result
        self.calls = 0

    def collect(self):
        self.calls += 1
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _dialog(result):
    probe = FakeProbe(result)
    dialog = NetworkInfoDialog(None, probe_factory=lambda: probe)
    return dialog, probe


def test_dialog_fetches_once_on_show_and_renders_snapshot():
    dialog, probe = _dialog(_snapshot())
    dialog.show()
    assert _wait_until(lambda: dialog.snapshot is not None)
    assert probe.calls == 1
    assert dialog.value_labels["ip"].text() == "1.2.3.4"
    assert "广东 深圳" in dialog.value_labels["place"].text()
    assert dialog.value_labels["china"].text().startswith("是")
    assert "均可访问" in dialog.value_labels["reachability"].text()
    assert "数据源：太平洋 IP 库" in dialog.hint_label.text()
    assert "12:34:56" in dialog.hint_label.text()

    # 重新显示不重复取（只在打开时取一次）；点「重新获取」才再取一次
    dialog.hide()
    dialog.show()
    app.processEvents()
    assert probe.calls == 1
    dialog.refresh_btn.click()
    assert _wait_until(lambda: probe.calls == 2 and not dialog.fetching)
    dialog.close()


def test_dialog_shows_failure_reason_instead_of_blank():
    dialog, probe = _dialog(_snapshot(
        geo=None, geo_error="太平洋 IP 库：连接失败（blocked）；ip-api.com：超时",
        ok=False,
    ))
    dialog.show()
    assert _wait_until(lambda: not dialog.fetching and probe.calls == 1)
    assert dialog.value_labels["ip"].text() == "获取失败"
    assert dialog.value_labels["china"].text() == "获取失败"
    assert "均不可访问" in dialog.value_labels["reachability"].text()
    assert "属地获取失败" in dialog.hint_label.text()
    dialog.close()


def test_dialog_reports_probe_exception_not_silently():
    dialog, probe = _dialog(RuntimeError("boom"))
    dialog.show()
    assert _wait_until(lambda: not dialog.fetching)
    assert dialog.value_labels["ip"].text() == "获取失败"
    assert "获取失败" in dialog.hint_label.text()
    assert dialog.snapshot is None
    dialog.close()


def test_dialog_disables_refresh_while_fetching():
    release = threading.Event()
    started = threading.Event()

    class BlockingProbe:
        def collect(self):
            started.set()
            release.wait(5.0)
            return _snapshot()

    dialog = NetworkInfoDialog(None, probe_factory=BlockingProbe)
    dialog.show()
    assert started.wait(5.0)
    app.processEvents()
    assert dialog.fetching is True
    assert dialog.refresh_btn.isEnabled() is False
    assert dialog.value_labels["ip"].text() == "获取中…"
    dialog.refresh_btn.click()  # 取数中重入：不得再起一个线程
    app.processEvents()
    assert dialog.fetching is True
    release.set()
    assert _wait_until(lambda: not dialog.fetching)
    assert dialog.value_labels["ip"].text() == "1.2.3.4"
    dialog.close()


def test_dialog_survives_close_before_result_arrives(monkeypatch):
    """关窗后结果才回来：回调先探活，不得触碰已销毁的 Qt 对象。

    背景线程里的异常默认只打到 stderr、不会让用例变红——所以这里挂
    ``threading.excepthook`` 把它变成断言（否则"没炸"是假绿）。
    """
    errors: list = []
    monkeypatch.setattr(threading, "excepthook", lambda args: errors.append(args))
    release = threading.Event()
    started = threading.Event()

    class BlockingProbe:
        def collect(self):
            started.set()
            release.wait(5.0)
            return _snapshot()

    dialog = NetworkInfoDialog(None, probe_factory=BlockingProbe)
    dialog.show()
    assert started.wait(5.0)
    dialog.close()
    dialog.deleteLater()
    app.processEvents()
    release.set()
    assert _wait_until(
        lambda: not [t for t in threading.enumerate() if t.name == "pet-network-info"],
        timeout=5.0)
    app.processEvents()
    assert errors == [], errors


def test_apply_snapshot_is_a_noop_on_deleted_dialog():
    """回调可能在窗口 C++ 对象销毁后才落地：探活守卫生效时静默返回。

    直接对已 ``shiboken6.delete`` 的窗口调回调——没有探活守卫时这里会抛
    RuntimeError（"Internal C++ object already deleted"）。
    """
    import shiboken6

    dialog = NetworkInfoDialog(None, probe_factory=lambda: FakeProbe(_snapshot()))
    shiboken6.delete(dialog)
    assert shiboken6.isValid(dialog) is False
    dialog._apply_snapshot(_snapshot())  # 不抛即通过


def test_dialog_labels_cover_user_visible_fields():
    dialog, _probe = _dialog(_snapshot())
    dialog.show()
    assert _wait_until(lambda: dialog.snapshot is not None)
    labels = [row[1] for row in dialog._labels]
    assert labels == ["公网 IP", "IP 属地", "在中国大陆", "外网连通性"]
    dialog.close()
