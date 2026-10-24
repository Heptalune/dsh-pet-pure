# -*- coding: utf-8 -*-
"""打包产物 TTS 自检（`scripts/verify_bundle_tts.py`）的「闭包该要求哪些模块」回归。

背景（2026-09-22 实测）：自检脚本对着**真实产物**跑出 FAIL——
``missing: cython_runtime, google, mpl_toolkits, pywin32_bootstrap``——而 edge-tts
明明在包里（PYZ 里有 ``edge_tts.communicate``）。根因是闭包按「``import edge_tts``
之后 ``sys.modules`` 里所有非标准库模块」算，于是三类与 edge-tts 无关的东西被当成
「必须有」，构建期被判红：

1. **启动期注入**：``.pth`` / ``sitecustomize`` 在解释器启动时就塞进 ``sys.modules``
   的模块（实测这台机器启动时已有 ``google`` / ``mpl_toolkits`` /
   ``pywin32_bootstrap``，来自 Dev-Cpp 自带的 site-packages）；
2. **运行时合成模块**：``cython_runtime`` 由 Cython 生成的扩展在 import 时现造，
   磁盘上根本没有文件，任何产物都不可能也不该为它准备文件；
3. **pkgutil 风格 namespace 空壳**：``backports`` 的 ``__init__.py`` 只有一行
   ``extend_path``，自身没有代码；aiohttp 的 ``from backports.zstd import ...``
   先把这个空壳建出来再失败（``backports.zstd`` 只是 aiohttp 的 speedups extra，
   未装，且包在 ``try/except ImportError`` 里）。

本文件钉住三条过滤，同时保证**真需要的模块仍然被要求**——否则把判定改成永不报错
也是一种"假绿"。
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def verify_tts():
    spec = importlib.util.spec_from_file_location(
        "verify_bundle_tts", REPO / "scripts" / "verify_bundle_tts.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["verify_bundle_tts"] = module
    spec.loader.exec_module(module)
    return module


def _fake_module(name: str, file_path: Path) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__file__ = str(file_path)
    return module


def test_startup_injected_module_is_not_part_of_closure(verify_tts, tmp_path):
    """``.pth`` 在启动期注入的模块不属于被检查库的闭包。

    探针模块刻意给一个真实存在的 ``.py``——这样唯一能排除它的理由就只剩
    「它在 ``import edge_tts`` 之前就已在 ``sys.modules`` 里」。
    """
    pytest.importorskip("edge_tts")
    source = tmp_path / "zz_startup_injected_probe.py"
    source.write_text("value = 1\n", encoding="utf-8")
    sys.modules["zz_startup_injected_probe"] = _fake_module(
        "zz_startup_injected_probe", source
    )
    try:
        closure = verify_tts.import_closure()
    finally:
        sys.modules.pop("zz_startup_injected_probe", None)

    assert closure is not None
    assert "zz_startup_injected_probe" not in closure


def test_synthesized_module_without_file_is_not_packable(verify_tts):
    """``cython_runtime`` 那类运行时合成模块没有文件，不该被要求进包。"""
    assert verify_tts._has_packable_source(types.ModuleType("cython_runtime")) is False


def test_pkgutil_namespace_shim_is_not_packable(verify_tts, tmp_path):
    """pkgutil 风格 namespace 空壳（实测 ``backports``）自身没有代码。"""
    package = tmp_path / "backports"
    package.mkdir()
    init = package / "__init__.py"
    init.write_text(
        "__path__ = __import__('pkgutil').extend_path(__path__, __name__)  # type: ignore\n",
        encoding="utf-8",
    )
    assert verify_tts._has_packable_source(_fake_module("backports", init)) is False


def test_regular_package_is_still_packable(verify_tts, tmp_path):
    """真包必须仍被判为「需要」，否则等于把判定改成永不报错。"""
    package = tmp_path / "yarl"
    package.mkdir()
    init = package / "__init__.py"
    init.write_text("from ._url import URL\n\n__all__ = ('URL',)\n", encoding="utf-8")
    assert verify_tts._has_packable_source(_fake_module("yarl", init)) is True


def test_module_missing_its_file_is_not_packable(verify_tts, tmp_path):
    """``__file__`` 指向不存在的路径（陈旧 ``sys.modules`` 残留）不算有源码。"""
    gone = _fake_module("zz_removed_probe", tmp_path / "zz_removed_probe.py")
    assert verify_tts._has_packable_source(gone) is False


def test_missing_modules_returns_sorted_difference(verify_tts):
    assert verify_tts.missing_modules({"a", "b"}, {"b", "c", "d"}) == ["c", "d"]


def test_minimum_modules_are_still_required(verify_tts):
    """兜底清单（含 PYZ-only 的 tabulate）不许在过滤里被削掉。"""
    assert "tabulate" in verify_tts.MINIMUM_MODULES
    assert "edge_tts" in verify_tts.MINIMUM_MODULES


@pytest.mark.parametrize("build_version,bundle_version,ok", [
    ("7.3.1", "7.3.1", True), ("7.2.8", "7.2.8", False),
    ("7.3.1", "7.2.8", False), ("7.3.1", None, False),
    ("7.3.1", "7.3.2", False), ("8.0.0", "8.0.0", False),
])
def test_bundle_version_gate(verify_tts, tmp_path, monkeypatch, build_version, bundle_version, ok):
    import importlib.metadata

    exe = tmp_path / "pet.exe"
    exe.write_bytes(b"archive is the OS boundary")
    if bundle_version:
        metadata = tmp_path / "_internal" / f"edge_tts-{bundle_version}.dist-info" / "METADATA"
        metadata.parent.mkdir(parents=True)
        metadata.write_text(f"Metadata-Version: 2.4\nName: edge-tts\nVersion: {bundle_version}\n",
                            encoding="utf-8")
    monkeypatch.setattr(importlib.metadata, "version", lambda name: build_version)
    everything = set(verify_tts.MINIMUM_MODULES) | set(verify_tts.platform_modules())
    # 闭包里的点号模块会让 verify 要求其顶层包名，present 必须一并具备。
    present = everything | {m.split(".")[0] for m in everything}
    monkeypatch.setattr(verify_tts, "import_closure", lambda: everything)
    monkeypatch.setattr(verify_tts, "frozen_module_names", lambda exe: present)
    code, summary = verify_tts.verify(tmp_path, exe)
    assert (code == 0) is ok, summary
    if ok:
        assert summary["build_version"] == "7.3.1"
        assert summary["bundle_version"] == "7.3.1"


def _gate_with(verify_tts, monkeypatch, tmp_path, platform, frozen):
    """在指定"目标平台"上跑一次 verify()，返回 (退出码, 摘要)。

    `frozen` 是"包里有这些模块"的模拟；闭包里的点号模块会被要求其顶层包，所以这里
    一并补上顶层名，避免把判定逻辑的顶层推导当成缺模块。
    """
    import importlib.metadata

    exe = tmp_path / "pet"
    exe.write_bytes(b"archive is the OS boundary")
    metadata = tmp_path / "_internal" / "edge_tts-7.3.1.dist-info" / "METADATA"
    metadata.parent.mkdir(parents=True, exist_ok=True)
    if not metadata.is_file():
        metadata.write_text("Metadata-Version: 2.4\nName: edge-tts\nVersion: 7.3.1\n",
                            encoding="utf-8")
    present = set(frozen) | {m.split(".")[0] for m in frozen}
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "7.3.1")
    monkeypatch.setattr(verify_tts.sys, "platform", platform)
    monkeypatch.setattr(verify_tts, "import_closure", lambda: set(verify_tts.MINIMUM_MODULES))
    monkeypatch.setattr(verify_tts, "frozen_module_names", lambda exe: set(present))
    return verify_tts.verify(tmp_path, exe)


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_windows_only_modules_are_not_required_on_other_targets(
        verify_tts, monkeypatch, tmp_path, platform):
    """干净 Linux/macOS 构建不装 Windows-only 依赖，门禁不许因此判红。

    astra 第 13 轮：`MINIMUM_MODULES` 无条件要求 `win32ctypes.pywin32.win32cred`，而
    `build_linux.sh` / `build_macos.sh` 调的是同一个 `verify()`——那是把正确的产物
    拒掉。修法是让这类依赖按目标平台生效。
    """
    code, summary = _gate_with(verify_tts, monkeypatch, tmp_path, platform,
                               set(verify_tts.MINIMUM_MODULES))
    assert code == 0, summary
    assert "win32ctypes.pywin32.win32cred" not in summary["checked"]


def test_windows_target_still_requires_its_backend_dependency(verify_tts, monkeypatch, tmp_path):
    """反过来：Windows 目标少带 win32ctypes 必须判红（别把条件化做成放水）。"""
    code, summary = _gate_with(verify_tts, monkeypatch, tmp_path, "win32",
                               set(verify_tts.MINIMUM_MODULES))
    assert code == 4, summary
    assert summary["missing"] == ["win32ctypes.pywin32.win32cred"]

    code, summary = _gate_with(verify_tts, monkeypatch, tmp_path, "win32",
                               set(verify_tts.MINIMUM_MODULES)
                               | set(verify_tts.WINDOWS_ONLY_MODULES))
    assert code == 0, summary


def test_platform_modules_is_empty_off_windows(verify_tts):
    assert verify_tts.platform_modules("linux") == ()
    assert verify_tts.platform_modules("darwin") == ()
    assert verify_tts.platform_modules("win32") == verify_tts.WINDOWS_ONLY_MODULES
