# -*- coding: utf-8 -*-
"""构建期帧序列素材供给入口（三端 CI 共用，2026-10-11）。

**为什么需要它**：帧序列产物 ``assets/characters/<角色>/frameseq/`` 是刻意不入库的
派生产物（``.gitignore``），干净检出（CI / 新克隆 / worktree）里天然没有；而构建
对素材整树拷贝 ``assets/characters``——素材在就进包、缺了也不报错。三端 CI 因此必须
有一步「缺就补、补完必验」的供给，且**三端同一口径**：否则 macOS / Linux 会打出
缺素材的静默降级包（Windows 只是靠 ``build_onedir.ps1`` 里的硬门挡下来）。

**语义（顺序不可换）**：

1. 先跑 ``scripts/check_frameseq_bundle.py``（只读硬门，判据与运行时采纳闸门同源）；
   退出码 0 ⇒ 素材已完整，**直接跳过转换**（缓存命中的唯一判据）；
2. 非 0 ⇒ 跑 ``tools/convert_frameseq.py --character <角色> --include random,events``
   （该 CLI 默认只转热集 11 段，而门要全树 106 段，``--include`` 不可省）；
3. 转换器非零 ⇒ **原样冒泡**它的退出码，不再往下走（不重试、不掩盖）；
4. 转完**再验一次**：仍非 0 即判红（``--force`` 走同一条路）。

**坏缓存不盲跳过**：唯一放行依据是第 1 步验证器的真实退出码。缓存里的陈旧档 /
半成品世代目录 / 缺 meta / 源身份不符都会被验证器判红，于是进入重转分支，
而不是「文件在就算缓存命中」。

**边界**：构建期入口，不启动 GUI（不 import ``pet`` 包，因而不会连带 PySide6）、
不碰用户运行时目录（配置 / 缓存 / 注册表一概不写），只调上面两个 CLI，且一律用
``subprocess`` 直调（无 shell、无管道、无重试），退出码原样透传。

``--characters-dir`` 只能指向仓库内的 ``assets/characters``：转换器（``tools/convert_frameseq.py``）
不接受外部素材根，允许指向别处会造成「验的是这棵树、转的是那棵树」的错配。

用法：
    python scripts/prepare_frameseq_bundle.py                   # 缺则转、转完必验
    python scripts/prepare_frameseq_bundle.py --force            # 全量重转
    python scripts/prepare_frameseq_bundle.py --print-cache-key  # 只算 CI 缓存 key

退出码：0 = 素材完整（无论是否触发过转换）；非 0 = 验证器 / 转换器的真实退出码。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import platform
import re
import subprocess
import sys
from importlib import metadata
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# 缓存 key 的构造版本：语义变了要换（旧 key 不允许被新语义复用）。
CACHE_KEY_SCHEMA = "frameseq-supply-v1"
DEFAULT_CHARACTERS_DIR = "assets/characters"
DEFAULT_CHARACTER = "shenshen"
DEFAULT_INCLUDE = "random,events"
VALIDATOR_REL = "scripts/check_frameseq_bundle.py"
CONVERTER_REL = "tools/convert_frameseq.py"
# 转换实现（含编码档、世代命名、供给范围）都在这两份源码里：改动即换 key。
CONVERTER_SOURCES = ("pet/frameseq_provision.py", "tools/convert_frameseq.py")
# 平台差异的真实来源：imageio-ffmpeg 每平台打包不同的 ffmpeg 构建，webp 编码走 Pillow。
DEPENDENCY_DISTS = ("Pillow", "imageio-ffmpeg")


def _load_gate():
    """按路径加载素材门模块（构建门不 import ``pet``，这里也不走包导入）。"""
    path = Path(__file__).resolve().parent / "check_frameseq_bundle.py"
    spec = importlib.util.spec_from_file_location("frameseq_bundle_gate", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载素材门脚本: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load_gate()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def platform_slug() -> str:
    """平台标识（缓存 key 的可读前缀；也进摘要，避免同名跨平台串用）。"""
    raw = f"{sys.platform}-{platform.machine()}"
    return re.sub(r"[^a-z0-9._-]+", "-", raw.lower()).strip("-")


def _dist_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "absent"


def environment_label() -> str:
    """平台 + Python + 编码相关依赖版本（任一项变即换 key）。"""
    major, minor = sys.version_info[:2]
    deps = ",".join(f"{name}={_dist_version(name)}" for name in DEPENDENCY_DISTS)
    return f"{platform_slug()}/py{major}.{minor}/{deps}"


def grade_label() -> str:
    """当前编码档（含反解标记）：改档让全部旧世代失效，key 必须跟着变。"""
    return f"{gate.ENCODER_DESC}|{gate.UNBLEND_MARK}"


def source_digest(characters_dir: Path) -> str:
    """素材源身份：按相对路径排序的 (路径, 文件 sha256) 汇总（只读，不改文件）。"""
    characters_dir = Path(characters_dir)
    if not characters_dir.is_dir():
        raise FileNotFoundError(f"素材目录不存在: {characters_dir}")
    webms = sorted(
        (path for path in characters_dir.rglob("*.webm") if path.is_file()),
        key=lambda path: path.relative_to(characters_dir).as_posix(),
    )
    digest = hashlib.sha256()
    for webm in webms:
        digest.update(webm.relative_to(characters_dir).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(_file_sha256(webm)))
    return digest.hexdigest()


def converter_digest(root: Path | None = None) -> str:
    """转换器 / 供给实现版本：两份源码的 sha256 汇总。

    取「文件内容」而不是某个手写版本号：任何实现改动都会换 key。宁可多丢一次
    缓存（重转 28 min），也不许命中语义已变的旧缓存。
    """
    base = REPO_ROOT if root is None else Path(root)
    digest = hashlib.sha256()
    for rel in CONVERTER_SOURCES:
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(_file_sha256(base / rel)))
    return digest.hexdigest()


def cache_key(characters_dir: Path | str = DEFAULT_CHARACTERS_DIR, *,
              env: str | None = None, root: Path | None = None) -> str:
    """CI 缓存 key：源素材摘要 + 转换器版本 + 编码档 + 平台/依赖版本。"""
    env_label = environment_label() if env is None else env
    components = (
        CACHE_KEY_SCHEMA,
        source_digest(Path(characters_dir)),
        converter_digest(root),
        grade_label(),
        env_label,
    )
    digest = hashlib.sha256("\n".join(components).encode("utf-8")).hexdigest()
    return f"frameseq-{platform_slug()}-{digest[:32]}"


def run_command(argv, cwd=None) -> subprocess.CompletedProcess:
    """跑一条命令并返回真实退出码：无 shell、无管道、无重试（失败码必须冒泡）。"""
    print(f"[frameseq-supply] $ {' '.join(str(part) for part in argv)}", flush=True)
    return subprocess.run(
        [str(part) for part in argv],
        cwd=None if cwd is None else str(cwd),
        check=False,
    )


def validator_argv(python_bin: str, characters_dir: Path) -> list[str]:
    return [python_bin, VALIDATOR_REL, "--characters-dir", str(characters_dir)]


def converter_argv(python_bin: str, character: str, include: str, *,
                   force: bool = False) -> list[str]:
    argv = [python_bin, CONVERTER_REL, "--character", character]
    if include:
        argv += ["--include", include]
    if force:
        argv.append("--force")
    return argv


def prepare(characters_dir: Path | str = DEFAULT_CHARACTERS_DIR,
            character: str = DEFAULT_CHARACTER,
            include: str = DEFAULT_INCLUDE, *,
            force: bool = False,
            python_bin: str | None = None,
            root: Path | str = REPO_ROOT,
            runner=run_command) -> int:
    """验 → （缺才）转 → 再验；返回最后一次真实退出码（0 = 素材完整）。"""
    python_bin = sys.executable if python_bin is None else python_bin
    characters = Path(characters_dir)
    base = Path(root)

    validator = validator_argv(python_bin, characters)
    print("[frameseq-supply] 先验证素材是否已完整（决定能否直接用缓存）...", flush=True)
    code = runner(validator, base).returncode
    if code == 0 and not force:
        print("[frameseq-supply] 素材已完整，跳过转换（缓存命中）", flush=True)
        return 0
    if code == 0:
        print("[frameseq-supply] --force：忽略已有素材，全量重转", flush=True)
    else:
        print(f"[frameseq-supply] 素材不完整（验证器退出码 {code}），开始转换"
              "（全量冷转约 28 min；缺多少见上面验证器列的清单）...", flush=True)

    convert_code = runner(converter_argv(python_bin, character, include, force=force),
                          base).returncode
    if convert_code != 0:
        print(f"[frameseq-supply] 转换失败（退出码 {convert_code}），不再继续验证",
              file=sys.stderr, flush=True)
        return convert_code

    print("[frameseq-supply] 转换完成，重新验证素材完整性...", flush=True)
    return runner(validator, base).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="构建期帧序列素材供给（先验完整，缺才转，转完再验）"
    )
    parser.add_argument("--characters-dir", default=DEFAULT_CHARACTERS_DIR,
                        help="角色素材根目录（默认 assets/characters，相对当前工作目录）")
    parser.add_argument("--character", default=DEFAULT_CHARACTER,
                        help="要转换的角色 id（默认 shenshen）")
    parser.add_argument("--include", default=DEFAULT_INCLUDE,
                        help="传给转换器的额外目录（默认 random,events；省略即只转热集 11 段，"
                             "而门要全树 106 段）")
    parser.add_argument("--force", action="store_true",
                        help="忽略已有素材全量重转（等价 tools/convert_frameseq.py --force）")
    parser.add_argument("--print-cache-key", action="store_true",
                        help="只打印 CI 缓存 key（源摘要 + 转换器版本 + 编码档 + 平台/依赖版本）")
    args = parser.parse_args(argv)

    # tools/convert_frameseq.py 只认仓库内的 assets/characters/<角色>/videos（没有
    # 「外部素材根」参数）：--characters-dir 指向别处会出现「验的是 A 树、转的是 B 树」，
    # 那比直接失败更糟。这里当场拒绝，不做静默错配。
    characters = Path(args.characters_dir)
    expected = REPO_ROOT / DEFAULT_CHARACTERS_DIR
    if characters.resolve() != expected.resolve():
        parser.error(f"转换器只服务仓库内的 {DEFAULT_CHARACTERS_DIR}（tools/convert_frameseq.py "
                     f"不接受外部素材根），--characters-dir 只能指向 {expected}")

    if args.print_cache_key:
        print(cache_key(characters))
        return 0
    return prepare(characters, args.character, args.include, force=args.force)


if __name__ == "__main__":
    sys.exit(main())
