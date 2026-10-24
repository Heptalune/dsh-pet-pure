# -*- coding: utf-8 -*-
"""``Config.reload()`` 必须区分「键缺失」与「显式 null」（缺陷 15）。

现象：设置页「恢复默认菜单布局」刻意把 ``context_menu_layout`` 写成 ``null``
（modern_settings_dialog 保存时 ``default_layout == 用户布局`` 即写 None），
但 reload 的白名单只认 ``raw[key] is not None``——显式 null 被当成"磁盘没提供"，
运行中的主进程保留内存旧值；此后主进程任何一次 ``save()``（整体写内存视图）
都把旧覆写写回磁盘，"恢复默认"被静默回滚，用户看到的是"点了没反应"。

修法口径（写死在 ``config._NULL_ACCEPTING_KEYS``）：只有把 None 当**合法取值**
（= 用内置默认）的键才采纳盘上的显式 null；数值/字符串键的 null 维持旧口径
（视为"未提供"，保留内存现值），否则消费者里的 ``float()`` / ``int()`` 强转
会在运行期炸——"修一个键顺手炸一片"。
"""
from __future__ import annotations

import json

import pytest

import pet.config as config_mod
from pet.config import APP_DIR_NAME, Config

LAYOUT_OVERRIDE = {
    "schema_version": 1,
    "layout_id": "user",
    "nodes": [
        {"type": "action", "id": "modern_settings", "visible": True},
        {"type": "action", "id": "quit", "visible": True},
    ],
}


def _config_path(tmp_path):
    return tmp_path / APP_DIR_NAME / "config.json"


def _read_raw(tmp_path) -> dict:
    return json.loads(_config_path(tmp_path).read_text(encoding="utf-8"))


def _write_raw(tmp_path, raw: dict) -> None:
    _config_path(tmp_path).write_text(
        json.dumps(raw, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------- 两进程视角
def test_settings_process_restore_default_is_visible_to_main_process(tmp_path):
    """设置页写 null → 主进程 reload 拿到 None → 主进程 save 不写回旧值。"""
    main = Config(tmp_path)
    main.set("context_menu_layout", LAYOUT_OVERRIDE)
    assert main.save() is True

    settings = Config(tmp_path)                       # 设置页是独立进程：独立实例
    assert settings.get("context_menu_layout") == LAYOUT_OVERRIDE
    settings.set("context_menu_layout", None)         # 「恢复默认布局」= 显式写 null
    assert settings.save() is True
    assert _read_raw(tmp_path)["context_menu_layout"] is None, "前提：设置页确实落了 null"

    main.reload()

    assert main.get("context_menu_layout") is None, \
        "主进程 reload 必须采纳显式 null（旧实现当'未提供'而保留旧覆写）"

    assert main.save() is True
    assert _read_raw(tmp_path)["context_menu_layout"] is None, \
        "主进程后续 save 不得把旧覆写写回磁盘（= 把「恢复默认」静默回滚）"


def test_config_reloaded_from_disk_sees_explicit_null(tmp_path):
    """冷启动同口径：磁盘上的显式 null 载入后就是 None（不是旧覆写）。"""
    config = Config(tmp_path)
    config.set("context_menu_layout", LAYOUT_OVERRIDE)
    assert config.save() is True

    raw = _read_raw(tmp_path)
    raw["context_menu_layout"] = None
    _write_raw(tmp_path, raw)

    assert Config(tmp_path).get("context_menu_layout") is None


def test_reload_keeps_value_when_key_is_absent(tmp_path):
    """键**缺失**仍是"磁盘没提供"：保留内存现值（不得把"缺失"也当成 null）。"""
    config = Config(tmp_path)
    config.set("context_menu_layout", LAYOUT_OVERRIDE)
    assert config.save() is True

    raw = _read_raw(tmp_path)
    raw.pop("context_menu_layout")
    _write_raw(tmp_path, raw)

    config.reload()

    assert config.get("context_menu_layout") == LAYOUT_OVERRIDE


# ---------------------------------------------------------------- 采纳 null 的口径
def test_null_accepting_keys_are_minimal_and_exist(tmp_path):
    """采纳显式 null 的键是显式白名单：口径写死在 config.py，键必须真存在。

    2026-10-07（issue #220 M10）加入位置三键：``rx``/``ry``/``screen_name`` 的消费者
    本来就把 None 当"没有位置记忆 → 落默认落位"（``window_placement.restore_position``
    / ``overlay_shell._restore_position`` / ``_spawn_sprite_pos``），而重置正是用 null
    表达它。不采纳的后果是运行期一直留着重置前的位置：正常退出 ``save_position()``
    把旧位置写回磁盘，重置被静默抵消。
    """
    assert config_mod._NULL_ACCEPTING_KEYS == frozenset(
        {"context_menu_layout", "rx", "ry", "screen_name",
         # 2026-10-21 功能模块激活：显式 null = 撤成「未判定」，下次启动重新走
         # 一次性迁移判定（pet/feature_activation.decide_and_freeze）
         "feature_activation"}
    )
    assert config_mod._NULL_ACCEPTING_KEYS <= set(Config(tmp_path).data)


def test_reload_keeps_in_memory_position_when_keys_are_absent(tmp_path):
    """位置键**缺失**仍是"磁盘没提供"：保留内存现值（不得当成"撤掉位置记忆"）。"""
    config = Config(tmp_path)
    config.set("rx", 0.5)
    config.set("ry", 0.25)
    config.set("screen_name", "secondary")
    assert config.save() is True

    raw = _read_raw(tmp_path)
    for key in ("rx", "ry", "screen_name"):
        raw.pop(key, None)
    _write_raw(tmp_path, raw)

    config.reload()

    assert (config.get("rx"), config.get("ry")) == (0.5, 0.25)
    assert config.get("screen_name") == "secondary"


@pytest.mark.parametrize("key,value", [
    ("playback_speed", 2.0),
    ("scale", 1.5),
    ("spawn_scale", 1.5),
    ("self_talk_image_chance", 40),
    ("click_sound_volume", 0.5),
])
def test_reload_ignores_explicit_null_for_keys_that_cannot_hold_none(tmp_path, key, value):
    """数值键的显式 null 不采纳：消费者是 float()/int() 强转，None 会运行期炸。"""
    config = Config(tmp_path)
    config.set(key, value)
    assert config.save() is True
    assert config.get(key) == value, "前提：键确实吃到了值"

    raw = _read_raw(tmp_path)
    raw[key] = None
    _write_raw(tmp_path, raw)

    config.reload()

    assert config.get(key) == value
