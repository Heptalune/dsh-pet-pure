# -*- coding: utf-8 -*-
"""语音合成凭据存取（pet/tts_secrets.py）回归测试。

两条路径都要锁：钥匙串可用（持久化）与不可用（退化为**写它的那个进程**的内存）。
不碰真实凭据管理器——keyring 一律 monkeypatch。

这里还锁一件跨进程的事：设置页默认跑在独立进程（``settings_process_isolation``），
所以进程内存兜底对主进程（真正合成的那个进程）等于不存在。这条性质是「所有提供
收费后端的变体都必须把 keyring 打进包」的**产品侧成因**，不是打包脚本的偏好。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from pet import tts_secrets  # noqa: E402


class _FakeKeyring:
    """最小 keyring 替身：记调用、可注入异常。"""

    def __init__(self, *, fail_set: bool = False) -> None:
        self.store: dict[tuple[str, str], str] = {}
        self.fail_set = fail_set

    def get_password(self, service, ref):
        return self.store.get((service, ref))

    def set_password(self, service, ref, value):
        if self.fail_set:
            raise RuntimeError("no backend")
        self.store[(service, ref)] = value

    def delete_password(self, service, ref):
        self.store.pop((service, ref), None)


def _patch(monkeypatch, module):
    monkeypatch.setattr(tts_secrets, "_keyring_module", lambda: module)


def test_round_trip_through_keyring(monkeypatch):
    """钥匙串可用：写入返回 True，读到同一个值，服务名与 ref 与 chat 侧一致。"""
    fake = _FakeKeyring()
    _patch(monkeypatch, fake)
    tts_secrets.reset_memory()

    assert tts_secrets.set("tts/mimo", "k-1") is True
    assert tts_secrets.get("tts/mimo") == "k-1"
    assert tts_secrets.persisted("tts/mimo") is True, "钥匙串里真的该有这一条"
    assert fake.store == {("dsh-pet-standalone", "tts/mimo"): "k-1"}

    tts_secrets.clear("tts/mimo")
    assert tts_secrets.get("tts/mimo") == ""
    assert tts_secrets.persisted("tts/mimo") is False
    assert fake.store == {}


def test_without_keyring_falls_back_to_process_memory(monkeypatch):
    """钥匙串不可用：写入返回 False（调用方据此报「没保存上」），本进程仍能读回。"""
    _patch(monkeypatch, None)
    tts_secrets.reset_memory()

    assert tts_secrets.set("tts/mimo", "k-2") is False
    assert tts_secrets.get("tts/mimo") == "k-2", "进程内存兜底必须能读回来"
    assert tts_secrets.persisted("tts/mimo") is False, "内存值不是持久化值"
    tts_secrets.clear("tts/mimo")
    assert tts_secrets.get("tts/mimo") == ""


def test_keyring_write_failure_keeps_memory_copy(monkeypatch):
    """钥匙串写入抛异常（系统拒绝）：不得丢值，退化为内存并返回 False。"""
    _patch(monkeypatch, _FakeKeyring(fail_set=True))
    tts_secrets.reset_memory()

    assert tts_secrets.set("tts/mimo", "k-3") is False
    assert tts_secrets.get("tts/mimo") == "k-3"
    assert tts_secrets.persisted("tts/mimo") is False, (
        "写失败的 Key 只是躺在当前进程内存里——「已保存」是假话的根源就在这里"
    )


def test_persisted_ignores_the_memory_fallback(monkeypatch):
    """``persisted()`` 只认钥匙串：内存里有值但没写进去时必须是 False。

    设置页用这个判据决定行状态——用「keyring 模块在不在」判断会在
    「模块在、系统拒绝写」时把没保存的 Key 说成「已保存」。
    """
    fake = _FakeKeyring(fail_set=True)
    _patch(monkeypatch, fake)
    tts_secrets.reset_memory()

    tts_secrets.set("tts/mimo", "k-6")
    assert tts_secrets.get("tts/mimo") == "k-6"
    assert tts_secrets.persisted("tts/mimo") is False

    fake.fail_set = False
    assert tts_secrets.set("tts/mimo", "k-7") is True
    assert tts_secrets.persisted("tts/mimo") is True


def test_empty_value_clears_both_stores(monkeypatch):
    """空值 = 清除：钥匙串与内存都不能留着旧 Key（用户清空输入框的语义）。"""
    fake = _FakeKeyring()
    _patch(monkeypatch, fake)
    tts_secrets.reset_memory()
    assert tts_secrets.set("tts/mimo", "k-4") is True

    assert tts_secrets.set("tts/mimo", "") is False
    assert tts_secrets.get("tts/mimo") == ""
    assert fake.store == {}


def test_keyring_read_error_degrades_to_memory(monkeypatch):
    """读取抛异常（凭据管理器被锁）：不向上抛，回落到内存值。"""
    class _Broken(_FakeKeyring):
        def get_password(self, service, ref):
            raise RuntimeError("locked")

    _patch(monkeypatch, _Broken())
    tts_secrets.reset_memory()
    tts_secrets.set("tts/mimo", "k-5")

    assert tts_secrets.get("tts/mimo") == "k-5"


def test_memory_fallback_does_not_cross_the_process_boundary(monkeypatch):
    """no-chat 变体（包里没有 keyring）：设置进程存的 Key，主进程一个字节都读不到。

    真实拓扑：设置页默认拉在独立进程里（``--settings``，见 pet/config.py 的
    ``settings_process_isolation``），用户关窗那个进程就退出；真正合成的
    ``VoiceChimeService`` 在主宠进程。所以内存兜底在这条链路上**等于没存**——
    「已保存」的说法从这里起就是假的。子进程用 meta_path 钩子挡住 keyring 的
    import，复现的就是冻结包没有那个模块时的真实形态。
    """
    child = subprocess.run(
        [sys.executable, "-c", _NO_KEYRING_CHILD],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONPATH=str(REPO)),
        cwd=str(REPO), check=False,
    )
    assert child.returncode == 0, child.stderr
    assert child.stdout.strip() == "'value-in-settings-process'", (
        f"子进程（= 设置进程）本该能读回自己写的值：{child.stdout!r} {child.stderr!r}"
    )

    # 主进程：内存里没有（写它的不是这个进程），keyring 又不在包里 → 读不到
    _patch(monkeypatch, None)
    tts_secrets.reset_memory()
    assert tts_secrets.get("tts/mimo") == "", (
        "跨进程读不到就必须读到空——收费后端会因此永远自报「没配 Key」"
    )


#: 子进程脚本：挡住 keyring 的 import（= 冻结包里没有那个模块），写一次凭据再读回。
_NO_KEYRING_CHILD = """
import sys
class _NoKeyring:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "keyring" or fullname.startswith("keyring."):
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0, _NoKeyring())
from pet import tts_secrets
assert tts_secrets.set("tts/mimo", "value-in-settings-process") is False
print(repr(tts_secrets.get("tts/mimo")))
"""


def test_mimo_ref_is_namespaced():
    """ref 取名规则：tts/<backend>，与 chat 的 provider/<id> 同构、天然不冲突。"""
    assert tts_secrets.MIMO_API_KEY_REF == "tts/mimo"
    assert tts_secrets.SERVICE_NAME == "dsh-pet-standalone"
