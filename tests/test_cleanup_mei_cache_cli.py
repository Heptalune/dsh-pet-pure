# -*- coding: utf-8 -*-
"""``scripts/cleanup_mei_cache.py`` 的 CLI 契约：帮助写清归属门禁，行为按归属删除。

脚本是 ``pet.runtime_cleanup`` 的命令行门面：删除前提是**本产品自己的归属标记**，
没有标记的 ``_MEI*`` 目录一律保留。这里在 CLI 这一层锁住同一份语义——帮助文本
必须写明，删除行为必须照做。所有目录都建在 pytest 的 tmp 里。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from pet.runtime_cleanup import OWNER_MARKER_NAME, mark_owned_runtime_dir

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "cleanup_mei_cache.py"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(ROOT),
        timeout=180,
    )


def _flat(text: str) -> str:
    """去掉全部空白：argparse 按终端宽度换行会把中文短语拆到两行。"""
    return re.sub(r"\s+", "", text)


def _owned_dir(path: Path) -> Path:
    path.mkdir()
    assert mark_owned_runtime_dir(path) is True
    return path


def test_help_states_the_ownership_gate() -> None:
    """``--help`` 必须说明：只清带本产品归属标记的目录，没有标记的一个都不删。"""
    completed = _run("--help")
    assert completed.returncode == 0, completed.stderr
    help_text = _flat(completed.stdout)

    assert "归属标记" in help_text
    assert "没有标记的_MEI*目录" in help_text
    assert "一律保留" in help_text
    assert "既不列出也不删除" in help_text


def test_preview_lists_only_owned_candidates(tmp_path: Path) -> None:
    owned = _owned_dir(tmp_path / "_MEI900001")
    unmarked = tmp_path / "_MEI900002"
    unmarked.mkdir()
    (unmarked / "OTHER_APP.txt").write_text("not ours", encoding="utf-8")

    completed = _run("--temp-dir", str(tmp_path), "--min-age-hours", "0")

    assert completed.returncode == 0, completed.stderr
    assert "_MEI900001" in completed.stdout
    assert "_MEI900002" not in completed.stdout
    assert owned.exists() and unmarked.exists()


def _dead_pid() -> int:
    """一个已退出进程的 PID（起一个立即退出的子进程并等它结束）。"""
    proc = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                          capture_output=True, text=True, timeout=60, check=True)
    return int(proc.stdout.strip())


def test_delete_removes_owned_and_keeps_unmarked(tmp_path: Path) -> None:
    owned = _owned_dir(tmp_path / "_MEI900003")
    # 模拟崩溃遗留：标记的写入进程已退出。否则 POSIX 的在用判定（标记 PID
    # 是否存活）会看到 pytest 自己活着而保守跳过——那是正确的产品行为。
    marker_path = owned / OWNER_MARKER_NAME
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["pid"] = _dead_pid()
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    unmarked = tmp_path / "_MEI900004"
    unmarked.mkdir()
    (unmarked / "OTHER_APP.txt").write_text("not ours", encoding="utf-8")
    foreign_owner = tmp_path / "_MEI900005"
    foreign_owner.mkdir()
    (foreign_owner / OWNER_MARKER_NAME).write_text(
        json.dumps({"owner": "some-other-app"}), encoding="utf-8"
    )

    completed = _run("--temp-dir", str(tmp_path), "--min-age-hours", "0", "--delete")

    assert completed.returncode == 0, completed.stderr
    assert not owned.exists()
    assert (unmarked / "OTHER_APP.txt").read_text(encoding="utf-8") == "not ours"
    assert foreign_owner.exists(), "别的应用的归属标记不采信"
