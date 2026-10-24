# -*- coding: utf-8 -*-
"""sprite 音效（4.1c）：点击音效 + 碰撞音效的产品化播放器。

音源解析与旧架构同一入口（click_sound.resolve_click_sound_candidates：
用户自定义 click.wav 优先，内置 default 包兜底；配置键
click_sound_enabled / click_sound_pack / click_sound_volume 与设置页一致）。
播放走 click_sound.play_sound（Windows 下 wav 直放 winmm，不拉起
QtMultimedia）。

音量契约（旧契约 window.py:3522/3532-3540，0 = 静音）：

- 点击音效：单击 sprite（点击/拖拽阈值判定路由）→ 音量 = ``click_sound_volume``
  （缺省 0.70），80ms 节流防抖；
- 碰撞音效：collision event.j >= 真撞击门槛（真撞击量级）→ 音量 =
  ``collision_sound_volume``（缺省 0.70），每次碰撞同一音量、与撞击强度无关
  （旧架构即如此；本文件曾有的轻档 0.45 / 重档 0.9 是新设计，已删除），
  80ms 节流（碰碰车一次接触连发多个事件）。门槛按 pair 取：普通对 =
  ``world.hit_min_dv``，pair 含静态成员（岛）= ``world.static_hit_min_dv``
  ——世界侧对岛击放宽带（``sprite_collision.py`` 的 ``_apply_results``：
  ``other_id in self._static_members`` 时取 static_hit_min_dv），音效门不跟
  就会"轻撞岛有 bump 无声"（旧 island_collision 的岛击同属放宽档）。
- 启用门（旧契约 window.py:770-783）：点击 = ``click_sound_enabled``、碰撞 =
  ``collision_sound_enabled``，两门独立、缺省 True；事件发生时与真正 ``play_sound``
  前各查一次（旧实现门判就在 play 之前），故关掉再打开立即恢复、排队中的音效在
  回调前被关也不发声。

音源口径（旧契约 window.py:3514-3523 点击 / :3525-3540 碰撞）：

- 点击门：``resolve_click_sound_candidates`` 的**第一个**候选（现状不动；
  duck 包的 press+release 两段与点击同属另案，不在本文件）；
- 碰撞门：``resolve_click_sound_pair`` 优先——duck 包命中即 press（pair[0]，
  解析时已转码成 wav，winmm 直放），未命中才从候选列表里 ``choose_sound``
  **每次现挑**（多文件包恢复随机，随机结果不进缓存）。

音效失败（无音频设备/后端错误/解析失败）一律静默降级，绝不影响主链路。
"""
from __future__ import annotations

import time
from pathlib import Path

from . import click_sound
from . import collision
from .sprite_collision import STATIC_HIT_MIN_DV

# 成员质量下界（collision.calculate_mass 的下钳），用于把世界侧 dv 门换算成 j 门。
_MIN_MEMBER_MASS = collision.calculate_mass(0.0, 0.0, scale=0.0)

_THROTTLE_S = 0.08
#: 候选列表缓存上限（键 = (gate, pack, data_dir)；多 sprite 各自的音效包各占一项）
_PATH_CACHE_LIMIT = 16
#: 碰撞门（``on_collision``）的启用键：解析口径按 gate 分流，见 ``_resolve_path``
_COLLISION_GATE = "collision_sound_enabled"
#: 音量键的既有缺省（``pet/config.py`` 的 click_sound_volume /
#: collision_sound_volume 都是 0.70）
_VOLUME_DEFAULT = 0.70
_DEFAULT_PACK = {"kind": "builtin", "id": "default"}


def _pack_cache_key(pack):
    """音源包 → 可哈希的缓存键（浅拷贝快照；原对象仍原样传给解析入口）。

    pack 是 config 里的 dict（``kind``/``id``/``path`` 三个字符串字段），不能直接
    当字典键；同时也不能持原对象引用当键——原地改 id 会让 ``==`` 拿对象和它自己比、
    判不出变化（``test_pack_id_mutated_in_place_resolves_new_source`` 锁着这条）。
    """
    if isinstance(pack, dict):
        return tuple(sorted((str(key), str(value)) for key, value in pack.items()))
    try:
        hash(pack)
    except TypeError:
        return repr(pack)
    return pack


class SpriteSoundPlayer:
    """点击/碰撞音效统一播放器（duck-typed config：get/dir 即可）。

    逐 sprite 取源（B7b）：旧架构每只桌宠是独立进程、各读自己的
    ``config-slot-N.json``（门/音量/音源包都取自己那份），点哪只响哪只的配置。
    本类通过 ``config_for(sprite)`` 把"身份 → 配置面"交给壳解析：

    - ``on_click(sprite)``：壳在 ``_on_sprite_click(sprite)`` 里带身份调用；
    - ``on_collision(event, sprite)``：事件只带成员 id，壳经
      ``sprite_for_event(event)`` 解析出参与的第一只宠物；
    - 两者都拿不到身份（裸播放器/脚本直调）时回退构造期的 config，行为与历史一致。
    """

    def __init__(self, config, world=None, *, clock=time.monotonic,
                 config_for=None, sprite_for_event=None) -> None:
        self._config = config
        self._world = world
        self._clock = clock
        self._config_for = config_for
        self._sprite_for_event = sprite_for_event
        self._last_play = 0.0
        # 音源按 (gate, pack, data_dir) 缓存候选列表：多 sprite 各自的音效包不同，
        # 单一槽位缓存会让每只宠的每次发声都重新解析一遍。上限很小（宠数量级）。
        self._path_cache: dict = {}
        self._source: object = None  # 兼容旧字段：最近一次解析的 source

    # ---------------------------------------------------------------- 内部
    def _cfg_for(self, sprite):
        """解析某只 sprite 的配置面（None = 用构造期 config）。"""
        if sprite is None or self._config_for is None:
            return self._config
        try:
            return self._config_for(sprite) or self._config
        except Exception:
            return self._config

    def _resolve_event_sprite(self, event):
        if self._sprite_for_event is None:
            return None
        try:
            return self._sprite_for_event(event)
        except Exception:
            return None

    def _config_volume(self, key: str, cfg=None) -> float:
        """读一个音量键（0~1）：不得把 0.0 当缺省回退，读不到则按 config 口径回落。

        0.0 是合法用户值（设置页 0~100%，0% = 静音）；不可解析的值走
        ``pet/config.py::_float_or_default`` 同口径的缺省回落（本读取在点击/碰撞
        listener 链上，异常会掀掉整条链路）。每次发声现读，故改完即生效。
        """
        try:
            value = float((self._config if cfg is None else cfg).get(key, _VOLUME_DEFAULT))
        except (TypeError, ValueError, OverflowError):
            return _VOLUME_DEFAULT
        return max(0.0, min(1.0, value))

    def _enabled(self, key: str, cfg=None) -> bool:
        """音效启用门（旧契约 window.py:770-783：点击/碰撞各一门，缺省 True）。

        事件发生时与真正播放前各查一次、不缓存——开关关掉再打开必须立刻恢复
        （缓存把禁用记住会让"关过就再也不响"）。
        """
        return bool((self._config if cfg is None else cfg).get(key, True))

    def _resolve_path(self, gate: str, cfg=None) -> Path | None:
        """按 gate 解析本次要播的音源（不含启用门；门由 ``_play`` 各查一次）。

        两路口径不同（旧契约 window.py:3514-3523 / :3525-3540）：

        - 点击门：同样 ``choose_sound`` **现挑**——候选列表缓存、随机结果不缓存。
          旧契约是"点击固定取第一个"，而设置页对「自定义文件夹（随机）」的承诺就是
          随机播放；两者打架时以用户可见文案为准（issue #308：9 个文件的目录永远只
          播文件名排序第一那个，整个进程内不变）。
        - 碰撞门：``resolve_click_sound_pair`` 优先（duck 包 = press，已转码 wav，
          winmm 直放），未命中才 ``choose_sound`` 在候选列表里**现挑**——候选
          列表缓存、随机结果不缓存，否则多文件包会被缓存钉死成固定一个。
          pair 每次现读而不进缓存：转码缓存是异步落地的，把第一次解析的源钉死
          会让后续仍播未转码的源（旧实现每次发声都调 pair，转码落地后即取 wav）。

        音效包/数据目录变化即重新解析：旧实现要求"切换音效包时不能复用上一次
        的旧 pair"（window.py:3245），缓存会把旧音源与解析失败一起钉死。
        缓存键存 pack 的**浅拷贝快照**而非原对象：pack 是 config 里的原对象，
        键持同一引用时原地改 id/path 会让 ``==`` 拿对象和它自己比、判不出变化。
        浅拷贝足够——pack 是平面配置（``config._clean_click_sound_pack`` 只产
        kind/id/path 三个字符串字段，``click_sound.py:871-873`` 也只读这三个）；
        解析入口仍传原 pack。

        B7b：``cfg`` = 该 sprite 自己的配置面（音效包也可以逐只不同）。
        """
        source = self._config if cfg is None else cfg
        pack = source.get("click_sound_pack", None) or _DEFAULT_PACK
        data_dir = getattr(source, "dir", None)
        if data_dir is None:
            data_dir = getattr(self._config, "dir", None)
        if gate == _COLLISION_GATE:
            try:
                pair = click_sound.resolve_click_sound_pair(pack, data_dir)
            except Exception:
                pair = None      # 解析入口异常按"未命中 pair"处理，走候选列表
            if pair is not None:
                return pair[0]
            candidates = self._candidates(gate, pack, data_dir)
            return click_sound.choose_sound(candidates) if candidates else None
        candidates = self._candidates(gate, pack, data_dir)
        return click_sound.choose_sound(candidates) if candidates else None

    def _candidates(self, gate: str, pack, data_dir) -> list:
        """候选列表（按 (gate, pack, data_dir) 缓存）：解析失败 = 空表，静默降级。"""
        key = (gate, _pack_cache_key(pack), data_dir)
        cached = self._path_cache.get(key)
        if cached is not None:
            return cached
        self._source = key
        try:
            paths = list(click_sound.resolve_click_sound_candidates(pack, data_dir))
        except Exception:
            paths = []     # 解析失败 = 无音效，静默降级
        if len(self._path_cache) >= _PATH_CACHE_LIMIT:
            self._path_cache.clear()
        self._path_cache[key] = paths
        return paths

    def _play(self, volume: float, gate: str, cfg=None) -> None:
        if not self._enabled(gate, cfg):
            return
        now = self._clock()
        if now - self._last_play < _THROTTLE_S:
            return
        # 解析放在节流之后（旧实现同序：window.py:3526 门 → :3529 冷却 → :3533
        # 解析）：碰撞 listener 在 tick 里，节流前解析等于每个事件都做一遍解析
        # （pair 每次现读时是每事件两次 stat）。
        path = self._resolve_path(gate, cfg)
        if path is None:
            return
        self._last_play = now
        self._submit(path, volume, gate, cfg)

    def _submit(self, path: Path, volume: float, gate: str, cfg=None) -> None:
        """提交播放：把 ``play_sound`` 推迟到下一轮事件循环再调。

        ``play_sound`` 是同步阻塞（winmm 首播 ~157ms / 其后 ~62ms，4.3 岛桥刀
        实测），在碰撞 listener 里直调会把该 tick 拖到 ~60ms（碰碰车场景每撞
        必卡）。``QTimer.singleShot(0, ...)`` **只是把这次调用延后**到下一轮事件
        循环，并没有把它搬离 GUI 线程——它仍会在事件循环里同步阻塞，故这里**不
        保证任何延时上限、也不保证不卡**（要真不阻塞需要后台播放通道，本轮未改
        backend）。无 QApplication（纯脚本/测试直调）退化为同步直放。"""
        try:
            from PySide6.QtCore import QTimer
            from PySide6.QtWidgets import QApplication
            if QApplication.instance() is not None:
                QTimer.singleShot(0, lambda: self._safe_play(path, volume, gate, cfg))
                return
        except Exception:
            pass
        self._safe_play(path, volume, gate, cfg)

    def _safe_play(self, path: Path, volume: float, gate: str, cfg=None) -> None:
        if not self._enabled(gate, cfg):
            return  # 排队期间该路门被关掉 → 不发声（旧实现门判在 play_sound 之前）
        try:
            click_sound.play_sound(path, volume)
        except Exception:
            pass  # 无音频设备/后端失败：静默降级

    # ---------------------------------------------------------------- 事件入口
    def on_click(self, sprite=None) -> None:
        """单击音效：音量/门取**触发 sprite** 那份配置（旧 window.py:3510 每窗）。

        ``sprite=None`` 且播放器已接 ``config_for``（= 壳在做逐 sprite 路由）时
        **不发声**：overlay 的 ``click_feedback`` 回调不带 sprite 身份，真正的播放
        由壳在紧随其后的 ``_on_sprite_click(sprite)`` 里按被点的那只补上（同一个
        点击分支，两者之间没有 return，所以一次点击只会响一声）。裸播放器
        （无 ``config_for``：测试/脚本直调）行为不变。
        """
        if sprite is None and self._config_for is not None:
            return
        cfg = self._cfg_for(sprite)
        self._play(self._config_volume("click_sound_volume", cfg),
                   "click_sound_enabled", cfg)

    def on_collision(self, event, sprite=None, *, world=None) -> None:
        """碰撞音效：j 闸门（真撞击量级）→ 音量 = ``collision_sound_volume``
        （旧契约：每次碰撞同一音量，无分档），门 = collision_sound_enabled
        （旧契约 window.py:3526，与点击门独立）。

        闸门按 pair 取（见 ``_hit_floor``）：pair 含静态成员（岛）时用世界侧同一条
        放宽带，否则普通对阈值——岛轻撞在世界侧算真撞击（``static_hit_min_dv``），
        音效不该被更紧的通用阈值吃掉。

        ``sprite`` = 本次碰撞的宠物（壳按事件成员 id 解析；旧版每进程播自己那只，
        单进程壳取参与碰撞的第一只宠物）。

        ``world`` = 撞出这一事件的那个碰撞世界（多屏下逐屏一份，播放器只持一块屏
        的世界做缺省）；不传就用构造期那个（单世界装配与历史一致）。
        """
        if float(getattr(event, "j", 0.0) or 0.0) < self._hit_floor(event, world):
            return
        if sprite is None:
            sprite = self._resolve_event_sprite(event)
        cfg = self._cfg_for(sprite)
        self._play(self._config_volume("collision_sound_volume", cfg),
                   "collision_sound_enabled", cfg)

    def _hit_floor(self, event, world=None) -> float:
        """该事件的音效闸门：pair 含静态成员用 ``static_hit_min_dv``，否则通用值。

        与 ``sprite_collision`` 的判定同源——世界侧对同一 pair 就是
        ``other_id in world._static_members`` 时取放宽档（``_apply_results``），
        静态成员集合没有公开访问器，只从世界登记表读；读不到就退回通用阈值
        （不猜、不新增机制）。世界对象缺失（``world=None``，直调/脚本场景）时
        闸门为 0，与改动前一致。``world`` 入参用于多屏：撞出事件的屏与构造期
        缺省世界不同时以入参为准。
        """
        world = self._world if world is None else world
        if world is None:
            return 0.0
        try:
            hit_min = float(getattr(world, "hit_min_dv", 0.0) or 0.0)
        except (TypeError, ValueError):
            hit_min = 0.0
        static_ids = getattr(world, "_static_members", None)
        if not static_ids:
            return hit_min
        ids = (getattr(event, "a", None), getattr(event, "b", None))
        if not any(member_id in static_ids for member_id in ids if member_id is not None):
            return hit_min
        # 世界侧岛击按 dv 放行，事件只带 j = dv × mass：换算成冲量门时取成员
        # 质量下界，否则轻量小鱼（mass 0.5）的岛击过了世界门却被音效门吞掉
        # （与 island_bridge._impulse_floor 同口径）。
        try:
            dv_floor = float(getattr(world, "static_hit_min_dv", STATIC_HIT_MIN_DV))
        except (TypeError, ValueError):
            dv_floor = float(STATIC_HIT_MIN_DV)
        return dv_floor * _MIN_MEMBER_MASS
