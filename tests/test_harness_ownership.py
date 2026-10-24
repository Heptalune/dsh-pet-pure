# -*- coding: utf-8 -*-
"""dsh web 自拉起的门控与归属收口（K1 门控收紧 / K2 退出收口）。

背景（实机调查 2026-09-27）：桌宠会拉起 ``dsh web --host 127.0.0.1 --port 38080
--no-open`` 的 node.exe（实测常驻 41.9MB）。两处缺口：

1. **门控太宽**：自动拉起只看 ``enable_chat and harness_autostart``，不看
   ``agent_link.dsh``——DSH 联动没开时那个服务没有任何消费者（桥接插件不装、
   DshMonitor（桥目录唯一读方）不跑），纯烧内存；
2. **退出无收口**：``_spawn`` 用 CREATE_NO_WINDOW 起进程，父死子不死（实机确认
   桌宠退出后 node 还活着），而 ``_on_about_to_quit`` 里没有任何 harness 调用。

本文件是这两处的守卫：门控（联动关不拉、联动开才拉）、收口（只杀自己拉起的
那一个，pid 登记 + 命令行复核双重复核）、保守（非自拉起 / 复核失败 / 关机注销
路径一律不碰）。

OS 边界全部打桩（绝不在测试里真的起/杀进程）：Popen、进程存活探活、命令行读取、
进程树终止。
"""
from __future__ import annotations

import os
import threading
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

import pet.app as app_mod
from pet import harness_launcher as hl
from pet.config import Config

# 本机实测的命令形态：``which("dsh")`` 解析到 npm 的 .cmd shim，``_wrap_cmd`` 把它
# 包成 ``cmd.exe /c <...>\dsh.CMD web``——直接子进程的镜像是 cmd.exe，真正持有
# 监听 socket 的是它的 node 子进程。
_DSH_CMD_LINE = (
    r'"C:\Windows\system32\cmd.exe" /c C:\Users\me\AppData\Roaming\npm\dsh.CMD '
    "web --host 127.0.0.1 --port 38080 --no-open"
)
_CMD_IMAGE = r"C:\Windows\system32\cmd.exe"
# 用户自己在终端跑的实例：命令行同样含 dsh + web，但不在我们的登记表里。
_USER_CMD_LINE = (
    r'"D:\NODEJS\node.exe" C:\Users\me\AppData\Roaming\npm'
    r"\node_modules\@deepseek-ai\dsh\lib\bin.js web --host 127.0.0.1 --port 38080"
)
_NODE_IMAGE = r"D:\NODEJS\node.exe"


def _qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _clean_self_launched_registry():
    """自拉起登记表是模块级状态：逐用例清空（退出收口只认它，串味会误杀）。"""
    hl._SELF_LAUNCHED_PIDS.clear()
    hl._SELF_LAUNCHED_KIND.clear()
    hl._LAUNCHED_CHILDREN.clear()
    yield
    hl._SELF_LAUNCHED_PIDS.clear()
    hl._SELF_LAUNCHED_KIND.clear()
    hl._LAUNCHED_CHILDREN.clear()


@pytest.fixture(autouse=True)
def _no_desktop_boundary(monkeypatch):
    """本文件全部用例钉 web 路径：桌面端安装 / 运行探测是机器相关的 OS 边界。

    开发机上装了**且正在跑**桌面端时，``auto`` 会解析成 desktop（单实例 → 直接
    返回 already），本文件要测的 web 自启链路就一条都测不到。
    """
    monkeypatch.setattr(hl, "desktop_install_path", lambda: None)
    monkeypatch.setattr(hl, "desktop_process_running", lambda: False)


class _FakeProc:
    """subprocess.Popen 替身：暴露 pid 与存活态（``_exit`` 非 None = 已退出）。"""

    def __init__(self, pid: int, exit_code=None):
        self.pid = pid
        self._exit = exit_code

    def poll(self):
        return self._exit


def _register_live_child(pid: int = 4242) -> _FakeProc:
    """登记一个**仍活着**的自拉起子进程（G3 修复后的收口身份门槛：无活体 Popen 不下刀）。"""
    proc = _FakeProc(pid)
    hl._LAUNCHED_CHILDREN.append(proc)
    return proc


def _stub_ownership(monkeypatch, *, alive_pid: int,
                    cmdline: str | None = _DSH_CMD_LINE,
                    image: str | None = _CMD_IMAGE) -> list[int]:
    """把收口路径的 OS 边界打桩，返回「被终止的 pid」记录表。

    ``_terminate_process_tree`` 是破坏性边界：任何测试都不得真的终止进程。
    打桩语义 = 终止**成功**（返回 True）且目标随后不再存活。
    """
    terminated: list[int] = []
    state = {"alive": True}

    def _terminate(pid, proc=None) -> bool:
        terminated.append(pid)
        state["alive"] = False
        # 终止成功的替身必须让对应假子进程同步「死亡」——产品的终止后确认
        # 已改用子进程句柄 poll()（R2 修复），只翻 OS 探活无法走到销登记分支。
        for proc in hl._LAUNCHED_CHILDREN:
            if getattr(proc, "pid", None) == pid:
                proc._exit = 0
        return True

    monkeypatch.setattr(hl, "is_running_pid",
                        lambda pid: pid == alive_pid and state["alive"])
    monkeypatch.setattr(hl, "process_command_line", lambda pid: cmdline)
    monkeypatch.setattr(hl, "_pid_image_path", lambda pid: image)
    monkeypatch.setattr(hl, "_terminate_process_tree", _terminate)
    return terminated


def _wait_for(predicate, timeout: float = 10.0) -> bool:
    """有界轮询（时序纪律：轮询状态 + 宽预算，不赌固定 sleep）。

    自启走的是**真线程**（产品代码里就是 ``threading.Thread(...).start()``）：
    绝不打桩 ``threading.Thread`` 本身——那会连带劫持同进程里所有别的线程
    （实测：DSH 监视器的 ``_work_loop`` 被就地执行，一个无限等待循环直接把
    测试挂死）。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


_SPAWN_COMMAND = [
    "cmd.exe", "/c", r"C:\Users\me\AppData\Roaming\npm\dsh.CMD",
    "web", "--host", "127.0.0.1", "--port", "38080", "--no-open",
]


@pytest.fixture
def spawn_probe(monkeypatch) -> list[list[str]]:
    """自动拉起链的可执行边界：Popen 记录命令行（并回一个 pid）。

    替身同时补挂 ``_LAUNCHED_CHILDREN``：产品 ``_spawn`` 只登记真
    ``subprocess.Popen`` 实例，替身需自行模拟这一副作用（G3 修复后，
    收口路径以「活体子进程句柄」为身份门槛）。
    """
    commands: list[list[str]] = []

    def fake_popen(command, **kwargs):
        commands.append(list(command))
        proc = _FakeProc(4242)
        hl._LAUNCHED_CHILDREN.append(proc)
        hl._record_self_launched(4242)  # 两条登记同进同出（产品契约已并入同一分支）
        return proc

    monkeypatch.setattr(hl, "is_running", lambda port=None: False)
    monkeypatch.setattr(hl, "_find_launch_command", lambda port=None: list(_SPAWN_COMMAND))
    monkeypatch.setattr(hl.subprocess, "Popen", fake_popen)
    return commands


def _gate_shell(tmp_path, *, enable_chat: bool = True, autostart: bool = True,
                dsh_link: bool = False):
    """只带门控所需字段的 AppShell 桩（真 Config + 真门方法）。"""
    cfg = Config(base=tmp_path)
    cfg.set("harness_autostart", autostart)
    agent_cfg = dict(cfg.get("agent_link") or {})
    agent_cfg["dsh"] = dsh_link
    cfg.set("agent_link", agent_cfg)
    shell = app_mod.AppShell.__new__(app_mod.AppShell)  # 绕开重型 __init__
    shell._enable_chat = enable_chat
    shell.config = cfg
    return shell


def _real_shell(tmp_path, monkeypatch, *, dsh_link: bool, autostart: bool = True,
                enable_chat: bool = True) -> app_mod.AppShell:
    """真 AppShell（真 Config + 真门方法），收口路径可直接调 ``_on_about_to_quit``。"""
    # 联动开着时 DshMonitor（单读方）会真起 3s 端口探活：只读探测也不许碰本机
    # 真实端口（用户机器上可能真跑着 dsh web）。
    monkeypatch.setattr(hl, "is_running", lambda port=None: False)
    cfg = Config(base=tmp_path)
    cfg.set("harness_autostart", autostart)
    agent_cfg = dict(cfg.get("agent_link") or {})
    agent_cfg["dsh"] = dsh_link
    cfg.set("agent_link", agent_cfg)
    cfg.save()
    shell = app_mod.AppShell(_qapp(), cfg, enable_chat=enable_chat)
    # 与本用例无关的进程级服务同步/启停全部置空（只测 harness 门控与收口）
    shell._apply_balance_timer = lambda: None
    shell._sync_dynamic_island = lambda: None
    shell._sync_todo_service = lambda: None
    shell._sync_chime_service = lambda: None
    shell._sync_festival_service = lambda: None
    return shell


# ---------------------------------------------------------------- K1：门控
def test_autostart_gated_off_when_dsh_link_disabled(tmp_path, spawn_probe):
    """联动（agent_link.dsh）没开：不自动拉起——那个服务没有任何消费者。"""
    shell = _gate_shell(tmp_path, dsh_link=False)
    shell._maybe_autostart_harness()
    assert spawn_probe == [], "联动关闭时不得拉起 dsh web（白烧一个常驻 node）"


def test_autostart_still_gated_by_master_toggles(tmp_path, spawn_probe):
    """旧口径不退化：system_tools 模块未激活 / harness_autostart 任一关着都不拉。

    2026-10-21：第一门从 enable_chat 的历史误绑改为 system_tools 模块激活
    （harness 与 AI 对话无关，模块显隐归功能投影）。冻结快照判负后必须复位，
    否则线程级拉起会漏进后续用例的 probe（实测）。
    """
    from pet import feature_activation as fa
    from pet.features import MIGRATION_BASELINE

    try:
        fa.freeze_snapshot_for_tests(
            [fid for fid in MIGRATION_BASELINE if fid != "system_tools"])
        app_mod.AppShell._maybe_autostart_harness(
            _gate_shell(tmp_path, dsh_link=True))
    finally:
        fa.reset_snapshot_for_tests()
    app_mod.AppShell._maybe_autostart_harness(
        _gate_shell(tmp_path, autostart=False, dsh_link=True))
    assert spawn_probe == []


def test_autostart_launches_and_registers_pid_when_dsh_link_enabled(tmp_path, spawn_probe):
    """联动 + 开关都开：照常静默拉起，并把自拉起的 pid 记进登记表（退出收口的依据）。"""
    shell = _gate_shell(tmp_path, dsh_link=True)
    shell._maybe_autostart_harness()
    assert _wait_for(lambda: hl._SELF_LAUNCHED_PIDS == [4242]), "联动开启时必须照常自动拉起"
    assert len(spawn_probe) == 1
    assert "web" in spawn_probe[0] and "--no-open" in spawn_probe[0]


def test_manual_menu_start_ignores_the_link_gate(tmp_path, monkeypatch, spawn_probe):
    """手动「启动 dsh web 界面 / 启动桌面端界面」不受门控影响：用户明示要开就开。"""
    calls: list[dict] = []
    monkeypatch.setattr(
        hl, "launch_harness",
        lambda *a, **kw: calls.append(kw) or ("already", "http://127.0.0.1:38080"),
    )
    # 该实例的配置：联动关着（按门控不该自动拉起）
    assert _gate_shell(tmp_path, dsh_link=False)._dsh_tracker_wanted() is False
    hl.launch_harness_gui(SimpleNamespace(show_bubble=lambda *a, **kw: None), action="start")
    # 拉起在 worker 线程执行：轮询等它落地（不赌固定 sleep）
    assert _wait_for(lambda: bool(calls)), "手动菜单启动不得经过 agent_link 门"
    assert calls, "手动菜单启动不得经过 agent_link 门"


# ---------------------------------------------------------------- K2：归属登记
def test_spawn_registers_self_launched_pid(monkeypatch):
    """``_spawn`` 拿到真 Popen 时两条登记同进同出（pid 表 + 活体句柄表）。

    替身必须是 ``subprocess.Popen`` 的实例：产品的两条登记收在 isinstance 分支里
    （R2 修复后的不变量「pid 有登记 ⇒ 句柄有登记」），非实例替身什么都登不上。
    """
    class _RealFakeProc(hl.subprocess.Popen):
        def __init__(self, pid):
            self.pid = pid
            self._exit = None

        def poll(self):
            return self._exit

    monkeypatch.setattr(hl.subprocess, "Popen", lambda command, **kw: _RealFakeProc(4242))
    hl._spawn(["cmd.exe", "/c", "dsh.CMD", "web"])
    assert hl._SELF_LAUNCHED_PIDS == [4242]
    assert any(getattr(p, "pid", None) == 4242 for p in hl._LAUNCHED_CHILDREN), \
        "句柄表与 pid 表必须同进同出"


def test_spawn_without_pid_registers_nothing(monkeypatch):
    """Popen 替身没有可用 pid 时不得写入登记表（宁缺勿滥）。"""
    monkeypatch.setattr(hl.subprocess, "Popen", lambda command, **kw: object())
    hl._spawn(["dsh", "web"])
    assert hl._SELF_LAUNCHED_PIDS == []


# ---------------------------------------------------------------- K2：收口
def test_stop_self_launched_terminates_registered_pid(monkeypatch):
    """自拉起的那一个：活体子进程 + pid 命中 + 命令行复核通过 → 终止并销登记。"""
    hl._SELF_LAUNCHED_PIDS.append(4242)
    _register_live_child(4242)
    killed = _stub_ownership(monkeypatch, alive_pid=4242)
    assert hl.stop_self_launched_harness() == [4242]
    assert killed == [4242]
    assert hl._SELF_LAUNCHED_PIDS == [], "收口成功后不得留在登记表里"


def test_stop_self_launched_ignores_user_started_instance(monkeypatch):
    """用户自己在终端跑的 dsh web 不在登记表里 → 一次终止都不许发生。"""
    killed = _stub_ownership(monkeypatch, alive_pid=13320,
                             cmdline=_USER_CMD_LINE, image=_NODE_IMAGE)
    assert hl._SELF_LAUNCHED_PIDS == []
    assert hl.stop_self_launched_harness() == []
    assert killed == [], "非自拉起的同特征进程绝不能被碰"


@pytest.mark.parametrize("cmdline", [None, "", "python.exe -m http.server"])
def test_stop_self_launched_is_conservative_when_identity_is_unclear(monkeypatch, cmdline):
    """复核不过（读不到命令行 / 特征不符）：保守放过，并保留登记以便下次再判。"""
    hl._SELF_LAUNCHED_PIDS.append(4242)
    _register_live_child(4242)
    killed = _stub_ownership(monkeypatch, alive_pid=4242, cmdline=cmdline, image=None)
    assert hl.stop_self_launched_harness() == []
    assert killed == [], "身份核验不通过时不杀（pid 复用/powershell 超时都走这条）"
    assert hl._SELF_LAUNCHED_PIDS == [4242]


def test_stop_self_launched_forgets_dead_pid(monkeypatch):
    """登记的那个已经自己退出了（OS 探活为死）：忘掉它，不做任何终止。

    活体子进程登记在册（``_child_for`` 能找到），但 OS 探活为死 → 走
    ``is_running_pid`` 分支销登记（R2 语义对齐：该分支重新被本用例守卫）。
    """
    hl._SELF_LAUNCHED_PIDS.append(4242)
    _register_live_child(4242)
    killed = _stub_ownership(monkeypatch, alive_pid=999)
    assert hl.stop_self_launched_harness() == []
    assert killed == []
    assert hl._SELF_LAUNCHED_PIDS == []


# ---------------------------------------------------------------- K2：正常退出
def test_normal_exit_terminates_the_harness_we_launched(tmp_path, monkeypatch):
    """正常退出（aboutToQuit）：本进程拉起的 dsh web 必须被终止。"""
    shell = _real_shell(tmp_path, monkeypatch, dsh_link=True)
    hl._SELF_LAUNCHED_PIDS.append(4242)
    _register_live_child(4242)
    killed = _stub_ownership(monkeypatch, alive_pid=4242)
    shell._on_about_to_quit()
    assert killed == [4242]


def test_autostart_then_normal_exit_stops_what_was_spawned(tmp_path, monkeypatch, spawn_probe):
    """端到端：自动拉起 → 正常退出 → 终止的就是自动拉起的那个 pid。"""
    shell = _real_shell(tmp_path, monkeypatch, dsh_link=True)
    shell._maybe_autostart_harness()
    assert _wait_for(lambda: hl._SELF_LAUNCHED_PIDS == [4242]), "用例前置：自启已落地"
    killed = _stub_ownership(monkeypatch, alive_pid=4242)
    shell._on_about_to_quit()
    assert len(spawn_probe) == 1
    assert killed == [4242]


def test_exit_never_touches_a_harness_we_did_not_launch(tmp_path, monkeypatch):
    """用户手动起的实例（端口在监听、命令行也像 dsh web）：退出时绝不能被自动杀。"""
    shell = _real_shell(tmp_path, monkeypatch, dsh_link=True)
    killed = _stub_ownership(monkeypatch, alive_pid=13320,
                             cmdline=_USER_CMD_LINE, image=_NODE_IMAGE)
    monkeypatch.setattr(hl, "is_running", lambda port=None: True)
    monkeypatch.setattr(hl, "listener_pids", lambda port: [13320])
    shell._on_about_to_quit()
    assert killed == [], "退出收口不得按端口反查去杀别人跑着的实例"


def test_session_end_exit_leaves_harness_alone(tmp_path, monkeypatch):
    """关机/注销路径（issue #111）：不再派生任何进程——收口整段跳过。"""
    shell = _real_shell(tmp_path, monkeypatch, dsh_link=True)
    shell._session_end_done = True
    hl._SELF_LAUNCHED_PIDS.append(4242)
    killed = _stub_ownership(monkeypatch, alive_pid=4242)
    shell._on_about_to_quit()
    assert killed == [], "会话结束路径维持现状（不 taskkill、不起 powershell）"
    assert hl._SELF_LAUNCHED_PIDS == [4242]


# ---------------------------------------------------------------- K2：联动运行期关闭
def test_disabling_dsh_link_stops_the_harness_we_launched(tmp_path, monkeypatch):
    """联动被关掉 → 立刻收掉自拉起的实例（它唯一的消费者就是这条管线）。"""
    shell = _real_shell(tmp_path, monkeypatch, dsh_link=True)
    hl._SELF_LAUNCHED_PIDS.append(4242)
    _register_live_child(4242)
    killed = _stub_ownership(monkeypatch, alive_pid=4242)
    agent_cfg = dict(shell.config.get("agent_link") or {})
    agent_cfg["dsh"] = False
    shell.config.set("agent_link", agent_cfg)
    shell._apply_external_config_change()
    assert _wait_for(lambda: killed == [4242]), "联动关闭后自拉起实例必须被收口（可在线程内完成）"


def test_keeping_dsh_link_on_does_not_stop_the_harness(tmp_path, monkeypatch):
    """联动还开着：配置变更链不许把服务停掉（否则每次保存设置都断一次）。"""
    shell = _real_shell(tmp_path, monkeypatch, dsh_link=True)
    hl._SELF_LAUNCHED_PIDS.append(4242)
    killed = _stub_ownership(monkeypatch, alive_pid=4242)
    shell._apply_external_config_change()
    import threading
    assert _wait_for(lambda: not any(
        t.name == "pet-harness-stop" and t.is_alive() for t in threading.enumerate()),
        timeout=5.0), "收口线程收尾后再断言（不赌时序）"
    assert killed == []
    assert hl._SELF_LAUNCHED_PIDS == [4242]


def test_disabling_link_never_touches_user_started_instance(tmp_path, monkeypatch):
    """联动关闭时同样只认登记表：用户手动起的实例不受影响。"""
    shell = _real_shell(tmp_path, monkeypatch, dsh_link=True)
    killed = _stub_ownership(monkeypatch, alive_pid=13320,
                             cmdline=_USER_CMD_LINE, image=_NODE_IMAGE)
    monkeypatch.setattr(hl, "is_running", lambda port=None: True)
    monkeypatch.setattr(hl, "listener_pids", lambda port: [13320])
    agent_cfg = dict(shell.config.get("agent_link") or {})
    agent_cfg["dsh"] = False
    shell.config.set("agent_link", agent_cfg)
    shell._apply_external_config_change()
    import threading
    assert _wait_for(lambda: not any(
        t.name == "pet-harness-stop" and t.is_alive() for t in threading.enumerate()),
        timeout=5.0), "收口线程收尾后再断言（不赌时序；R2 曾把等待加错到相邻用例）"
    assert killed == []


# ---------------------------------------------------------------- 缺口回归（四方审查 2026-10-01）
def test_stop_self_launched_keeps_pid_when_terminate_fails(monkeypatch):
    """G1：taskkill 失败不得被记成「已终止」——保留登记、不进 terminated 名单。

    回归：``_terminate_process_tree`` 原先对 taskkill 非零返回码只记 warning 就返回，
    调用方无条件销登记+报成功——进程其实还在跑，且永远失去了对它的追踪。
    """
    hl._SELF_LAUNCHED_PIDS.append(4242)
    _register_live_child(4242)
    calls: list[int] = []
    monkeypatch.setattr(hl, "is_running_pid", lambda pid, proc=None: True)
    monkeypatch.setattr(hl, "process_command_line", lambda pid: _DSH_CMD_LINE)
    monkeypatch.setattr(hl, "_pid_image_path", lambda pid: _CMD_IMAGE)
    monkeypatch.setattr(hl, "_terminate_process_tree",
                        lambda pid, proc=None: calls.append(pid) or False)  # 终止失败
    out = hl.stop_self_launched_harness()
    assert calls == [4242], "终止命令必须确实发出过"
    assert out == [], "终止失败不得计入 terminated"
    assert hl._SELF_LAUNCHED_PIDS == [4242], "终止失败必须保留登记（下次收口再试）"


def test_stop_self_launched_never_kills_pid_after_child_exited(monkeypatch):
    """G3：原拉起进程已退出、PID 被系统复用（命令行仍是 dsh web）→ 绝不下刀，只销登记。

    回归：登记表只记 pid，原子进程死亡后 pid 复用给**用户自己的** dsh 实例时，
    命令行复核会通过——会误杀用户实例。活体 Popen 是唯一能证明身份的东西。
    """
    hl._SELF_LAUNCHED_PIDS.append(4242)
    hl._LAUNCHED_CHILDREN.append(_FakeProc(4242, exit_code=0))  # 原进程已死
    killed = _stub_ownership(monkeypatch, alive_pid=4242)  # 4242 活着且是 dsh（被复用）
    out = hl.stop_self_launched_harness()
    assert killed == [], "原进程已死的 pid 绝不可按命令行猜着杀"
    assert out == []
    assert hl._SELF_LAUNCHED_PIDS == [], "身份不可考：销登记（不残留不追踪）"


def test_autostart_rechecks_gate_before_launch(tmp_path, monkeypatch, spawn_probe):
    """G4a：慢探测期间联动被关 → 不得再拉起（拉起窗口期的竞态回归）。"""
    shell = _real_shell(tmp_path, monkeypatch, dsh_link=True)

    def _flip_gate(*args, **kwargs):
        # is_running 探测（慢路径）之后、launch 之前：联动被关掉
        agent_cfg = dict(shell.config.get("agent_link") or {})
        agent_cfg["dsh"] = False
        shell.config.set("agent_link", agent_cfg)
        return False

    monkeypatch.setattr(hl, "is_running", _flip_gate)
    shell._maybe_autostart_harness()
    import threading
    assert _wait_for(lambda: not any(
        t.name == "pet-harness-autostart" and t.is_alive() for t in threading.enumerate()),
        timeout=5.0), "自动拉起线程必须收尾后再断言（不赌时序）"
    assert spawn_probe == [], "探测完成后联动已关：绝不拉起"


def test_autostart_stops_immediately_if_gate_flips_after_launch(tmp_path, monkeypatch):
    """G4b：拉起刚完成联动就被关 → 立刻收口，不留「关了还在跑」的窗口。"""
    shell = _real_shell(tmp_path, monkeypatch, dsh_link=True)
    monkeypatch.setattr(hl, "is_running", lambda port=None: False)
    monkeypatch.setattr(hl, "_find_launch_command", lambda port=None: ["dsh", "web"])
    monkeypatch.setattr(hl.subprocess, "Popen", lambda command, **kw: _FakeProc(4242))
    stops: list[int] = []
    monkeypatch.setattr(hl, "stop_self_launched_harness",
                        lambda: stops.append(1) or [])

    real_launch = hl.launch_harness

    def _launch_then_flip(*args, **kwargs):
        result = real_launch(*args, **kwargs)
        agent_cfg = dict(shell.config.get("agent_link") or {})
        agent_cfg["dsh"] = False
        shell.config.set("agent_link", agent_cfg)
        return result

    monkeypatch.setattr(hl, "launch_harness", _launch_then_flip)
    shell._maybe_autostart_harness()
    assert _wait_for(lambda: stops == [1]), "拉起后联动已关：必须立刻触发一次自拉起收口"


def test_config_change_stop_harness_runs_off_caller_thread(tmp_path, monkeypatch):
    """G2：联动关闭触发的 harness 收口不得在调用线程（GUI 线程）同步执行进程操作。

    回归：进程反查（PowerShell ~10s 超时）+ taskkill（~15s 超时）原先在配置去抖
    回调里同步执行——外部改配置关联动会卡死所有桌宠共用的 GUI 线程。
    """
    shell = _real_shell(tmp_path, monkeypatch, dsh_link=False)
    seen: dict = {}
    monkeypatch.setattr(
        hl, "stop_self_launched_harness",
        lambda: seen.setdefault("thread", __import__("threading").current_thread()) or [],
    )
    shell._apply_external_config_change()
    assert _wait_for(lambda: "thread" in seen), "联动关闭必须触发一次收口"
    import threading
    assert seen["thread"] is not threading.current_thread(), \
        "进程查询/终止必须离开调用线程（GUI 线程不许被秒级阻塞）"


def test_ownership_registry_writes_are_serialized(monkeypatch):
    """G2 并发补丁（gemini R2 发现）：登记表/子进程列表的读写必须过互斥锁。

    G2 把收口放进 daemon 线程后，登记表（``_SELF_LAUNCHED_PIDS``）与子进程列表
    可能被「收口线程 / 自动拉起 worker / 退出主线程」三方并发读写——无锁即竞态。
    测试：主线程持锁时，另一个线程的写登记必须阻塞到锁释放。
    """
    import threading
    entered = threading.Event()
    release = threading.Event()
    done: list[bool] = []
    monkeypatch.setattr(hl, "is_running_pid", lambda pid: False)  # 防误触真进程
    hl._OWNERSHIP_LOCK.acquire()

    def _rec():
        entered.set()
        hl._record_self_launched(4242)
        done.append(True)
        release.set()

    t = threading.Thread(target=_rec, daemon=True)
    t.start()
    try:
        assert entered.wait(2.0)
        assert not done, "锁被持有时，写登记必须阻塞（无锁即竞态）"
    finally:
        hl._OWNERSHIP_LOCK.release()  # 断言失守也不许把锁留给后续用例（挂死比红更难查）
    assert release.wait(2.0) and done
    assert hl._SELF_LAUNCHED_PIDS == [4242]


# ---------------------------------------------------------------- R2 修复批补充覆盖
def test_stop_self_launched_keeps_pid_when_target_survives_terminate(monkeypatch):
    """G1 后半分支（ds/sol 点名补盖）：终止命令成功（True）但目标仍存活 → 保留登记、不计 terminated。"""
    hl._SELF_LAUNCHED_PIDS.append(4242)
    _register_live_child(4242)  # 替身终止后不置死 → 子进程句柄确认「仍存活」
    terminated: list[int] = []
    monkeypatch.setattr(hl, "is_running_pid", lambda pid, proc=None: True)
    monkeypatch.setattr(hl, "process_command_line", lambda pid: _DSH_CMD_LINE)
    monkeypatch.setattr(hl, "_pid_image_path", lambda pid: _CMD_IMAGE)
    monkeypatch.setattr(hl, "_terminate_process_tree",
                        lambda pid, proc=None: terminated.append(pid) or True)  # 成功但进程不死
    out = hl.stop_self_launched_harness()
    assert terminated == [4242]
    assert out == [], "目标仍存活不得计入 terminated"
    assert hl._SELF_LAUNCHED_PIDS == [4242], "目标仍存活必须保留登记（下次收口再试）"


def test_async_stop_aborts_when_link_reenabled(tmp_path, monkeypatch):
    """G2 反向竞态（ds/sol R2 发现）：异步收口执行前联动已重开 → 本次收口作废。"""
    shell = _real_shell(tmp_path, monkeypatch, dsh_link=False)
    calls: list[int] = []
    monkeypatch.setattr(hl, "stop_self_launched_harness", lambda: calls.append(1) or [])
    # 线程入口复核时联动已是「开」：直接调命名方法（确定性，不赌线程时序）
    monkeypatch.setattr(shell, "_dsh_tracker_wanted", lambda: True)
    shell._stop_harness_if_still_unwanted()
    assert calls == [], "联动已重开时，过期的异步收口必须作废"
    monkeypatch.setattr(shell, "_dsh_tracker_wanted", lambda: False)
    shell._stop_harness_if_still_unwanted()
    assert calls == [1], "联动仍关着时，收口必须照常执行"


def test_in_flight_autostart_is_reaped_when_quitting(tmp_path, monkeypatch):
    """R2 第五缺口：拉起窗口期内进程进入退出 → 立刻补偿收口，不留无人收的进程。"""
    shell = _real_shell(tmp_path, monkeypatch, dsh_link=True)
    monkeypatch.setattr(hl, "is_running", lambda port=None: False)
    monkeypatch.setattr(hl, "_find_launch_command", lambda port=None: ["dsh", "web"])
    monkeypatch.setattr(
        hl.subprocess, "Popen",
        lambda command, **kw: _FakeProc(4242))
    stops: list[int] = []
    monkeypatch.setattr(hl, "stop_self_launched_harness", lambda: stops.append(1) or [])

    real_launch = hl.launch_harness

    def _launch_then_mark_quitting(*args, **kwargs):
        result = real_launch(*args, **kwargs)
        shell._quitting = True  # launch 返回前进程进入退出（aboutToQuit 已置标记）
        return result

    monkeypatch.setattr(hl, "launch_harness", _launch_then_mark_quitting)
    shell._maybe_autostart_harness()
    assert _wait_for(lambda: stops == [1]), "退出标记置位后，在途拉起必须立刻补偿收口"


def test_autostart_never_launches_when_already_quitting(tmp_path, monkeypatch, spawn_probe):
    """R2 第五缺口（前置分支）：退出标记已置位 → 自动拉起整段不启动。"""
    shell = _real_shell(tmp_path, monkeypatch, dsh_link=True)
    shell._quitting = True
    shell._maybe_autostart_harness()
    import threading
    assert _wait_for(lambda: not any(
        t.name == "pet-harness-autostart" and t.is_alive() for t in threading.enumerate()),
        timeout=5.0), "worker 收尾后再断言（不赌时序）"
    assert spawn_probe == [], "退出标记置位后绝不再拉起"


def test_session_end_gate_also_marks_quitting(tmp_path, monkeypatch, spawn_probe):
    """R3-gemini P1：会话结束闸门（_mark_session_ending）与退出标记同源。

    只走 aboutToQuit 置位会漏掉「会话结束观察路径直接调 _mark_session_ending」
    的分支——在途 autostart worker 会无视退出继续拉起。修复后：闸门落 = 退出标记落。
    """
    shell = _real_shell(tmp_path, monkeypatch, dsh_link=True)
    shell._mark_session_ending()
    assert getattr(shell, "_quitting", False) is True, \
        "_mark_session_ending 必须同步置 _quitting（否则会话结束路径漏防第五缺口）"
    shell._maybe_autostart_harness()
    assert spawn_probe == [], "会话结束闸门落下后绝不再拉起"


def test_poll_based_confirm_distinguishable_from_pid_probe(monkeypatch):
    """判别性守卫（ds/sol R3）：终止后确认必须用子进程句柄，不是 OS 探活。

    构造僵尸语义：OS 探活恒为「存活」（``is_running_pid`` 对僵尸返回成功），
    但子进程句柄已退出。若产品退回 ``is_running_pid`` 确认，本用例必红
    （会误判「仍存活」而保留登记）；正确实现必须计 terminated 并销登记。
    """
    hl._SELF_LAUNCHED_PIDS.append(4242)
    child = _register_live_child(4242)
    monkeypatch.setattr(hl, "is_running_pid", lambda pid, proc=None: True)  # 僵尸语义：OS 探活恒活
    monkeypatch.setattr(hl, "process_command_line", lambda pid: _DSH_CMD_LINE)
    monkeypatch.setattr(hl, "_pid_image_path", lambda pid: _CMD_IMAGE)

    def _terminate_ok(pid) -> bool:
        child._exit = 0  # 终止成功且子进程真的退出（只是 OS 探活仍说活——僵尸）
        return True

    monkeypatch.setattr(hl, "_terminate_process_tree",
                        lambda pid, proc=None: _terminate_ok(pid))
    out = hl.stop_self_launched_harness()
    assert out == [4242], "句柄已死（即使 OS 探活恒活）必须计 terminated——判别 child.poll() 确认"
    assert hl._SELF_LAUNCHED_PIDS == []


# ---------------------------------------------------------------- 终审修复批（ops5.5/astra）
def test_session_end_done_not_set_by_plain_about_to_quit(tmp_path, monkeypatch):
    """ops5.5 终审 C2：aboutToQuit 兜底 arm 不得置 ``_session_end_done``。

    正常退出（托盘/最后一窗）也会经 SessionWatcher 汇进 ``_on_session_end``——
    那不是「会话结束」。主退出先行置 ``_plain_about_to_quit``（Qt 按连接序调槽），
    ``_on_session_end`` 凭它分流：兜底路径只做冻结/闸门，不置会话结束闩——
    否则 :2137/:2185/:2369 三处会把正常退出误当会话结束跳过收口。
    """
    shell = _real_shell(tmp_path, monkeypatch, dsh_link=True)
    shell._on_about_to_quit()
    shell._on_session_end()  # = watcher arm 的兜底回调（aboutToQuit 路径）
    assert not getattr(shell, "_session_end_done", False), \
        "aboutToQuit 兜底路径不得置 _session_end_done（那是正常退出，不是会话结束）"


def test_session_end_done_set_by_real_session_end(tmp_path, monkeypatch):
    """对照：真会话结束（无 aboutToQuit 先行）→ 必须置 ``_session_end_done``。"""
    shell = _real_shell(tmp_path, monkeypatch, dsh_link=True)
    shell._on_session_end()
    assert getattr(shell, "_session_end_done", False) is True


def test_autostart_aborts_when_quitting_lands_during_probe(tmp_path, monkeypatch, spawn_probe):
    """astra 终审 P1：慢命令解析期间退出标记落地 → launch 中止，绝不 spawn。

    cancel_check 在 ``_find_launch_command`` 之后、``_spawn`` 之前评估——
    没有这道闸，慢探测（npm 最长 15s）期间落地的退出/会话结束标记形同虚设。
    """
    shell = _real_shell(tmp_path, monkeypatch, dsh_link=True)

    def _probe_then_flip(port=None):
        shell._quitting = True  # 慢探测期间 aboutToQuit 落地
        return ["dsh", "web"]

    monkeypatch.setattr(hl, "_find_launch_command", _probe_then_flip)
    status, _url = hl.launch_harness(
        open_browser=False,
        cancel_check=lambda: getattr(shell, "_quitting", False)
        or getattr(shell, "_session_end_done", False),
    )
    assert status == "aborted", "慢探测后退出标记已落：launch 必须中止"
    assert spawn_probe == [], "中止路径绝不 spawn"


def test_terminate_binds_to_child_identity_before_signaling(monkeypatch):
    """astra 终审 P1（身份绑定）：句柄已死 → 直接成功，不向数字 pid 发任何命令。

    并发收口场景：另一方已终止并回收同一 child，数字 pid 可能已被系统复用——
    凭过期检查结果向 pid 发 taskkill/SIGTERM 会打错目标。身份绑定后：
    句柄已死 = 我们的子进程已终止 = 不再发任何信号。
    """
    calls: list = []
    monkeypatch.setattr(hl.subprocess, "run",
                        lambda *a, **kw: calls.append(a) or type("R", (), {"returncode": 0})())
    dead = _FakeProc(4242, exit_code=0)
    assert hl._terminate_process_tree(4242, proc=dead) is True
    assert calls == [], "句柄已死时绝不向数字 pid 发终止命令（taskkill 不许执行）"


def test_stop_self_launched_single_flight(monkeypatch):
    """终审修复（单飞）：一条收口进行中时，第二条直接返回空表、不查杀。"""
    import threading
    hl._SELF_LAUNCHED_PIDS.append(4242)
    _register_live_child(4242)
    gate = threading.Event()
    in_inner = threading.Event()
    terminated: list[int] = []
    monkeypatch.setattr(hl, "is_running_pid", lambda pid: True)
    monkeypatch.setattr(hl, "_pid_image_path", lambda pid: _CMD_IMAGE)

    def _slow_cmdline(pid):
        in_inner.set()
        assert gate.wait(5.0)
        return _DSH_CMD_LINE

    monkeypatch.setattr(hl, "process_command_line", _slow_cmdline)
    monkeypatch.setattr(hl, "_terminate_process_tree",
                        lambda pid, proc=None: terminated.append(pid) or True)

    t = threading.Thread(target=hl.stop_self_launched_harness, daemon=True)
    t.start()
    assert in_inner.wait(2.0), "前置：第一条收口已进入慢查询"
    t0 = time.monotonic()
    out2 = hl.stop_self_launched_harness()
    assert out2 == [] and time.monotonic() - t0 < 1.0, \
        "进行中的收口未走完时，第二条必须立即返回空表（单飞合并）"
    gate.set()
    t.join(timeout=5.0)
    assert terminated == [4242]


# ---------------------------------------------------------------- 双目标自启
def test_autostart_delegates_existing_instance_check_to_launcher(tmp_path, monkeypatch):
    """已有 web 实例在线时，自启也必须把判定交给 launcher——不能在 app 层提前 return。

    回归（2026-10 双目标）：app 层用「web 端口在线就 return」当复用判定，会把
    ``harness_launch_target=desktop`` 的选择一起挡掉（桌面端不听 web 端口，
    本机有 web 实例 ≠ 用户要的桌面端已在跑）。auto 现已解析成 service，本用例
    钉的仍是「判定下沉、一次都不 spawn」这条契约。
    """
    shell = _gate_shell(tmp_path, dsh_link=True)
    launched: list[dict] = []
    monkeypatch.setattr(hl, "is_running", lambda port=None: True)  # 38080 已有实例
    monkeypatch.setattr(
        hl, "launch_harness",
        lambda **kw: launched.append(kw) or ("already", "http://127.0.0.1:38080"),
    )
    shell._maybe_autostart_harness()
    assert _wait_for(lambda: bool(launched)), "已有实例的判定必须交给 launch_harness"
    assert launched[0].get("target") == "auto", "自启必须带上配置里的目标"


def test_autostart_default_target_starts_service_without_gui_or_browser(
        tmp_path, monkeypatch, spawn_probe):
    """默认目标（auto）在装了桌面端的机器上：只拉服务，不拉 GUI、不开浏览器。

    回归（2026-10 语义收紧）：auto 曾解析成 desktop（检测到桌面端安装优先），
    于是每次桌宠启动都把整个桌面端 GUI 拉起来。产品口径：自启只负责把 dsh 服务
    拉起来，要界面必须显式选 web / 桌面端目标。

    消融判别（实测）：把 ``resolve_launch_target`` 的 auto 改回 desktop，本用例
    变红——服务没被拉起，反而是桌面端 ``_spawn_desktop`` 被调用。
    """
    shell = _gate_shell(tmp_path, dsh_link=True)  # 目标取配置默认 auto
    exe = tmp_path / "DeepSeek Harness.exe"
    exe.write_bytes(b"MZ")
    desktop_spawned: list = []
    opened: list[str] = []
    monkeypatch.setattr(hl, "DESKTOP_SUPPORTED", True)
    monkeypatch.setattr(hl, "desktop_install_path", lambda: exe)
    monkeypatch.setattr(hl, "desktop_process_running", lambda: False)
    monkeypatch.setattr(hl, "_spawn_desktop", lambda path: desktop_spawned.append(path))
    monkeypatch.setattr(hl.webbrowser, "open", lambda url: opened.append(url))

    shell._maybe_autostart_harness()
    assert _wait_for(lambda: bool(spawn_probe)), "auto 必须照常把 dsh 服务拉起来"
    assert desktop_spawned == [], "自启不得拉起桌面端 GUI"
    assert opened == [], "自启不得打开浏览器"


def test_autostart_desktop_not_spawned_when_toggle_flipped_during_probe(tmp_path, monkeypatch):
    """探测期间关掉「随桌宠启动」→ 桌面端不得被拉起（走产品的 cancel_check 门）。

    回归（2026-10 双目标）：``_harness_autostart_wanted`` 只在调度时查一次，
    launch 的 cancel_check 原来只认退出/会话结束标记——用户在慢探测（真机是注册表
    枚举）期间关掉开关不算数。desktop 目标下这一格的代价是真弹出一个用户刚说不
    想要的 GUI 窗口。

    本用例走产品真线程 + 真 ``launch_harness``，只打桩 OS 边界（安装探测 / 进程
    枚举 / 拉起），并借「返回后立即收口」那一分支当线程收尾信号（不赌固定 sleep）。

    消融判别：删掉 app 里 cancel_check 的 ``not self._harness_autostart_wanted()``
    一项，本用例必须变红（``_spawn_desktop`` 被调用）。
    """
    shell = _gate_shell(tmp_path, dsh_link=True)
    shell.config.set("harness_launch_target", "desktop")
    exe = tmp_path / "DeepSeek Harness.exe"
    exe.write_bytes(b"MZ")
    spawned: list = []
    done = threading.Event()

    def _probe_then_flip():
        # 慢探测期间用户把开关关掉（这正是 launch_harness 里 cancel_check 的窗口）
        shell.config.set("harness_autostart", False)
        return exe

    monkeypatch.setattr(hl, "DESKTOP_SUPPORTED", True)
    monkeypatch.setattr(hl, "desktop_install_path", _probe_then_flip)
    monkeypatch.setattr(hl, "desktop_process_running", lambda: False)
    monkeypatch.setattr(hl, "_spawn_desktop", lambda path: spawned.append(path))
    # launch 返回后 app 会走「刚拉起又被关 → 立即收口」分支：用它当线程收尾信号
    monkeypatch.setattr(
        hl, "stop_self_launched_harness", lambda: done.set() or [])

    shell._maybe_autostart_harness()
    assert done.wait(timeout=10.0), "自启线程没跑完（探测/中止链路卡住）"
    assert spawned == [], "开关已被关掉：desktop 不得被拉起"


def test_autostart_desktop_spawns_when_toggle_stays_on(tmp_path, monkeypatch):
    """对照：开关一直开着 → desktop 照常拉起（别把门焊死）。

    没有这条，上面那条「不拉起」在 cancel_check 永远返回 True 的实现下也是绿的。
    """
    shell = _gate_shell(tmp_path, dsh_link=True)
    shell.config.set("harness_launch_target", "desktop")
    exe = tmp_path / "DeepSeek Harness.exe"
    exe.write_bytes(b"MZ")
    spawned: list = []

    monkeypatch.setattr(hl, "DESKTOP_SUPPORTED", True)
    monkeypatch.setattr(hl, "desktop_install_path", lambda: exe)
    monkeypatch.setattr(hl, "desktop_process_running", lambda: False)
    monkeypatch.setattr(hl, "_spawn_desktop", lambda path: spawned.append(path))

    shell._maybe_autostart_harness()
    assert _wait_for(lambda: bool(spawned)), "开关开着时 desktop 目标必须被拉起"
    assert spawned == [exe]
