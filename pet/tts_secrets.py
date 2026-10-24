# -*- coding: utf-8 -*-
"""语音合成凭据的存取（系统钥匙串优先，进程内存兜底）。

为何不直接复用 ``pet/chat/models.py`` 的 ``SecretStore``：no-chat 打包变体会
``exclude`` 掉 ``pet.chat``，而语音报时设置页在 chat / no-chat 两条链路上都要能
打开（历史事故：设置页在打包版整体打不开）。这里只做「取/存一个字符串」，
不依赖 pet.chat、不依赖 Qt，模块顶层不 import keyring（惰性导入，省启动成本）。

服务名与 chat 的 SecretStore 保持一致（``dsh-pet-standalone``），因此凭据统一
落在同一个「Windows 凭据管理器 → 通用凭据」条目下，用户不会看到两处。

keyring 不可用（未随包 / 系统拒绝写入）时退化为**写它的那个进程**的内存：重启即丢，
且**别的进程读不到**。这不是细节——设置页默认跑在独立进程（``settings_process_isolation``，
见 pet/config.py），真正合成的 ``VoiceChimeService`` 在主宠进程，所以这条兜底在
「设置页存、主程序用」那条链路上等于没存。调用方（设置页）据 ``set()`` 的返回值与
``persisted()`` 判断该怎么告诉用户，**不许**把内存里的值说成「已保存」或
「本次运行有效」。这也是所有提供收费后端的变体都必须随包带 keyring 的原因。
"""

from __future__ import annotations

SERVICE_NAME = "dsh-pet-standalone"
#: 小米 MiMo TTS 的 API Key 引用名（与 chat provider 的 provider/<id> 同构）
MIMO_API_KEY_REF = "tts/mimo"

#: keyring 不可用时的进程内兜底（只活到进程退出，绝不落盘）
_memory: dict[str, str] = {}


def _keyring_module():
    try:
        import keyring  # noqa: PLC0415  —— 惰性导入：没有 keyring 也不该拖慢启动
    except Exception:
        return None
    return keyring


def persisted(ref: str) -> bool:
    """该凭据是否真的躺在系统钥匙串里（内存兜底不算持久化）。

    ``get()`` 会连内存兜底一起读，所以它无法回答「这条凭据重启后还在不在」；
    设置页据本函数决定行状态，从而不会把「写失败、只在内存」说成「已保存」。
    """
    if not ref:
        return False
    module = _keyring_module()
    if module is None:
        return False
    try:
        return bool(module.get_password(SERVICE_NAME, ref))
    except Exception:
        return False


def get(ref: str) -> str:
    """读凭据：钥匙串优先，其次进程内存兜底；都没有返回空串。"""
    if not ref:
        return ""
    module = _keyring_module()
    if module is not None:
        try:
            value = module.get_password(SERVICE_NAME, ref)
            if value:
                return str(value)
        except Exception:
            pass
    return _memory.get(ref, "")


def set(ref: str, value: str) -> bool:  # noqa: A001 —— 与 keyring/SecretStore 的命名对齐
    """写凭据；返回是否已持久化到钥匙串（False = 只存进了进程内存）。

    空值视为「清除」：钥匙串与内存一并清掉，避免用户清空输入框后旧 Key 还在用。
    """
    if not ref:
        return False
    text = str(value or "")
    if not text:
        clear(ref)
        return False
    _memory[ref] = text
    module = _keyring_module()
    if module is None:
        return False
    try:
        module.set_password(SERVICE_NAME, ref, text)
        return True
    except Exception:
        return False


def clear(ref: str) -> None:
    """清除凭据（钥匙串 + 内存）。"""
    if not ref:
        return
    _memory.pop(ref, None)
    module = _keyring_module()
    if module is None:
        return
    try:
        module.delete_password(SERVICE_NAME, ref)
    except Exception:
        pass


def reset_memory() -> None:
    """清空进程内兜底（测试用：避免用例之间互相污染）。"""
    _memory.clear()
