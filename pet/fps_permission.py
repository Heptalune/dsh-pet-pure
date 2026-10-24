# -*- coding: utf-8 -*-
"""游戏帧率的采集权限：把当前用户加入 ``Performance Log Users`` 组（一次性、用户点了才做）。

PresentMon 的 ETW 会话只对管理员或 ``Performance Log Users``（内置组，SID
``S-1-5-32-559``）成员开放。本模块替代已归档的「按需 UAC 独立助手」：

- **只做一件事**：用户在设置页点「授权…」后，弹一次系统 UAC，以管理员身份执行
  ``Add-LocalGroupMember -SID S-1-5-32-559 -Member <当前用户>``。桌宠本体始终普通权限，
  不装服务、不改注册表、不常驻提权进程。
- **按 SID 定位组**，不按组名（组名随系统语言变化）。
- **生效需要重新登录**：组成员关系写进的是新登录令牌，当前会话的令牌不变。所以状态
  分三种：已生效 / 已加入待重新登录 / 未授权——不把"已加入"说成"已能采集"。
- **身份取自进程令牌**：当前用户 SID 用 ``GetTokenInformation(TokenUser)`` 读，不信
  ``USERNAME``/``USERDOMAIN`` 环境变量（可被启动器改写）；SID 经严格正则校验（只含数字和
  连字符）后才进脚本，脚本里不出现任何用户可控文本，杜绝注入。
- **组成员判定用 ``CheckTokenMembership``**：只认令牌里**已启用**的该组（deny-only 不算），
  进程内 API 调用，不起子进程、不卡 GUI。
- **两层 PowerShell 均用系统目录绝对路径**，不走搜索路径。
- **提权脚本自带期限**：超过等待上限后才被批准的 UAC 不再执行加组；等待超时报「结果未知」，
  并以重新查询为准，不把超时当作已结束。
- 用户在 UAC 框点「否」是正常结果（``cancelled``），不是错误。
"""
from __future__ import annotations

import base64
import ctypes
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass

PERF_LOG_USERS_SID = "S-1-5-32-559"

#: 外层 PowerShell 在 UAC 被取消时用的退出码（与 Win32 ERROR_CANCELLED 同值）。
EXIT_CANCELLED = 1223
#: 管理员脚本的退出码：加组失败 / 已过期未执行 / 无法拉起管理员进程。
EXIT_ADD_FAILED = 2
EXIT_EXPIRED = 3
EXIT_LAUNCH_FAILED = 4
#: 等管理员进程结束的上限（秒）：UAC 框可能挂着等用户，给足但有界。
GRANT_TIMEOUT_S = 300.0
#: 管理员脚本的执行期限比等待上限早这么多秒：超时后才批准的 UAC 不再加组。
DEADLINE_MARGIN_S = 10.0

STATUS_ACTIVE = "active"            # 当前令牌已含该组：普通权限即可采集
STATUS_PENDING = "pending_relogon"  # 本进程已成功加入，需注销/重新登录后生效
STATUS_MISSING = "missing"          # 未加入
STATUS_UNSUPPORTED = "unsupported"  # 非 Windows 或无法判定

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
#: 只含数字与连字符的 SID：进脚本前必须过这道校验。
_SID_RE = re.compile(r"S-1-\d+(?:-\d+)+")

#: 进程级状态（不随设置窗口销毁）：本进程是否已成功加入过。
_state = {"granted": False}


@dataclass(frozen=True)
class GrantResult:
    """一次授权请求的结果。

    ``outcome``：``ok`` / ``cancelled`` / ``error`` / ``unknown``（等待超时，以重新查询为准）
    / ``unsupported``。
    """

    outcome: str
    detail: str = ""


def is_supported() -> bool:
    return sys.platform == "win32"


def mark_granted() -> None:
    _state["granted"] = True


def granted_this_process() -> bool:
    return bool(_state["granted"])


def is_valid_sid(sid: str) -> bool:
    return bool(sid) and _SID_RE.fullmatch(sid) is not None


# ---------------------------------------------------------------- 令牌（进程内 Win32 API）
def _advapi():
    from ctypes import wintypes

    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                     ctypes.POINTER(wintypes.HANDLE)]
    adv.OpenProcessToken.restype = wintypes.BOOL
    adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    adv.GetTokenInformation.restype = wintypes.BOOL
    adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p,
                                           ctypes.POINTER(wintypes.LPWSTR)]
    adv.ConvertSidToStringSidW.restype = wintypes.BOOL
    adv.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    adv.ConvertStringSidToSidW.restype = wintypes.BOOL
    adv.CheckTokenMembership.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                         ctypes.POINTER(wintypes.BOOL)]
    adv.CheckTokenMembership.restype = wintypes.BOOL
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.LocalFree.argtypes = [ctypes.c_void_p]
    k32.LocalFree.restype = ctypes.c_void_p
    return adv, k32, wintypes


def current_user_sid() -> str:
    """当前进程令牌的用户 SID（字符串形式）；读不到返回空串。"""
    if not is_supported():
        return ""
    try:
        adv, k32, wt = _advapi()
        token = wt.HANDLE()
        if not adv.OpenProcessToken(k32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            return ""
        try:
            need = wt.DWORD(0)
            adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(need))  # TokenUser
            if not need.value:
                return ""
            buf = ctypes.create_string_buffer(need.value)
            if not adv.GetTokenInformation(token, 1, buf, need, ctypes.byref(need)):
                return ""
            psid = ctypes.c_void_p.from_buffer(buf).value  # TOKEN_USER.User.Sid
            text = wt.LPWSTR()
            if not adv.ConvertSidToStringSidW(psid, ctypes.byref(text)):
                return ""
            try:
                sid = text.value or ""
            finally:
                k32.LocalFree(ctypes.cast(text, ctypes.c_void_p))
        finally:
            k32.CloseHandle(token)
    except (OSError, AttributeError, ValueError):
        return ""
    return sid if is_valid_sid(sid) else ""


def token_has_group() -> bool | None:
    """当前令牌是否含**已启用**的该组（``CheckTokenMembership``）；判不出返回 ``None``。"""
    if not is_supported():
        return None
    try:
        adv, k32, wt = _advapi()
        psid = ctypes.c_void_p()
        if not adv.ConvertStringSidToSidW(PERF_LOG_USERS_SID, ctypes.byref(psid)):
            return None
        try:
            member = wt.BOOL(0)
            if not adv.CheckTokenMembership(None, psid, ctypes.byref(member)):
                return None
            return bool(member.value)
        finally:
            k32.LocalFree(psid)
    except (OSError, AttributeError, ValueError):
        return None


def account_in_group(user_sid: str | None = None) -> bool | None:
    """当前账户是否**已在**该组的成员列表里（不看令牌；判不出返回 ``None``）。

    这是跨进程的真值：设置页是独立进程，关掉重开后靠它区分「已加入待重新登录」与「未授权」。
    组名用 ``LookupAccountSidW`` 由 SID 反查（随系统语言变），成员按 SID 比对。只看**直接成员**
    （本模块只做直接加入），不展开嵌套组。
    """
    if not is_supported():
        return None
    sid = current_user_sid() if user_sid is None else user_sid
    if not is_valid_sid(sid):
        return None
    try:
        from ctypes import wintypes

        adv, k32, _ = _advapi()
        net = ctypes.WinDLL("netapi32", use_last_error=True)
        group_sid = ctypes.c_void_p()
        if not adv.ConvertStringSidToSidW(PERF_LOG_USERS_SID, ctypes.byref(group_sid)):
            return None
        try:
            name = ctypes.create_unicode_buffer(256)
            dom = ctypes.create_unicode_buffer(256)
            n_name, n_dom, use = wintypes.DWORD(256), wintypes.DWORD(256), wintypes.DWORD()
            adv.LookupAccountSidW.argtypes = [
                wintypes.LPCWSTR, ctypes.c_void_p, wintypes.LPWSTR,
                ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR,
                ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)]
            if not adv.LookupAccountSidW(None, group_sid, name, ctypes.byref(n_name),
                                         dom, ctypes.byref(n_dom), ctypes.byref(use)):
                return None
        finally:
            k32.LocalFree(group_sid)
        net.NetLocalGroupGetMembers.argtypes = [
            wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p), wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(ctypes.c_size_t)]
        net.NetLocalGroupGetMembers.restype = wintypes.DWORD
        net.NetApiBufferFree.argtypes = [ctypes.c_void_p]
        resume = ctypes.c_size_t(0)
        found = False
        while True:
            buf = ctypes.c_void_p()
            read, total = wintypes.DWORD(), wintypes.DWORD()
            # level 0 = LOCALGROUP_MEMBERS_INFO_0 { PSID }；ERROR_MORE_DATA(234) 时续枚举
            rc = net.NetLocalGroupGetMembers(None, name.value, 0, ctypes.byref(buf),
                                             0xFFFFFFFF, ctypes.byref(read),
                                             ctypes.byref(total), ctypes.byref(resume))
            try:
                if rc not in (0, 234):
                    return None
                ptrs = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))
                for i in range(read.value if buf.value and not found else 0):
                    text = wintypes.LPWSTR()
                    if adv.ConvertSidToStringSidW(ptrs[i], ctypes.byref(text)):
                        try:
                            if (text.value or "").upper() == sid.upper():
                                # 命中也要把 234 续枚举排空到 rc==0，否则服务端枚举句柄泄漏
                                found = True
                                break
                        finally:
                            k32.LocalFree(ctypes.cast(text, ctypes.c_void_p))
            finally:
                if buf.value:
                    net.NetApiBufferFree(buf)
            if rc == 0:
                break
        return found
    except (OSError, AttributeError, ValueError):
        return None


# ---------------------------------------------------------------- 系统 PowerShell
def system_powershell() -> str:
    """系统目录下 Windows PowerShell 的绝对路径（不走搜索路径）；取不到返回空串。"""
    if not is_supported():
        return ""
    try:
        buf = ctypes.create_unicode_buffer(260)
        n = ctypes.windll.kernel32.GetSystemDirectoryW(buf, 260)
    except (OSError, AttributeError):
        return ""
    if not n or n >= 260:
        return ""
    path = os.path.join(buf.value, "WindowsPowerShell", "v1.0", "powershell.exe")
    return path if os.path.isfile(path) else ""


def _ps_quote(value: str) -> str:
    """PowerShell 单引号字面量。只用于程序自产、已校验的值（路径/SID），不接用户文本。"""
    if any(ch in value for ch in "'\u2018\u2019\u201a\u201b"):
        raise ValueError("值里不允许出现引号")
    return "'" + value + "'"


def _encode(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


# ---------------------------------------------------------------- 状态与授权
def query_status(granted_this_session: bool | None = None, *, check=token_has_group,
                 in_group=account_in_group) -> str:
    """当前权限状态（进程内 API，不起子进程）。

    令牌含已启用的组 → 已生效；否则以**账户实际组成员关系**为准（跨进程真值：独立设置进程
    关掉重开也判得出「待重新登录」）；成员列表读不到时退回本进程记录 ``granted_this_session``
    （缺省取 ``granted_this_process()``）。
    """
    if not is_supported():
        return STATUS_UNSUPPORTED
    member = check()
    if member is None:
        return STATUS_UNSUPPORTED
    if member:
        return STATUS_ACTIVE
    listed = in_group()
    if listed is not None:
        # 成员列表读得到就以它为准（含"本进程加过、之后又被移出"→ 未授权）
        return STATUS_PENDING if listed else STATUS_MISSING
    granted = granted_this_process() if granted_this_session is None else granted_this_session
    return STATUS_PENDING if granted else STATUS_MISSING


def build_inner_script(sid: str, deadline_unix: int) -> str:
    """管理员进程里执行的脚本：过期不执行；已是成员视为成功（幂等）。"""
    if not is_valid_sid(sid):
        raise ValueError("SID 不合法")
    return (
        "$ErrorActionPreference='Stop';"
        f"if([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() -gt {int(deadline_unix)})"
        f"{{exit {EXIT_EXPIRED}}};"
        "try{"
        f"Add-LocalGroupMember -SID '{PERF_LOG_USERS_SID}' -Member {_ps_quote(sid)};exit 0"
        "}catch{"
        f"if($_.FullyQualifiedErrorId -like 'MemberExists*'){{exit 0}};exit {EXIT_ADD_FAILED}"
        "}"
    )


def build_command(sid: str, powershell: str, deadline_unix: int) -> list[str]:
    """普通权限下执行的外层命令：``Start-Process -Verb RunAs`` 拉起管理员 PowerShell 并等它结束。

    只有原生错误码为 ``ERROR_CANCELLED``（用户在 UAC 点「否」）才映射为取消，其余启动失败另报。
    """
    inner = _encode(build_inner_script(sid, deadline_unix))
    outer = (
        "try{"
        f"$p=Start-Process -FilePath {_ps_quote(powershell)} -Verb RunAs -WindowStyle Hidden "
        "-Wait -PassThru -ErrorAction Stop -ArgumentList "
        f"'-NoProfile','-NonInteractive','-EncodedCommand','{inner}';"
        "exit $p.ExitCode"
        "}catch{"
        "$e=$_.Exception;"
        "while($e){if($e -is [ComponentModel.Win32Exception] -and $e.NativeErrorCode -eq "
        f"{EXIT_CANCELLED}){{exit {EXIT_CANCELLED}}};$e=$e.InnerException}};"
        f"exit {EXIT_LAUNCH_FAILED}"
        "}"
    )
    return [powershell, "-NoProfile", "-NonInteractive", "-EncodedCommand", _encode(outer)]


def grant(*, run=subprocess.run, sid: str | None = None, powershell: str | None = None,
          now=time.time) -> GrantResult:
    """弹 UAC 并把当前用户加入组。阻塞直到管理员进程结束——调用方放到后台线程。"""
    if not is_supported():
        return GrantResult("unsupported", "仅 Windows 需要此授权")
    sid = current_user_sid() if sid is None else sid
    if not is_valid_sid(sid):
        return GrantResult("error", "无法确定当前账户")
    powershell = system_powershell() if powershell is None else powershell
    if not powershell:
        return GrantResult("error", "找不到系统 PowerShell")
    deadline = int(now() + GRANT_TIMEOUT_S - DEADLINE_MARGIN_S)
    try:
        proc = run(build_command(sid, powershell, deadline), capture_output=True, text=True,
                   timeout=GRANT_TIMEOUT_S, creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return GrantResult("unknown", "等待授权超时：超时后才批准的请求不会执行；"
                                      "若已在超时前批准，注销或重启电脑后生效")
    except ValueError as exc:  # build_command 拒绝了不安全的输入（理论上前面已校验）
        return GrantResult("error", f"授权参数不安全：{exc}")
    except (OSError, subprocess.SubprocessError) as exc:
        return GrantResult("error", f"无法启动授权：{exc}")
    code = proc.returncode
    if code == 0:
        mark_granted()
        return GrantResult("ok", "已加入 Performance Log Users，注销或重启电脑后生效")
    if code == EXIT_CANCELLED:
        return GrantResult("cancelled", "已取消授权")
    if code == EXIT_EXPIRED:
        return GrantResult("error", "授权请求已过期，请重试")
    if code == EXIT_LAUNCH_FAILED:
        return GrantResult("error", "无法拉起系统授权窗口")
    return GrantResult("error", f"授权失败（退出码 {code}）")
