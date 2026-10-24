# -*- coding: utf-8 -*-
"""聊天输入框的粘贴/拖拽图片与文件（2026-10-14 实机反馈后补的两条入口）。

覆盖：
- Ctrl+V 粘贴内存图 → 落成临时 PNG 进附件（图片优先于剪贴板里的占位文本）；
- 粘贴/拖拽本地文件（Explorer 复制的文件）→ 直接进附件；
- 纯文本粘贴 → 走原文本路径，不碰附件；
- 拖拽内存图（浏览器拖图，无本地 URL）→ 同粘贴落盘路；
- 临时文件的归属纪律：只有我们落盘的 paste-*.png 会被清（移除/发送后），
  用户自己的文件一个字节不动。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from PySide6.QtCore import QMimeData, QUrl
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from pet.chat.widgets import (
    AttachmentTextEdit,
    ChatComposer,
    _is_clipboard_cache_file,
    _save_clipboard_image,
)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _solid_image(w: int = 32, h: int = 24) -> QImage:
    image = QImage(w, h, QImage.Format.Format_RGB32)
    image.fill(QColor(20, 120, 200))
    return image


def _image_mime(image: QImage) -> QMimeData:
    mime = QMimeData()
    mime.setImageData(image)
    return mime


class _FakeDropEvent:
    """drag/drop 事件的最小替身（真 QDropEvent 构造不起来，链路只读这几样）。"""

    def __init__(self, mime: QMimeData) -> None:
        self._mime = mime
        self.accepted = False

    def mimeData(self) -> QMimeData:
        return self._mime

    def acceptProposedAction(self) -> None:
        self.accepted = True


def test_pasted_clipboard_image_becomes_an_attachment():
    _app()
    edit = AttachmentTextEdit()
    received = []
    edit.files_dropped.connect(received.extend)

    edit.insertFromMimeData(_image_mime(_solid_image()))

    assert len(received) == 1
    path = Path(received[0])
    assert path.exists() and path.suffix == ".png" and path.name.startswith("paste-")
    assert _is_clipboard_cache_file(path)
    assert QImage(str(path)).size() == _solid_image().size()
    path.unlink(missing_ok=True)


def test_pasted_image_wins_over_placeholder_text():
    _app()
    """截图工具的剪贴板常同时带图和一段占位文本：图优先，文本不粘进去。"""
    edit = AttachmentTextEdit()
    received = []
    edit.files_dropped.connect(received.extend)
    mime = _image_mime(_solid_image())
    mime.setText("image.png")

    edit.insertFromMimeData(mime)

    assert len(received) == 1
    assert edit.toPlainText() == ""
    Path(received[0]).unlink(missing_ok=True)


def test_pasted_local_files_become_attachments(tmp_path):
    _app()
    edit = AttachmentTextEdit()
    received = []
    edit.files_dropped.connect(received.extend)
    target = tmp_path / "notes.txt"
    target.write_text("hello", encoding="utf-8")
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(target))])

    edit.insertFromMimeData(mime)

    # Qt 的 toLocalFile 在 Windows 上给正斜杠：按路径语义比，不比字符串
    assert [Path(p) for p in received] == [target]


def test_plain_text_paste_stays_text():
    _app()
    edit = AttachmentTextEdit()
    received = []
    edit.files_dropped.connect(received.extend)
    mime = QMimeData()
    mime.setText("就是一句话")

    edit.insertFromMimeData(mime)

    assert received == []
    assert edit.toPlainText() == "就是一句话"


def test_dropped_memory_image_without_url_becomes_an_attachment():
    _app()
    """浏览器里直接拖图：没有本地 URL，只有内存图——走同一条落盘路。"""
    edit = AttachmentTextEdit()
    received = []
    edit.files_dropped.connect(received.extend)
    event = _FakeDropEvent(_image_mime(_solid_image()))

    edit.dropEvent(event)

    assert event.accepted is True
    assert len(received) == 1
    Path(received[0]).unlink(missing_ok=True)


def test_composer_clear_removes_only_our_cache_files(tmp_path):
    _app()
    composer = ChatComposer()
    ours = _save_clipboard_image(_solid_image())
    assert ours is not None
    theirs = tmp_path / "user-photo.png"
    theirs.write_bytes(b"\x89PNG")  # 内容不重要，存在即可
    composer.add_attachments([ours, theirs])
    assert len(composer.attachment_paths) == 2

    composer.clear_attachments()

    assert not ours.exists(), "我们落盘的临时图该被收回"
    assert theirs.exists(), "用户自己的文件绝不动"


def test_clipboard_cache_file_guard(tmp_path):
    _app()
    assert _is_clipboard_cache_file(tmp_path / "paste-123.png") is False   # 不在缓存目录
    ours = _save_clipboard_image(_solid_image())
    assert ours is not None
    assert _is_clipboard_cache_file(ours) is True
    ours.unlink(missing_ok=True)


def test_saving_a_null_image_is_a_noop():
    _app()
    assert _save_clipboard_image(QImage()) is None


def test_office_style_text_wins_over_the_rendered_image():
    _app()
    """Office 复制单元格：剪贴板同时带真文本+位图渲染——文本优先（盲审 M4）。

    截图工具的占位文本（裸文件名）才让图赢（见 test_pasted_image_wins_...）。
    """
    edit = AttachmentTextEdit()
    received = []
    edit.files_dropped.connect(received.extend)
    mime = _image_mime(_solid_image())
    mime.setText("第一季度销售额\n环比 +12%")   # Office 式真内容

    edit.insertFromMimeData(mime)

    assert received == [], "真文本被剪贴板里的位图渲染顶替了"
    assert "第一季度销售额" in edit.toPlainText()
