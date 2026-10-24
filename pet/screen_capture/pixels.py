# -*- coding: utf-8 -*-
"""「快速识屏」像素链路：冻结帧抓取 → 选区裁剪 → 统一 sRGB 8-bit。

硬指标是**"截图与肉眼色彩一致"**，所以这一层有两个刻意的取舍：

1. **底图是"设备像素 + dpr=1.0"的裸像素图**。Qt 在高 DPI 屏上返回"设备像素
   尺寸 + devicePixelRatio 标记"的图（例如 150% 缩放的 2560×1440 屏给出
   2560×1440 像素、dpr=1.5）；本模块在抓帧时就把 dpr 归一为 1.0，调用方按
   ``SelectionRect.device_rect(屏幕 dpr)`` 直接得到像素坐标，不再有"这个坐标
   是逻辑还是物理"的歧义。
2. **色彩空间必须真的转换，不能只换格式**。``QImage.convertToFormat`` 只改
   存储宽度（16→8 位），不管色彩空间；HDR/广色域帧直接这么截会发灰发闷。
   ``to_srgb8`` 先把图像转到 sRGB 色彩空间再做 8 位量化，进 OCR 之前色彩口径
   唯一。裁剪放在转换之前：逐像素变换与顺序无关，先裁只需处理选区那一块。

隐私：全程只在内存里，不落盘（与 ``pet/vision.py`` 同一约定）。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QRect
from PySide6.QtGui import QColorSpace, QImage, QPixmap

from .hdr_capture import grab_hdr_sdr, hdr_capture_disabled, hdr_state
from .session import SelectionRect

log = logging.getLogger("dsh-pet-standalone")

__all__ = ["SRGB8_FORMAT", "SRGB_COLOR_SPACE", "crop_selection", "grab_screen_image", "to_srgb8"]

#: 进下游（OCR/翻译）的唯一像素格式：8-bit sRGB，字节序与 OpenCV/PIL 兼容。
SRGB8_FORMAT = QImage.Format.Format_RGB888

SRGB_COLOR_SPACE = QColorSpace(QColorSpace.NamedColorSpace.SRgb)


def _grab_hdr_screen(screen) -> QImage | None:
    """HDR 屏优先走 DXGI FP16 通道；不适用或失败一律 ``None``（调用方落回 GDI）。

    三层闸，任何一层不满足都直接回退：

    1. 逃生门环境变量 ``DSH_PET_DISABLE_HDR_CAPTURE``（见 ``hdr_capture``）；
    2. 这块屏的 HDR 是否开着（DisplayConfig，只读；SDR 屏在这一层就返回，不碰 DXGI）；
    3. ``grab_hdr_sdr`` 自己那一路的判据（DXGI 输出必须真在 HDR10/PQ、抓到的帧
       必须是 FP16 scRGB……）。

    这里再包一层 ``except``：``grab_hdr_sdr`` 已经承诺不抛，但抓帧是主路径，
    "HDR 通道的任何意外都不许影响它"这条要在这里也成立。
    """
    if hdr_capture_disabled():
        return None
    try:
        enabled, _white = hdr_state(screen)
        if not enabled:
            return None
        # 传 QScreen 而不是 screen.name()：多屏同型号时名字会撞车，物理位置不会。
        return grab_hdr_sdr(screen)
    except Exception as exc:  # noqa: BLE001 - 见 docstring：主路径不受 HDR 通道影响
        log.warning("HDR 抓帧通道异常，回退 GDI：%s", exc)
        return None


def grab_screen_image(screen) -> QImage:
    """抓一整屏作为冻结帧底图；抓不到返回空图（调用方按失败处理）。

    路径选择：**HDR 屏先走 DXGI FP16 通道**（真机 A/B 对照探针见
    ``.scratch/screen-capture/probe_hdr_capture_ab.py``），拿不到就落回
    ``screen.grabWindow(0)``（Qt 的整屏抓取入口，Windows 上走 BitBlt）——回退
    路径的行为一字未改。本函数只保证返回值的坐标口径确定。
    """
    hdr_image = _grab_hdr_screen(screen)
    if hdr_image is not None:
        return hdr_image
    try:
        grabbed = screen.grabWindow(0)
    except (AttributeError, RuntimeError, TypeError) as exc:
        log.warning("快速识屏：抓屏失败 (%s)", exc)
        return QImage()
    if grabbed is None:
        return QImage()
    # ``QScreen.grabWindow`` 在 Qt6 返回 QPixmap（真机实测），拆到 QImage 才能做
    # 逐像素操作；有些平台/替身会直接给 QImage，两种都收。
    if isinstance(grabbed, QPixmap):
        image = grabbed.toImage()
    elif isinstance(grabbed, QImage):
        image = grabbed
    else:
        log.warning("快速识屏：抓屏返回了非图像对象 (%s)", type(grabbed).__name__)
        return QImage()
    if image.isNull():
        return QImage()
    # 统一成"裸设备像素"：dpr 只用于坐标换算，不留在图里干扰下游。
    image.setDevicePixelRatio(1.0)
    return image


def crop_selection(frame: QImage, selection: SelectionRect, dpr: float) -> QImage:
    """从冻结帧裁出选区（设备像素），产出图 ``devicePixelRatio == 1.0``。

    ``dpr`` 是**屏幕**的逻辑/物理比（不是图的），因为 ``selection`` 是覆盖层
    上的逻辑坐标。越界按下图边界钳制（分数 DPR 的向外取整会在屏幕边缘溢出
    一像素，这是常态而非错误）；钳制后什么都不剩才算失败。
    """
    if dpr <= 0:
        raise ValueError(f"dpr 必须为正数，收到 {dpr!r}")
    if frame is None or frame.isNull():
        raise ValueError("冻结帧为空")
    if not isinstance(selection, SelectionRect):
        raise TypeError(f"crop_selection() 需要 SelectionRect，收到 {type(selection).__name__}")
    if selection.is_empty:
        raise ValueError(f"空选区 {selection!r}")

    left, top, width, height = selection.device_rect(dpr)
    rect = QRect(left, top, width, height).intersected(frame.rect())
    if rect.isEmpty():
        raise ValueError(f"选区 {selection!r} 超出冻结帧 {frame.size().toTuple()}")

    crop = frame.copy(rect)
    if crop.isNull():
        raise ValueError(f"选区 {selection!r} 裁剪结果为空")
    crop.setDevicePixelRatio(1.0)
    return crop


def to_srgb8(image: QImage) -> QImage:
    """把任意来源的图统一成 sRGB 8-bit（无色彩空间标记的按 sRGB 处理）。

    先转色彩空间再量化位深：线性 0.5 的输入应得到 188 而不是 128——后者是
    "只换格式"的结果，也是 HDR 屏上截图发灰的成因。
    """
    if image is None:
        return QImage()
    if image.isNull():
        return image

    space = image.colorSpace()
    if space.isValid() and space != SRGB_COLOR_SPACE:
        converted = image.convertedToColorSpace(SRGB_COLOR_SPACE)
        if not converted.isNull():
            image = converted
        else:
            # 自定义 ICC 变换不支持时退化为只量化位深：宁可口径不完美，也不
            # 交一张空图下去（空图会让整条 OCR 链路以为"截图成功但没有内容"）。
            log.warning("快速识屏：色彩空间转换失败，退化为直接量化 (%s)", space)
    return image.convertToFormat(SRGB8_FORMAT)
