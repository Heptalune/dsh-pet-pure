# -*- coding: utf-8 -*-
"""Qt 图片解码的内存上限（进程级一处设定）。

**为什么要动它**：``QImageReader`` 默认给单张图解码设了内存上限，超过就直接
读不出来（``QImageReader.error() == QImageReader.ImageAllocationError``），
而不是"慢一点"。对看图类工具这是拒绝服务级的问题——Flameshot 为此显式把它抬到
1024 MB。桌宠读图的地方不止一处（聊天附件预览、设置页头像选择、趣味图片弹窗、
动画缩略图），而且**多屏截图**这种尺寸的图正好是用户会拿来做素材的东西。

**本机的实测口径（不照抄 Flameshot 的 128 MB 说法）**：Qt 6.11.2 / x64 的默认值
是 **256 MB**（探针：``.scratch/screen-capture/probe_image_allocation.txt``）。
逐档量的结论是：

- 4K（31.6 MB）、4K×2/×3/×4 并排（63.3 / 94.9 / 126.6 MB）、8K（126.6 MB）、
  8K×2（253.1 MB）在默认值下**都能读**——"4K 截图直接失败"在这套 Qt 上不成立；
- 10000×8000（305.2 MB）默认**读不了**，抬到 1024 MB 后可读——上限真实生效，
  只是门槛比流传的 128 MB 高一倍（32 位构建与旧版本 Qt 才是 128 MB）。

所以这次设定买到的是**余量**，不是"修好了 4K 截图"：把门槛从 256 MB 抬到
1024 MB，代价是极端尺寸的图可能多占最多 1 GB 内存，换取"用户给的图读得进来"。
刻意**不**设 -1（无上限）：损坏的/恶意的图头可以声明一个天文数字，无上限等于
把"拒绝服务"从默认值那里搬到自己手上。
"""

from __future__ import annotations

import logging

log = logging.getLogger("dsh-pet-standalone")

__all__ = ["DEFAULT_ALLOCATION_LIMIT_MB", "apply_image_allocation_limit"]

#: 单张图解码的内存上限（MB）。1024 = 本机默认（256 MB）的四倍，够放一张
#: 26000×10000 的 32 位图；比它更大的"截图"不存在，但比它更大的**坏图头**随时会有，
#: 所以不设 -1。
DEFAULT_ALLOCATION_LIMIT_MB = 1024


def apply_image_allocation_limit(limit_mb: int = DEFAULT_ALLOCATION_LIMIT_MB) -> int:
    """把 ``QImageReader`` 的解码上限抬到 ``limit_mb``；返回设定前的值。

    **只抬不降**：当前值已经是更宽的选择时原样保留——用户可能用
    ``QT_IMAGE_ALLOCATION_LIMIT`` 环境变量给了自己的答案，进程启动不该把它改小。
    两个"更宽"的形态都按本机实测口径处理（Qt 6.11.2 / PySide6）：

    - ``0`` = **无上限**（实测：0 下 305 MB 的图照样读得出来）→ 保留；
    - 负值：``setAllocationLimit(-1)`` 在本绑定里是 no-op（设完再读回来还是旧值），
      所以实际上取不到；这里仍按"更宽"处理，免得将来某版支持 -1 时被改小。

    零依赖、可在任何 QApplication 之前/之后调用（``QImageReader`` 的设置是静态的，
    不做解码就不需要事件循环）；重复调用是幂等的。
    """
    from PySide6.QtGui import QImageReader

    previous = int(QImageReader.allocationLimit())
    if previous <= 0 or previous >= int(limit_mb):
        return previous
    QImageReader.setAllocationLimit(int(limit_mb))
    log.debug("图片解码上限：%d MB → %d MB", previous, limit_mb)
    return previous
