# -*- coding: utf-8 -*-
"""``pet/qt_image_limits.py``：进程级图片解码上限（只抬不降 + 启动点接线）。

探针/实测证据见 ``.scratch/screen-capture/probe_image_allocation.txt``（本机 Qt 6.11.2
默认 256 MB；305 MB 的图默认读不了、抬到 1024 MB 后可读）。
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QBuffer, QByteArray  # noqa: E402
from PySide6.QtGui import QColor, QImage, QImageReader  # noqa: E402

from pet import qt_image_limits  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_limit():
    """用例结束把静态上限还原：它是进程级状态，泄漏出去会影响别的测试文件。"""
    original = QImageReader.allocationLimit()
    yield
    QImageReader.setAllocationLimit(original)


def test_raises_the_limit_and_reports_the_previous_value():
    QImageReader.setAllocationLimit(256)

    previous = qt_image_limits.apply_image_allocation_limit(1024)

    assert previous == 256
    assert QImageReader.allocationLimit() == 1024


def test_never_lowers_a_limit_the_user_already_raised():
    """用户/环境变量给的值更大时原样保留：启动不该把别人的选择改小。"""
    QImageReader.setAllocationLimit(4096)

    previous = qt_image_limits.apply_image_allocation_limit(1024)

    assert previous == 4096
    assert QImageReader.allocationLimit() == 4096


def test_keeps_the_unlimited_setting():
    """0 = 无上限（本机实测：0 下 305 MB 的图照样读得出来），不许被"设定"成 1024。"""
    QImageReader.setAllocationLimit(0)

    previous = qt_image_limits.apply_image_allocation_limit(1024)

    assert previous == 0
    assert QImageReader.allocationLimit() == 0


def test_is_idempotent():
    QImageReader.setAllocationLimit(128)

    assert qt_image_limits.apply_image_allocation_limit() == 128
    assert qt_image_limits.apply_image_allocation_limit() == (
        qt_image_limits.DEFAULT_ALLOCATION_LIMIT_MB
    )
    assert QImageReader.allocationLimit() == qt_image_limits.DEFAULT_ALLOCATION_LIMIT_MB


def test_the_default_is_higher_than_qt_stock():
    """门槛必须真的比 Qt 默认宽——否则这次改动什么都没买到（本机默认 256 MB）。"""
    assert qt_image_limits.DEFAULT_ALLOCATION_LIMIT_MB >= 512


def test_a_blocked_image_decodes_after_the_limit_is_raised():
    """端到端口径：图大于当前上限时读不出来，抬上限之后同一份字节读得出来。

    **为什么不用 10000×8000（305 MB）那张**：它证明的只是"上限挡住了更大的图"，
    与绝对尺寸无关，却要每个跑测试的机器真分配 305 MB（CI 更贵）。这里用同一台机器
    上量出来的等价形态——**小上限（2 MB）+ 中等图（1200×900 = 4.3 MB）**：先证明
    样本确实超限（``blocked is False`` 那一句就是自证），再证明抬起来就通得过。
    305 MB 那组数字仍在 ``.scratch/screen-capture/probe_image_allocation.txt`` 里，
    它是"默认 256 MB 门槛"这个结论的来源，不需要每次 pytest 都复现一遍。
    """
    width, height = 1200, 900            # 32 位 → 4.3 MB
    source = QImage(width, height, QImage.Format.Format_RGB32)
    source.fill(QColor(200, 100, 50))
    payload = QByteArray()
    sink = QBuffer(payload)
    sink.open(QBuffer.OpenModeFlag.WriteOnly)
    assert source.save(sink, "PNG")
    sink.close()
    del source

    QImageReader.setAllocationLimit(2)
    blocked = _read(payload)
    assert blocked is False, "样本图没超当前上限，这条用例就失去意义了"
    assert not 1200 * 900 * 4 <= 2 * 1024 * 1024, "上限与图尺寸的关系要写死在断言里"

    qt_image_limits.apply_image_allocation_limit()

    assert _read(payload) is True


def _read(payload: QByteArray) -> bool:
    """用只读 QBuffer 真解码一次，返回是否成功。

    这里必须把 ``QByteArray`` 绑在局部变量上：``QBuffer`` 只**引用**缓冲区、
    不持有它，写成 ``QBuffer(QByteArray(payload))`` 会让临时对象在读取前就被回收
    ——指针悬空，实测直接原生段错误（同尺寸的图在安全写法下能正常读出来）。
    """
    from PySide6.QtCore import QBuffer

    source = QByteArray(payload)
    buffer = QBuffer(source)
    buffer.open(QBuffer.OpenModeFlag.ReadOnly)
    image = QImageReader(buffer).read()
    buffer.close()
    return not image.isNull()


def test_both_entry_points_apply_the_limit():
    """启动点接线必须真的存在（漏了就是"写了函数没人调"）。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    app_main = (root / "pet" / "app.py").read_text(encoding="utf-8")
    settings_main = (root / "pet" / "__main__.py").read_text(encoding="utf-8")

    assert "apply_image_allocation_limit" in app_main, "pet 进程启动点没接线"
    assert "apply_image_allocation_limit" in settings_main, "设置进程启动点没接线"
