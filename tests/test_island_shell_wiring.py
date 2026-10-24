# -*- coding: utf-8 -*-
"""灵动岛 AppShell 接线测试：碰撞体生命周期/挂起、no-chat 变体守卫。

审查补票（P0-1/P0-2/P1-6）：碰撞体重开必须换新 session；pet.chat 被排除的
打包变体不能崩；碰撞体随桌宠可见性挂起。
"""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from pet.app import AppShell
from pet.chat.service import ChatService
from pet.config import Config


def _qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def _make_shell(tmp_path: Path, **island_cfg) -> AppShell:
    """最小 AppShell 桩：__new__ 绕过完整初始化，只接灵动岛链路。"""
    cfg = Config(base=tmp_path)
    cfg.set("dynamic_island", {
        "enabled": True, "x": 400, "y": 300,
        **island_cfg,
    })
    shell = AppShell.__new__(AppShell)
    shell.config = cfg
    shell.island = None
    shell.island_collision = None
    shell.instance = None
    shell._instances = []
    return shell


def _teardown_shell(shell) -> None:
    try:
        body = getattr(shell, "island_collision", None)
        if body is not None:
            body.stop()
    finally:
        island = getattr(shell, "island", None)
        if island is not None:
            island.hide()
            island.deleteLater()
        ChatService.unregister_global_finished(shell._on_global_chat_finished)
        # 4.4b：collision_ipc 会话已随退役层删除，无需再收口
        QApplication.processEvents()


def test_shell_creates_island_and_local_collision_body(tmp_path):
    """接线全景：岛创建、本进程碰撞体启动、几何/可见性回调接好。"""
    app = _qapp()
    shell = _make_shell(tmp_path)
    try:
        shell._sync_dynamic_island()
        island = shell.island
        body = shell.island_collision
        assert island is not None and island.isVisible()
        assert body is not None and body._running is True
        # 几何变化钩子与可见性回调都指向碰撞体
        assert island.on_geometry_changed == body.submit
        assert island.on_pet_visibility_changed == body.set_own_pet_visible
        # pets_provider 返回本进程全部桌宠窗口（无窗时为空）
        assert body._pets_provider() == []
    finally:
        _teardown_shell(shell)
        app.processEvents()


def test_collision_body_restart_after_disable(tmp_path):
    """关→开果冻墙：本地碰撞体停/开都干净（无定时器、无 IPC session 语义）。"""
    app = _qapp()
    shell = _make_shell(tmp_path)
    try:
        shell._sync_dynamic_island()
        body = shell.island_collision
        # 关掉碰撞（设置里关果冻墙）
        cfg = dict(shell.config.get("dynamic_island"))
        cfg["collision_enabled"] = False
        shell._sync_island_collision(cfg)
        assert body._running is False
        # 再打开 → 重新运行
        cfg["collision_enabled"] = True
        shell._sync_island_collision(cfg)
        assert body._running is True
    finally:
        _teardown_shell(shell)
        app.processEvents()


def test_island_survives_no_chat_packaging_variant(tmp_path, monkeypatch):
    """P0-2 回归：pet.chat 被排除的打包变体里创建灵动岛不得抛异常。"""
    app = _qapp()
    monkeypatch.setitem(sys.modules, "pet.chat.service", None)  # 模拟变体排除
    shell = _make_shell(tmp_path)
    try:
        shell._sync_dynamic_island()  # 不应抛 ModuleNotFoundError
        assert shell.island is not None
        # 未注册全局订阅（无 chat 可订）
        assert shell._on_global_chat_finished not in ChatService._global_finished_listeners
    finally:
        _teardown_shell(shell)
        app.processEvents()


def test_quiet_balance_refresh_paths(tmp_path, monkeypatch):
    """卡片展开静默刷新：无 Key 明示 / 缓存命中直接用 / 后台查询只更新岛。"""
    import hashlib
    import time as _time
    from types import SimpleNamespace

    app = _qapp()
    shell = _make_shell(tmp_path)
    shell._balance_busy = False
    shell._balance_cache = None
    shell._balance_cache_path = Path(shell.config.dir) / "balance_cache.json"
    try:
        shell._sync_dynamic_island()
        island = shell.island

        provider = SimpleNamespace(id="p", base_url="http://x", api_key="", verify_ssl=True)
        monkeypatch.setattr(shell.config, "chat_settings",
                            lambda: SimpleNamespace(active_config=provider))
        monkeypatch.setattr(shell.config, "resolve_api_key", lambda _p: "")

        # 1) 未配置 API Key → 明确提示，不再停留 "余额 --"
        island.expand_card()  # card_expanded → _quiet_balance_refresh
        assert island._balance_text.startswith("未配置 API Key")

        # 2) 有 Key + 新鲜内存缓存 → 直接命中，不起后台查询
        provider.api_key = "sk-test"
        monkeypatch.setattr(shell.config, "resolve_api_key", lambda _p: "sk-test")
        digest = hashlib.sha256(b"sk-test").hexdigest()[:12]
        provider_key = "|".join(["p", "http://x", digest])
        shell._balance_cache = (_time.monotonic(), {"text": "余额 ¥9.9", "info": {}}, provider_key)
        shell._quiet_balance_refresh()
        assert island._balance_text == "余额 ¥9.9"

        # 3) 无缓存 → 后台查询：结果回来只更新岛卡片（不冒泡/不播动画）
        island._finish_animations()  # 展开动画落定（动画表停），隔离基线
        assert not island._anim_timer.isActive()
        shell._balance_cache = None
        shell._quiet_balance_last = None

        def fake_worker(bridge, base_url, api_key, verify_ssl, provider_key='', **_kw):
            bridge.done.emit(True, {"text": "余额 ¥8.8", "info": {}})

        monkeypatch.setattr(shell, "_balance_worker", fake_worker)
        shell._quiet_balance_refresh()
        for _ in range(50):
            QApplication.processEvents()
            _time.sleep(0.02)
            if island._balance_text == "余额 ¥8.8":
                break
        assert island._balance_text == "余额 ¥8.8"
        assert not island._anim_timer.isActive()  # 静默路径不播动画
    finally:
        shell._balance_busy = False
        _teardown_shell(shell)
        app.processEvents()


def test_toggle_pet_from_island_routes_to_overlay_shell():
    """GPT 审查阻断：overlay 拓扑 instances[].win 恒为 None，岛单击显隐
    必须路由到 OverlayShell._toggle_pet_visible（否则岛永远调不动桌宠）。"""
    shell = AppShell.__new__(AppShell)
    calls: list = []

    class _OverlayShell:
        def _toggle_pet_visible(self):
            calls.append(1)

    shell._overlay_shell = _OverlayShell()
    shell._instances = []  # overlay 拓扑：win 全 None，旧路径必静默失效
    shell.island = None
    AppShell._toggle_pet_from_island(shell)
    assert calls == [1]


def test_aggregate_pet_visible_reads_overlay_window():
    """聚合可见态在 overlay 拓扑读唯一合成窗（不再恒 False）。"""
    import types

    shell = AppShell.__new__(AppShell)
    overlay = types.SimpleNamespace(
        overlay=types.SimpleNamespace(isVisible=lambda: True))
    shell._overlay_shell = overlay
    shell._instances = []
    assert AppShell._aggregate_pet_visible(shell) is True
    overlay.overlay = types.SimpleNamespace(isVisible=lambda: False)
    assert AppShell._aggregate_pet_visible(shell) is False


def test_aggregate_pet_visible_accounts_for_individually_hidden_sprites(tmp_path):
    """逐只藏光 = 聚合不可见：合成窗常驻可见，判据必须落到 sprite 上。

    合成窗是常驻的透明全屏窗，逐只显隐（托盘「显示这只」/ 逐只菜单的
    ``set_sprite_visible``）只翻 ``sprite.visible``、不动窗口。只看
    ``overlay.isVisible()`` 的话，用户逐只藏光所有桌宠之后岛仍认为桌宠可见：
    快速对话锚到一只看不见的 sprite，回复到达也不再弹岛气泡。主宠与子宠同等计入。
    """
    from tests.test_overlay_spawn import _make_persistent_shell

    shell = AppShell.__new__(AppShell)
    shell._instances = []
    overlay, _made = _make_persistent_shell(tmp_path / "overlay")
    shell._overlay_shell = overlay
    try:
        overlay.overlay.show()
        _qapp().processEvents()
        assert overlay.overlay.isVisible(), "前提：合成窗本身可见"
        assert AppShell._aggregate_pet_visible(shell) is True

        overlay.set_sprite_visible(overlay.sprite, False)      # 主宠逐只隐藏
        assert overlay.overlay.isVisible(), "前提：逐只隐藏不动合成窗"
        assert AppShell._aggregate_pet_visible(shell) is False

        overlay.set_sprite_visible(overlay.sprite, True)       # 恢复 → 聚合回来
        assert AppShell._aggregate_pet_visible(shell) is True

        # 子宠同等计入：任一 sprite 可见即聚合可见
        overlay.spawn_pet()
        spawned = overlay._spawned[0]
        overlay.set_sprite_visible(overlay.sprite, False)
        assert AppShell._aggregate_pet_visible(shell) is True
        overlay.set_sprite_visible(spawned, False)
        assert AppShell._aggregate_pet_visible(shell) is False

        # 整窗隐藏兜底不变：窗不可见时无论 sprite 如何都算不可见
        overlay.set_sprite_visible(spawned, True)
        overlay.overlay.hide()
        assert AppShell._aggregate_pet_visible(shell) is False
    finally:
        overlay._delete_runtime_marker()
        QApplication.processEvents()


# ---------------------------------------------------------------- overlay 岛墙开关（G1）
class _IslandOverlayHost:
    """overlay 拓扑最小宿主：真实 attach_island + 真实 SpriteCollisionWorld。

    ``AppShell._sync_dynamic_island`` 的 overlay 分支只碰四个面：``attach_island``、
    ``overlay``（桥的原点来源）、``icon_pixmap``（岛头像 provider 兜底）、``driver``
    （tick 档位的 kinetic 去路，可为 None = 未起 tick）。这里给足这四个面，
    ``attach_island`` 直接用 ``OverlayShell`` 的真实实现（不是记录入参的假件），
    碰撞世界也是真实对象——墙成员的注册/注销走真实产品路径。

    多屏宿主面（``_main_host`` / ``host_for_screen``）按"只有一块屏"的最小形态补齐：
    岛墙挂到岛当前所在屏的宿主，本桩里那块屏就是唯一一块。岛换屏必然经
    ``_rebind_island_host``（桥的 ``on_sync`` 挂点），故它和 ``_island_screen_host``
    一样必须取真实实现——本桩的 ``host_for_screen`` 恒回同一宿主，重绑是 no-op。
    """

    def __init__(self):
        import types

        from pet.overlay_shell import OverlayShell
        from pet.overlay_window import OverlayWindow
        from pet.sprite_collision import SpriteCollisionWorld

        self.overlay = OverlayWindow()
        self.collision = SpriteCollisionWorld()
        self._sound = None
        self.driver = None
        self.island_bridge = None
        self._island = None
        self._island_host = None
        host = types.SimpleNamespace(
            screen=None, overlay=self.overlay, collision=self.collision,
            is_main=True, probe=None, throw_egg=None)
        self._hosts = [host]
        self.attach_island = OverlayShell.attach_island.__get__(self)
        self._island_screen_host = OverlayShell._island_screen_host.__get__(self)
        self._rebind_island_host = OverlayShell._rebind_island_host.__get__(self)

    @property
    def _main_host(self):
        return self._hosts[0]

    def host_for_screen(self, screen):
        # 单屏最小形态：这台"机器"只有一块屏，岛也在这块屏上
        return self._hosts[0]

    def icon_pixmap(self, _size):
        return None


def _make_overlay_shell(tmp_path, **island_cfg):
    """overlay 拓扑 AppShell 桩（真实岛 + 真实世界 + 真实 attach_island）。"""
    cfg = Config(base=tmp_path)
    cfg.set("dynamic_island", {
        "enabled": True, "x": 400, "y": 300,
        **island_cfg,
    })
    shell = AppShell.__new__(AppShell)
    shell.config = cfg
    shell.island = None
    shell.island_collision = None
    shell.instance = None
    shell._instances = []
    host = _IslandOverlayHost()
    shell._overlay_shell = host
    return shell, host


def _teardown_overlay_shell(shell, host) -> None:
    bridge = getattr(host, "island_bridge", None)
    if bridge is not None:
        bridge.close()
        host.island_bridge = None
    island = getattr(shell, "island", None)
    if island is not None and hasattr(island, "_finish_animations"):
        island._finish_animations()
    host.overlay.close()
    _teardown_shell(shell)


def test_overlay_island_wall_follows_collision_switch(tmp_path):
    """G1：岛显示时关掉「岛碰撞」不得挂墙，开/关来回切必须撤墙且不留残留。"""
    from pet import collision as collision_mod

    app = _qapp()
    shell, host = _make_overlay_shell(tmp_path, collision_enabled=False)
    try:
        shell._sync_dynamic_island()
        island = shell.island
        assert island is not None and island.isVisible()  # 岛本体不受该开关影响
        assert host.island_bridge is None  # 关着：不建桥
        assert collision_mod.ISLAND_MEMBER_ID not in host.collision._static_members
        assert host.collision._listeners == []
        # 关着时岛 hide/show 不得凭空注册（没有桥可复活墙）
        island.hide()
        island.show()
        assert collision_mod.ISLAND_MEMBER_ID not in host.collision._static_members

        # 开：真实桥把真实岛几何注册进真实世界
        cfg = dict(shell.config.get("dynamic_island"))
        cfg["collision_enabled"] = True
        shell.config.set("dynamic_island", cfg)
        shell._sync_dynamic_island()
        bridge = host.island_bridge
        assert bridge is not None
        assert island.on_geometry_changed == bridge.sync_geometry  # 几何回调归桥
        g = island.geometry()
        origin = host.overlay.geometry().topLeft()
        assert host.collision._static_members.get(collision_mod.ISLAND_MEMBER_ID) == (
            g.x() - origin.x(), g.y() - origin.y(), g.width(), g.height())
        assert len(host.collision._listeners) == 1  # 重挂不叠 listener

        # 再关：撤墙 + 桥摘净（切开关不留残留）
        cfg["collision_enabled"] = False
        shell.config.set("dynamic_island", cfg)
        shell._sync_dynamic_island()
        assert collision_mod.ISLAND_MEMBER_ID not in host.collision._static_members
        assert host.island_bridge is None
        assert host.collision._listeners == []
        assert island.on_geometry_changed is None
        island.bump(1.0, 0.0, -1.0)  # 岛自身反馈面照常可用
    finally:
        _teardown_overlay_shell(shell, host)
        app.processEvents()


def test_overlay_island_wall_defaults_on_without_key(tmp_path):
    """老配置缺 ``dynamic_island.collision_enabled`` 键 → 按旧契约默认挂墙。"""
    from pet import collision as collision_mod

    app = _qapp()
    shell, host = _make_overlay_shell(tmp_path)
    try:
        # 绕过 Config 清洗，模拟历史配置文件里没有这个键
        shell.config.data["dynamic_island"] = {"enabled": True, "x": 400, "y": 300}
        shell._sync_dynamic_island()
        assert host.island_bridge is not None
        assert collision_mod.ISLAND_MEMBER_ID in host.collision._static_members
    finally:
        _teardown_overlay_shell(shell, host)
        app.processEvents()


def test_overlay_island_wall_withdrawn_on_island_hide(tmp_path):
    """开着开关时：岛 hide 撤墙 / show 复墙（桥既有语义，本修复不得破坏）。"""
    from pet import collision as collision_mod

    app = _qapp()
    shell, host = _make_overlay_shell(tmp_path, collision_enabled=True)
    try:
        shell._sync_dynamic_island()
        island = shell.island
        assert collision_mod.ISLAND_MEMBER_ID in host.collision._static_members
        island.hide()
        assert collision_mod.ISLAND_MEMBER_ID not in host.collision._static_members
        island.show()
        assert collision_mod.ISLAND_MEMBER_ID in host.collision._static_members
    finally:
        _teardown_overlay_shell(shell, host)
        app.processEvents()


# ---------------------------------------------------------------- 岛拖拽唤醒 tick（M-1 kinetic）
class _Clock:
    """可注入假钟（tick 档位判定用；不 sleep 赌时序）。"""

    def __init__(self, now=1000.0):
        self.now = float(now)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)
        return self.now


def _drag_event(kind, global_pos, buttons=None):
    """合成鼠标事件（只在本用例内局部 import Qt）。"""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    if buttons is None:
        buttons = Qt.MouseButton.LeftButton
    return QMouseEvent(kind, QPointF(global_pos), QPointF(global_pos),
                       Qt.MouseButton.LeftButton, buttons,
                       Qt.KeyboardModifier.NoModifier)


def test_overlay_island_drag_wakes_tick_tier(tmp_path):
    """岛拖拽必须把 tick 档位同步唤回活跃档（M-1 kinetic）。

    桌宠静止时 tick 会降到 T2（250ms 心跳，基本不跑仿真）；岛拖拽是几何回调
    驱动的用户输入，不经过 overlay 的鼠标事件（岛是独立顶层窗），没有这条
    kinetic 通道就没人告诉 tick「有活动」——快速甩岛过鱼可能整个手势期间一次
    碰撞结算都没发生（用户反馈「拖岛撞鱼没反馈」的档位侧成因）。
    """
    from PySide6.QtCore import QEvent, QPoint

    from pet.tick_driver import TickDriver
    from pet.tick_governor import TIER_ACTIVE, TIER_IDLE_STILL

    app = _qapp()
    shell, host = _make_overlay_shell(tmp_path)
    clock = _Clock()
    driver = TickDriver(clock=clock)
    host.driver = driver
    try:
        shell._sync_dynamic_island()
        island = shell.island
        assert host.island_bridge is not None, "岛桥未接线（本用例前提）"
        host.overlay.show()  # 可见：静默后降到 T2（不可见会降到 T3）
        driver.attach(host.overlay)

        clock.advance(1.0)
        driver.on_tick(1 / 60)  # 静默计时起算
        clock.advance(1.0)
        driver.on_tick(1 / 60)  # 连续静默 > DOWNGRADE_HOLD_MS → T2
        assert driver.applied_tier == TIER_IDLE_STILL, "基线：静止桌宠应已降档"

        press = island.pos() + QPoint(60, 20)
        island.mousePressEvent(_drag_event(QEvent.Type.MouseButtonPress, press))
        island.mouseMoveEvent(_drag_event(
            QEvent.Type.MouseMove, press + QPoint(30, 0)))
        assert island._dragging, "拖拽未起手"

        assert driver.applied_tier == TIER_ACTIVE, \
            "岛拖拽未唤醒 tick 档位（甩岛过鱼不结算）"
    finally:
        driver.stop()
        _teardown_overlay_shell(shell, host)
        app.processEvents()
