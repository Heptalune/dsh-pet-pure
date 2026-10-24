# -*- coding: utf-8 -*-
"""截图问答的云端通道：选区图 → 内存 JPEG → 既有 vision/chat 管线。

为什么要有这一层（而不是在编排里直接发请求）：**网络边界要能一眼看全**。
整条链路只有这里碰网络，而且只做三件事——把图缩到 ``pet/vision.py`` 的
``MAX_EDGE``、按既有口径解析聊天配置、调既有的 ``ask_about_screen``。
不新开 HTTP 路径、不自己拼 payload、不碰密钥存储。

隐私（与 ``pet/vision.py`` 同一约定）：图像全程只在内存里，不落盘；发出去的
唯一出口是用户自己配置的聊天/视觉 API，且**发送动作只发生在问答浮窗里用户
亲手点「发送」那一刻**（浮窗本身即确认面：发不发、发什么，用户都看得见）。
"""

from __future__ import annotations

import copy
import logging
from typing import Any

from PySide6.QtCore import QBuffer, QIODevice, Qt
from PySide6.QtGui import QImage

log = logging.getLogger("dsh-pet-standalone")

__all__ = ["SELECTION_PROMPT", "ChatVisionChannel", "VisionConversation",
           "image_to_jpeg_bytes", "resolve_chat_context"]

#: 框选区域的问题文本：默认那条「这是主人当前的屏幕截图，回应一两句」是给
#: **整屏**看的（重在陪伴式反应），对一块被框选的图文不对题，所以问答通道自带
#: 一条点名的问法，其余口径（人设 system prompt、模型/端点解析、重试）全部复用。
SELECTION_PROMPT = (
    "主人从自己的屏幕上框选了一块区域发给你。请看看这块区域里是什么，"
    "用你的人设口吻回答主人：说清你看到的内容，能给出判断或建议时就直接给。"
    "不要罗列画面元素，也不要评价截图这件事本身。"
)


def image_to_jpeg_bytes(image: QImage, *, max_edge: int, quality: int) -> bytes:
    """选区图 → 内存 JPEG bytes（缩到最长边 ``max_edge``）。

    纯 Qt 编码（``QImage.save`` 到 ``QBuffer``）：不必把选区再绕一趟 PIL，
    也就不会在无图片依赖的构建里引入新的导入路径。图是 slice-1 已经统一过的
    8-bit sRGB，这里只做缩放与编码。
    """
    if image is None or image.isNull():
        raise ValueError("选区图为空，没有可发送的画面")
    scaled = image
    longest = max(int(image.width()), int(image.height()))
    if max_edge > 0 and longest > max_edge:
        scaled = image.scaled(
            int(max_edge), int(max_edge),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        if scaled.isNull():
            raise RuntimeError("选区图缩放失败")
    buffer = QBuffer()
    if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
        raise RuntimeError("无法打开内存缓冲用于编码截图")
    try:
        if not scaled.save(buffer, "JPEG", int(quality)):
            raise RuntimeError("截图编码 JPEG 失败")
        return bytes(buffer.data())
    finally:
        buffer.close()


def resolve_chat_context(config) -> tuple[Any, str]:
    """解析聊天/视觉通道上下文：``(provider, system_prompt)``。

    与 ``pet/window.py::_on_look_screen`` 逐点同口径：取当刻聊天设置的活动
    provider 副本、把 API Key 解析进去（拷副本，避免把解析后的密钥写回配置
    里的共享对象）、system prompt 用人设那份。配置面不可用时抛异常——由调用方
    转成给用户看的理由，不在这里静默降级。
    """
    settings = config.chat_settings()
    provider = copy.copy(settings.active_config)
    provider.api_key = config.resolve_api_key(provider)
    return provider, str(settings.default_system_prompt or "")


class ChatVisionChannel:
    """默认的问答通道：把选区喂给既有视觉模型，返回人设口吻的答复。

    ``__call__`` 是**同步阻塞**的（真实实现在内部重试、超时 60s 起）；调用方
    （``ScreenCaptureActions``）负责把它放到后台线程，GUI 线程不许直接调。
    """

    def __init__(self, config, *, prompt: str | None = None) -> None:
        self._config = config
        self._prompt = SELECTION_PROMPT if prompt is None else str(prompt)

    def __call__(self, image: QImage) -> str:
        from .. import vision as vision_mod

        provider, system_prompt = resolve_chat_context(self._config)
        payload = image_to_jpeg_bytes(
            image, max_edge=vision_mod.MAX_EDGE, quality=vision_mod.JPEG_QUALITY,
        )
        app_info = vision_mod.foreground_app_info()
        return vision_mod.ask_about_screen(
            payload, app_info, system_prompt, provider, user_text=self._prompt,
        )


class VisionConversation:
    """截图问答浮窗的多轮会话：首条用户消息带图，后续轮次纯文本，历史自持。

    与一次性通道（``ChatVisionChannel``）的区别只在组包：图只在第一轮出现
    （之后的轮次引用的是同一段历史，模型看得到上下文），system 用人设那份
    （``resolve_chat_context`` 同一口径）。

    ``ask`` 是**同步阻塞**的（超时/重试口径与 ``vision.ask_about_screen`` 相同）；
    调用方（问答浮窗）负责放后台线程。

    隐私不变量：图只在 :meth:`start` 时编码进内存（JPEG bytes），不落盘；
    每次 ``ask`` 发出的内容 = system + 历史 + 本轮文字——调用方（浮窗）把每一次
    发送都呈现在消息列表里，用户看得见自己发出去过什么。
    """

    def __init__(self, config) -> None:
        self._provider, self._system_prompt = resolve_chat_context(config)
        self._jpeg: bytes | None = None
        self._context_text = ""
        self._history: list[dict] = []

    def start(self, image: QImage, context_text: str = "") -> None:
        """开新会话：换图即清空历史（旧历史配新图是语义错乱）。

        ``context_text``：随首轮一起发出的参考资料（现在是 OCR 已识别出的文字——
        图小字糊时它能救场），只进首轮用户消息，不占后续轮次。
        """
        from .. import vision as vision_mod

        self._jpeg = image_to_jpeg_bytes(
            image, max_edge=vision_mod.MAX_EDGE, quality=vision_mod.JPEG_QUALITY,
        )
        self._context_text = str(context_text or "").strip()[:4000]
        self._history = []

    @property
    def ready(self) -> bool:
        return self._jpeg is not None

    def ask(self, text: str) -> str:
        """发一轮：首轮把图一起带上，之后纯文本。返回模型答复（同步阻塞）。"""
        import base64

        from .. import vision as vision_mod

        if self._jpeg is None:
            raise RuntimeError("会话还没有图（先 start）")
        question = str(text or "").strip() or SELECTION_PROMPT
        if not self._history:
            if self._context_text:
                question += f"\n\n（附图的文字识别参考：{self._context_text}）"
            b64 = base64.b64encode(self._jpeg).decode("ascii")
            self._history.append({"role": "user", "content": [
                {"type": "text", "text": question},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ]})
        else:
            self._history.append({"role": "user", "content": question})
        messages = [{"role": "system", "content": self._system_prompt}, *self._history]
        try:
            reply = str(vision_mod.post_vision_messages(messages, self._provider) or "").strip()
        except Exception:
            # 失败要回滚历史：留着这条 user 消息，下一轮就会出现两条连续 user 消息
            # （严格的服务端会直接拒绝这种历史形状）
            self._history.pop()
            raise
        # 空回复不进历史（部分接口不接受空 content 的 assistant 消息）
        if reply:
            self._history.append({"role": "assistant", "content": reply})
        return reply
