# -*- coding: utf-8 -*-
"""清理所有权与冻结卸载入口的边界测试（终审 A01 / A12）。

覆盖两层：
- ``pet/runtime_cleanup.py``：归属标记是删除前提，链接/junction/重解析点不跟随，
  删除前复核归属（TOCTOU），当前运行目录与在用目录受保护；
- 冻结入口与安装器：``--uninstall-cleanup`` 必须在 import ``pet.app`` 之前分流，
  不建 Qt、不跑正常 main；ISS 在删除 exe 前调用清理并留痕。

所有目录都建在 pytest 的 tmp 里；不碰真实临时目录、用户配置、注册表，
也不执行任何真实卸载或清理。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from pet import runtime_cleanup
from pet.config import Config
from pet import uninstall_cleanup

ROOT = Path(__file__).resolve().parents[1]
FROZEN_ENTRIES = ("packaging/pet_entry.py", "packaging/pet_entry_no_chat.py")


def _touch_dir(path: Path, age_seconds: float) -> Path:
    path.mkdir()
    _age_dir(path, age_seconds)
    return path


def _age_dir(path: Path, age_seconds: float) -> None:
    """把目录（连同它的标记文件）改老。

    往目录里写东西会刷新目录自身的 mtime，所以每条用例在判定前都要重设年龄，
    否则"因为太年轻"会和"因为无归属"混成一个结果（每个否定断言都配了正对照）。
    """
    timestamp = time.time() - age_seconds
    marker = path / runtime_cleanup.OWNER_MARKER_NAME
    try:
        marker_is_plain = marker.is_file() and not marker.is_symlink()
    except OSError:
        marker_is_plain = False
    if marker_is_plain:
        os.utime(marker, (timestamp, timestamp))
    os.utime(path, (timestamp, timestamp))


def _owned_dir(path: Path, age_seconds: float) -> Path:
    _touch_dir(path, age_seconds)
    assert runtime_cleanup.mark_owned_runtime_dir(path) is True
    _age_dir(path, age_seconds)
    return path


def _stale_mei_candidates(temp_dir: Path) -> list[Path]:
    return runtime_cleanup.find_stale_runtime_dirs(
        temp_dir=temp_dir, min_age_seconds=24 * 3600, now=time.time()
    )


# --------------------------------------------------------------- A01：归属门禁

def test_foreign_temp_mei_dir_with_other_app_file_is_untouched(tmp_path: Path) -> None:
    """终审 A01 的复现形态：系统 temp 里别的应用留下的 `_MEI数字` 不许被当遗留清理。"""
    control = _owned_dir(tmp_path / "_MEI111111", 48 * 3600)  # 正对照：同样形态但归属本产品
    foreign = _touch_dir(tmp_path / "_MEI123456", 48 * 3600)
    (foreign / "OTHER_APP.txt").write_text("other app payload", encoding="utf-8")
    _age_dir(foreign, 48 * 3600)

    candidates = _stale_mei_candidates(tmp_path)
    result = runtime_cleanup.cleanup_stale_runtime_dirs(
        temp_dir=tmp_path, min_age_seconds=24 * 3600, now=time.time()
    )

    assert candidates == [control], "正对照必须入选，否则本用例的否定结论没有意义"
    assert result.removed == [control]
    assert (foreign / "OTHER_APP.txt").read_text(encoding="utf-8") == "other app payload"


def test_foreign_owner_marker_is_not_trusted(tmp_path: Path) -> None:
    """标记内容必须属于本产品；别的产品写的标记不算归属证据。"""
    control = _owned_dir(tmp_path / "_MEI24681", 48 * 3600)
    foreign = _touch_dir(tmp_path / "_MEI24680", 48 * 3600)
    (foreign / runtime_cleanup.OWNER_MARKER_NAME).write_text(
        json.dumps({"owner": "other-product", "pid": 1}), encoding="utf-8"
    )
    _age_dir(foreign, 48 * 3600)

    assert _stale_mei_candidates(tmp_path) == [control]
    assert foreign.exists()


def test_symlinked_marker_file_is_not_followed(tmp_path: Path) -> None:
    """标记文件是符号链接时不采信——链接可以指向任何地方。"""
    control = _owned_dir(tmp_path / "_MEI13580", 48 * 3600)
    outside = tmp_path / "elsewhere.json"
    outside.write_text(json.dumps({"owner": runtime_cleanup.PRODUCT_OWNER_ID}), encoding="utf-8")
    candidate = _touch_dir(tmp_path / "_MEI13579", 48 * 3600)
    try:
        os.symlink(outside, candidate / runtime_cleanup.OWNER_MARKER_NAME)
    except (OSError, NotImplementedError):
        pytest.skip("本机不允许创建符号链接（需要开发者模式/权限）")
    _age_dir(candidate, 48 * 3600)

    assert _stale_mei_candidates(tmp_path) == [control]
    assert candidate.exists()


def test_marker_must_be_a_regular_file(tmp_path: Path) -> None:
    """标记位置被目录占用（不是普通文件）时不采信——不是普通文件就不算标记。"""
    control = _owned_dir(tmp_path / "_MEI13582", 48 * 3600)
    candidate = _touch_dir(tmp_path / "_MEI13581", 48 * 3600)
    (candidate / runtime_cleanup.OWNER_MARKER_NAME).mkdir()
    _age_dir(candidate, 48 * 3600)

    assert _stale_mei_candidates(tmp_path) == [control]
    assert candidate.exists()


def test_symlinked_mei_directory_is_not_followed(tmp_path: Path) -> None:
    """候选目录本身是符号链接时不跟随、不删除，也不删它指向的真实目录。"""
    control = _owned_dir(tmp_path / "_MEI97532", 48 * 3600)
    victim = _owned_dir(tmp_path / "victim", 48 * 3600)
    link = tmp_path / "_MEI97531"
    try:
        os.symlink(victim, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("本机不允许创建符号链接（需要开发者模式/权限）")

    result = runtime_cleanup.cleanup_stale_runtime_dirs(
        temp_dir=tmp_path, min_age_seconds=24 * 3600, now=time.time()
    )

    assert result.candidates == [control]
    assert result.removed == [control]
    assert victim.exists()
    assert (victim / runtime_cleanup.OWNER_MARKER_NAME).is_file()


@pytest.mark.skipif(os.name != "nt", reason="junction 只在 Windows 上存在")
def test_junction_inside_owned_runtime_dir_blocks_removal(tmp_path: Path) -> None:
    """目录树内部有 junction（重解析点）时整条不删：rmtree 会跟着它删到别处。"""
    candidate = _owned_dir(tmp_path / "_MEI86420", 48 * 3600)
    outside = _touch_dir(tmp_path / "outside", 48 * 3600)
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    created = subprocess.run(
        f'cmd /c mklink /J "{candidate / "link"}" "{outside}"',
        capture_output=True,
        text=True,
        shell=True,
    )
    if created.returncode != 0:
        pytest.skip(f"本机无法创建 junction：{created.stderr or created.stdout}")
    _age_dir(candidate, 48 * 3600)

    result = runtime_cleanup.cleanup_stale_runtime_dirs(
        temp_dir=tmp_path, min_age_seconds=24 * 3600, now=time.time()
    )

    assert result.candidates == [candidate], "候选筛选不该被 junction 影响"
    assert result.removed == []
    assert (outside / "keep.txt").is_file()
    assert candidate.exists()


def test_directory_swapped_during_probe_is_not_removed(tmp_path: Path, monkeypatch) -> None:
    """扫描与删除之间目录被替换（TOCTOU）时，删除前复核必须拦住。"""
    candidate = _owned_dir(tmp_path / "_MEI11223", 48 * 3600)
    replacement = _touch_dir(tmp_path / "replacement", 48 * 3600)
    (replacement / "OTHER_APP.txt").write_text("not ours", encoding="utf-8")
    _age_dir(replacement, 48 * 3600)

    def swap(directory: Path) -> bool:
        shutil.rmtree(directory)
        shutil.move(str(replacement), str(candidate))
        assert (candidate / "OTHER_APP.txt").is_file()
        return False

    monkeypatch.setattr(runtime_cleanup, "_dir_in_use", swap)

    result = runtime_cleanup.cleanup_stale_runtime_dirs(
        temp_dir=tmp_path, min_age_seconds=24 * 3600, now=time.time()
    )

    assert result.candidates == [candidate], "扫描阶段确实选中了它"
    assert result.removed == []
    assert (candidate / "OTHER_APP.txt").is_file()


def test_process_alive_probe_never_signals_on_windows() -> None:
    """Windows 上 ``os.kill`` 的非 0 信号会真的结束进程：探活必须整体禁用。

    Windows 侧的在用判定只有改名探测（`_dir_in_use`），PID 探活仅用于 POSIX。
    """
    if os.name == "nt":
        assert runtime_cleanup._process_alive(os.getpid()) is False
        assert runtime_cleanup._process_alive(4) is False
        assert runtime_cleanup._process_alive(0) is False
        return

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert runtime_cleanup._process_alive(child.pid) is True
        assert runtime_cleanup._process_alive(os.getpid()) is False  # 自己不算“别人在用”
    finally:
        child.terminate()
        child.wait(timeout=10)
    assert runtime_cleanup._process_alive(child.pid) is False


def test_frozen_runtime_dir_marking_covers_temp_mei_dirs_only(tmp_path, monkeypatch) -> None:
    """标记只打在系统临时目录下的 `_MEI数字`（onefile 解包目录）上。

    onedir 的 `sys._MEIPASS` 是安装目录里的 `_internal`：既不是临时目录里的
    解包目录，也没有可清理的对象，不打标记。标记过的目录下一轮能被采集。
    """
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    monkeypatch.setattr(runtime_cleanup.tempfile, "gettempdir", lambda: str(temp_root))
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    # 非冻结进程：不打标记
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(temp_root / "_MEI50001"), raising=False)
    (temp_root / "_MEI50001").mkdir()
    assert runtime_cleanup.mark_frozen_runtime_dir() is None
    assert not (temp_root / "_MEI50001" / runtime_cleanup.OWNER_MARKER_NAME).exists()

    # onedir：_MEIPASS 是安装目录里的 _internal，不打标记
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    onedir_internal = tmp_path / "app" / "_internal"
    onedir_internal.mkdir(parents=True)
    monkeypatch.setattr(sys, "_MEIPASS", str(onedir_internal), raising=False)
    assert runtime_cleanup.mark_frozen_runtime_dir() is None
    assert not (onedir_internal / runtime_cleanup.OWNER_MARKER_NAME).exists()

    # onefile：临时目录下的 _MEI数字 打标记，且下一轮会被当候选采集
    runtime_dir = temp_root / "_MEI50002"
    runtime_dir.mkdir()
    monkeypatch.setattr(sys, "_MEIPASS", str(runtime_dir), raising=False)
    assert runtime_cleanup.mark_frozen_runtime_dir() == runtime_dir
    marker = json.loads(
        (runtime_dir / runtime_cleanup.OWNER_MARKER_NAME).read_text(encoding="utf-8")
    )
    assert marker["owner"] == runtime_cleanup.PRODUCT_OWNER_ID
    assert marker["pid"] == os.getpid()

    monkeypatch.delattr(sys, "_MEIPASS", raising=False)  # 下一轮是别的进程
    timestamp = time.time() - 48 * 3600
    os.utime(runtime_dir / runtime_cleanup.OWNER_MARKER_NAME, (timestamp, timestamp))
    os.utime(runtime_dir, (timestamp, timestamp))
    result = runtime_cleanup.cleanup_stale_runtime_dirs(
        temp_dir=temp_root, min_age_seconds=24 * 3600, now=time.time()
    )
    assert result.removed == [runtime_dir]


# ------------------------------------------------------- A12：卸载清理入口接线

def test_frozen_entries_dispatch_uninstall_cleanup_before_importing_app() -> None:
    for name in FROZEN_ENTRIES:
        source = (ROOT / name).read_text(encoding="utf-8")
        dispatch_at = source.index('"--uninstall-cleanup" in sys.argv')
        app_import_at = source.index("from pet.app import main")
        assert dispatch_at < app_import_at, f"{name}: --uninstall-cleanup 必须在 import pet.app 之前分流"
        assert "from pet.uninstall_cleanup import main as uninstall_cleanup_main" in source
        assert "mark_frozen_runtime_dir()" in source


_RUNNER = r"""
import json, runpy, sys, types

entry_path = sys.argv[1]
sys.path.insert(0, sys.argv[2])  # 仓库根：入口要能 import pet（子进程 cwd 不进 sys.path）
record = {"mark": None, "cleanup": None, "app_main": None, "py_modules": sorted(sys.modules)}

runtime_cleanup = types.ModuleType("pet.runtime_cleanup")
def mark_frozen_runtime_dir():
    record["mark"] = "called"
    return None
runtime_cleanup.mark_frozen_runtime_dir = mark_frozen_runtime_dir

uninstall = types.ModuleType("pet.uninstall_cleanup")
def main(argv=None):
    record["cleanup"] = list(sys.argv[1:])  # runpy 会把 argv[0] 换成入口路径
    return 7
uninstall.main = main

app = types.ModuleType("pet.app")
def app_main(*args, **kwargs):
    record["app_main"] = list(sys.argv)
    return 99
app.main = app_main

import pet
pet.runtime_cleanup = runtime_cleanup
pet.uninstall_cleanup = uninstall
pet.app = app
sys.modules["pet.runtime_cleanup"] = runtime_cleanup
sys.modules["pet.uninstall_cleanup"] = uninstall
sys.modules["pet.app"] = app

sys.argv = ["dsh-pet-standalone", "--uninstall-cleanup"]
exit_code = 0
try:
    runpy.run_path(entry_path, run_name="__main__")
except SystemExit as exc:
    exit_code = exc.code if isinstance(exc.code, int) else 1

qt_loaded = sorted(n for n in sys.modules if n == "PySide6" or n.startswith("PySide6."))
print(json.dumps({
    "exit": exit_code,
    "mark": record["mark"],
    "cleanup": record["cleanup"],
    "app_main": record["app_main"],
    "qt": qt_loaded,
}))
"""


@pytest.mark.parametrize("entry", FROZEN_ENTRIES)
def test_frozen_entry_uninstall_cleanup_runs_without_qt_or_normal_main(entry: str, tmp_path) -> None:
    """真实入口进程里跑 `--uninstall-cleanup`：走清理、不建 Qt、不跑正常 main。

    spy 通过 sys.modules 注入（清理实现、pet.app、运行目录标记）：即使入口漏了
    分流，也只会调到 spy，不会真的启动桌宠或碰真实自启/注册表。
    """
    runner = tmp_path / "_entry_runner.py"
    runner.write_text(_RUNNER, encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, str(runner), str(ROOT / entry), str(ROOT)],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload["cleanup"] == ["--uninstall-cleanup"]
    assert payload["exit"] == 7, "入口必须把清理实现的退出码原样返回"
    assert payload["app_main"] is None, "分流后不许再跑正常桌宠 main"
    assert payload["qt"] == [], f"卸载清理路径不该加载 Qt：{payload['qt']}"
    assert payload["mark"] == "called"


def test_uninstall_cleanup_main_reports_steps_and_records_failure(tmp_path, monkeypatch, capsys) -> None:
    """清理入口输出逐步结果、失败留档，并按失败返回非零退出码。"""
    config = Config(base=tmp_path)
    monkeypatch.setattr(
        uninstall_cleanup,
        "run_uninstall_cleanup",
        lambda cfg: {"autostart": True, "claude_hooks": "skipped", "dsh_bridge": False},
    )

    exit_code = uninstall_cleanup.main(config=config)
    output = capsys.readouterr().out

    assert exit_code == 1
    for token in ("autostart", "claude_hooks", "dsh_bridge", "skipped"):
        assert token in output
    record = (config.dir / uninstall_cleanup.RESULT_LOG_NAME).read_text(encoding="utf-8")
    assert "dsh_bridge" in record


def test_uninstall_cleanup_main_returns_zero_when_no_step_failed(tmp_path, monkeypatch, capsys) -> None:
    config = Config(base=tmp_path)
    monkeypatch.setattr(
        uninstall_cleanup,
        "run_uninstall_cleanup",
        lambda cfg: {"autostart": True, "claude_hooks": "skipped", "dsh_bridge": True},
    )

    assert uninstall_cleanup.main(config=config) == 0
    assert "dsh_bridge" in capsys.readouterr().out


def test_uninstall_cleanup_main_reports_crash_and_records_it(tmp_path, monkeypatch, capsys) -> None:
    """清理实现抛异常时也要有可诊断结果与非零退出码，不能只留 traceback。"""
    config = Config(base=tmp_path)

    def boom(cfg):
        raise RuntimeError("bridge uninstall exploded")

    monkeypatch.setattr(uninstall_cleanup, "run_uninstall_cleanup", boom)

    assert uninstall_cleanup.main(config=config) == 1
    output = capsys.readouterr().out
    assert "bridge uninstall exploded" in output
    record = (config.dir / uninstall_cleanup.RESULT_LOG_NAME).read_text(encoding="utf-8")
    assert "bridge uninstall exploded" in record


def test_uninstall_cleanup_main_reports_unreadable_config(monkeypatch, capsys) -> None:
    """配置读不出来时也要有结果与非零退出码（卸载流程不该被 traceback 打断）。"""
    import pet.config as config_mod

    def boom(*args, **kwargs):
        raise RuntimeError("config dir unreadable")

    monkeypatch.setattr(config_mod, "Config", boom)

    assert uninstall_cleanup.main() == 1
    assert "config dir unreadable" in capsys.readouterr().out


def test_iss_uninstall_runs_cleanup_before_removing_files() -> None:
    iss = (ROOT / "packaging" / "dsh-pet.iss").read_text(encoding="utf-8")
    # 只看生效的指令：注释里会提到旧写法，不能拿来判定行为
    live = "\n".join(
        line for line in iss.splitlines() if line.strip() and not line.strip().startswith(";")
    )

    assert "CurUninstallStepChanged" in live
    assert "usUninstall" in live, "清理必须挂在卸载步骤（删除文件之前）"
    assert "--uninstall-cleanup" in live
    assert "{#MyAppExeName}" in live
    assert "ResultCode" in live and "Log(" in live, "清理失败必须有退出码记录"
    assert "SuppressibleMsgBox" in live, "交互式卸载里失败的清理要能被用户看到"
    # 按名字通配删除 `_MEI*` 无法保证归属：必须去掉（可能是别的应用的解包目录）
    assert "[UninstallDelete]" not in live
    assert "_MEI*" not in live
    # .iss 是 UTF-8 无 BOM（Inno 按 ANSI 读取）：字符串字面量必须保持 ASCII，
    # 否则用户看到的提示与卸载日志会变成乱码。中文说明留在注释里。
    code_section = iss[iss.index("\n[Code]") :]
    _code_without_comments(code_section).encode("ascii")
    # Pascal 的 `{ }` 注释不嵌套：注释里再写 `{`（例如把常量名写进注释）会让注释
    # 提前结束，后面的字变成代码 -> 编译失败。
    assert re.search(r"\{[^{}]*\{", code_section) is None, "Pascal 注释里不能再出现 `{`"


def _code_without_comments(text: str) -> str:
    """去掉 Pascal 的 `{ 注释 }`（单引号串内的花括号不算注释）。"""
    out: list[str] = []
    in_string = False
    in_comment = False
    for char in text:
        if in_comment:
            in_comment = char != "}"
            continue
        if in_string:
            out.append(char)
            in_string = char != "'"
            continue
        if char == "{":
            in_comment = True
            continue
        if char == "'":
            in_string = True
        out.append(char)
    return "".join(out)


def test_shared_bridge_protected_when_another_variant_uses_agent(tmp_path) -> None:
    """跨变体保护在 slot 档案结构下依然成立：别的变体在用就不许删共享桥接/hooks。"""
    ours = tmp_path / "dsh-pet-standalone-webm-chat"
    ours.mkdir()
    (ours / "config.json").write_text(
        json.dumps({"agent_link": {"dsh": False, "claude": False}}), encoding="utf-8"
    )
    (ours / "config-slot-0.json").write_text(json.dumps({"character": "shenshen"}), encoding="utf-8")
    (ours / "config-slot-2.json").write_text(json.dumps({"scale": 0.8}), encoding="utf-8")
    peer = tmp_path / "dsh-pet-standalone-webm"
    peer.mkdir()
    (peer / "config.json").write_text(
        json.dumps({"agent_link": {"dsh": True, "claude": False}}), encoding="utf-8"
    )
    config = Config(base=tmp_path)

    assert uninstall_cleanup.other_instances_use_agent(config, "dsh") is True
    assert uninstall_cleanup.other_instances_use_agent(config, "claude") is False

    # 没有任何变体在用 → 不保护，照常执行卸载
    (peer / "config.json").write_text(
        json.dumps({"agent_link": {"dsh": False, "claude": False}}), encoding="utf-8"
    )
    assert uninstall_cleanup.other_instances_use_agent(config, "dsh") is False
