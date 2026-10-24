# -*- coding: utf-8 -*-
"""运行目录清理的归属与年龄契约。

删除前提是**本产品自己的归属标记**：目录名（``_MEI数字``）+ 年龄不足以判定
归属，按它删除会把别的 PyInstaller 应用的解包目录当遗留清掉（终审 A01）。
没有标记的目录一律保留——这是刻意的安全降级。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from pet.runtime_cleanup import (
    OWNER_MARKER_NAME,
    cleanup_stale_runtime_dirs,
    find_stale_runtime_dirs,
    mark_owned_runtime_dir,
)


def _touch_dir(path: Path, age_seconds: float) -> None:
    path.mkdir()
    _age_dir(path, age_seconds)


def _age_dir(path: Path, age_seconds: float) -> None:
    """把目录（连同标记文件）改老：往目录里写东西会刷新它自己的 mtime。"""
    timestamp = time.time() - age_seconds
    marker = path / OWNER_MARKER_NAME
    if marker.is_file() and not marker.is_symlink():
        os.utime(marker, (timestamp, timestamp))
    os.utime(path, (timestamp, timestamp))


def _owned_dir(path: Path, age_seconds: float) -> Path:
    """建目录、打本产品归属标记，再把目录（连同标记）改老。"""
    _touch_dir(path, age_seconds)
    assert mark_owned_runtime_dir(path) is True
    _age_dir(path, age_seconds)
    return path


def test_find_stale_runtime_dirs_only_returns_owned_old_mei_directories(tmp_path: Path) -> None:
    owned_old = _owned_dir(tmp_path / "_MEI12345", 48 * 3600)
    _owned_dir(tmp_path / "_MEI67890", 30 * 60)  # 名字对、有标记，但太年轻
    _owned_dir(tmp_path / "_MEI-not-a-number", 48 * 3600)  # 名字不是解包目录形态
    unowned_old = tmp_path / "_MEI22222"  # 别的应用留下的解包目录：名字对、很老、无标记
    _touch_dir(unowned_old, 48 * 3600)
    foreign_owner = tmp_path / "_MEI33333"  # 有标记，但标记不属于本产品
    _touch_dir(foreign_owner, 48 * 3600)
    (foreign_owner / OWNER_MARKER_NAME).write_text(
        json.dumps({"owner": "some-other-app"}), encoding="utf-8"
    )
    _age_dir(foreign_owner, 48 * 3600)

    result = find_stale_runtime_dirs(
        temp_dir=tmp_path,
        min_age_seconds=24 * 3600,
        now=time.time(),
    )

    assert result == [owned_old]


def test_find_stale_runtime_dirs_skips_current_runtime_directory(tmp_path: Path) -> None:
    current_dir = _owned_dir(tmp_path / "_MEI12345", 48 * 3600)
    old_other_dir = _owned_dir(tmp_path / "_MEI67890", 48 * 3600)

    result = find_stale_runtime_dirs(
        temp_dir=tmp_path,
        current_dir=current_dir,
        min_age_seconds=24 * 3600,
        now=time.time(),
    )

    assert result == [old_other_dir]


def test_cleanup_stale_runtime_dirs_removes_owned_and_keeps_unowned(tmp_path: Path) -> None:
    owned_dir = _owned_dir(tmp_path / "_MEI12345", 48 * 3600)
    (owned_dir / "readme.txt").write_text("stale", encoding="utf-8")
    _age_dir(owned_dir, 48 * 3600)
    foreign_dir = tmp_path / "_MEI67890"
    _touch_dir(foreign_dir, 48 * 3600)
    (foreign_dir / "OTHER_APP.txt").write_text("not ours", encoding="utf-8")
    _age_dir(foreign_dir, 48 * 3600)

    result = cleanup_stale_runtime_dirs(
        temp_dir=tmp_path,
        min_age_seconds=24 * 3600,
        now=time.time(),
    )

    assert result.candidates == [owned_dir]
    assert result.removed == [owned_dir]
    assert result.failed == {}
    assert not owned_dir.exists()
    # A01 回归点：没有本产品归属标记的解包目录一个字节都不许动
    assert (foreign_dir / "OTHER_APP.txt").read_text(encoding="utf-8") == "not ours"


def test_cleanup_dry_run_does_not_delete_candidates(tmp_path: Path) -> None:
    owned_dir = _owned_dir(tmp_path / "_MEI12345", 48 * 3600)

    result = cleanup_stale_runtime_dirs(
        temp_dir=tmp_path,
        min_age_seconds=24 * 3600,
        now=time.time(),
        dry_run=True,
    )

    assert result.candidates == [owned_dir]
    assert result.removed == []
    assert result.failed == {}
    assert owned_dir.exists()


def test_missing_or_empty_temp_dir_is_harmless(tmp_path: Path) -> None:
    """临时目录不存在 / 为空：两个入口都安静返回，不抛异常。"""
    missing = tmp_path / "nope"
    empty = tmp_path / "empty"
    empty.mkdir()

    for root in (missing, empty):
        assert find_stale_runtime_dirs(temp_dir=root, now=time.time()) == []
        result = cleanup_stale_runtime_dirs(temp_dir=root, now=time.time())
        assert result.candidates == [] and result.removed == [] and result.failed == {}


def test_garbage_owner_marker_is_not_trusted(tmp_path: Path) -> None:
    """标记文件内容坏掉（非法 JSON / 空 / 非对象）时按“无归属”处理，不删。"""
    for name, payload in (("_MEI40001", "{not json"), ("_MEI40002", ""), ("_MEI40003", "[]")):
        candidate = tmp_path / name
        _touch_dir(candidate, 48 * 3600)
        (candidate / OWNER_MARKER_NAME).write_text(payload, encoding="utf-8")
        _age_dir(candidate, 48 * 3600)

    assert find_stale_runtime_dirs(temp_dir=tmp_path, now=time.time()) == []
    for name in ("_MEI40001", "_MEI40002", "_MEI40003"):
        assert (tmp_path / name).exists()
