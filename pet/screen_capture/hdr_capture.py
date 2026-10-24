# -*- coding: utf-8 -*-
"""HDR 显示器抓帧：DXGI Desktop Duplication 抓 FP16 scRGB → 普通 sRGB 8-bit。

## 为什么需要这条通道

HDR 屏上 ``QScreen.grabWindow``（Windows 走 GDI BitBlt）拿到的是 Windows
**按 SDR 参考折叠过**的 8-bit 图：高光被削、观感发白雾。本模块绕开 GDI，用
DXGI Desktop Duplication 直接读合成器给的 **FP16 scRGB** 桌面，自己做三步换算：

1. **归一到本机 SDR 白点**——scRGB 里 ``1.0 == 80 nits``，而 SDR 白点是
   DisplayConfig 的 ``SDRWhiteLevel / 1000`` 个 scRGB 单位（本机 3500 ⇒ 3.5
   ⇒ 280 nits）。除以它，SDR 内容的白正好落回 1.0，屏幕上看什么样截出来就
   什么样；
2. **钳制到 SDR 范围**——比 SDR 白更亮的（HDR 高光）在 8-bit SDR 图里没有
   对应值，一律钳到 1.0；
3. **sRGB 传递函数编码成 8-bit**——线性 0.5 要得到 188，不是直接截断的 128。

参考实现是 flameshot PR #4974 的 ``src/utils/windowshdrcapture.cpp``（同一口径：
DisplayConfig 判定 → ``DuplicateOutput1`` 抓 FP16 → 按 SDR 白点归一 → 钳制 →
sRGB 编码）。本模块的差异都写明了理由：

- **纯 ctypes，不引 comtypes**：仓库里的 DXGI 用法（``.scratch/screen-capture/
  probe_hdr.py``、``pet/gpu_monitor.py``）一律是 ctypes 手写虚表调用，沿用同一
  套路可以不动依赖与打包脚本；
- **开复制会话时 ``DuplicateOutput1`` 之后还有经典 ``DuplicateOutput`` 兜底**，
  且**判据落在"这一帧是不是 FP16"上**：只有 ``DuplicateOutput1`` 给的才是带真实
  内容的 FP16 帧；经典接口的稳态帧是 BGRA8（唯一那张 FP16 是未初始化的全零面），
  而 ``DuplicateOutput1`` 在本机**反复无常**（整段时间全失败、过一阵全成功）——
  所以「协商失败」按抖动退避，「协商成功却仍给 BGRA8」才按判定退避（见
  ``_duplicate_output`` 与 ``_FormatMismatch`` 的 docstring）；
- **接受 ``LastPresentTime == 0`` 的帧**：刚开的复制会话第一帧带着桌面图，但它的
  ``LastPresentTime`` 报 0（本机实测），参考实现那条规则在静态桌面上会把这一帧丢掉；
  该帧也**可能是未初始化的全零面**，所以另有"全零再等一帧"的防守（见 ``grab_srgb8``）；
- **失败一律回退，不抛给调用方**：任何一步失败都返回 ``None``，由
  ``pixels.grab_screen_image`` 落回 GDI 路径；
- **失败后分级退避**：见 ``_NEGATIVE_TTL_S`` / ``_FORMAT_VERDICT_TTL_S``。

## ABI 出处（不是凭记忆，逐条对着本机 Windows SDK 10.0.26100.0 的头文件核过）

虚表槽位是 0 基、含 ``IUnknown`` 三槽：

| 接口 | 槽位 | 方法 | 头文件 |
|---|---|---|---|
| IDXGIFactory1 | 12 | EnumAdapters1 | ``shared/dxgi.h:2569`` |
| IDXGIAdapter1 | 7 / 13 | EnumOutputs / GetDesc1 | ``shared/dxgi.h:2763`` |
| IDXGIOutput | 7 | GetDesc | ``shared/dxgi.h:1565`` |
| IDXGIOutput1 | 22 | DuplicateOutput | ``shared/dxgi1_2.h:2347`` |
| IDXGIOutput5 | 26 | DuplicateOutput1 | ``shared/dxgi1_5.h:134`` |
| IDXGIOutput6 | 27 | GetDesc1 | ``shared/dxgi1_6.h:444`` |
| IDXGIOutputDuplication | 8 / 14 | AcquireNextFrame / ReleaseFrame | ``shared/dxgi1_2.h:347`` |
| ID3D11Device | 5 | CreateTexture2D | ``um/d3d11.h:14448`` |
| ID3D11Texture2D | 10 | GetDesc | ``um/d3d11.h:2892`` |
| ID3D11DeviceContext | 14 / 15 / 47 | Map / Unmap / CopyResource | ``um/d3d11.h:8573`` |

常量出处：``DXGI_FORMAT_R16G16B16A16_FLOAT``=10、``B8G8R8A8_UNORM``=87
（``shared/dxgiformat.h:23,100``）；``DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020``=12
（``shared/dxgicommon.h:41``）；``DXGI_MODE_ROTATION_*``（``shared/dxgitype.h:74``）；
``DXGI_ERROR_WAIT_TIMEOUT``=0x887A0027（``shared/winerror.h:59499``）；
``D3D11_USAGE_STAGING``=3、``D3D11_CPU_ACCESS_READ``=0x20000、``D3D11_MAP_READ``=1、
``D3D11_CREATE_DEVICE_BGRA_SUPPORT``=0x20、``D3D11_SDK_VERSION``=7
（``um/d3d11.h:1222,1244,1275,15007,15014``）；``D3D_DRIVER_TYPE_UNKNOWN``=0
（``um/d3dcommon.h:85``）。

**槽位数错一个就是原生崩溃**（调错槽位 = 用错签名调别人的函数），所以上表是
本模块最该被复核的部分：它来自本机 SDK 头文件的虚表声明顺序，不是任何记忆。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import logging
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass

import numpy as np
from PySide6.QtGui import QColorSpace, QImage

log = logging.getLogger("dsh-pet-standalone")

__all__ = ["DISABLE_ENV", "fp16_scrgb_to_srgb8", "grab_hdr_sdr", "hdr_capture_disabled",
           "hdr_state", "release_cached_sessions"]

#: 逃生门：置为 1（或 true/yes/on 之类的真值）时整条 HDR 通道不启用，抓帧直接
#: 走 GDI。给"新通道在某个驱动/某个显示器上出问题"留一条不改代码就能回退的路。
DISABLE_ENV = "DSH_PET_DISABLE_HDR_CAPTURE"

# --------------------------------------------------------------- Win32 常量

QDC_ONLY_ACTIVE_PATHS = 0x00000002
DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME = 1
DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME = 2
DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO = 9
DISPLAYCONFIG_DEVICE_INFO_GET_SDR_WHITE_LEVEL = 11
MONITOR_DEFAULTTONEAREST = 2
CCHDEVICENAME = 32

DXGI_FORMAT_R16G16B16A16_FLOAT = 10
DXGI_FORMAT_B8G8R8A8_UNORM = 87
DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020 = 12
DXGI_MODE_ROTATION_UNSPECIFIED = 0
DXGI_MODE_ROTATION_IDENTITY = 1
DXGI_ERROR_NOT_CURRENTLY_AVAILABLE = 0x887A0022
DXGI_ERROR_ACCESS_LOST = 0x887A0026
DXGI_ERROR_WAIT_TIMEOUT = 0x887A0027

D3D_DRIVER_TYPE_UNKNOWN = 0
D3D11_CREATE_DEVICE_BGRA_SUPPORT = 0x20
D3D11_SDK_VERSION = 7
D3D11_USAGE_STAGING = 3
D3D11_CPU_ACCESS_READ = 0x20000
D3D11_MAP_READ = 1

#: 首帧/重建复制会话时等待桌面帧的总预算与单次轮询（参考实现同值）。
FRAME_WAIT_MS = 500
FRAME_POLL_MS = 100

#: 失败后的退避秒数：一次失败（设备丢了、别的应用正占着复制会话、超时……）
#: 之后这段时间内不再尝试，避免"每次截图都白付一遍建 D3D 设备的钱"。
_NEGATIVE_TTL_S = 60.0

#: "这个桌面模式给不出 FP16 帧"这种**判定级**结论的退避秒数，比抖动型失败长得多。
#: 取舍：判定本身要花一次建设备的钱（本机实测冷启动 ≈0.6s），而它跟桌面模式/驱动
#: 绑定、短时间重试不会变；静态桌面上更没有任何理由每隔一分钟再交一次学费。
#: 15 分钟后自动重试一次，进程重启也会清空——真换了显示器/换了 HDR 状态不会卡死。
_FORMAT_VERDICT_TTL_S = 900.0

# --------------------------------------------------------------- Win32 结构体
# 布局与 .scratch/screen-capture/probe_hdr.py 一致（该探针已在本机跑通），
# 另外补了复制会话与 D3D11 侧要用的几个。
#
# 字段一律用固定宽度类型，不用 wintypes 别名：wintypes.DWORD/LONG 是
# c_ulong/c_long，在非 Windows 上是 8 字节；wintypes.BOOL 在 Linux 上是 8
# 字节（CI 实测）；c_wchar 在非 Windows 上是 4 字节——结构体尺寸会随宿主
# 平台漂移，ABI 契约测试（tests/test_screen_capture_hdr_capture.py）在非
# Windows CI 上就会误报。Win32 ABI 的宽度是死的，跟宿主平台无关。

_DWORD = ctypes.c_uint32
_LONG = ctypes.c_int32
#: Win32 BOOL 固定 4 字节（wintypes.BOOL 在 Linux 上是 8）。
_BOOL = ctypes.c_int32
#: Win32 WCHAR 固定 2 字节；ctypes.c_wchar 在非 Windows 上是 4 字节。
_WCHAR = ctypes.c_uint16


def _wstr(chars) -> str:
    """固定宽度 UTF-16 码位数组 → str（到第一个 NUL 为止）。

    结构体里的 WCHAR 字段是 ``_WCHAR`` 数组（ctypes 不会像 c_wchar 那样
    自动转 str），读出来统一过这里。
    """
    buf = bytearray()
    for unit in chars:
        if unit == 0:
            break
        buf += unit.to_bytes(2, "little")
    return bytes(buf).decode("utf-16-le", errors="replace")


class _POINT(ctypes.Structure):
    _fields_ = [("x", _LONG), ("y", _LONG)]


class _RECT(ctypes.Structure):
    _fields_ = [("left", _LONG), ("top", _LONG), ("right", _LONG), ("bottom", _LONG)]


class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", _DWORD), ("HighPart", _LONG)]


class _DEVICE_INFO_HEADER(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_uint32),
        ("size", ctypes.c_uint32),
        ("adapterId", _LUID),
        ("id", ctypes.c_uint32),
    ]


class _PATH_SOURCE_INFO(ctypes.Structure):
    _fields_ = [
        ("adapterId", _LUID),
        ("id", ctypes.c_uint32),
        ("modeInfoIdx", ctypes.c_uint32),
        ("statusFlags", ctypes.c_uint32),
    ]


class _RATIONAL(ctypes.Structure):
    _fields_ = [("Numerator", ctypes.c_uint32), ("Denominator", ctypes.c_uint32)]


class _PATH_TARGET_INFO(ctypes.Structure):
    _fields_ = [
        ("adapterId", _LUID),
        ("id", ctypes.c_uint32),
        ("modeInfoIdx", ctypes.c_uint32),
        ("outputTechnology", ctypes.c_uint32),
        ("rotation", ctypes.c_uint32),
        ("scaling", ctypes.c_uint32),
        ("refreshRate", _RATIONAL),
        ("scanLineOrdering", ctypes.c_uint32),
        ("targetAvailable", _BOOL),
        ("statusFlags", ctypes.c_uint32),
    ]


class _PATH_INFO(ctypes.Structure):
    _fields_ = [
        ("sourceInfo", _PATH_SOURCE_INFO),
        ("targetInfo", _PATH_TARGET_INFO),
        ("flags", ctypes.c_uint32),
    ]


class _MODE_INFO(ctypes.Structure):
    """``DISPLAYCONFIG_MODE_INFO``：``QueryDisplayConfig`` 要求这块缓冲区大小对得上。

    头部 16 字节（type/id/adapterId）+ 联合体 48 字节 = 64。联合体不读，按字节
    占位即可；``tests/test_screen_capture_hdr_capture.py`` 有一条断言守着 64。
    """

    _fields_ = [
        ("infoType", ctypes.c_uint32),
        ("id", ctypes.c_uint32),
        ("adapterId", _LUID),
        ("payload", ctypes.c_byte * 48),
    ]


class _SOURCE_DEVICE_NAME(ctypes.Structure):
    _fields_ = [
        ("header", _DEVICE_INFO_HEADER),
        ("viewGdiDeviceName", _WCHAR * CCHDEVICENAME),
    ]


class _TARGET_DEVICE_NAME(ctypes.Structure):
    _fields_ = [
        ("header", _DEVICE_INFO_HEADER),
        ("flags", ctypes.c_uint32),
        ("outputTechnology", ctypes.c_uint32),
        ("edidManufactureId", ctypes.c_uint16),
        ("edidProductCodeId", ctypes.c_uint16),
        ("connectorInstance", ctypes.c_uint32),
        ("monitorFriendlyDeviceName", _WCHAR * 64),
        ("monitorDevicePath", _WCHAR * 128),
    ]


class _ADVANCED_COLOR_INFO(ctypes.Structure):
    """``value`` 的第 0/1 位分别是"支持"与"已启用"高级颜色（HDR）。"""

    _fields_ = [
        ("header", _DEVICE_INFO_HEADER),
        ("value", ctypes.c_uint32),
        ("colorEncoding", ctypes.c_uint32),
        ("bitsPerColorChannel", ctypes.c_uint32),
    ]


class _SDR_WHITE_LEVEL(ctypes.Structure):
    _fields_ = [("header", _DEVICE_INFO_HEADER), ("SDRWhiteLevel", ctypes.c_uint32)]


class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", _DWORD),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", _DWORD),
        ("szDevice", _WCHAR * CCHDEVICENAME),
    ]


class _DXGI_OUTPUT_DESC(ctypes.Structure):
    _fields_ = [
        ("DeviceName", _WCHAR * 32),
        ("DesktopCoordinates", _RECT),
        ("AttachedToDesktop", _BOOL),
        ("Rotation", ctypes.c_uint32),
        ("Monitor", wintypes.HANDLE),
    ]


class _DXGI_OUTPUT_DESC1(ctypes.Structure):
    _fields_ = [
        ("DeviceName", _WCHAR * 32),
        ("DesktopCoordinates", _RECT),
        ("AttachedToDesktop", _BOOL),
        ("Rotation", ctypes.c_uint32),
        ("Monitor", wintypes.HANDLE),
        ("BitsPerColor", ctypes.c_uint32),
        ("ColorSpace", ctypes.c_uint32),
        ("RedPrimary", ctypes.c_float * 2),
        ("GreenPrimary", ctypes.c_float * 2),
        ("BluePrimary", ctypes.c_float * 2),
        ("WhitePoint", ctypes.c_float * 2),
        ("MinLuminance", ctypes.c_float),
        ("MaxLuminance", ctypes.c_float),
        ("MaxFullFrameLuminance", ctypes.c_float),
    ]


class _OUTDUPL_POINTER_POSITION(ctypes.Structure):
    _fields_ = [("Position", _POINT), ("Visible", _BOOL)]


class _OUTDUPL_FRAME_INFO(ctypes.Structure):
    """``AcquireNextFrame`` 的出参。**判据不看它的 ``LastPresentTime``**：
    本机实测刚开的复制会话第一帧报 0（"桌面自上次抓取后没变过"）但带着桌面图，
    所以"这一帧有没有图"只看资源指针（见 ``_Session._acquire``）。
    """

    _fields_ = [
        ("LastPresentTime", ctypes.c_int64),
        ("LastMouseUpdateTime", ctypes.c_int64),
        ("AccumulatedFrames", ctypes.c_uint32),
        ("RectsCoalesced", _BOOL),
        ("ProtectedContentMaskedOut", _BOOL),
        ("PointerPosition", _OUTDUPL_POINTER_POSITION),
        ("TotalMetadataBufferSize", ctypes.c_uint32),
        ("PointerShapeBufferSize", ctypes.c_uint32),
    ]


class _TEXTURE2D_DESC(ctypes.Structure):
    # DXGI_SAMPLE_DESC（Count/Quality）就地展开：同一内存布局，少一层嵌套。
    _fields_ = [
        ("Width", ctypes.c_uint32),
        ("Height", ctypes.c_uint32),
        ("MipLevels", ctypes.c_uint32),
        ("ArraySize", ctypes.c_uint32),
        ("Format", ctypes.c_uint32),
        ("SampleCount", ctypes.c_uint32),
        ("SampleQuality", ctypes.c_uint32),
        ("Usage", ctypes.c_uint32),
        ("BindFlags", ctypes.c_uint32),
        ("CPUAccessFlags", ctypes.c_uint32),
        ("MiscFlags", ctypes.c_uint32),
    ]


class _MAPPED_SUBRESOURCE(ctypes.Structure):
    _fields_ = [
        ("pData", ctypes.c_void_p),
        ("RowPitch", ctypes.c_uint32),
        ("DepthPitch", ctypes.c_uint32),
    ]


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _iid(text: str) -> _GUID:
    return _GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


IID_IDXGIFACTORY1 = _iid("770aae78-f26f-4dba-a829-253c83d1b387")
IID_IDXGIOUTPUT6 = _iid("068346e8-aaec-4b84-add7-137f513f77a1")
IID_ID3D11TEXTURE2D = _iid("6f15aaf2-d208-4e89-9ab4-489535d34f9c")


class _Unavailable(Exception):
    """内部信号：这条通道这次用不了（外部一律折成 ``None`` + 一行日志）。"""


class _FormatMismatch(_Unavailable):
    """复制会话给的不是 FP16 帧——不是"这次超时了"那种抖动。

    分两种情况，退避长短不同（``grab_hdr_sdr`` 按 ``negotiated`` 决定）：

    - **协商过（``DuplicateOutput1`` 成功）还拿到 BGRA8**：这是桌面模式级的结论
      ——"这个模式给不出 HDR 面"，短期重试不会变，退避用
      ``_FORMAT_VERDICT_TTL_S``；
    - **没协商上（``DuplicateOutput1`` 被refused，走的是经典接口）**：经典接口的
      稳态帧本来就是 BGRA8（文档口径 + 本机实测），所以拿不到 FP16 说明的是
      "协商通道这次不给用"——而它在本机**反复无常**（同一输出时好时坏），必须按
      抖动处理，退避用 ``_NEGATIVE_TTL_S``。
    """

    def __init__(self, message: str, *, negotiated: bool = False) -> None:
        super().__init__(message)
        self.negotiated = negotiated


# ------------------------------------------------------------------ DLL 装载

_LIB_LOCK = threading.Lock()
_LIBS: dict[str, object] = {}


def _platform_ok() -> bool:
    """本模块只在 Windows 上有实现；其余平台的一切入口都退化成"没有 HDR 通道"。"""
    return sys.platform == "win32"


def _libs() -> dict[str, object]:
    """惰性装载 user32/dxgi/d3d11 并声明函数原型（只读调用，不改任何系统状态）。"""
    with _LIB_LOCK:
        if _LIBS:
            return _LIBS
        user32 = ctypes.WinDLL("user32", use_last_error=False)
        user32.GetDisplayConfigBufferSizes.argtypes = [ctypes.c_uint32,
                                                      ctypes.POINTER(ctypes.c_uint32),
                                                      ctypes.POINTER(ctypes.c_uint32)]
        user32.GetDisplayConfigBufferSizes.restype = ctypes.c_long
        user32.QueryDisplayConfig.argtypes = [
            ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(_PATH_INFO),
            ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(_MODE_INFO), ctypes.c_void_p]
        user32.QueryDisplayConfig.restype = ctypes.c_long
        user32.DisplayConfigGetDeviceInfo.argtypes = [ctypes.c_void_p]
        user32.DisplayConfigGetDeviceInfo.restype = ctypes.c_long
        user32.MonitorFromPoint.argtypes = [wintypes.POINT, ctypes.c_uint32]
        user32.MonitorFromPoint.restype = ctypes.c_void_p
        user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MONITORINFOEXW)]
        user32.GetMonitorInfoW.restype = wintypes.BOOL

        dxgi = ctypes.WinDLL("dxgi", use_last_error=False)
        dxgi.CreateDXGIFactory1.argtypes = [ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
        dxgi.CreateDXGIFactory1.restype = ctypes.c_long

        d3d11 = ctypes.WinDLL("d3d11", use_last_error=False)
        d3d11.D3D11CreateDevice.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        d3d11.D3D11CreateDevice.restype = ctypes.c_long

        _LIBS.update({"user32": user32, "dxgi": dxgi, "d3d11": d3d11})
        return _LIBS


def _ptr_value(ptr) -> int:
    """各种"指针"写法（int / ``c_void_p`` / 空）统一成整数地址。"""
    if ptr is None or isinstance(ptr, int):
        return ptr or 0
    value = getattr(ptr, "value", ptr)
    if isinstance(value, bytes):                 # c_char_p 之类的字节串写法
        return int.from_bytes(value, "little")
    return int(value or 0)


def _com_call(ptr, slot: int, restype, argtypes, *args):
    """按虚表槽位调一个 COM 方法（``ptr`` 必须是该槽位所属接口或其派生接口）。

    空指针直接抛 ``_Unavailable``：ctypes 自己会抛 "NULL pointer access"
    那种看不出所以然的错，而这里 99% 是"上层少建了一个对象"。
    """
    address = _ptr_value(ptr)
    if not address:
        raise _Unavailable(f"COM 指针为空（槽位 {slot}）")
    vtable = ctypes.cast(address, ctypes.POINTER(ctypes.c_void_p))[0]
    entry = ctypes.cast(vtable, ctypes.POINTER(ctypes.c_void_p))[slot]
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return proto(entry)(ctypes.c_void_p(address), *args)


def _release(ptr) -> None:
    address = _ptr_value(ptr)
    if address:
        _com_call(address, 2, ctypes.c_ulong, [])   # IUnknown::Release


def _hr(value: int) -> int:
    """HRESULT 归一成 32 位无符号——失败码（如 0x887A0027）在这台机器上会读成负数。"""
    return int(value) & 0xFFFFFFFF


def _succeeded(value: int) -> bool:
    return _hr(value) < 0x80000000


def _dxgi_reason(hr: int) -> str:
    """DXGI 失败码 → 一句话（只翻真有区分度的两个，其余给码）。

    这两个是真实世界最常见的两种失败，日志里能看出区别很值：
    "别的应用正占着这一屏"（DXGI 最多四个复制客户端）与"会话失效"
    （桌面切换/锁屏/权限变化）——两者的处置完全不同。
    """
    known = {
        DXGI_ERROR_ACCESS_LOST: "复制会话失效（桌面切换/锁屏/权限变化）",
        DXGI_ERROR_NOT_CURRENTLY_AVAILABLE: "已有应用占满这一屏的复制会话（DXGI 上限 4）",
    }
    return f"hr={hr:#010x}" + (f"（{known[hr]}）" if hr in known else "")


# ------------------------------------------------------- DisplayConfig（只读）


def _active_paths() -> list[_PATH_INFO]:
    """当前所有活动显示路径（``QDC_ONLY_ACTIVE_PATHS``）。读不到返回空表。"""
    user32 = _libs()["user32"]
    num_paths = ctypes.c_uint32(0)
    num_modes = ctypes.c_uint32(0)
    if user32.GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS,
                                          ctypes.byref(num_paths),
                                          ctypes.byref(num_modes)) != 0:
        return []
    if not num_paths.value:
        return []
    paths = (_PATH_INFO * num_paths.value)()
    modes = (_MODE_INFO * max(1, num_modes.value))()
    rc = user32.QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS, ctypes.byref(num_paths),
                                   paths, ctypes.byref(num_modes), modes, None)
    if rc != 0:
        return []
    return list(paths[:num_paths.value])


def _device_info(request, adapter_luid, target_id) -> bool:
    user32 = _libs()["user32"]
    request.header.adapterId = adapter_luid
    request.header.id = target_id
    request.header.size = ctypes.sizeof(type(request))
    return user32.DisplayConfigGetDeviceInfo(ctypes.byref(request)) == 0


def _source_device_name(path) -> str:
    """该路径对应的 GDI 设备名（``\\\\.\\DISPLAY1``）。取不到返回 ""。

    这个查询打在 **source**（显卡侧 device，即 ``\\\\.\\DISPLAY1``），不是 target
    （显示器侧；target 问出来的是型号友好名，见 ``_target_friendly_name``）。
    """
    request = _SOURCE_DEVICE_NAME()
    request.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME
    if not _device_info(request, path.sourceInfo.adapterId, path.sourceInfo.id):
        return ""
    return _wstr(request.viewGdiDeviceName)


def _target_friendly_name(path) -> str:
    """该路径对应显示器的友好名（本机 Qt 的 ``QScreen.name()`` 返回的就是它）。"""
    request = _TARGET_DEVICE_NAME()
    request.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME
    if not _device_info(request, path.targetInfo.adapterId, path.targetInfo.id):
        return ""
    return _wstr(request.monitorFriendlyDeviceName)


def _advanced_color(path) -> _ADVANCED_COLOR_INFO | None:
    request = _ADVANCED_COLOR_INFO()
    request.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO
    if not _device_info(request, path.targetInfo.adapterId, path.targetInfo.id):
        return None
    return request


def _sdr_white_raw(path) -> int | None:
    request = _SDR_WHITE_LEVEL()
    request.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_SDR_WHITE_LEVEL
    if not _device_info(request, path.targetInfo.adapterId, path.targetInfo.id):
        return None
    return request.SDRWhiteLevel


def _gdi_name_for_point(x: int, y: int) -> str:
    """屏幕上某点所在显示器的 GDI 设备名（``\\\\.\\DISPLAY1``）；失败返回 ""。"""
    user32 = _libs()["user32"]
    handle = user32.MonitorFromPoint(wintypes.POINT(int(x), int(y)), MONITOR_DEFAULTTONEAREST)
    if not handle:
        return ""
    info = _MONITORINFOEXW()
    info.cbSize = ctypes.sizeof(_MONITORINFOEXW)
    if not user32.GetMonitorInfoW(handle, ctypes.byref(info)):
        return ""
    return _wstr(info.szDevice)


def _gdi_name_for_screen(screen) -> str:
    """QScreen → GDI 设备名：按**物理矩形**匹配显示器枚举，不靠约定猜坐标。

    盲审 M5：Qt6 在 Windows 混合 DPI 下的 ``QScreen.geometry()`` 换算口径
    （原点是否也乘 DPR）随版本/平台插件有分歧，单靠"中心点×dpr"会把副屏
    定位到另一块屏上。这里两条约定各算一个候选物理矩形（A=原点也乘 dpr；
    B=原点原生、只乘宽高），跟 ``EnumDisplayMonitors`` 拿到的真实显示器矩形
    （±2px）对得上哪个用哪个；都对不上返回 ""（调用方按失败回退 GDI——
    抓错屏比抓不到更糟，绝不蒙一个）。
    """
    geometry = screen.geometry()
    dpr = float(screen.devicePixelRatio() or 1.0)
    candidates = [
        (round(geometry.x() * dpr), round(geometry.y() * dpr),
         round(geometry.width() * dpr), round(geometry.height() * dpr)),
        (geometry.x(), geometry.y(),
         round(geometry.width() * dpr), round(geometry.height() * dpr)),
    ]
    for name, left, top, right, bottom in _enumerate_monitors():
        for cx, cy, cw, ch in candidates:
            if (abs(left - cx) <= 2 and abs(top - cy) <= 2
                    and abs(right - (cx + cw)) <= 2 and abs(bottom - (cy + ch)) <= 2):
                return name
    return ""


def _enumerate_monitors() -> list[tuple[str, int, int, int, int]]:
    """（设备名, 原生矩形 left/top/right/bottom）列表；失败返回空列表。"""
    user32 = _libs()["user32"]
    found: list[tuple[str, int, int, int, int]] = []
    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def _cb(handle, _hdc, rect_ptr, _lparam):
        info = _MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(_MONITORINFOEXW)
        if user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            rect = info.rcMonitor
            found.append((_wstr(info.szDevice), rect.left, rect.top, rect.right, rect.bottom))
        return True

    callback = MONITORENUMPROC(_cb)
    if not user32.EnumDisplayMonitors(None, None, callback, 0):
        return []
    return found


def _screen_name(screen) -> str:
    if isinstance(screen, str):
        return screen.strip()
    if screen is None:
        return ""
    try:
        return str(screen.name() or "").strip()
    except Exception:  # noqa: BLE001 - 替身/异常实现一律当"没有名字"
        return ""


def _gdi_name_for_name(name: str, paths) -> str:
    """名字 → GDI 设备名。

    先按**显示器友好名**匹配（Qt6 的 ``QScreen.name()`` 在本机返回 "H27T22S"
    这种型号名，不是 ``\\\\.\\DISPLAY1``），再按 **GDI 设备名**匹配（Qt5 与手工
    传进来的都是它）。同名命中多个活动输出时返回 ""——多屏同型号无法分辨，
    宁可退回 GDI 抓当前屏（那一屏是对的），也不猜错屏。
    """
    lowered = name.casefold()
    if not lowered:
        return ""
    friendly = [path for path in paths if _target_friendly_name(path).casefold() == lowered]
    if not friendly:
        friendly = [path for path in paths if _source_device_name(path).casefold() == lowered]
    if len(friendly) != 1:
        return ""
    return _source_device_name(friendly[0])


def _primary_gdi_name(paths) -> str:
    """主屏的 GDI 设备名：先拿桌面原点问 ``MonitorFromPoint``（这才是"主屏"的定义），
    拿不到才退回第一条活动路径——不靠路径顺序猜主屏。"""
    name = _gdi_name_for_point(0, 0)
    if name:
        return name
    return _source_device_name(paths[0]) if paths else ""


def _resolve_gdi_name(screen, paths) -> str:
    """调用方给的 screen（QScreen / 名字 / None）→ 唯一的 GDI 设备名。

    QScreen 走物理位置（多屏同型号也唯一），失败再退回它的名字；名字命中不了
    就返回 ""——由调用方当"定位不到这块屏"处理。
    """
    if screen is not None and not isinstance(screen, str):
        try:
            name = _gdi_name_for_screen(screen)
        except Exception:  # noqa: BLE001 - 替身/异常实现没有 geometry 之类
            name = ""
        if name:
            return name
        return _gdi_name_for_name(_screen_name(screen), paths)
    if isinstance(screen, str) and screen.strip():
        return _gdi_name_for_name(screen, paths)
    return _primary_gdi_name(paths)


def _path_for_gdi_name(gdi_name: str, paths):
    if not gdi_name:
        return None
    for path in paths:
        if _source_device_name(path).casefold() == gdi_name.casefold():
            return path
    return None


# ------------------------------------------------------------ FP16 → sRGB 8bit

#: 查表缓存：键 = 归一系数（本机 3.5），值 = 65536 项 ``uint8`` 表。
#: 表本身按 FP16 的**全部位模式**建，一次 ~4ms；用完缓存（同一块屏的系数不会变）。
_LUT_CACHE: dict[float, np.ndarray] = {}


def _srgb_lut(sdr_white_scale: float) -> np.ndarray:
    """65536 项查表：FP16 位模式 → sRGB 8-bit。

    为什么用表：逐像素走 ``astype(float32) + np.power`` 在 2560×1440 上实测
    **318ms**（多个 44MB 临时数组 + 1100 万次 pow），是整条 HDR 抓帧里最贵的一步；
    改成"建一张 64KB 的表（4ms）+ 一次 gather"后**41ms**，且结果与逐像素算法
    **逐位相同**（``tests/test_screen_capture_hdr_capture.py`` 里有一条对全部
    65536 个位模式的等价性断言守着）。

    表里已经把三件事都做完：除以 SDR 白点、按 SDR 范围钳制、sRGB 编码 + 四舍五入
    量化。NaN（多种位模式）全部落 0，正无穷落 255。
    """
    key = round(float(sdr_white_scale), 9)
    table = _LUT_CACHE.get(key)
    if table is not None:
        return table

    values = np.arange(65536, dtype=np.uint16).view(np.float16).astype(np.float32)
    # 表要覆盖**全部**位模式，其中包含 0x7c01~0x7cff 这些 signaling NaN：它们的
    # 转换会置 IEEE 的 invalid 标志，numpy 于是报 "invalid value encountered"。
    # 那是这一格 0/NaN 的正常分类结果，不是缺陷——表这边就是要让它们落到 0。
    with np.errstate(invalid="ignore"):
        linear = values / np.float32(sdr_white_scale)
        linear = np.where(np.isnan(linear), np.float32(0.0), linear)
        np.clip(linear, 0.0, 1.0, out=linear)
        srgb = np.where(linear <= 0.0031308,
                        linear * np.float32(12.92),
                        np.float32(1.055) * np.power(linear, np.float32(1.0 / 2.4))
                        - np.float32(0.055))
        table = np.clip(srgb * 255.0 + 0.5, 0.0, 255.0).astype(np.uint8)
    if len(_LUT_CACHE) >= 4:      # 一块屏一个系数，正常只会有一条
        _LUT_CACHE.clear()
    _LUT_CACHE[key] = table
    return table


def fp16_scrgb_to_srgb8(buffer, width: int, height: int, row_pitch: int,
                        sdr_white_scale: float) -> np.ndarray:
    """FP16 scRGB 帧 → sRGB 8-bit RGB 数组 ``(height, width, 3)``（纯函数，不碰 Qt）。

    - ``buffer``：按 ``row_pitch`` 行距排布的 R16G16B16A16_FLOAT 原始字节
      （Desktop Duplication 的映射缓冲；行尾可能有填充字节，要按行距切）；
    - ``sdr_white_scale``：本机 SDR 白点（``SDRWhiteLevel / 1000``，scRGB 单位）。
      除以它让 SDR 白落回 1.0；它同时也是"超过就削掉"的上限；
    - 越界值：负与 NaN 归 0（scRGB 里的域外值），正无穷与超白点归 1（截 255）。

    实现走 ``_srgb_lut`` 查表（口径与逐像素算法逐位相同，理由见它的 docstring）。
    参数不合法就抛 ``ValueError``（调用方 ``grab_hdr_sdr`` 会折成 ``None``）。
    """
    if width <= 0 or height <= 0:
        raise ValueError(f"尺寸必须为正：{width}x{height}")
    if not (row_pitch >= width * 8):
        raise ValueError(f"行距 {row_pitch} 装不下 {width} 个 RGBA FP16 像素")
    if not np.isfinite(sdr_white_scale) or sdr_white_scale <= 0:
        raise ValueError(f"SDR 白点系数必须为正的有限数：{sdr_white_scale!r}")

    table = _srgb_lut(sdr_white_scale)
    raw = np.frombuffer(buffer, dtype=np.uint8, count=row_pitch * height)
    # 行距带填充时要先按行距切、再压实（无填充时 ascontiguousarray 不复制）；
    # 之后才能按 uint16 视角看（view 要求最后一维连续）。
    rows = np.ascontiguousarray(raw.reshape(height, row_pitch)[:, : width * 8])
    bits = rows.view(np.uint16).reshape(height, width, 4)
    return table[bits[:, :, :3]]


def _qimage_rgb888(rgb: np.ndarray) -> QImage:
    """``(h, w, 3)`` uint8 → ``Format_RGB888`` 的 QImage（dpr=1.0，标 sRGB）。

    直接写进 QImage 自己的缓冲区：``Format_RGB888`` 每行按 4 字节对齐，
    ``bytesPerLine()`` 之外的填充不动。
    """
    height, width, _ = rgb.shape
    image = QImage(width, height, QImage.Format.Format_RGB888)
    if image.isNull():
        raise _Unavailable("QImage 分配失败")
    stride = image.bytesPerLine()
    rows = np.frombuffer(image.bits(), dtype=np.uint8).reshape(height, stride)
    rows[:, : width * 3] = rgb.reshape(height, width * 3)
    image.setColorSpace(QColorSpace(QColorSpace.NamedColorSpace.SRgb))
    image.setDevicePixelRatio(1.0)
    return image


# ------------------------------------------------------------- DXGI 复制会话


@dataclass
class _Session:
    """一个显示器一份：DXGI 输出 + D3D11 设备 + 复制会话（跨截图缓存复用）。

    不持有 adapter 引用：设备自己拿着它（COM 引用计数），我们多留一份只会让
    释放顺序更容易出错。
    """

    key: str
    output: int
    device: int
    context: int
    duplication: int = 0
    width: int = 0
    height: int = 0
    color_space: int = -1
    rotation: int = -1
    #: 复制会话是不是走 ``DuplicateOutput1``（格式协商）开的。决定"帧不是 FP16"
    #: 这件事的退避长短：协商过还要不到 FP16 是桌面模式级判定；没协商上就只是
    #: 这台机器当前拒绝了协商通道——本机实测它会反复无常，得按抖动处理。
    negotiated: bool = False

    def release(self) -> None:
        """放掉全部 COM 引用（先放派生的、后放被依赖的），字段清零防二次释放。"""
        for name in ("duplication", "context", "device", "output"):
            ptr = getattr(self, name)
            setattr(self, name, 0)
            if ptr:
                _release(ptr)

    def device_alive(self) -> bool:
        """设备没掉（驱动重置/拔线后 ``GetDeviceRemovedReason`` 会失败）。"""
        try:
            reason = _com_call(self.device, 39, ctypes.c_long, [])   # GetDeviceRemovedReason
        except Exception:  # noqa: BLE001
            return False
        return _succeeded(reason)

    def output_meta_ok(self) -> bool:
        """输出的色彩空间/旋转/尺寸与建会话时一致（用户中途关了 HDR 或改了
        分辨率就重建——尺寸不符不比对的话会抓到错尺寸的帧，盲审 m8）。"""
        try:
            desc = _DXGI_OUTPUT_DESC1()
            if not _succeeded(_com_call(self.output, 27, ctypes.c_long,
                                        [ctypes.POINTER(_DXGI_OUTPUT_DESC1)],
                                        ctypes.byref(desc))):
                return False
        except Exception:  # noqa: BLE001
            return False
        coords = desc.DesktopCoordinates
        width_match = abs((coords.right - coords.left) - self.width) <= 1
        height_match = abs((coords.bottom - coords.top) - self.height) <= 1
        return (desc.ColorSpace == self.color_space and desc.Rotation == self.rotation
                and width_match and height_match)

    def is_reusable(self) -> bool:
        return self.device_alive() and self.output_meta_ok()

    # ---- 抓帧 ----

    def _restart_duplication(self) -> bool:
        """丢掉复制会话重开一个（保留设备：建设备才是贵的那一步）。

        静态桌面上不会再有"下一帧"交给一个用过的会话——``AcquireNextFrame``
        只会一直超时（本机实测：第一次抓帧成功后立刻零等待重抓 ⇒
        ``DXGI_ERROR_WAIT_TIMEOUT``）。重开的会话第一帧就是当前桌面图（只是
        未初始化时可能是全零，见 ``grab_srgb8``），代价又只是开复制会话本身。
        """
        if self.duplication:
            _release(self.duplication)
            self.duplication = 0
        self.duplication, self.negotiated = _duplicate_output(self.device, self.output)
        return bool(self.duplication)

    def _acquire(self, timeout_ms: int, poll_ms: int):
        """等到一帧桌面图并锁进 staging 纹理；返回映射信息（调用方负责 Unmap）。"""
        deadline = time.monotonic() + timeout_ms / 1000.0
        resource = None
        while True:
            info = _OUTDUPL_FRAME_INFO()
            handle = ctypes.c_void_p()
            hr = _com_call(self.duplication, 8, ctypes.c_long,
                           [ctypes.c_uint32, ctypes.POINTER(_OUTDUPL_FRAME_INFO),
                            ctypes.POINTER(ctypes.c_void_p)],
                           poll_ms, ctypes.byref(info), ctypes.byref(handle))
            if _succeeded(hr):
                # 判据是**资源指针**，不是 LastPresentTime：本机实测刚开的复制
                # 会话第一帧就是当前桌面图，但 LastPresentTime 报 0（"桌面自上次
                # 抓取后没变过"），按参考实现那条 `LastPresentTime != 0` 的规则
                # 会把手上这一帧丢掉、然后一路超时。只有资源为空（纯指针更新的
                # 那种帧）才真的"这一帧没有桌面图"。
                if handle.value:
                    resource = handle.value
                    break
                _com_call(self.duplication, 14, ctypes.c_long, [])   # ReleaseFrame
            elif _hr(hr) != DXGI_ERROR_WAIT_TIMEOUT:
                raise _Unavailable(f"AcquireNextFrame {_dxgi_reason(_hr(hr))}")
            if timeout_ms <= 0 or time.monotonic() >= deadline:
                return None

        staging = ctypes.c_void_p()
        texture = ctypes.c_void_p()
        try:
            if not _succeeded(_com_call(resource, 0, ctypes.c_long,
                                        [ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)],
                                        ctypes.byref(IID_ID3D11TEXTURE2D),
                                        ctypes.byref(texture))):
                raise _Unavailable("帧资源不是 ID3D11Texture2D")
            desc = _TEXTURE2D_DESC()
            _com_call(texture, 10, None, [ctypes.POINTER(_TEXTURE2D_DESC)],
                      ctypes.byref(desc))                        # ID3D11Texture2D::GetDesc
            if desc.Format != DXGI_FORMAT_R16G16B16A16_FLOAT:
                # 协商过还给 BGRA8 = 这个桌面模式给不出 FP16，判定级退避（长）
                raise _FormatMismatch(
                    f"帧不是 FP16 (format={desc.Format} negotiated={self.negotiated})",
                    negotiated=self.negotiated)
            if desc.Width != self.width or desc.Height != self.height:
                # 尺寸不符是一过性事件（多半是用户刚改了分辨率），走抖动级退避
                # （60s）——别跟"给不出 FP16"共用 900s 判定退避（盲审 m8）
                raise _Unavailable(
                    f"帧尺寸不符（{desc.Width}x{desc.Height} != "
                    f"{self.width}x{self.height}，多半是分辨率刚变）")
            staging_desc = _TEXTURE2D_DESC()
            for field, value in (("Width", desc.Width), ("Height", desc.Height),
                                 ("MipLevels", 1), ("ArraySize", 1),
                                 ("Format", desc.Format),
                                 ("SampleCount", 1), ("SampleQuality", 0),
                                 ("Usage", D3D11_USAGE_STAGING), ("BindFlags", 0),
                                 ("CPUAccessFlags", D3D11_CPU_ACCESS_READ),
                                 ("MiscFlags", 0)):
                setattr(staging_desc, field, value)
            hr = _com_call(self.device, 5, ctypes.c_long,
                           [ctypes.POINTER(_TEXTURE2D_DESC), ctypes.c_void_p,
                            ctypes.POINTER(ctypes.c_void_p)],
                           ctypes.byref(staging_desc), None,
                           ctypes.byref(staging))              # CreateTexture2D
            if not _succeeded(hr):
                raise _Unavailable(f"CreateTexture2D hr={_hr(hr):#010x}")
            _com_call(self.context, 47, None,
                      [ctypes.c_void_p, ctypes.c_void_p],
                      staging, texture)                   # CopyResource
            # CopyResource 返回 void，没有 HRESULT 可查：它的失败只会以"设备丢了"
            # 的形式体现出来，所以拷完问一句设备还在不在（Map 也会以失败兜住）。
            if not self.device_alive():
                raise _Unavailable("CopyResource 后设备已丢失")
        finally:
            # 桌面图一拷进 staging 就立刻放掉，读回期间不再占着 DWM 的帧
            _release(texture)
            _release(resource)
            _com_call(self.duplication, 14, ctypes.c_long, [])     # ReleaseFrame

        mapped = _MAPPED_SUBRESOURCE()
        hr = _com_call(self.context, 14, ctypes.c_long,
                       [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
                        ctypes.POINTER(_MAPPED_SUBRESOURCE)],
                       staging, 0, D3D11_MAP_READ, 0, ctypes.byref(mapped))
        if not _succeeded(hr) or not mapped.pData:
            _release(staging)
            raise _Unavailable(f"Map hr={_hr(hr):#010x}")
        return staging, mapped

    def _grab_rgb8(self, timeout_ms: int, poll_ms: int,
                   sdr_white_scale: float) -> np.ndarray | None:
        """抓一帧（含读回与换算）；超时返回 ``None``，格式不对抛 ``_FormatMismatch``。"""
        acquired = self._acquire(timeout_ms, poll_ms)
        if acquired is None:
            return None
        staging, mapped = acquired
        try:
            view = (ctypes.c_ubyte * (mapped.RowPitch * (self.height - 1)
                                      + self.width * 8)).from_address(mapped.pData)
            return fp16_scrgb_to_srgb8(view, self.width, self.height,
                                       mapped.RowPitch, sdr_white_scale)
        finally:
            _com_call(self.context, 15, None, [ctypes.c_void_p, ctypes.c_uint32],
                      staging, 0)                              # Unmap
            _release(staging)

    def grab_srgb8(self, sdr_white_scale: float) -> np.ndarray | None:
        """抓一帧并换算成 sRGB 8-bit 数组；拿不到返回 ``None``。

        复制会话**用完即放**（finally 里 release）：DXGI 对每块屏的复制客户端有
        上限（本机实测文案里的"上限 4"是系统级名额，OBS/远程桌面/录屏软件共用），
        常驻桌宠不许一直占着（盲审 M3）。缓存里只留设备/上下文/输出——建设备才是
        贵的那一步；重开复制会话实测只要 ~2.3ms（见 ``_restart_duplication``）。
        """
        try:
            # 静态桌面上"用过的会话"只交付超时（AcquireNextFrame 一直等不到新帧），
            # 所以每次抓帧都重开复制会话：重开的第一帧就是当前桌面图。
            if not self._restart_duplication():
                return None
            rgb = self._grab_rgb8(FRAME_WAIT_MS, FRAME_POLL_MS, sdr_white_scale)
            if rgb is None:
                return None
            if not rgb.any():
                # 全零帧不是桌面内容：本机实测刚开的复制会话会先给一张**未初始化的
                # 全零 FP16 面**（desc 报 FP16、内容全 0）。再等一帧；还是全零就返回
                # ``None`` 交给 GDI——真·全黑桌面在 GDI 那边同样全黑，不会因此丢内容。
                log.info("HDR 抓帧：拿到全零帧（未初始化的 FP16 面），再等一帧")
                rgb = self._grab_rgb8(FRAME_WAIT_MS, FRAME_POLL_MS, sdr_white_scale)
                if rgb is None or not rgb.any():
                    return None
            return rgb
        finally:
            if self.duplication:
                _release(self.duplication)
                self.duplication = 0


def _duplicate_output(device: int, output: int) -> tuple[int, bool]:
    """开一个复制会话：先试 ``IDXGIOutput5::DuplicateOutput1``，再退回经典
    ``IDXGIOutput1::DuplicateOutput``。返回 ``(复制会话指针, 是否走了格式协商)``，
    都失败时返回 ``(0, False)``。

    格式表按文档要求**必须含 B8G8R8A8_UNORM**，FP16 放第一位表示优先要它。
    但**真正决定"这一帧能不能按 scRGB 解释"的是抓到的帧本身**——``_acquire``
    会检查它是不是 FP16，不是就回退 GDI。这层检查之所以必需，是本机实测的结论：

    - 经典 ``DuplicateOutput`` 的稳态帧是 **BGRA8**（与文档一致："用原始
      DuplicateOutput 总是把表面转成 32 位 BGRA"）；它唯一给出的 FP16 帧是**刚
      开会话那一张未初始化的全零面**（`desc` 报 FP16、内容全 0）——正是
      ``grab_srgb8`` 里那条全零重试要挡掉的东西；
    - ``DuplicateOutput1``（文档指定的 HDR/高色深通道）成功时给的才是**带真实
      桌面内容的 FP16 帧**；
    - 而 ``DuplicateOutput1`` 在这台机器上**反复无常**：同一输出、同一设备，
      实测有整段时间对任何格式表都返回 ``DXGI_ERROR_UNSUPPORTED``（0x887A0004；
      同一槽位传空设备指针返回 E_INVALIDARG，可确认槽位没记错），过一阵又全部
      成功。所以失败不能当成"这台机器永远不行"——退避要短（见
      ``_FormatMismatch`` 与 ``session.negotiated``）。
    """
    formats = (ctypes.c_uint32 * 2)(DXGI_FORMAT_R16G16B16A16_FLOAT,
                                    DXGI_FORMAT_B8G8R8A8_UNORM)
    duplication = ctypes.c_void_p()
    hr = _com_call(output, 26, ctypes.c_long,
                   [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                    ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_void_p)],
                   device, 0, 2, formats, ctypes.byref(duplication))
    if _succeeded(hr) and duplication.value:
        return duplication.value, True
    duplication = ctypes.c_void_p()
    hr = _com_call(output, 22, ctypes.c_long,
                   [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
                   device, ctypes.byref(duplication))
    if not _succeeded(hr) or not duplication.value:
        return 0, False
    return duplication.value, False


def _create_device(adapter: int):
    """在该适配器上建 D3D11 设备（复制会话必须与输出同适配器）。"""
    d3d11 = _libs()["d3d11"]
    last_hr = 0
    for flags in (D3D11_CREATE_DEVICE_BGRA_SUPPORT, 0):
        device = ctypes.c_void_p()
        context = ctypes.c_void_p()
        last_hr = d3d11.D3D11CreateDevice(
            ctypes.c_void_p(adapter), D3D_DRIVER_TYPE_UNKNOWN, None, flags,
            None, 0, D3D11_SDK_VERSION,
            ctypes.byref(device), None, ctypes.byref(context))
        if _succeeded(last_hr) and device.value and context.value:
            return device.value, context.value
        _release(device.value)
        _release(context.value)
    raise _Unavailable(f"D3D11CreateDevice hr={_hr(last_hr):#010x}")


def _create_session(gdi_name: str) -> _Session | None:
    """给指定显示器建一份 DXGI+D3D11 会话（找不到输出/不是 HDR/PQ 就返回 None）。"""
    dxgi = _libs()["dxgi"]
    factory = ctypes.c_void_p()
    if not _succeeded(dxgi.CreateDXGIFactory1(ctypes.byref(IID_IDXGIFACTORY1),
                                              ctypes.byref(factory))):
        return None
    try:
        for adapter_index in range(16):
            adapter = ctypes.c_void_p()
            hr = _com_call(factory.value, 12, ctypes.c_long,
                           [ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)],
                           adapter_index, ctypes.byref(adapter))     # EnumAdapters1
            if not _succeeded(hr) or not adapter.value:
                break                       # DXGI_ERROR_NOT_FOUND = 适配器枚举到头
            session = None
            try:
                session = _scan_adapter_outputs(adapter.value, gdi_name)
            finally:
                _release(adapter.value)      # 会话不持有适配器引用（设备自己拿着它）
            if session is not None:
                return session
    finally:
        _release(factory.value)
    return None


def _scan_adapter_outputs(adapter: int, gdi_name: str) -> _Session | None:
    for output_index in range(16):
        output = ctypes.c_void_p()
        hr = _com_call(adapter, 7, ctypes.c_long,
                       [ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)],
                       output_index, ctypes.byref(output))           # EnumOutputs
        if not _succeeded(hr) or not output.value:
            return None                      # 这块适配器上没有更多输出了
        session = None
        try:
            session = _session_from_output(adapter, output.value, gdi_name)
        finally:
            _release(output.value)           # 会话自己另 QI 了 IDXGIOutput6，这份引用照放
        if session is not None:
            return session
    return None


def _session_from_output(adapter: int, output: int, gdi_name: str) -> _Session | None:
    """这个输出是不是要找的显示器、且真的在 HDR(PQ) 模式？是就建会话。

    只放本函数自己新建的引用（``output6`` 与建失败时的设备/上下文）；调用方给的
    ``output`` 引用归调用方释放——两边都放就是双重释放。``adapter`` 只是建设备时
    要用的入参，不存进会话。
    """
    desc = _DXGI_OUTPUT_DESC()
    hr = _com_call(output, 7, ctypes.c_long, [ctypes.POINTER(_DXGI_OUTPUT_DESC)],
                   ctypes.byref(desc))                          # IDXGIOutput::GetDesc
    if not _succeeded(hr) or _wstr(desc.DeviceName).casefold() != gdi_name.casefold():
        return None
    output6 = ctypes.c_void_p()
    if not _succeeded(_com_call(output, 0, ctypes.c_long,
                                [ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)],
                                ctypes.byref(IID_IDXGIOUTPUT6), ctypes.byref(output6))):
        return None
    if not output6.value:
        return None
    desc1 = _DXGI_OUTPUT_DESC1()
    if not _succeeded(_com_call(output6.value, 27, ctypes.c_long,
                                [ctypes.POINTER(_DXGI_OUTPUT_DESC1)], ctypes.byref(desc1))):
        _release(output6.value)
        return None
    # 只有 HDR10/PQ 的输出，FP16 才是"scRGB + 本机 SDR 白点"这套解释成立的场景；
    # 旋转过的输出 Desktop Duplication 给的是未旋转的图，也回退 GDI。
    if (desc1.ColorSpace != DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020
            or desc1.Rotation not in (DXGI_MODE_ROTATION_IDENTITY,
                                      DXGI_MODE_ROTATION_UNSPECIFIED)):
        _release(output6.value)
        return None
    session = _Session(
        key=gdi_name,
        output=output6.value,
        device=0,
        context=0,
        width=desc1.DesktopCoordinates.right - desc1.DesktopCoordinates.left,
        height=desc1.DesktopCoordinates.bottom - desc1.DesktopCoordinates.top,
        color_space=desc1.ColorSpace,
        rotation=desc1.Rotation,
    )
    try:
        session.device, session.context = _create_device(adapter)
        session.duplication, session.negotiated = _duplicate_output(session.device,
                                                                    session.output)
        if not session.duplication:
            raise _Unavailable("开桌面复制会话失败（DuplicateOutput1 与经典 DuplicateOutput 都没成）")
    except BaseException:
        # 建失败就把这一路新建的引用全放掉（含 output6），再往上抛；
        # 调用方给的 output 引用不在这里放。
        session.release()
        raise
    return session


# ------------------------------------------------------------- 会话缓存（按屏）


_SESSIONS: dict[str, _Session] = {}
_FAILED_UNTIL: dict[str, float] = {}
_SESSION_LOCK = threading.RLock()

#: 缓存会话（D3D11 设备 + 输出句柄 ≈ 几十 MB GPU 侧资源）的闲置回收窗口：
#: 最后一次抓帧后这么久没再用，就整组还回系统（识屏不是高频常驻功能，
#: 用完一直占着就是白占——实机内存剖析 2026-10-14）。
_IDLE_RELEASE_S = 30.0
_idle_timer: threading.Timer | None = None


def _arm_idle_release() -> None:
    """每次抓帧后重新武装闲置回收器（并发安全：持 ``_SESSION_LOCK`` 调用）。"""
    global _idle_timer
    if _idle_timer is not None:
        _idle_timer.cancel()
    _idle_timer = threading.Timer(_IDLE_RELEASE_S, release_cached_sessions)
    _idle_timer.daemon = True
    _idle_timer.start()


def _drop_session(key: str) -> None:
    session = _SESSIONS.pop(key, None)
    if session is not None:
        session.release()


def _session_for(screen, paths) -> _Session | None:
    """取该显示器可用的会话（缓存复用；失败退避；调用方须持 ``_SESSION_LOCK``）。"""
    gdi_name = _resolve_gdi_name(screen, paths)
    if not gdi_name:
        return None
    if time.monotonic() < _FAILED_UNTIL.get(gdi_name, 0.0):
        return None
    session = _SESSIONS.get(gdi_name)
    if session is not None:
        if session.is_reusable():
            return session
        _drop_session(gdi_name)      # 元数据变了/设备掉了：整套重建
    try:
        session = _create_session(gdi_name)
    except Exception as exc:  # noqa: BLE001 - 建会话的任何失败都算"这条通道这次用不了"
        log.warning("HDR 抓帧通道不可用，回退 GDI：%s", exc)
        _FAILED_UNTIL[gdi_name] = time.monotonic() + _NEGATIVE_TTL_S
        return None
    if session is None:
        _FAILED_UNTIL[gdi_name] = time.monotonic() + _NEGATIVE_TTL_S
        return None
    _SESSIONS[gdi_name] = session
    return session


def release_cached_sessions() -> None:
    """放掉全部缓存会话与退避表（闲置回收器/进程退出/测试用）。

    会话里握着 D3D11 设备与 DXGI 复制会话：进程退出后由系统回收，所以正常路径
    不需要调它；闲置回收（``_arm_idle_release``，最后一次抓帧 30s 后）与
    "这片屏不抓了、想把 GPU 资源还回去"的场景需要一个显式出口。
    """
    with _SESSION_LOCK:
        for key in list(_SESSIONS):
            _drop_session(key)
        _FAILED_UNTIL.clear()


def hdr_capture_disabled() -> bool:
    """``DSH_PET_DISABLE_HDR_CAPTURE`` 为真值时跳过整条 HDR 通道。

    每次调用都读环境变量（不缓存）：逃生门的意义就是"不用改代码、重启也不用"。
    空值 / ``0`` / ``false`` / ``no`` / ``off`` 视为没开;其余任何值都算开。
    """
    value = os.environ.get(DISABLE_ENV, "").strip().casefold()
    return value not in ("", "0", "false", "no", "off")


def _read_hdr_state(screen, paths) -> tuple[bool, float]:
    """``(HDR 是否启用, SDR 白点系数)``；读不到/不支持一律 ``(False, 1.0)``。"""
    path = _path_for_gdi_name(_resolve_gdi_name(screen, paths), paths)
    if path is None:
        return False, 1.0
    color = _advanced_color(path)
    if color is None or not (color.value >> 1) & 1:      # advancedColorEnabled
        return False, 1.0
    raw = _sdr_white_raw(path)
    if raw is None or raw <= 0:
        return False, 1.0
    return True, raw / 1000.0


def hdr_state(screen=None) -> tuple[bool, float]:
    """该显示器的 HDR(高级颜色) 状态与 SDR 白点系数。

    ``screen`` 可以是 ``QScreen``（按物理位置定位显示器）、屏幕名字符串（按
    Qt 的 ``QScreen.name()`` 或 ``\\\\.\\DISPLAY1`` 匹配）或 ``None``（主屏）。
    返回值 ``(是否启用 HDR, SDR 白点系数)``——系数是 scRGB 单位下的 SDR 白点
    （``SDRWhiteLevel / 1000``，本机 3500 ⇒ 3.5 ⇒ 280 nits；除以前先不做
    ``/80`` 的 nits 换算）。非 Windows、读不到、显示器不支持、HDR 没开、
    ``SDRWhiteLevel`` 为 0，全部返回 ``(False, 1.0)``，绝不抛。
    """
    if not _platform_ok():
        return False, 1.0
    try:
        return _read_hdr_state(screen, _active_paths())
    except Exception as exc:  # noqa: BLE001 - 判定失败就按"没开 HDR"处理
        log.warning("HDR 状态读取失败（按未启用处理）：%s", exc)
        return False, 1.0


def grab_hdr_sdr(screen_name=None) -> QImage | None:
    """抓一整个显示器并转成 sRGB 8-bit QImage（``dpr=1.0``，裸设备像素）。

    仅在"该显示器 HDR 已启用 + DXGI 输出是 HDR10/PQ"时才走这条路；**任何一步
    失败都返回 ``None``**（HDR 没开、读不到、分辨率/格式不符、驱动拒绝、
    别的应用占着复制会话、QImage 分配失败……），由调用方落回 ``grabWindow``。

    ``screen_name`` 同 ``hdr_state``：``QScreen`` / 名字 / ``None``（主屏）。
    抓的是**单个显示器**（Desktop Duplication 天然按 output），多屏时按名字或
    物理位置定位，定位不到返回 ``None``。

    ``QImage`` 的色彩空间标成 sRGB、``devicePixelRatio`` 为 1.0——与
    ``pixels.grab_screen_image`` 的返回值口径一致。线程安全（模块级锁串行化
    整条抓帧），不碰 Qt 事件循环。
    """
    if not _platform_ok() or hdr_capture_disabled():
        return None
    try:
        with _SESSION_LOCK:
            paths = _active_paths()
            enabled, white = _read_hdr_state(screen_name, paths)
            if not enabled or white <= 0:
                return None
            session = _session_for(screen_name, paths)
            if session is None:
                return None
            backoff = _NEGATIVE_TTL_S
            try:
                rgb = session.grab_srgb8(white)
            except _FormatMismatch as exc:
                # 帧格式不符：协商过才算"桌面模式给不出 HDR 面"的判定级结论；
                # 没协商上说明只是协商通道这次不给用（本机实测它反复无常），
                # 按抖动退避，别让用户被一次 OS 抽风关在 GDI 里 15 分钟。
                log.warning("HDR 抓帧不可用（帧格式不符，回退 GDI）%s：%s",
                            "（已协商）" if exc.negotiated else "（未协商）", exc)
                rgb = None
                backoff = (_FORMAT_VERDICT_TTL_S if exc.negotiated else _NEGATIVE_TTL_S)
            except Exception as exc:  # noqa: BLE001 - 会话内部的任何异常都算这次抓帧失败
                log.warning("HDR 抓帧失败，回退 GDI：%s", exc)
                rgb = None
            if rgb is None:
                # 会话已经不可靠（掉驱动/被抢/超时/格式不符/异常中断在抓帧中途）：
                # 丢掉缓存整套重建，并让这一屏退避一会儿——别让下一张截图
                # 再等一遍同样的超时。
                _drop_session(session.key)
                _FAILED_UNTIL[session.key] = time.monotonic() + backoff
                return None
            _arm_idle_release()   # 用上了才武装：闲置 30s 后设备整组还回系统
            return _qimage_rgb888(rgb)
    except Exception as exc:  # noqa: BLE001 - 抓帧这条通道绝不许把异常抛给调用方
        log.warning("HDR 抓帧失败，回退 GDI：%s", exc)
        return None
