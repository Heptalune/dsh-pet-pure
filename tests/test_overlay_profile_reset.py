from __future__ import annotations

import json

import pytest
from PySide6.QtCore import QPointF

from pet import catalog, overlay_settings_command as commands, pet_profiles as profiles
from pet import settings_reset_defaults
from pet.config import Config
from pet.settings_slot_config import SlotRoutedConfig
from tests import test_overlay_collision_toggle as toggle
from tests import test_sprite_menu_facade as fac


def _route(cfg, slot):
    return SlotRoutedConfig(Config(base=cfg.dir.parent, instance_id=f"slot-{slot}"),
                            Config(base=cfg.dir.parent), slot)


def _reset(cfg, slot):
    route = _route(cfg, slot)
    assert route.reset_to_defaults()
    host = type("Host", (), {"config": route, "parentWidget": lambda self: None})()
    settings_reset_defaults._request_runtime_alignment(host)
    assert commands.read_command(cfg.dir)["command"] == commands.CMD_PROFILE_RESET


def _child(shell, slot=1):
    return next(s for s in shell._spawned if shell._spawned_slots[s] == slot)


def test_route_reset_consumes_preferences_and_preserves_anchor_then_restart(tmp_path):
    shell, cfg = toggle._make_slot_shell(tmp_path, scale=1.2, playback_speed=2.0)
    shell._instance._create_library = lambda cid: fac.RichLibrary()
    try:
        shell.spawn_pet()
        shell.spawn_pet()
        child, other = _child(shell), _child(shell, 2)
        route = _route(cfg, 1)
        for key, value in {"scale": 1.4, "playback_speed": 0.5,
                           "click_sound_enabled": False, "bubble_text_scale": 180,
                           "self_talk_enabled": True, "self_talk_texts": ["old"],
                           "character": "other-character"}.items():
            route.set(key, value)
        assert route.save()
        shell._refresh_slot_settings_if_changed()
        child = _child(shell)
        child.set_pos(QPointF(350, 270))
        shell.save_spawned_positions()
        anchor = child.body_rect().center()
        screen = child.home_screen
        other_bytes = profiles.slot_profile_path(cfg.dir, 2).read_bytes()
        main_bytes = cfg.path.read_bytes()
        _reset(cfg, 1)
        shell._consume_settings_command()
        child = _child(shell)
        assert child.scale == pytest.approx(profiles.same_source_default("scale"))
        assert child.playback_speed == profiles.same_source_default("playback_speed")
        assert shell._sprite_config(child).get("character") == catalog.DEFAULT_CHARACTER
        assert shell._sprite_config(child).get("click_sound_enabled") is True
        assert shell._self_talk_hosts[child]._self_talk_enabled is False
        assert child.body_rect().center() == anchor
        assert child.home_screen is screen
        assert other.scale != child.scale
        assert profiles.slot_profile_path(cfg.dir, 2).read_bytes() == other_bytes
        assert cfg.path.read_bytes() == main_bytes
        assert commands.read_command(cfg.dir) is None
        shell.save_spawned_positions()
        saved = profiles.read_slot_profile(cfg.dir, 1)
        assert saved.value("scale") == child.scale
        assert saved.value("character") == catalog.DEFAULT_CHARACTER
    finally:
        toggle._teardown(shell)
    restored, _ = toggle._make_slot_shell(tmp_path)
    try:
        child = _child(restored)
        assert child.scale == pytest.approx(profiles.same_source_default("scale"))
        assert child.body_rect().center() == anchor
    finally:
        toggle._teardown(restored)


def test_reset_blocks_old_geometry_before_consume(tmp_path):
    shell, cfg = toggle._make_slot_shell(tmp_path)
    try:
        shell.spawn_pet()
        child = _child(shell)
        shell.persist_sprite_setting(child, "scale", 1.6)
        shell._refresh_slot_settings_if_changed()
        old_view = shell._sprite_config(child)
        _reset(cfg, 1)
        shell.save_spawned_positions()
        assert "scale" not in profiles.read_slot_profile(cfg.dir, 1).data
        assert profiles.write_profile_geometry(cfg.dir, 1, {"scale": 1.7},
                                               generation=old_view.generation).status == "stale-generation"
        shell._consume_settings_command()
        shell.save_spawned_positions()
        assert profiles.read_slot_profile(cfg.dir, 1).value("scale") == profiles.same_source_default("scale")
    finally:
        toggle._teardown(shell)


def test_watcher_before_command_is_idempotent_and_switches_library(tmp_path):
    shell, cfg = toggle._make_slot_shell(tmp_path)
    made = []
    shell._instance._create_library = lambda cid: made.append(cid) or fac.RichLibrary()
    try:
        shell.spawn_pet()
        child = _child(shell)
        shell.switch_character("other-character", child)
        old_lib = child.library
        _reset(cfg, 1)
        shell._refresh_slot_settings_if_changed()
        child = _child(shell)
        assert child.library is not old_lib
        assert made[-1] == catalog.DEFAULT_CHARACTER
        after = child.library
        shell._consume_settings_command()
        assert _child(shell).library is after
        shell.save_spawned_positions()
        assert profiles.read_slot_profile(cfg.dir, 1).value("scale") == child.scale
    finally:
        toggle._teardown(shell)


@pytest.mark.parametrize("target", [None, 129])
def test_unhandled_profile_target_finishes_failed_without_starving_exit(tmp_path, target, caplog):
    shell, cfg = toggle._make_slot_shell(tmp_path)
    try:
        shell.spawn_pet()
        child = _child(shell)
        child.scale = 1.7
        assert commands.write_command(cfg.dir, commands.CMD_PROFILE_RESET, target=target)
        assert commands.write_command(cfg.dir, commands.CMD_EXIT_SPAWNED_PETS, target=1)
        shell._consume_settings_command()
        assert commands.read_command(cfg.dir)["command"] == commands.CMD_EXIT_SPAWNED_PETS
        assert child.scale == 1.7
        assert "失败结束" in caplog.text
        shell._consume_settings_command()
        assert child not in shell._spawned
    finally:
        toggle._teardown(shell)


def test_closed_target_does_not_respawn_or_touch_other(tmp_path):
    shell, cfg = toggle._make_slot_shell(tmp_path)
    try:
        shell.spawn_pet()
        shell.spawn_pet()
        child, other = _child(shell), _child(shell, 2)
        shell.exit_pet(child)
        other.scale = 1.7
        _reset(cfg, 1)
        shell._consume_settings_command()
        assert len(shell._spawned) == 1 and _child(shell, 2) is other
        assert other.scale == 1.7
        assert commands.read_command(cfg.dir) is None
    finally:
        toggle._teardown(shell)


def test_missing_and_bad_values_match_settings_reader_and_are_cached(tmp_path, monkeypatch):
    shell, cfg = toggle._make_slot_shell(tmp_path, playback_speed=2.5)
    try:
        shell.spawn_pet()
        child = _child(shell)
        path = profiles.slot_profile_path(cfg.dir, 1)
        for data in ({"version": 4}, {"version": 4, "playback_speed": "bad",
                                     "bubble_text_scale": -10, "drop_gravity": "false",
                                     "click_sound_pack": [], "throw_strength": {}}):
            path.write_text(json.dumps(data), encoding="utf-8")
            shell._refresh_slot_settings_if_changed()
            view, route = shell._sprite_config(child), _route(cfg, 1)
            for key in ("playback_speed", "bubble_text_scale", "drop_gravity",
                        "click_sound_pack", "throw_strength"):
                assert view.get(key) == route.get(key)
            monkeypatch.setattr(profiles, "read_slot_snapshot",
                                lambda *a, **kw: pytest.fail("cached read did disk IO"))
            for _ in range(1000):
                assert view.get("playback_speed") == 1.0
            monkeypatch.undo()
    finally:
        toggle._teardown(shell)


def test_corrupt_snapshot_keeps_command_and_old_token(tmp_path):
    shell, cfg = toggle._make_slot_shell(tmp_path)
    try:
        shell.spawn_pet()
        child = _child(shell)
        token = child._profile_generation
        child.scale = 1.7
        profiles.generation_path(cfg.dir).write_text("{broken", encoding="utf-8")
        assert commands.write_command(cfg.dir, commands.CMD_PROFILE_RESET, target=1)
        shell._consume_settings_command()
        assert commands.read_command(cfg.dir) is not None
        assert child._profile_generation == token and child.scale == 1.7
        before = profiles.slot_profile_path(cfg.dir, 1).read_bytes()
        shell.save_spawned_positions()
        assert profiles.slot_profile_path(cfg.dir, 1).read_bytes() == before
    finally:
        toggle._teardown(shell)


def test_settings_write_failure_reports_false_without_mutating_child(tmp_path, monkeypatch):
    shell, cfg = toggle._make_slot_shell(tmp_path)
    try:
        shell.spawn_pet()
        child = _child(shell)
        before = profiles.slot_profile_path(cfg.dir, 1).read_bytes()
        monkeypatch.setattr(profiles, "write_profile_settings",
                            lambda *a, **kw: profiles.ProfileResult("partial"))
        assert shell.persist_sprite_setting(child, "scale", 1.7) is False
        assert profiles.slot_profile_path(cfg.dir, 1).read_bytes() == before
    finally:
        toggle._teardown(shell)


def test_same_slot_rebuild_invalidates_previous_runtime_token(tmp_path):
    shell, cfg = toggle._make_slot_shell(tmp_path)
    try:
        shell._spawn_slot(1)
        previous = _child(shell)
        token = previous._profile_generation
        shell.exit_pet(previous)
        shell._spawn_slot(1)
        current = _child(shell)
        assert current._profile_generation > token
        assert profiles.write_profile_geometry(cfg.dir, 1, {"scale": 1.7},
                                               generation=token).status == "stale-generation"
        assert shell._save_child_geometry(current)
    finally:
        toggle._teardown(shell)


def test_child_missing_character_falls_back_without_dirtying_main(tmp_path):
    shell, cfg = toggle._make_slot_shell(tmp_path)
    def create(cid):
        if cid == "missing-character":
            raise FileNotFoundError(cid)
        return fac.RichLibrary()
    shell._instance._create_library = create
    try:
        shell.spawn_pet()
        child = _child(shell)
        before = cfg.path.read_bytes()
        shell.switch_character("missing-character", child)
        assert child._profile_character == catalog.DEFAULT_CHARACTER
        assert profiles.read_slot_profile(cfg.dir, 1).value("character") == catalog.DEFAULT_CHARACTER
        assert cfg.path.read_bytes() == before
    finally:
        toggle._teardown(shell)


def test_no_directory_substitute_uses_memory_without_fabricated_token(tmp_path):
    shell, _ = fac._make_shell(tmp_path)
    try:
        shell._config.dir = None
        shell._spawn_slot(1)
        child = _child(shell)
        assert shell._sprite_config(child).generation is None
        assert fac.SpriteMenuFacade(shell, child).set_playback_speed(0.5)
        assert shell._sprite_config(child).get("playback_speed") == 0.5
        assert child.playback_speed == 0.5
    finally:
        toggle._teardown(shell)
