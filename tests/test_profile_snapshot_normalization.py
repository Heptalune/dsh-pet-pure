from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from pet import catalog, config as config_mod, pet_profiles as profiles
from pet.config import Config
from pet.overlay_shell import _SpriteConfig
from pet.settings_slot_config import SlotRoutedConfig


def _views(tmp_path, raw):
    main = Config(base=tmp_path)
    main.dir.mkdir(parents=True, exist_ok=True)
    path = profiles.slot_profile_path(main.dir, 1)
    path.write_text(json.dumps(raw), encoding="utf-8")
    instance = SimpleNamespace(dir=main.dir, path=path, instance_id="slot-1", data={})
    route = SlotRoutedConfig(instance, main, 1)
    sprite = object()
    shell = SimpleNamespace(_spawned_slots={sprite: 1}, _config=main,
                            _spawn_config_dir=lambda: main.dir)
    snapshot = profiles.read_slot_snapshot(main.dir, 1)
    return route, _SpriteConfig(shell, sprite, snapshot=snapshot), path


@pytest.mark.parametrize("version", [1, None, "bad", float("nan")])
def test_legacy_scale_uses_same_default_at_both_consumers(tmp_path, version):
    route, runtime, _ = _views(tmp_path, {"version": version, "scale": 1.7})
    assert runtime.get("scale") == route.get("scale") == catalog.DEFAULT_SCALE


@pytest.mark.parametrize("pack", [None, [], {"kind": "invalid"}, {"kind": ""}])
def test_legacy_custom_sound_survives_at_both_consumers(tmp_path, pack):
    raw = {"version": 4, "click_sound_path": "custom.wav", "click_sound_pack": pack}
    route, runtime, _ = _views(tmp_path, raw)
    expected = {"kind": "file", "id": "custom", "path": "custom.wav"}
    assert runtime.get("click_sound_pack") == route.get("click_sound_pack") == expected


def test_settings_normalize_captured_raw_without_reopening_config(tmp_path, monkeypatch):
    route, runtime, path = _views(tmp_path, {"version": 4, "playback_speed": 0.5})
    route._slot_values()
    token = route._slot_snapshot.generation
    path.write_text(json.dumps({"version": 4, "playback_speed": 3.0}), encoding="utf-8")
    monkeypatch.setattr(Config, "__init__", lambda *a, **kw: pytest.fail("Config constructed"))
    monkeypatch.setattr(profiles, "read_slot_snapshot", lambda *a, **kw: pytest.fail("extra IO"))
    assert route.get("playback_speed") == runtime.get("playback_speed") == 0.5
    assert route._slot_snapshot.generation == token
    for _ in range(1000):
        assert route.get("playback_speed") == 0.5


@pytest.mark.parametrize("raw", [None, [], {},
    {"version": 4, "playback_speed": "bad", "bubble_text_scale": float("nan"),
     "drop_gravity": "false", "click_sound_pack": []},
    {"version": 4, "scale": 1.7, "playback_speed": 0.5, "self_talk_texts": ["hello"],
     "click_sound_pack": {"kind": "folder", "id": "custom", "path": "sounds"}},
])
def test_pure_snapshot_no_io_mutation_or_default_alias(raw, monkeypatch):
    normalize = getattr(config_mod, "normalize_pet_profile_snapshot", None)
    assert callable(normalize)
    before = copy.deepcopy(raw)
    monkeypatch.setattr(Config, "__init__", lambda *a, **kw: pytest.fail("Config constructed"))
    monkeypatch.setattr(Config, "_normalize_pet_settings", lambda *a: pytest.fail("global normalization"))
    result = normalize(raw)
    assert repr(raw) == repr(before)
    assert set(result) == profiles.PER_PET_KEYS
    result["self_talk_texts"].append("changed")
    assert "changed" not in normalize({})["self_talk_texts"]
    result["click_sound_pack"]["path"] = "changed"
    assert normalize({})["click_sound_pack"]["path"] == ""


def test_setter_and_defaults_do_not_mutate_snapshot(tmp_path):
    raw = {"version": 4, "self_talk_texts": ["original"]}
    route, runtime, _ = _views(tmp_path, raw)
    route.set("self_talk_texts", ["draft"])
    assert runtime.get("self_talk_texts") == ["original"]
    assert route.get("self_talk_texts") == ["draft"]
    assert route._slot_values()["self_talk_texts"] == ["original"]
    assert profiles.same_source_default("self_talk_texts") != ["draft"]


def test_untrusted_snapshot_defaults_without_unlocked_read(tmp_path, monkeypatch):
    route, _, path = _views(tmp_path, {"version": 4, "playback_speed": 3.0})
    path.write_text("{broken", encoding="utf-8")
    before = path.read_bytes()
    monkeypatch.setattr(profiles, "read_slot_snapshot", lambda *a, **kw:
                        profiles.SlotSnapshot(slot=1, status="lock-unavailable", profile=None,
                                              generation=None))
    monkeypatch.setattr(profiles, "read_slot_profile", lambda *a, **kw: pytest.fail("unlocked IO"))
    assert route.get("playback_speed") == profiles.same_source_default("playback_speed")
    route.set("playback_speed", 0.5)
    assert route.save() is False
    assert path.read_bytes() == before


def test_config_migration_save_retains_scale_and_sound_delta(tmp_path):
    cfg = Config(base=tmp_path)
    cfg.dir.mkdir(parents=True, exist_ok=True)
    cfg.path.write_text(json.dumps({"version": 1, "scale": 1.7,
                                   "click_sound_pack": {"kind": "invalid"},
                                   "click_sound_path": "custom.wav"}), encoding="utf-8")
    cfg = Config(base=tmp_path)
    assert cfg.get("scale") == catalog.DEFAULT_SCALE
    assert cfg.get("click_sound_pack")["path"] == "custom.wav"
    assert cfg.save()
    saved = dict(profiles.read_slot_profile(cfg.dir, 0).data)
    assert saved["scale"] == catalog.DEFAULT_SCALE
    assert saved["click_sound_pack"]["path"] == "custom.wav"
    assert "click_sound_path" not in saved


@pytest.mark.parametrize("value", ["bad", {}, [], float("nan"), 10**400])
def test_bad_scale_and_numeric_types_match_config_defaults(tmp_path, value):
    raw = {"version": 4, "scale": value, "playback_speed": value}
    route, runtime, _ = _views(tmp_path, raw)
    for key in ("scale", "playback_speed"):
        assert runtime.get(key) == route.get(key) == profiles.same_source_default(key)


def test_valid_modern_values_preserved_and_pack_wins_over_legacy(tmp_path):
    raw = {"version": 4, "scale": 1.7, "playback_speed": 0.5,
           "click_sound_pack": {"kind": "folder", "id": "custom", "path": "sounds"},
           "click_sound_path": "old.wav", "self_talk_texts": ["hello"]}
    route, runtime, _ = _views(tmp_path, raw)
    for key in ("scale", "playback_speed", "click_sound_pack", "self_talk_texts"):
        assert route.get(key) == runtime.get(key) == raw[key]


def test_memory_only_modern_settings_are_not_migrated_as_v1():
    sprite = object()
    main = SimpleNamespace(get=lambda key, default=None: default)
    shell = SimpleNamespace(_spawned_slots={sprite: 1}, _config=main,
                            _slot_memory={1: {"scale": 1.7}}, _spawn_config_dir=lambda: None)
    runtime = _SpriteConfig(shell, sprite)
    assert runtime.get("scale") == 1.7
    assert runtime.generation is None


def test_pure_entry_imports_no_qt_or_keyring_in_fresh_process():
    import subprocess
    import sys

    code = '''
import sys
from pet.config import normalize_pet_profile_snapshot
assert normalize_pet_profile_snapshot({"version": 1, "scale": 1.7})["scale"] == 0.72
assert not any(name.startswith(("PySide6", "keyring", "pet.chat")) for name in sys.modules)
'''
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
