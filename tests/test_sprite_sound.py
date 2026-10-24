# -*- coding: utf-8 -*-
"""SpriteSoundPlayer + shell 音效接线 offscreen 单测（4.1c）。

覆盖：点击音量走 click_sound_volume、碰撞 j 闸门与音量、80ms 节流、
点击/碰撞**各自的**启用门（旧契约两门独立）、门在延迟回调前翻转即不发声、
开关关掉再打开能恢复（门与音源都不被缓存钉死）、解析失败静默降级、
shell 单击路由触发 click_feedback、shell 碰撞监听器挂接。
play_sound 全程打桩，不出声。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from pet import click_sound
from pet.sprite_sound import SpriteSoundPlayer

app = QApplication.instance() or QApplication([])


class _Cfg:
    def __init__(self, values=None):
        self._v = dict(values or {})
        self.dir = None

    def get(self, key, default=None):
        return self._v.get(key, default)


def _pump():
    # 播放已改事件循环下一轮提交（tick 路径零阻塞）：抽干 0ms 定时器
    QApplication.processEvents()
    QApplication.processEvents()


def _player(monkeypatch, values=None, hit_min_dv=100.0, clock=None):
    plays = []
    monkeypatch.setattr(click_sound, "play_sound",
                        lambda path, volume: plays.append(volume) or True)
    monkeypatch.setattr(click_sound, "resolve_click_sound_candidates",
                        lambda pack, data_dir=None: ["/tmp/click.wav"])
    # 先把上一条用例延后提交的那一声抽干再清表：播放走
    # ``QTimer.singleShot(0)`` 且音效池是进程级的，别处（如撞出真碰撞的用例）
    # 排下的播放会落在本用例的第一次 ``_pump`` 里，混进"我的动作响了几声"。
    _pump()
    plays.clear()
    world = SimpleNamespace(hit_min_dv=hit_min_dv)
    player = SpriteSoundPlayer(_Cfg(values), world,
                               clock=clock or (lambda: 1000.0))
    return player, plays


def test_click_volume_from_config(monkeypatch):
    player, plays = _player(monkeypatch, {"click_sound_volume": 0.3})
    player.on_click()
    _pump()
    assert plays == [0.3]


def test_collision_gate_and_volume_from_config(monkeypatch):
    """j 闸门 + 音量取 ``collision_sound_volume`` 缺省（0.70）。

    本次修正显式替换了本用例此前的两档断言（原 ``[0.45]`` / ``[0.45, 0.9]``）：
    轻/重两档是 sprite 架构的新设计，不是旧用户契约，已随实现删除——
    这里是旧契约基准，不代表"除代码回归外行为不变"。
    """
    player, plays = _player(monkeypatch, hit_min_dv=100.0)
    player.on_collision(SimpleNamespace(j=50.0))    # 不到闸门不发声
    _pump()
    assert plays == []
    player.on_collision(SimpleNamespace(j=150.0))   # 过闸门
    _pump()
    assert plays == [0.70]
    player._last_play = 0.0
    player.on_collision(SimpleNamespace(j=250.0))   # 撞击更强，音量不变
    _pump()
    assert plays == [0.70, 0.70]


def test_throttle(monkeypatch):
    now = [1000.0]
    player, plays = _player(monkeypatch, clock=lambda: now[0])
    player.on_click()
    player.on_click()                               # 80ms 内第二次被节流
    _pump()
    assert len(plays) == 1
    now[0] += 0.1
    player.on_click()
    _pump()
    assert len(plays) == 2


def test_click_gate_does_not_silence_collision(monkeypatch):
    """旧契约：点击与碰撞**各一门**（window.py:3510 点击 / :3526 碰撞）。

    本用例此前用 ``click_sound_enabled=False`` 断言"两路都静音"，钉住的正是
    overlay 的单门错语义；现按旧契约改为逐门验证——不是为了让实现过测而降低要求。
    """
    player, plays = _player(monkeypatch, {"click_sound_enabled": False})
    player.on_click()
    _pump()
    assert plays == []                              # 点击门关 → 点击静音
    player._last_play = 0.0
    player.on_collision(SimpleNamespace(j=999.0))
    _pump()
    assert plays == [0.70]                          # 碰撞门仍开 → 照响


def test_collision_gate_does_not_silence_click(monkeypatch):
    player, plays = _player(monkeypatch, {"collision_sound_enabled": False})
    player.on_collision(SimpleNamespace(j=999.0))
    _pump()
    assert plays == []                              # 碰撞门关 → 碰撞静音
    player._last_play = 0.0
    player.on_click()
    _pump()
    assert plays == [0.70]                          # 点击门仍开 → 照响


def test_resolve_failure_degrades(monkeypatch):
    plays = []
    monkeypatch.setattr(click_sound, "play_sound",
                        lambda path, volume: plays.append(volume))
    def boom(pack, data_dir=None):
        raise RuntimeError("no pack")
    monkeypatch.setattr(click_sound, "resolve_click_sound_candidates", boom)
    player = SpriteSoundPlayer(_Cfg(), SimpleNamespace(hit_min_dv=0.0))
    player.on_click()                               # 不抛异常
    player.on_collision(SimpleNamespace(j=1.0))
    _pump()
    assert plays == []


# ---------------------------------------------------------------- 启用门（两门独立，旧契约）
# 旧契约证据（exports/base-2786c15/pet/window.py:770-783 / 3510 / 3526）：
# ``click_sound_enabled`` 与 ``collision_sound_enabled`` 各管一路、缺省都是 True；
# 且门判在**真正 play_sound 之前**（:3515 点击 / :3526 碰撞）——本播放器把播放推到
# 下一轮事件循环，故同一判定必须落在延迟回调里。
# 另核：**不存在独立 ``collision_sound_pack``**（旧/新全仓 grep 均无），碰撞与点击
# 共用 ``click_sound_pack``，故本轮不需要独立的碰撞音源补齐。


def _gated_player(monkeypatch, values=None, hit_min_dv=100.0):
    """记录 ``(path, volume)`` 的播放器；音源随 pack 身份变化（供缓存失效用例）。"""
    cfg = _Cfg(values)
    events = []
    monkeypatch.setattr(click_sound, "play_sound",
                        lambda path, volume: events.append((str(path), volume)) or True)
    monkeypatch.setattr(
        click_sound, "resolve_click_sound_candidates",
        lambda pack, data_dir=None: [
            "/tmp/%s.wav" % ((pack if isinstance(pack, dict) else {}) or {}).get("id", "default")])
    player = SpriteSoundPlayer(cfg, SimpleNamespace(hit_min_dv=hit_min_dv),
                               clock=lambda: 1000.0)
    return player, events, cfg


def _fire_collision_gated(player, j):
    player._last_play = 0.0
    player.on_collision(SimpleNamespace(j=j))
    _pump()


@pytest.mark.parametrize("click_on, coll_on, expect_click, expect_coll", [
    (True, True, True, True),
    (True, False, True, False),
    (False, True, False, True),
    (False, False, False, False),
])
def test_enable_gates_are_independent(monkeypatch, click_on, coll_on,
                                     expect_click, expect_coll):
    player, events, _ = _gated_player(monkeypatch, {
        "click_sound_enabled": click_on, "collision_sound_enabled": coll_on})
    player.on_click()
    _pump()
    assert (len(events) == 1) is expect_click
    _fire_collision_gated(player, 999.0)
    assert (len(events) == (1 + int(expect_click))) is expect_coll


def test_enable_gates_default_on(monkeypatch):
    """两键都缺省 → 缺省 True，两路都响。"""
    player, events, _ = _gated_player(monkeypatch)
    player.on_click()
    _pump()
    assert events == [("/tmp/default.wav", 0.70)]
    _fire_collision_gated(player, 999.0)
    assert events == [("/tmp/default.wav", 0.70), ("/tmp/default.wav", 0.70)]


def test_gate_reopen_plays_again(monkeypatch):
    """门关过再打开必须恢复：禁用/无音源都不得被缓存钉死。"""
    player, events, cfg = _gated_player(monkeypatch, {
        "click_sound_enabled": False, "collision_sound_enabled": False})
    player.on_click()
    _pump()
    assert events == []
    cfg._v["click_sound_enabled"] = True
    player._last_play = 0.0
    player.on_click()
    _pump()
    assert len(events) == 1
    cfg._v["collision_sound_enabled"] = True
    _fire_collision_gated(player, 999.0)
    assert len(events) == 2


def test_gate_flip_before_deferred_play_cancels(monkeypatch):
    """排队后、延迟回调执行前关掉该路门 → 不发声（旧实现门判在 play_sound 之前）。"""
    player, events, cfg = _gated_player(monkeypatch)
    player.on_collision(SimpleNamespace(j=999.0))   # 已排队
    cfg._v["collision_sound_enabled"] = False       # 回调执行前关掉
    _pump()
    assert events == []
    player._last_play = 0.0
    player.on_click()                               # 已排队
    cfg._v["click_sound_enabled"] = False
    _pump()
    assert events == []


def test_pack_change_resolves_new_source(monkeypatch):
    """音效包变化不复用旧音源（旧实现：window.py:3245「切换音效包时不能复用旧 pair」）。"""
    player, events, cfg = _gated_player(
        monkeypatch, {"click_sound_pack": {"kind": "builtin", "id": "duck"}})
    player.on_click()
    _pump()
    assert events == [("/tmp/duck.wav", 0.70)]
    cfg._v["click_sound_pack"] = {"kind": "builtin", "id": "default"}
    player._last_play = 0.0
    player.on_click()
    _pump()
    assert events[-1] == ("/tmp/default.wav", 0.70)


def test_pack_id_mutated_in_place_resolves_new_source(monkeypatch):
    """config 里的 pack **原对象**被原地改 id → 不得复用旧音源。

    缓存键若持 pack 同一引用，``==`` 会拿对象和它自己比、判不出变化。
    已知 pack 是平面配置（`config._clean_click_sound_pack` 只产 kind/id/path
    三个字符串字段；`click_sound.py:871-873` 也只读这三个），故浅拷贝快照足够；
    解析入口仍必须拿到**原始** pack 对象。
    """
    pack = {"kind": "builtin", "id": "duck"}
    events, seen = [], []
    monkeypatch.setattr(click_sound, "play_sound",
                        lambda path, volume: events.append((str(path), volume)) or True)

    def resolve(received, data_dir=None):
        seen.append(received)
        fields = received if isinstance(received, dict) else {}
        return ["/tmp/%s.wav" % (fields or {}).get("id", "default")]

    monkeypatch.setattr(click_sound, "resolve_click_sound_candidates", resolve)
    player = SpriteSoundPlayer(_Cfg({"click_sound_pack": pack}),
                               SimpleNamespace(hit_min_dv=100.0), clock=lambda: 1000.0)
    player.on_click()
    _pump()
    assert events == [("/tmp/duck.wav", 0.70)]
    assert seen[0] is pack                          # 解析参数仍是原 pack
    pack["id"] = "default"                          # 原地改同一对象
    player._last_play = 0.0
    player.on_click()
    _pump()
    assert events[-1] == ("/tmp/default.wav", 0.70)


# ---------------------------------------------------------------- 碰撞音量（旧用户契约）
# 旧契约（exports/base-2786c15/pet/window.py:786-791 / 3524-3540）：
# 碰撞音量 = ``collision_sound_volume``（缺省 0.70，0~1 由 config 层 clamp），
# 每次碰撞**同一音量**、与撞击强度无关；点击音量是另一个键。
# 注：本仓 sprite 架构曾有的"轻档 0.45 / 重档 0.9"两档表达已于本次修正删除，
# 那是新设计、不是旧契约；这里的断言是旧契约基准，不是"行为不变"的复述。
# 另外：音效**启用门**目前仍只有 ``click_sound_enabled``（``collision_sound_enabled``
# 未接线，见 FEATURE-MATRIX G 缺口），故两路音量键独立、启用门尚不独立。


def _mutable_player(monkeypatch, values=None, hit_min_dv=100.0):
    """同 ``_player``，但把 config 对象交回以便运行期改值。"""
    cfg = _Cfg(values)
    plays = []
    monkeypatch.setattr(click_sound, "play_sound",
                        lambda path, volume: plays.append(volume) or True)
    monkeypatch.setattr(click_sound, "resolve_click_sound_candidates",
                        lambda pack, data_dir=None: ["/tmp/click.wav"])
    player = SpriteSoundPlayer(cfg, SimpleNamespace(hit_min_dv=hit_min_dv),
                               clock=lambda: 1000.0)
    return player, plays, cfg


def _fire_collision(player, plays, j):
    """固定时钟下手工拨回上一发时刻，绕开 80ms 节流。"""
    player._last_play = 0.0
    player.on_collision(SimpleNamespace(j=j))
    _pump()


@pytest.mark.parametrize("configured, expected", [
    (None, 0.70),      # 键缺失 = config 既有缺省
    (0.0, 0.0),        # 设置页 0% = 静音
    (0.25, 0.25),
    (0.70, 0.70),      # 设置页默认
    (1.0, 1.0),
])
@pytest.mark.parametrize("j", [150.0, 250.0])   # 轻 j / 重 j，两者必须同音量
def test_collision_volume_is_config_value_for_any_intensity(
        monkeypatch, configured, expected, j):
    values = {} if configured is None else {"collision_sound_volume": configured}
    player, plays, _ = _mutable_player(monkeypatch, values)
    _fire_collision(player, plays, j)
    assert plays == [expected]


def test_collision_j_gate_keeps_and_volume_uses_old_contract(monkeypatch):
    """j 闸门保留；过闸门后音量 = collision_sound_volume，撞击更强不改变音量。"""
    player, plays, _ = _mutable_player(monkeypatch, {"collision_sound_volume": 0.3})
    player.on_collision(SimpleNamespace(j=50.0))    # 不到闸门不发声
    _pump()
    assert plays == []
    _fire_collision(player, plays, 150.0)
    _fire_collision(player, plays, 250.0)
    assert plays == [0.3, 0.3]


def test_collision_volume_change_at_runtime_takes_effect(monkeypatch):
    """设置页改完即生效（音量每次发声现读，不缓存到构造期）。"""
    player, plays, cfg = _mutable_player(monkeypatch, {})
    _fire_collision(player, plays, 150.0)
    assert plays == [0.70]
    cfg._v["collision_sound_volume"] = 0.0
    _fire_collision(player, plays, 150.0)
    assert plays[-1] == 0.0
    assert len(plays) == 2                            # 静音是"0 音量发声"，不是被吞掉
    cfg._v["collision_sound_volume"] = 1.0
    _fire_collision(player, plays, 150.0)
    assert plays[-1] == 1.0


def test_click_volume_ignores_collision_config(monkeypatch):
    player, plays, _ = _mutable_player(monkeypatch, {"collision_sound_volume": 0.25})
    player.on_click()
    _pump()
    assert plays == [0.70]


def test_collision_volume_ignores_click_config(monkeypatch):
    player, plays, _ = _mutable_player(monkeypatch, {"click_sound_volume": 0.0})
    _fire_collision(player, plays, 250.0)
    assert plays == [0.70]


def test_click_volume_zero_is_silent(monkeypatch):
    """0% 是合法用户值（设置页 0~100%），不得被当作缺省回退成 0.70。"""
    player, plays, _ = _mutable_player(monkeypatch, {"click_sound_volume": 0.0})
    player.on_click()
    _pump()
    assert plays == [0.0]


@pytest.mark.parametrize("bad", [None, "loud"])
def test_collision_volume_unparsable_falls_back_to_default(monkeypatch, bad):
    """读不到的数按 config 既有缺省处理（同 ``_float_or_default`` 口径）：
    该读取在碰撞 listener 链上，异常会掀掉整条链路。"""
    player, plays, _ = _mutable_player(monkeypatch, {"collision_sound_volume": bad})
    _fire_collision(player, plays, 150.0)
    assert plays == [0.70]


# ---------------------------------------------------------------- 音源口径（旧契约）
# 旧契约（exports/base-2786c15/pet/window.py:3514-3523 点击 / :3525-3540 碰撞）：
# 点击门 = ``resolve_click_sound_candidates`` 的第一个候选；碰撞门 =
# ``resolve_click_sound_pair`` 优先（duck 包 = press/ya1，解析时已同步转码成 wav，
# winmm 直放），未命中才 ``choose_sound`` 随机。新版此前两路共用
# ``_resolve_path`` 且一律取 ``candidates[0]``——duck 碰撞"恰好"是 press 文件
# （目录排序巧合）、多文件包失去随机、且拿到的是未转码的 mp3（winmm 直放不了）。


def _recording_player(monkeypatch, values=None, *, world=None):
    """记录 ``(path, volume)`` 的播放器；解析入口由用例各自打桩。"""
    played = []
    monkeypatch.setattr(click_sound, "play_sound",
                        lambda path, volume: played.append((path, volume)) or True)
    if world is None:
        world = SimpleNamespace(hit_min_dv=100.0)
    player = SpriteSoundPlayer(_Cfg(values), world, clock=lambda: 1000.0)
    return player, played


def _fire(player, event):
    player._last_play = 0.0
    player.on_collision(event)
    _pump()


def test_duck_collision_plays_press_source(monkeypatch, tmp_path):
    """duck 包碰撞播 pair[0]（press，已转码 wav）——旧 window.py:3533-3535。"""
    press = tmp_path / "Ya1-9f8d7c.wav"      # 解析入口返回的已是转码产物
    release = tmp_path / "Ya2-1a2b3c.wav"
    monkeypatch.setattr(click_sound, "resolve_click_sound_pair",
                        lambda pack, data_dir=None: (press, release))
    monkeypatch.setattr(click_sound, "resolve_click_sound_candidates",
                        lambda pack, data_dir=None: [tmp_path / "Ya1.mp3"])
    player, played = _recording_player(
        monkeypatch, {"click_sound_pack": {"kind": "builtin", "id": "duck"}})
    _fire(player, SimpleNamespace(j=999.0, a="sprite-1", b="sprite-2"))
    assert [str(path) for path, _ in played] == [str(press)]
    assert str(played[0][0]).endswith(".wav")   # winmm 直放得认 wav


def test_folder_pack_collision_uses_random_pick(monkeypatch, tmp_path):
    """非 duck 包（自定义文件夹多文件）碰撞每次随机挑，旧 window.py:3537-3540。

    候选列表进缓存、随机结果不进缓存：缓存把随机钉死成固定一个就等于取消随机。
    """
    candidates = [tmp_path / "a.wav", tmp_path / "b.wav", tmp_path / "c.wav"]
    resolved, picks = [], []

    monkeypatch.setattr(click_sound, "resolve_click_sound_pair",
                        lambda pack, data_dir=None: None)

    def resolve(pack, data_dir=None):
        resolved.append(pack)
        return list(candidates)

    def choose(paths):
        picks.append(list(paths))
        return candidates[len(picks) - 1]       # 每次换一个 → 证明没被缓存钉死

    monkeypatch.setattr(click_sound, "resolve_click_sound_candidates", resolve)
    monkeypatch.setattr(click_sound, "choose_sound", choose)
    player, played = _recording_player(
        monkeypatch, {"click_sound_pack": {"kind": "folder", "path": str(tmp_path)}})
    _fire(player, SimpleNamespace(j=999.0, a="sprite-1", b="sprite-2"))
    _fire(player, SimpleNamespace(j=999.0, a="sprite-1", b="sprite-2"))
    assert picks == [candidates, candidates]    # 每次拿到的是**整个**候选列表
    assert [str(path) for path, _ in played] == [str(candidates[0]), str(candidates[1])]
    assert len(resolved) == 1                   # 候选列表解析一次（缓存）


def test_click_and_collision_sources_do_not_cross_gates(monkeypatch, tmp_path):
    """两路 gate 解析口径不同，缓存键含 gate → 来回切换不串源。"""
    press = tmp_path / "Ya1.wav"
    click_src = tmp_path / "Ya1.mp3"
    monkeypatch.setattr(click_sound, "resolve_click_sound_pair",
                        lambda pack, data_dir=None: (press, tmp_path / "Ya2.wav"))
    monkeypatch.setattr(click_sound, "resolve_click_sound_candidates",
                        lambda pack, data_dir=None: [click_src])
    player, played = _recording_player(
        monkeypatch, {"click_sound_pack": {"kind": "builtin", "id": "duck"}})

    player.on_click()                                   # 点击门 → candidates[0]
    _pump()
    player._last_play = 0.0
    _fire(player, SimpleNamespace(j=999.0, a="sprite-1", b="sprite-2"))   # 碰撞门 → press
    player._last_play = 0.0
    player.on_click()                                   # 回点击门：仍是点击口径
    _pump()
    assert [str(path) for path, _ in played] == [str(click_src), str(press), str(click_src)]


def test_island_light_hit_uses_static_floor(monkeypatch, tmp_path):
    """岛（静态成员）轻撞要出声：音效门跟世界侧同一条放宽带。

    世界侧对 pair 含静态成员用 ``static_hit_min_dv``（60，sprite_collision.py 的
    ``_apply_results``），音效门若一律用 ``hit_min_dv``（300）→ 60~300 的岛击
    "有 bump 无声"；旧 island_collision 的岛击门槛同样在放宽档（60px/s）。
    """
    world = SimpleNamespace(
        hit_min_dv=300.0, static_hit_min_dv=60.0,
        _static_members={"island": (0.0, 0.0, 400.0, 44.0)})
    monkeypatch.setattr(click_sound, "resolve_click_sound_pair",
                        lambda pack, data_dir=None: None)
    monkeypatch.setattr(click_sound, "resolve_click_sound_candidates",
                        lambda pack, data_dir=None: [tmp_path / "click.wav"])
    player, played = _recording_player(monkeypatch, world=world)

    _fire(player, SimpleNamespace(j=100.0, a="island", b="sprite-1"))
    assert len(played) == 1                     # 岛轻撞（60~300）出声
    _fire(player, SimpleNamespace(j=100.0, a="sprite-1", b="sprite-2"))
    assert len(played) == 1                     # 宠-宠仍按 300 闸门：不出声
    _fire(player, SimpleNamespace(j=400.0, a="sprite-1", b="sprite-2"))
    assert len(played) == 2


# ---------------------------------------------------------------- shell 接线
def test_shell_click_feedback_and_collision_listener(tmp_path, monkeypatch):
    import tests.test_overlay_window_capabilities as cap

    shell = cap._make_shell(tmp_path)
    try:
        clicks = []
        shell.overlay.click_feedback = lambda: clicks.append(1)
        # 单击路由：threshold 内 press→release
        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtCore import QEvent

        sprite = shell.sprite
        center = sprite.rect().center()
        shell.overlay._mouse_grab = sprite
        shell.overlay._press_pos = QPointF(center)
        event = QMouseEvent(
            QEvent.Type.MouseButtonRelease, QPointF(center), QPointF(center),
            Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier)
        shell.overlay.behavior = type("B", (), {
            "on_sprite_clicked": lambda self, s: True})()
        shell.overlay.mouseReleaseEvent(event)
        assert clicks == [1]
        # 抽干 _submit 排到下一轮事件循环的延迟播放：本用例没打桩 play_sound，
        # 漏给下一个用例会让对方的播放记录里多一声（test_overlay_per_slot_settings
        # 的 test_click_sound_uses_clicked_sprite_config 因此红过）。
        _pump()
        # 碰撞监听器已挂接
        # 碰撞监听器已挂接：共享播放器经壳按事件所在屏的世界分派（多屏逐屏一份
        # 世界，播放器只持一块屏做缺省）
        assert shell._on_collision_sound in shell.collision._listeners
    finally:
        shell._delete_runtime_marker()


def test_light_pet_island_hit_below_raw_dv_floor_still_sounds():
    """轻量小鱼（mass 0.5）岛击：dv=65 过世界门，j=32.5 < 60，音效不该被吞。"""
    from types import SimpleNamespace

    from pet import sprite_sound

    world = SimpleNamespace(hit_min_dv=300.0, static_hit_min_dv=60.0,
                            _static_members={"island": object()})
    player = sprite_sound.SpriteSoundPlayer.__new__(sprite_sound.SpriteSoundPlayer)
    player._world = world
    event = SimpleNamespace(a="pet-1", b="island", j=32.5)
    assert player._hit_floor(event) <= 32.5
    assert player._hit_floor(SimpleNamespace(a="pet-1", b="pet-2", j=32.5)) == 300.0


def test_click_gate_picks_from_the_whole_candidate_list(monkeypatch, tmp_path):
    """点击门在多文件包里**现挑**，不再固定文件名排序第一那个（issue #308）。

    设置页对「自定义文件夹（随机）」的承诺就是随机播放；旧实现取 ``candidates[0]``，
    整个进程内永远播同一个文件（报告者 9 个 .mp3 的目录只播排序第一）。
    """
    files = [tmp_path / f"{index}.mp3" for index in range(3)]
    monkeypatch.setattr(click_sound, "resolve_click_sound_candidates",
                        lambda pack, data_dir=None: list(files))
    picks: list[int] = []

    def choose(paths):
        picks.append(len(paths))
        return paths[len(picks) - 1]          # 每次换一个 → 证明真的走了现挑

    monkeypatch.setattr(click_sound, "choose_sound", choose)
    player, played = _recording_player(
        monkeypatch, {"click_sound_pack": {"kind": "folder", "path": str(tmp_path)}})
    for _ in range(3):
        player._last_play = 0.0
        player.on_click()
        _pump()

    assert picks == [3, 3, 3], "点击门必须把**整个**候选列表交给 choose_sound"
    paths = [str(path) for path, _ in played]
    assert len(set(paths)) > 1, f"多文件包点击必须换文件，实际：{paths}"
