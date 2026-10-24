# -*- coding: utf-8 -*-
"""CI 主套件分片执行器（Windows 专用，见 .github/workflows/pr-test.yml）。

为什么存在：Windows CI（hosted runner，Python 3.11.9 toolcache + PySide6 6.12.0）上
整套件单进程跑到 ~2/3 处会偶发 access violation（崩点漂移、堆腐蚀签名，mac/Linux
不复现，本机 6.11.2 不复现；取证见 .scratch/ci243/WIN-AV-HUNT-2026-10-13.md）。
按收集序切成 4 个独立 pytest 进程后崩溃不再发生（诊断分支两轮零崩）——每个分片
进程的堆状态从头开始，跨族腐蚀没有积累窗口。这不是掩盖失败：任何分片里的测试
失败照常红，只是崩溃归因更准（崩在哪个分片、哪个用例，日志直接点名）。

分片按收集序等分（pytest --collect-only 的真实顺序），新测试文件自动入片；
收集失败/零条目会当场报错，不会静默缩水。
"""
from __future__ import annotations

import subprocess
import sys

#: 与主套件一致的忽略清单（webm 生命周期族在独立步骤跑，见 workflow 注释）。
IGNORES = [
    "tests/test_webm_reader_lifecycle.py",
    "tests/test_webm_clip_lifecycle.py",
    "tests/test_webm_first_frame_lock.py",
    "tests/test_low_priority_warm_interaction_yield.py",
]
SHARDS = 4


def _collect_files() -> list[str]:
    """主套件收集序的测试文件清单（去重保序）。"""
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "--collect-only", "-q",
         *[f"--ignore={p}" for p in IGNORES], "-p", "no:cacheprovider"],
        capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"收集失败（exit {out.returncode}）：\n{out.stdout[-2000:]}")
    items = [line.split("::")[0] for line in out.stdout.splitlines() if "::" in line]
    if not items:
        raise SystemExit("收集到 0 条用例——pytest 或目录结构出了问题")
    files: list[str] = []
    for f in items:
        if f not in files:
            files.append(f)
    return files


def main() -> int:
    files = _collect_files()
    total = len(files)
    # 按文件数均匀切片（收集序连续块——崩溃归因按「这片在收集序的哪段」可读）
    bounds = [round(i * total / SHARDS) for i in range(SHARDS + 1)]
    failed: list[int] = []
    for i in range(SHARDS):
        shard = files[bounds[i]:bounds[i + 1]]
        print(f"\n===== shard {i + 1}/{SHARDS}（{len(shard)} 个文件"
              f"：{shard[0]} … {shard[-1]}）=====", flush=True)
        rc = subprocess.call([sys.executable, "-m", "pytest", "-q", *shard])
        if rc != 0:
            failed.append(i + 1)
    if failed:
        print(f"\n分片失败：{failed}", flush=True)
        return 1
    print(f"\n全部 {SHARDS} 个分片通过", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
