from __future__ import annotations

import multiprocessing

from pet import overlay_settings_command as cmd


def _writer(directory, started, done):
    started.set()
    assert cmd.write_command(directory, cmd.CMD_EXIT_SPAWNED_PETS, target=2)
    done.set()


def _consumer(directory, read, release, output):
    real_read = cmd._read_text
    def controlled(path):
        raw = real_read(path)
        if raw and not read.is_set():
            read.set()
            assert release.wait(15)
        return raw
    cmd._read_text = controlled
    output.put(cmd.consume_command(directory))


def test_unconsumed_a_and_later_b_both_execute(tmp_path):
    assert cmd.write_command(tmp_path, cmd.CMD_PROFILE_RESET, target=1)
    observed = cmd.read_command(tmp_path)
    assert cmd.write_command(tmp_path, cmd.CMD_EXIT_SPAWNED_PETS, target=2)
    first = cmd.consume_command(tmp_path)
    second = cmd.consume_command(tmp_path)
    assert first["command"] == observed["command"]
    assert first["target"] == 1 and second["target"] == 2


def test_process_writer_b_during_consumer_read_is_not_deleted(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    read, release, started, done = [ctx.Event() for _ in range(4)]
    output = ctx.Queue()
    assert cmd.write_command(tmp_path, cmd.CMD_PROFILE_RESET, target=1)
    consumer = ctx.Process(target=_consumer, args=(str(tmp_path), read, release, output))
    writer = ctx.Process(target=_writer, args=(str(tmp_path), started, done))
    consumer.start()
    try:
        assert read.wait(15)
        writer.start()
        assert started.wait(15)
        release.set()
        consumer.join(15)
        writer.join(15)
        assert consumer.exitcode == writer.exitcode == 0
        first = output.get(timeout=5)
        second = cmd.consume_command(tmp_path)
        assert first["target"] == 1 and second["target"] == 2
        assert done.is_set()
    finally:
        release.set()
        for process in (consumer, writer):
            if process.is_alive():
                process.terminate()
                process.join(5)
        output.close()


def test_queue_cap_rejects_instead_of_overwriting(tmp_path):
    for slot in range(32):
        assert cmd.write_command(tmp_path, cmd.CMD_EXIT_SPAWNED_PETS, target=slot)
    assert cmd.write_command(tmp_path, cmd.CMD_PROFILE_RESET, target=1) is False
    assert [cmd.consume_command(tmp_path)["target"] for _ in range(32)] == list(range(32))
    assert cmd.consume_command(tmp_path) is None


def test_claim_ack_only_removes_a_not_later_b(tmp_path):
    assert cmd.write_command(tmp_path, cmd.CMD_PROFILE_RESET, target=1)
    claim = cmd.claim_command(tmp_path)
    assert claim["claim"]
    assert cmd.write_command(tmp_path, cmd.CMD_EXIT_SPAWNED_PETS, target=2)
    assert cmd.finish_command(tmp_path, claim)
    assert cmd.consume_command(tmp_path)["target"] == 2


def test_bad_entry_is_skipped_without_blocking_legal_entry(tmp_path):
    import json
    import time

    now = time.time()
    legal = {"version": 1, "command": cmd.CMD_EXIT_SPAWNED_PETS,
             "target": 2, "issued_at": now, "id": "legal"}
    cmd.command_path(tmp_path).write_text(json.dumps({"pending": [
        {"version": 1, "command": "bad", "issued_at": now}, legal]}), encoding="utf-8")
    assert cmd.consume_command(tmp_path)["target"] == 2
    assert not cmd.command_path(tmp_path).exists()


def test_failed_claim_rotates_then_expires_after_three_attempts(tmp_path, caplog):
    assert cmd.write_command(tmp_path, cmd.CMD_PROFILE_RESET, target=1)
    assert cmd.write_command(tmp_path, cmd.CMD_EXIT_SPAWNED_PETS, target=2)
    first = cmd.claim_command(tmp_path)
    assert cmd.finish_command(tmp_path, first, retry=True)
    assert cmd.consume_command(tmp_path)["target"] == 2
    for _ in range(2):
        assert cmd.finish_command(tmp_path, cmd.claim_command(tmp_path), retry=True)
    assert cmd.read_command(tmp_path) is None
    assert "重试耗尽" in caplog.text


def test_profile_apply_is_outside_lock_and_b_executes(tmp_path, monkeypatch):
    from tests import test_overlay_collision_toggle as toggle

    shell, cfg = toggle._make_slot_shell(tmp_path)
    ctx = multiprocessing.get_context("spawn")
    started, done = ctx.Event(), ctx.Event()
    process = ctx.Process(target=_writer, args=(str(cfg.dir), started, done))
    try:
        shell.spawn_pet()
        shell.spawn_pet()
        other = shell._spawned[1]
        real_apply = shell._apply_child_profile
        def apply(slot):
            process.start()
            assert done.wait(15), "角色应用持有目录锁，阻塞 writer B"
            process.join(5)
            return real_apply(slot)
        monkeypatch.setattr(shell, "_apply_child_profile", apply)
        assert cmd.write_command(cfg.dir, cmd.CMD_PROFILE_RESET, target=1)
        shell._consume_settings_command()
        assert cmd.read_command(cfg.dir)["target"] == 2
        shell._consume_settings_command()
        assert other not in shell._spawned
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
        toggle._teardown(shell)
