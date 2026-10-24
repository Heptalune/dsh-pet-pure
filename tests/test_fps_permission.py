# -*- coding: utf-8 -*-
"""fps_permission：令牌判定、SID 校验防注入、命令构造、授权结果映射（不真的弹 UAC）。"""
from __future__ import annotations

import base64
import subprocess
import sys
from types import SimpleNamespace

import pytest

from pet import fps_permission as fp

SID = "S-1-5-21-111-222-333-1001"
PS = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"


@pytest.fixture(autouse=True)
def _reset_state():
    fp._state["granted"] = False
    yield
    fp._state["granted"] = False


def _decode(b64: str) -> str:
    return base64.b64decode(b64).decode("utf-16-le")


@pytest.mark.parametrize(
    "member, granted, expected",
    [
        (True, False, fp.STATUS_ACTIVE),
        (False, False, fp.STATUS_MISSING),
        (False, True, fp.STATUS_PENDING),
        (None, False, fp.STATUS_UNSUPPORTED),
    ],
)
def test_query_status(monkeypatch, member, granted, expected):
    monkeypatch.setattr(fp, "is_supported", lambda: True)
    assert fp.query_status(granted, check=lambda: member, in_group=lambda: None) == expected


def test_membership_false_overrides_stale_process_grant(monkeypatch):
    """本进程加过、之后被移出组：成员列表明确 False → 未授权，不沿用旧记录。"""
    monkeypatch.setattr(fp, "is_supported", lambda: True)
    fp.mark_granted()
    assert fp.query_status(check=lambda: False, in_group=lambda: False) == fp.STATUS_MISSING


@pytest.mark.parametrize("in_group, expected", [
    (True, fp.STATUS_PENDING), (False, fp.STATUS_MISSING), (None, fp.STATUS_MISSING)])
def test_query_status_uses_real_membership_across_processes(monkeypatch, in_group, expected):
    """独立设置进程重开：本进程没记录，但账户已在组里 → 待重新登录。"""
    monkeypatch.setattr(fp, "is_supported", lambda: True)
    assert fp.query_status(check=lambda: False, in_group=lambda: in_group) == expected


def test_query_status_uses_process_level_grant(monkeypatch):
    monkeypatch.setattr(fp, "is_supported", lambda: True)
    assert fp.query_status(check=lambda: False, in_group=lambda: None) == fp.STATUS_MISSING
    fp.mark_granted()
    assert fp.query_status(check=lambda: False, in_group=lambda: None) == fp.STATUS_PENDING


def test_query_status_non_windows(monkeypatch):
    monkeypatch.setattr(fp, "is_supported", lambda: False)
    assert fp.query_status(check=lambda: True) == fp.STATUS_UNSUPPORTED


@pytest.mark.parametrize(
    "sid, ok",
    [
        (SID, True),
        ("S-1-5-32-559", True),
        ("", False),
        ("S-1-5-21-1'; Remove-Item C:\\", False),
        ("S-1-5-21-1\u2019;calc;#", False),
        ("PC\\me", False),
        ("S-1", False),
    ],
)
def test_is_valid_sid(sid, ok):
    assert fp.is_valid_sid(sid) is ok


def test_inner_script_targets_sids_and_checks_deadline():
    script = fp.build_inner_script(SID, 1_700_000_000)
    assert f"-Member '{SID}'" in script
    assert "-SID 'S-1-5-32-559'" in script
    assert "MemberExists" in script
    assert f"-gt 1700000000){{exit {fp.EXIT_EXPIRED}}}" in script
    # 过期检查必须在加组之前
    assert script.index("ToUnixTimeSeconds") < script.index("Add-LocalGroupMember")


@pytest.mark.parametrize("bad", ["PC\\o'brien", "S-1-5-21-1\u2018x", ""])
def test_inner_script_rejects_non_sid(bad):
    with pytest.raises(ValueError):
        fp.build_inner_script(bad, 1)


def test_ps_quote_rejects_all_quote_kinds():
    for ch in "'\u2018\u2019\u201a\u201b":
        with pytest.raises(ValueError):
            fp._ps_quote(f"a{ch}b")


def test_build_command_uses_absolute_powershell_and_maps_cancel_by_native_code():
    cmd = fp.build_command(SID, PS, 1)
    assert cmd[:4] == [PS, "-NoProfile", "-NonInteractive", "-EncodedCommand"]
    outer = _decode(cmd[4])
    assert f"-FilePath '{PS}'" in outer
    assert "-Verb RunAs" in outer and "-Wait" in outer and "-ErrorAction Stop" in outer
    assert f"NativeErrorCode -eq {fp.EXIT_CANCELLED}" in outer
    assert outer.rstrip("}").endswith(f"exit {fp.EXIT_LAUNCH_FAILED}")
    inner_b64 = outer.split("'-EncodedCommand','")[1].split("'")[0]
    assert f"-Member '{SID}'" in _decode(inner_b64)


def _runner(code=0, exc=None):
    calls = []

    def run(cmd, **kw):
        calls.append((cmd, kw))
        if exc:
            raise exc
        return SimpleNamespace(returncode=code, stdout="", stderr="")

    return run, calls


@pytest.mark.parametrize(
    "code, outcome",
    [
        (0, "ok"),
        (fp.EXIT_CANCELLED, "cancelled"),
        (fp.EXIT_ADD_FAILED, "error"),
        (fp.EXIT_EXPIRED, "error"),
        (fp.EXIT_LAUNCH_FAILED, "error"),
    ],
)
def test_grant_maps_exit_codes(monkeypatch, code, outcome):
    monkeypatch.setattr(fp, "is_supported", lambda: True)
    run, calls = _runner(code)
    res = fp.grant(run=run, sid=SID, powershell=PS, now=lambda: 1000.0)
    assert res.outcome == outcome
    assert len(calls) == 1 and calls[0][1]["timeout"] == fp.GRANT_TIMEOUT_S
    assert fp.granted_this_process() is (code == 0)
    inner = _decode(_decode(calls[0][0][4]).split("'-EncodedCommand','")[1].split("'")[0])
    deadline = int(1000 + fp.GRANT_TIMEOUT_S - fp.DEADLINE_MARGIN_S)
    assert f"-gt {deadline})" in inner


def test_grant_timeout_is_unknown_not_finished(monkeypatch):
    monkeypatch.setattr(fp, "is_supported", lambda: True)
    run, _ = _runner(exc=subprocess.TimeoutExpired("x", 1))
    res = fp.grant(run=run, sid=SID, powershell=PS)
    assert res.outcome == "unknown"
    assert not fp.granted_this_process()


def test_grant_oserror(monkeypatch):
    monkeypatch.setattr(fp, "is_supported", lambda: True)
    run, _ = _runner(exc=OSError("nope"))
    assert fp.grant(run=run, sid=SID, powershell=PS).outcome == "error"


def test_grant_refuses_bad_inputs_without_running(monkeypatch):
    monkeypatch.setattr(fp, "is_supported", lambda: False)
    run, calls = _runner()
    assert fp.grant(run=run).outcome == "unsupported"
    monkeypatch.setattr(fp, "is_supported", lambda: True)
    assert fp.grant(run=run, sid="", powershell=PS).outcome == "error"
    assert fp.grant(run=run, sid="PC\\me'x", powershell=PS).outcome == "error"
    assert fp.grant(run=run, sid=SID, powershell="").outcome == "error"
    assert not calls


@pytest.mark.skipif(sys.platform != "win32", reason="真实令牌 API 仅 Windows")
def test_real_token_apis():
    assert fp.is_valid_sid(fp.current_user_sid())
    assert fp.token_has_group() in (True, False)
    assert fp.system_powershell().lower().endswith("powershell.exe")
    assert fp.query_status() in {fp.STATUS_ACTIVE, fp.STATUS_MISSING, fp.STATUS_PENDING}
    assert fp.account_in_group() in (True, False)
    assert fp.account_in_group("S-1-5-21-1-2-3-999") is False
    assert fp.account_in_group("not-a-sid") is None


def test_grant_value_error_from_command_builder_is_error(monkeypatch):
    monkeypatch.setattr(fp, "is_supported", lambda: True)

    def bad(*_a, **_k):
        raise ValueError("引号")

    monkeypatch.setattr(fp, "build_command", bad)
    run, calls = _runner()
    assert fp.grant(run=run, sid=SID, powershell=PS).outcome == "error"
    assert not calls and not fp.granted_this_process()
