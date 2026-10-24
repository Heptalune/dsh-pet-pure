# -*- coding: utf-8 -*-
"""WebM 元数据跨进程文件缓存的回归测试。

多开场景下，每个实例不应各自重复拉起 ffmpeg 探测同一段动画的
帧数/时长；文件缓存（key 含 mtime+size）应让第二个“进程”直接命中。

批 6-8b 修 3（5.6sol 全审 P2）：缓存写入必须单调累积——后写进程带着
旧内存快照写入时，写前重读磁盘合并，绝不覆盖先写进程刚加入的条目。
"""
from __future__ import annotations

import json
import os
import threading
import types

from PySide6.QtWidgets import QApplication

import pet.webm_clip as webm_clip


def test_meta_file_cache_shared_across_instances(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])

    fake = types.SimpleNamespace(count_frames_and_secs=lambda path: (24, 1.0))
    monkeypatch.setattr(webm_clip, "imageio_ffmpeg", fake)
    monkeypatch.setattr(webm_clip, "_META_FILE_CACHE_PATH", tmp_path / "meta.json")
    monkeypatch.setattr(webm_clip, "_META_FILE_CACHE", None)
    monkeypatch.setattr(webm_clip, "_META_CACHE", {})

    video = tmp_path / "a.webm"
    video.write_bytes(b"fake")

    clip1 = webm_clip.WebMClip(video)
    clip1.warm_meta()
    assert clip1.duration() > 0
    assert (tmp_path / "meta.json").exists()

    # 模拟全新进程：清空内存缓存，只依赖文件缓存
    monkeypatch.setattr(webm_clip, "_META_FILE_CACHE", None)
    monkeypatch.setattr(webm_clip, "_META_CACHE", {})
    calls: list[str] = []
    fake.count_frames_and_secs = lambda path: calls.append(str(path)) or (24, 1.0)

    clip2 = webm_clip.WebMClip(video)
    clip2.warm_meta()
    assert calls == []  # 未再次调用 ffmpeg 探测
    assert clip2.duration() > 0

    app.processEvents()


def test_meta_file_cache_merges_stale_snapshot_entries(tmp_path, monkeypatch):
    """批 6-8b 修 3：后写进程带着旧快照写入时不得覆盖先写进程的新条目——
    写前重读磁盘合并（read-modify-write），缓存单调累积。"""
    app = QApplication.instance() or QApplication([])

    fake = types.SimpleNamespace(count_frames_and_secs=lambda path: (24, 1.0))
    monkeypatch.setattr(webm_clip, "imageio_ffmpeg", fake)
    monkeypatch.setattr(webm_clip, "_META_FILE_CACHE_PATH", tmp_path / "meta.json")
    monkeypatch.setattr(webm_clip, "_META_FILE_CACHE", None)
    monkeypatch.setattr(webm_clip, "_META_CACHE", {})

    video_a = tmp_path / "a.webm"
    video_a.write_bytes(b"fake-a")
    video_b = tmp_path / "b.webm"
    video_b.write_bytes(b"fake-b")

    # 进程 A：写入 a 的条目
    clip_a = webm_clip.WebMClip(video_a)
    clip_a.warm_meta()

    # 进程 B 的旧快照：在 A 写入前就已读盘（模拟旧内存快照，A 的条目不可见）
    monkeypatch.setattr(webm_clip, "_META_FILE_CACHE", {})

    # 进程 B：写入 b 的条目——必须重读磁盘合并，不得用旧快照覆盖
    clip_b = webm_clip.WebMClip(video_b)
    clip_b.warm_meta()

    raw = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))
    assert len(raw) == 2, "后写进程不得覆盖先写进程的新条目（缓存必须单调累积）"
    assert any("a.webm" in k for k in raw), "a 的条目必须保留"
    assert any("b.webm" in k for k in raw), "b 的条目必须写入"
    app.processEvents()


def test_meta_file_cache_concurrent_writers_accumulate(tmp_path, monkeypatch):
    """批 6-8b 修 3：并发写入（同进程多线程 + 跨进程锁文件）不丢条目。"""
    app = QApplication.instance() or QApplication([])

    fake = types.SimpleNamespace(count_frames_and_secs=lambda path: (24, 1.0))
    monkeypatch.setattr(webm_clip, "imageio_ffmpeg", fake)
    monkeypatch.setattr(webm_clip, "_META_FILE_CACHE_PATH", tmp_path / "meta.json")
    monkeypatch.setattr(webm_clip, "_META_FILE_CACHE", None)
    monkeypatch.setattr(webm_clip, "_META_CACHE", {})

    def _write(name: str) -> None:
        video = tmp_path / f"{name}.webm"
        video.write_bytes(b"fake")
        clip = webm_clip.WebMClip(video)
        clip.warm_meta()

    ts = [
        threading.Thread(target=_write, args=(n,), daemon=True)
        for n in ("a", "b", "c", "d")
    ]
    for t in ts:
        t.start()
    for t in ts:
        t.join(5.0)

    raw = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))
    assert len(raw) == 4, "并发写入不得互相覆盖（4 个条目必须全部保留）"
    app.processEvents()


def test_meta_file_cache_evicts_entries_whose_source_file_is_gone(
    tmp_path, monkeypatch
):
    """源文件已不存在的条目在加载/落盘边界被逐出（N3，2026-09-29 审计）。

    pytest 临时路径这类条目在源文件删掉后永不失效（key 的 ``(mtime+size)``
    再无匹配者），跨运行常驻在 ``%TEMP%/dsh-pet-media-meta-cache.json`` 里。
    本进程第一次读该文件时做一次全表体检（按父目录分组列名单，比逐条
    ``os.path.exists`` 快一个数量级），逐出的 key 由写路径带出磁盘文件——
    都不进 ``_ensure_meta`` 热路径，条数上限语义不变。
    """
    app = QApplication.instance() or QApplication([])
    live = tmp_path / "live.webm"
    live.write_bytes(b"fake-live")
    live_stat = live.stat()
    live_key = f"{live}|{live_stat.st_mtime_ns}|{live_stat.st_size}"
    # 大小写变体的存活是平台语义：Windows 路径大小写不敏感，大写变体指向同一
    # 文件，必须算「存在」；posix 大小写敏感，大写后的父目录根本不存在，该
    # 条目就是死条目，必须被逐出。
    upper_key = f"{str(live).upper()}|{live_stat.st_mtime_ns}|{live_stat.st_size}"
    live_keys = {live_key, upper_key} if os.name == "nt" else {live_key}

    cache_file = tmp_path / "meta.json"
    cache_file.write_text(
        json.dumps({
            live_key: {"frames": 24, "duration": 1.0},
            upper_key: {"frames": 24, "duration": 1.0},
            f"{tmp_path / 'gone-a.webm'}|1700000000000000000|7": {
                "frames": 12, "duration": 0.5,
            },
            str(tmp_path / "gone-b.webm"): {"frames": 8, "duration": 0.25},
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(webm_clip, "_META_FILE_CACHE_PATH", cache_file)
    monkeypatch.setattr(webm_clip, "_META_FILE_CACHE", None)
    monkeypatch.setattr(webm_clip, "_META_FILE_CACHE_DEAD", set())

    # 加载边界：源文件已删的条目逐出，活条目保留（大小写变体按平台语义，见上）
    assert set(webm_clip._get_meta_file_cache()) == live_keys

    # 落盘边界：下一次写入把死条目从磁盘文件里一并带走
    webm_clip._META_FILE_CACHE = None
    webm_clip._save_meta_file_cache_entry(
        f"{live}|{live_stat.st_mtime_ns}|{live_stat.st_size}", 24, 1.0)
    raw = json.loads(cache_file.read_text(encoding="utf-8"))
    assert set(raw) == live_keys
    app.processEvents()


def test_meta_cache_source_path_parses_only_versioned_keys():
    """key → 源文件路径：只有尾部两段都是数字才认版本后缀（路径带 | 也不误切）。"""
    parse = webm_clip._meta_cache_source_path
    assert parse("D:\\a\\b.webm|123|456") == "D:\\a\\b.webm"
    assert parse("D:\\a\\b.webm") == "D:\\a\\b.webm"
    assert parse("D:\\a|b\\c.webm|123|456") == "D:\\a|b\\c.webm"
    assert parse("D:\\a\\b.webm|123") == "D:\\a\\b.webm|123"
