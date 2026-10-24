# -*- coding: utf-8 -*-
"""Compatibility seams for fixed profile identities and active-set v2."""
from pathlib import Path

from . import pet_profiles as profiles
from .config import normalize_pet_profile_snapshot

ACTIVE_PETS_FILENAME = profiles.ACTIVE_SET_FILENAME
ACTIVE_PETS_VERSION = profiles.ACTIVE_SET_VERSION
MAX_SCAN_SLOTS = profiles.SLOT_MAX
PER_SLOT_SETTING_KEYS = profiles.PER_PET_SETTING_KEYS - {'collision_enabled'}
SpawnStateError = profiles.CapacityFullError


def active_pets_path(config_dir):
    return profiles.active_set_path(config_dir)


def normalize_slots(slots):
    return profiles.normalize_slots(slots)


def load_active_slots(config_dir):
    return list(profiles.load_active_set(config_dir).slots)


def save_active_slots(config_dir, slots):
    return profiles.save_active_set(config_dir, slots).ok


def slot_config_path(config_dir, slot_id):
    return profiles.slot_profile_path(config_dir, slot_id)


def scan_slot_configs(config_dir):
    if not config_dir:
        return set()
    return {n for n in range(MAX_SCAN_SLOTS + 1)
            if profiles.slot_profile_exists(Path(config_dir), n)}


def allocate_slot(config_dir=None, *, active_slots=(), max_slots=MAX_SCAN_SLOTS):
    return profiles.allocate_slot(active_slots=active_slots, max_slot=max_slots)


def _load_slot_config(config_dir, slot_id):
    return dict(profiles.read_slot_profile(config_dir, slot_id).data)


def _clean_geometry(geometry):
    return profiles._clean_geometry(geometry)[0]


def slot_config_exists(config_dir, slot_id):
    return profiles.slot_profile_exists(config_dir, slot_id)


def read_slot_geometry(config_dir, slot_id):
    return _clean_geometry(_load_slot_config(config_dir, slot_id))


def read_slot_config(config_dir, slot_id):
    profile = profiles.read_slot_profile(config_dir, slot_id)
    return dict(profile.data) if profile.exists else None


def write_slot_setting(config_dir, slot_id, key, value, *, user_customized=None):
    return profiles.write_profile_settings(config_dir, slot_id, {key: value},
                                          user_customized=user_customized).ok


def read_slot_collision_enabled(config_dir, slot_id, *, default=True, fallback=None):
    return bool(normalize_pet_profile_snapshot(_load_slot_config(config_dir, slot_id)).get(
        'collision_enabled', default))


def write_slot_geometry(config_dir, slot_id, geometry):
    return profiles.write_profile_geometry(config_dir, slot_id, geometry).ok
