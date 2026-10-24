from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pet import config as config_mod
from pet import config_write_lock as cwl
from pet.config import Config


_CHILD = """
import json, sys
from pet.config import Config
cfg = Config(base=sys.argv[1])
print('ready', flush=True)
for line in sys.stdin:
    action = json.loads(line)
    if action['op'] == 'reset':
        ok = cfg.reset_to_defaults()
    else:
        for path, value in action.get('set', []):
            node = cfg.data
            for key in path[:-1]:
                node = node.setdefault(key, {})
            node[path[-1]] = value
        for path in action.get('delete', []):
            node = cfg.data
            for key in path[:-1]:
                node = node[key]
            node.pop(path[-1], None)
        ok = cfg.save()
    print(json.dumps(ok), flush=True)
"""


@pytest.fixture
def writers(tmp_path):
    seed = Config(base=tmp_path)
    seed.data['character_aliases'] = {'removed': 'old', 'kept': 'keep'}
    seed.set('weather_city', 'old city')
    assert seed.save()
    processes = []

    def start():
        process = subprocess.Popen(
            [sys.executable, '-u', '-c', _CHILD, str(tmp_path)],
            cwd=Path(__file__).resolve().parents[1], env={**os.environ, 'DSH_PET_INSTANCE': ''},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8',
        )
        processes.append(process)
        assert process.stdout.readline().strip() == 'ready'
        return process

    yield seed, start
    for process in processes:
        process.stdin.close()
        process.wait(timeout=20)
        assert process.returncode == 0, process.stderr.read()


def _commit(process, **action):
    process.stdin.write(json.dumps({'op': 'save', **action}) + '\n')
    process.stdin.flush()
    return json.loads(process.stdout.readline())


def _disk(cfg):
    return json.loads(cfg.path.read_text(encoding='utf-8'))


def test_subprocess_disjoint_and_same_key_commit_order(writers):
    cfg, start = writers
    first, second = start(), start()
    assert _commit(first, set=[(['weather_city'], 'first'), (['on_top'], False)])
    assert _commit(second, set=[(['weather_city'], 'second'), (['no_move'], True)])
    raw = _disk(cfg)
    assert raw['weather_city'] == 'second'
    assert raw['on_top'] is False
    assert raw['no_move'] is True


def test_subprocess_nested_chat_and_deletion(writers):
    cfg, start = writers
    first, second = start(), start()
    provider = ['chat', 'providers', 'openai-main']
    assert _commit(first, set=[(provider + ['model'], 'first-model'),
                               (provider + ['new_object', 'first'], 1)],
                   delete=[['character_aliases', 'removed']])
    assert _commit(second, set=[(provider + ['timeout'], 33),
                               (provider + ['new_object', 'second'], 2),
                               (['character_aliases', 'new'], 'new name')])
    raw = _disk(cfg)
    assert raw['chat']['providers']['openai-main']['model'] == 'first-model'
    assert raw['chat']['providers']['openai-main']['timeout'] == 33
    assert raw['chat']['providers']['openai-main']['new_object'] == {'first': 1, 'second': 2}
    assert raw['character_aliases'] == {'kept': 'keep', 'new': 'new name'}


def test_subprocess_reset_rejects_old_dirty_snapshot(writers):
    cfg, start = writers
    old, resetter = start(), start()
    assert _commit(resetter, op='reset')
    after = cfg.path.read_bytes()
    assert not _commit(old, set=[(['weather_city'], 'late city')])
    assert cfg.path.read_bytes() == after
    assert _disk(cfg)['weather_city'] == ''


def test_deleted_default_not_restored_by_reload_or_normalize(tmp_path):
    cfg = Config(base=tmp_path)
    assert cfg.save()
    other = Config(base=tmp_path)
    cfg.data.pop('click_sound_volume')
    assert cfg.save()
    assert 'click_sound_volume' not in _disk(cfg)
    other.reload()
    other.set('on_top', False)
    assert other.save()
    assert 'click_sound_volume' not in _disk(cfg)


def test_failed_save_keeps_changes_for_retry(tmp_path, monkeypatch):
    cfg = Config(base=tmp_path)
    assert cfg.save()
    other = Config(base=tmp_path)
    cfg.set('on_top', False)
    real = config_mod.atomic_replace_with_retry
    with monkeypatch.context() as patch:
        patch.setattr(config_mod, 'atomic_replace_with_retry',
                      lambda *args: (_ for _ in ()).throw(OSError('injected')))
        assert cfg.save() is False
    other.set('no_move', True)
    assert other.save()
    assert cfg.get('on_top') is False
    monkeypatch.setattr(config_mod, 'atomic_replace_with_retry', real)
    assert cfg.save()
    assert _disk(cfg)['on_top'] is False
    assert _disk(cfg)['no_move'] is True
    assert not list(cfg.dir.glob('*.tmp'))


@pytest.mark.parametrize('broken', [b'{bad', b'[]', b'\xff\xfe'])
def test_broken_disk_refused_and_preserved(tmp_path, broken):
    cfg = Config(base=tmp_path)
    assert cfg.save()
    cfg.path.write_bytes(broken)
    cfg.set('on_top', False)
    assert cfg.save() is False
    assert cfg.path.read_bytes() == broken
    assert cfg.get('on_top') is False


def test_lock_failure_and_secret_redaction(tmp_path, monkeypatch):
    cfg = Config(base=tmp_path)
    assert cfg.save()
    provider = cfg.data['chat']['providers']['openai-main']
    provider['api_key'] = 'memory-only-api'
    provider['vision_api_key'] = 'memory-only-vision'
    cfg.set('on_top', False)
    with monkeypatch.context() as patch:
        def timeout(*args, **kwargs):
            raise cwl.WriteLockTimeout('injected timeout')
        patch.setattr(cwl, 'config_write_lock', timeout)
        assert cfg.save() is False
    assert cfg.save()
    assert cfg.data['chat']['providers']['openai-main']['api_key'] == 'memory-only-api'
    assert 'memory-only' not in cfg.path.read_text(encoding='utf-8')


def test_normalization_and_repeated_deletion(tmp_path):
    cfg = Config(base=tmp_path)
    assert cfg.save()
    cfg.data['quiet_minutes_default'] = -9
    assert cfg.save()
    assert _disk(cfg)['quiet_minutes_default'] >= 0
    cfg.data.pop('quiet_minutes_default')
    cfg.set('weather_city', 'city')
    assert cfg.save()
    assert cfg.save()
    assert 'quiet_minutes_default' not in _disk(cfg)


def test_reset_partial_failure_is_safe_and_retryable(tmp_path, monkeypatch):
    cfg = Config(base=tmp_path)
    cfg.set('weather_city', 'old city')
    assert cfg.save()
    old = Config(base=tmp_path)
    before = cfg.path.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(config_mod, 'atomic_replace_with_retry',
                      lambda *args: (_ for _ in ()).throw(OSError('injected')))
        assert cfg.reset_to_defaults() is False
    assert cfg.get('weather_city') == 'old city'
    assert cfg.path.read_bytes() == before
    old.set('weather_city', 'late city')
    assert old.save() is False
    assert cfg.reset_to_defaults()
    assert _disk(cfg)['weather_city'] == ''


def test_first_save_race_only_commits_changed_keys(tmp_path):
    first, second = Config(base=tmp_path), Config(base=tmp_path)
    first.set('no_move', True)
    second.set('on_top', False)
    assert first.save()
    assert second.save()
    assert _disk(first)['no_move'] is True
    assert _disk(first)['on_top'] is False


def test_legacy_schema_migration_still_persists(tmp_path):
    cfg = Config(base=tmp_path)
    cfg.dir.mkdir(parents=True)
    cfg.path.write_text(json.dumps({'version': 1, 'scale': 999,
                                    'chat_enabled': False, 'chat_model': 'legacy-model'}),
                        encoding='utf-8')
    cfg = Config(base=tmp_path)
    assert cfg.save()
    raw = _disk(cfg)
    assert raw['version'] == 4
    from pet import pet_profiles

    assert 'scale' not in raw
    assert pet_profiles.read_slot_profile(cfg.dir, 0).value('scale') == cfg.get('scale')
    assert raw['chat']['enabled'] is False
    assert raw['chat']['providers']['openai-main']['model'] == 'legacy-model'
    assert 'chat_model' not in raw


def test_real_subprocess_lock_timeout_preserves_dirty(writers):
    cfg, _start = writers
    script = """
import sys
from pet.config_write_lock import config_write_lock
with config_write_lock(sys.argv[1]):
    print('locked', flush=True)
    sys.stdin.readline()
"""
    holder = subprocess.Popen(
        [sys.executable, '-u', '-c', script, str(cfg.dir)],
        cwd=Path(__file__).resolve().parents[1], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
    try:
        assert holder.stdout.readline().strip() == 'locked'
        before = cfg.path.read_bytes()
        cfg.set('on_top', False)
        assert cfg.save() is False
        assert cfg.get('on_top') is False
        assert cfg.path.read_bytes() == before
    finally:
        holder.stdin.close()
        holder.wait(timeout=20)
        assert holder.returncode == 0, holder.stderr.read()
    assert cfg.save()
    assert _disk(cfg)['on_top'] is False


def test_nested_directory_lock_save_is_reentrant(tmp_path):
    cfg = Config(base=tmp_path)
    with cwl.config_write_lock(cfg.dir):
        assert cfg.save()


def test_partial_reset_cannot_be_laundered_by_reload(tmp_path, monkeypatch):
    cfg = Config(base=tmp_path)
    cfg.set('weather_city', 'old city')
    assert cfg.save()
    old = Config(base=tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(config_mod, 'atomic_replace_with_retry',
                      lambda *args: (_ for _ in ()).throw(OSError('injected')))
        assert cfg.reset_to_defaults() is False
    before = cfg.path.read_bytes()
    old.reload()
    old.set('on_top', False)
    assert old.save() is False
    fresh = Config(base=tmp_path)
    assert fresh.save() is False
    assert cfg.path.read_bytes() == before
    assert fresh.reset_to_defaults()
    assert Config(base=tmp_path).get('weather_city') == ''


@pytest.mark.parametrize('phase', ['begin', 'config', 'complete'])
def test_reset_each_write_failure_is_recoverable(tmp_path, monkeypatch, phase):
    cfg = Config(base=tmp_path)
    cfg.set('weather_city', 'old city')
    assert cfg.save()
    before = cfg.path.read_bytes()
    real_write = cwl.atomic_write_json
    calls = 0
    with monkeypatch.context() as patch:
        def write(path, data):
            nonlocal calls
            calls += 1
            if (phase == 'begin' and calls == 1 or
                    phase == 'complete' and calls == 2):
                raise cwl.AtomicWriteFailed('injected sidecar failure')
            return real_write(path, data)
        patch.setattr(cwl, 'atomic_write_json', write)
        if phase == 'config':
            patch.setattr(config_mod, 'atomic_replace_with_retry',
                          lambda *args: (_ for _ in ()).throw(OSError('injected')))
        assert cfg.reset_to_defaults() is False
    assert cfg.get('weather_city') == 'old city'
    if phase != 'complete':
        assert cfg.path.read_bytes() == before
    if phase != 'begin':
        assert Config(base=tmp_path).save() is False
    assert cfg.reset_to_defaults()
    assert Config(base=tmp_path).get('weather_city') == ''


@pytest.mark.parametrize('token', [7, 'old-token'])
def test_legacy_generation_sidecar_compatible(tmp_path, token):
    cfg = Config(base=tmp_path)
    assert cfg.save()
    sidecar = cfg.dir / '.config-reset-generations.json'
    sidecar.write_text(json.dumps({'version': 1, 'generations': {'config.json': token}}),
                       encoding='utf-8')
    fresh = Config(base=tmp_path)
    assert fresh.save()
    assert fresh.reset_to_defaults()
    assert json.loads(sidecar.read_text(encoding='utf-8'))['version'] == 2


@pytest.mark.parametrize('value', [True, -1, None, {}, {'token': 'x', 'pending': 1}])
def test_invalid_generation_sidecar_refuses_every_write(tmp_path, value):
    cfg = Config(base=tmp_path)
    assert cfg.save()
    sidecar = cfg.dir / '.config-reset-generations.json'
    sidecar.write_text(json.dumps({'version': 2, 'generations': {'config.json': value}}),
                       encoding='utf-8')
    before = cfg.path.read_bytes(), sidecar.read_bytes()
    assert cfg.save() is False
    assert cfg.reset_to_defaults() is False
    assert (cfg.path.read_bytes(), sidecar.read_bytes()) == before


def test_dirty_config_reload_cannot_rebind_to_completed_reset(tmp_path):
    cfg = Config(base=tmp_path)
    assert cfg.save()
    old = Config(base=tmp_path)
    old.set('weather_city', 'draft')
    assert cfg.reset_to_defaults()
    before = cfg.path.read_bytes()
    old.reload()
    assert old.save() is False
    assert old.get('weather_city') == 'draft'
    assert cfg.path.read_bytes() == before


def test_click_sound_legacy_path_save_reload_preserves_custom_pack(tmp_path):
    cfg = Config(base=tmp_path)
    cfg.dir.mkdir(parents=True)
    cfg.path.write_text(json.dumps({'version': 4, 'click_sound_path': 'custom.wav'}),
                        encoding='utf-8')
    cfg = Config(base=tmp_path)
    expected = {'kind': 'file', 'id': 'custom', 'path': 'custom.wav'}
    assert cfg.get('click_sound_pack') == expected
    assert cfg.save()
    from pet.settings_slot_config import route_settings_config

    assert route_settings_config(Config(base=tmp_path, instance_id='slot-0')).get('click_sound_pack') == expected
    assert 'click_sound_path' not in _disk(cfg)


def test_provider_ref_migration_persists_without_plaintext(tmp_path, monkeypatch):
    class Store:
        def get(self, ref):
            return ''

        def set(self, ref, value):
            return True
    monkeypatch.setattr('pet.chat.models.SecretStore', Store)
    cfg = Config(base=tmp_path)
    cfg.dir.mkdir(parents=True)
    cfg.path.write_text(json.dumps({'version': 4, 'chat': {'providers': {
        'custom': {'api_key_ref': 'provider/openai-main',
                   'vision_api_key': 'fake-test-secret'}}}}), encoding='utf-8')
    cfg = Config(base=tmp_path)
    assert cfg.save()
    provider = _disk(cfg)['chat']['providers']['custom']
    assert provider['api_key_ref'] == 'provider/custom'
    assert provider['vision_api_key_ref'] == 'provider/custom/vision'
    assert 'fake-test-secret' not in cfg.path.read_text(encoding='utf-8')


def test_reload_save_gap_keeps_other_commit(tmp_path):
    cfg = Config(base=tmp_path)
    assert cfg.save()
    other = Config(base=tmp_path)
    cfg.reload()
    other.set('no_move', True)
    assert other.save()
    cfg.set('on_top', False)
    assert cfg.save()
    assert _disk(cfg)['no_move'] is True
