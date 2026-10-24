# -*- coding: utf-8 -*-
"""Windows 本地语音（SAPI5）合成后端：**零网络、零 Key、毫秒级**。

为什么加它（2026-10-12 机主反馈「TTS 合成速度太慢、延迟太高」＋现场排查）：
- edge-tts 当时被微软侧 403（`WSServerHandshakeError 403`）。**2026-10-17 更正**：
  那是 `edge-tts` 7.2.8 自己的版本问题，不是「服务端不可修」——同机同网络 7.3.1 起
  正常出音频，依赖下限已提到 `edge-tts>=7.3.1,<8`（实测见
  `docs/PR-REPORT-TTS-PROVIDERS-2026-10-14.md` §五）；
- MiMo 走公网，秒级延迟；
- 本机实测 SAPI5（Microsoft Huihui Desktop，zh-CN）：**首句 111 ms、同进程第二句 19 ms**，
  写出的就是标准 wav，直接进既有缓存链。

**它到今天仍然值得留着**（不是「edge 修好了就可以删」）：在线后端要网络、要凭据、
冷启动秒级；SAPI5 零网络、零 Key、毫秒级，是断网 / 代理异常 / 服务端抖动时的兜底。

实现要点：
1. **晚绑定**（`CreateObject(..., dynamic=True)`）——本机 `comtypes.client.GetModule('sapi.dll')`
   会报 `TYPE_E_CANTLOADLIBRARY`，别走 typelib 生成那条路；
2. 用 `SAPI.SpFileStream`（`Open(path, 3)` = SSFM_CREATE）把语音直接写成 wav 文件，
   与 `synth(text, plan, out_path)` 契约一致；
3. 音色列表来自系统已装语音，模块导入时取一次并缓存（取不到就给空表，不抛）。
"""

from __future__ import annotations

import threading

from .base import TtsField, TtsProvider, TtsUnavailable, register

PROVIDER_ID = "sapi"
LABEL = "Windows 本地语音（离线，毫秒级）"
AVAILABILITY_CODE = "sapi_missing"
MISSING_MESSAGE = (
    "系统里没有可用的 SAPI5 语音。可在「设置 → 时间和语言 → 语音」里添加中文语音包后重试。"
)
#: SpFileStream.Open 的 SSFM_CREATE（覆写已有文件）
SSFM_CREATE = 3
#: 逐线程的 COM 初始化标记（thread-local；线程退出时随之消失，不需要清理）
_com_ready = threading.local()
#: 音色列表探测结果只取一次（COM 调用很便宜，但没必要每次问系统）
_OPTIONS_CACHE: tuple[tuple[str, str], ...] | None = None


def _client():
    from comtypes import client

    return client


def _ensure_com() -> None:
    """确保**当前线程**进过 COM 初始化。

    COM 是线程级的：GUI 线程里 Qt 早就初始化过，后台线程没有。合成跑在
    ``_TTSWorker`` 线程里，缺这一步时 ``CreateObject`` 直接抛
    ``WinError -2147221008 尚未调用 CoInitialize``——报时 / 试听 / 台词语音
    三条链路在后台合成时全部退化成「本地语音合成失败」。

    调用方要么是常驻 GUI 线程，要么是短命 worker；两者都不该把 ``CoInitialize``
    的引用计数一路加上去，所以这里按线程只做一次（见下方 thread-local 开关）。
    """
    import comtypes

    # 每**线程**只初始化一次：``CoInitialize`` 是引用计数语义，逐次调用会把计数
    # 一直加上去而没人 ``CoUninitialize``（合成的 worker 线程是短命的，GUI 线程
    # 是长寿的——前者随线程退出由系统回收，后者会一路涨）。加一个 thread-local
    # 开关把计数钉在 1，语义仍是"这个线程进过 COM"。
    flag = getattr(_com_ready, "value", None)
    if flag is True:
        return
    comtypes.CoInitialize()
    _com_ready.value = True


def release_thread_com() -> None:
    """本线程若因本模块进过 COM，就配对 ``CoUninitialize`` 释放。

    只在**我们自己的短命线程**（合成 worker）的收尾里调用：那种线程的生命周期由
    本进程掌握，配对释放是干净的。常驻 GUI 线程不调——注意口径：配对释放只撤掉
    **本模块自己加的那一次**计数，不会撤销 Qt 自己持有的另一份初始化；不在 GUI 线程
    释放的理由是那里没有"线程结束"这个收尾点，提前释放反而可能让同一线程上后续的
    COM 调用落进已拆除的 apartment。线程从未初始化过时是 no-op。
    """
    if getattr(_com_ready, "value", None) is not True:
        return
    _com_ready.value = False
    try:
        import comtypes

        comtypes.CoUninitialize()
    except Exception:  # pragma: no cover - 非 Windows / 库缺失
        pass


def _voice_objects():
    """返回 SAPI 的 ``GetVoices()`` 集合（拿不到就返回 None）。

    ``_ensure_com()`` 必须在 try 内：本函数在模块导入期就会被
    ``installed_voices()`` 调到，而 comtypes 只在 Windows 存在——放外面会让
    非 Windows 的导入直接抛 ModuleNotFoundError。
    """
    try:
        _ensure_com()
        voice = _client().CreateObject("SAPI.SpVoice", dynamic=True)
        return voice, voice.GetVoices()
    except Exception:
        return None, None


def installed_voices(force: bool = False) -> tuple[tuple[str, str], ...]:
    """已安装的 SAPI5 音色：``((描述, 描述), ...)``（值＝描述，显示名同）。"""
    global _OPTIONS_CACHE
    if _OPTIONS_CACHE is not None and not force:
        return _OPTIONS_CACHE
    _voice, voices = _voice_objects()
    options: list[tuple[str, str]] = []
    if voices is not None:
        for index in range(voices.Count):
            try:
                description = str(voices.Item(index).GetDescription())
            except Exception:
                continue
            options.append((description, description))
    _OPTIONS_CACHE = tuple(options)
    return _OPTIONS_CACHE


def pick_default_voice(voice, prefer: tuple[str, ...] = ("Chinese", "中文", "Huihui", "Yaoyao", "Kangkang")):
    """挑一个音色：优先中文，其次第一个可用项；一个都没有返回 None。"""
    _voice, voices = _voice_objects()
    if voices is None or voices.Count == 0:
        return None
    fallback = None
    for index in range(voices.Count):
        try:
            item = voices.Item(index)
            description = str(item.GetDescription())
        except Exception:
            continue
        if fallback is None:
            fallback = item
        if any(key in description for key in prefer):
            return item
    return fallback


def find_voice(description: str):
    """按描述找音色（先精确、再子串；描述为空返回 None）。"""
    wanted = str(description or "").strip()
    if not wanted:
        return None
    _voice, voices = _voice_objects()
    if voices is None:
        return None
    partial = None
    for index in range(voices.Count):
        try:
            item = voices.Item(index)
            text = str(item.GetDescription())
        except Exception:
            continue
        if text == wanted:
            return item
        if partial is None and wanted.lower() in text.lower():
            partial = item
    return partial


class SapiTtsProvider(TtsProvider):
    id = PROVIDER_ID
    label = LABEL
    is_fallback = False
    availability_code = AVAILABILITY_CODE
    unavailable_message = MISSING_MESSAGE
    fields = (
        TtsField(
            "voice", "voice_chime_sapi_voice", "音色",
            "系统里已经装好的声音。",
            kind="select", default="",
            # 空值＝自动挑中文音色：把它作为一个明确的选项写出来，别让用户对着
            # 「不在清单里：」这种空值占位猜（设置页只在值非空时才标「不在清单里」）。
            options=(("", "自动挑（推荐）"),) + installed_voices(),
            allow_custom=True,
        ),
        TtsField(
            "rate", "voice_chime_sapi_rate", "语速",
            "−10 最慢到 10 最快，0 默认。",
            kind="number", default=0, minimum=-10, maximum=10,
        ),
    )

    # ------------------------------------------------------------ 合同
    def plan(self, values: dict) -> dict:
        return {
            "provider": self.id,
            "voice": str(values.get("voice") or "").strip(),
            "rate": max(-10, min(10, _as_int(values.get("rate"), 0))),
            "ext": "wav",
        }

    def flavor(self, values: dict) -> str:
        # 前缀 sapi 与 edge/mimo 天然不同命名空间
        return f"sapi|{str(values.get('voice') or '').strip()}|{_as_int(values.get('rate'), 0)}"

    def availability(self, values: dict) -> str:
        return "" if installed_voices() else AVAILABILITY_CODE

    def synth(self, text: str, plan: dict, out_path) -> None:
        """把文本合成到 ``out_path``（标准 wav）。失败一律转 TtsUnavailable。"""
        client = _client()
        try:
            _ensure_com()
            voice = client.CreateObject("SAPI.SpVoice", dynamic=True)
            target = find_voice(str(plan.get("voice") or "")) or pick_default_voice(voice)
            if target is None:
                raise TtsUnavailable(AVAILABILITY_CODE)
            voice.Voice = target
            voice.Rate = max(-10, min(10, _as_int(plan.get("rate"), 0)))
            stream = client.CreateObject("SAPI.SpFileStream", dynamic=True)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            stream.Open(str(out_path), SSFM_CREATE)
            voice.AudioOutputStream = stream
            try:
                voice.Speak(str(text or ""))
            finally:
                stream.Close()
        except TtsUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 —— 统一转成可读错误
            raise TtsUnavailable(AVAILABILITY_CODE, f"本地语音合成失败：{exc}") from exc
        try:
            if not out_path.exists() or out_path.stat().st_size == 0:
                raise TtsUnavailable(AVAILABILITY_CODE, "本地语音合成没有产出音频")
        except OSError as exc:
            raise TtsUnavailable(AVAILABILITY_CODE, f"本地语音产物不可读：{exc}") from exc


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


PROVIDER = register(SapiTtsProvider())
