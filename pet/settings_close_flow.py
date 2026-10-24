# -*- coding: utf-8 -*-
"""设置窗关闭前的保存闸门：失败不再是"关不掉"的死路，代次冲突也不再冒充写盘失败。

问题（2026-10-10 实机 ``.scratch/product-approved-gui-20261010/REPORT.md`` §4）：设置窗
开着的时候这只宠被托盘退出又重新生成（slot 代次 1 → 2，合法写者），旧窗口再点
「保存并退出」/ X / Esc 时，代次防覆盖门拒绝旧表单落盘 → ``_write_config`` 返回 False →
``closeEvent`` ignore、``reject`` 不关窗，用户手上只剩"杀进程"一条路；而提示还把
"代次落后"说成"配置未能写入磁盘"，把排查方向指错。

本模块把"关窗前的最后一次落盘"收口成一处，三种结局各自明确：

- **已落盘**：照常关窗，``_apply_autostart`` 一并应用（它抛异常时不放行异常——见下）；
- **失败 + 用户选「继续编辑」**：不关窗，草稿一个字节都不动（默认钮与 Esc 都落在这一支，
  误触不会丢改动）；
- **失败 + 用户选「放弃未保存的改动并关闭」**：不再往磁盘写任何东西，窗口照常关闭。

文案按**失败类别**（``settings_slot_config.SaveFailure.kind``）分：代次落后说的是"这只
桌宠已被重新生成 / 设置在别处更新过，请重新打开设置"；档案不可信说的是"为避免覆盖，**未
写入这只桌宠的档案**"（全局那一半写没写由 ``slot_written`` / ``global_written`` 在正文里
如实说——``save()`` 先写档案再写主配置，坏档案挡住档案那一路时全局那半常常已经落盘了）；
表单校验没过说的是布局诊断；开机自启（落盘之后的 OS 侧收尾）出错说的是"设置已保存、登录项
可能没生效"；只有真正的锁/写盘失败才说"配置未能写入磁盘"。

**放弃的边界**：只放弃**还没写成功**的部分。失败的 ``save()`` 先写档案、再写主配置，所以
档案那一路被代次门挡住时，全局那一半通常已经落盘了——它不会撤回，文案必须如实说出来
（``SaveFailure.slot_written`` / ``global_written``）。"已回滚"是做不到的承诺，界面不许许。

**放弃必须真的作废草稿**（2026-10-11 增量审查 CF01）：关窗只关掉窗口，草稿留在配置面上
——进程内 fallback（设置进程隔离关闭 / ``startDetached`` 失败）下，设置窗与运行期共用
**同一个** ``SlotRoutedConfig``（``app.py`` 把它挂在 ``PetInstance`` 上，``modern_settings_
dialog`` 的 ``route_settings_config`` 对已路由对象原样返回），而 ``Config.set()`` 改的就是
那份内存。只记 ``_close_decided`` 就关窗，待写草稿会留在那里，下一次**无关**保存（改播放
速度）把它一起 flush 落盘：用户明确放弃过的改动重新生效。所以选放弃时调用
:func:`discard_unsaved_drafts`：作废两路待写区、把共享内存退回成功基线并重读核对（不写盘、
不洗编辑代次、不回滚已写成功的那一半）。核对不上时**可见地**说明"这次没能核对磁盘"
（:func:`notify_discard_unverified`）——作废是真的，磁盘现状没核对也是真的，两句话都要有。

**未预期异常不静默**：``_write_config`` 抛异常时不再"记一笔日志就当作写成功放行"（那是
静默丢草稿），而是同样把决定交回用户；此时是否已有部分写入**无法确认**，文案比"一项都
没写"更弱。``_apply_autostart`` 是同一条纪律的第二处：它跑在落盘**之后**，抛异常时设置
已经在盘上（所以文案是"设置已保存、自启没成"），但异常一旦穿出关闭路径，这一次关闭就
永远没有结果（点 X 没反应）——同样收进这一组出口。

**再入纪律**：Qt 的 ``closeEvent`` 内部会转成 ``reject()``，两者都会走本闸门；用户在关闭
那一刻做的决定记在 ``host._close_decided`` 上（由本模块持有语义）：重入只关窗，不再保存、
不再弹第二个框（历史实现里 X 关闭会把整表写回两次）。用户选「继续编辑」时**不**记这个
决定——下一次关闭必须重新走一遍保存，否则草稿会被静默丢掉。

**会话结束（注销/关机）另走一条**：Qt 在 ``aboutToQuit`` 之后还会 ``closeAllWindows``，
设置窗因此照样进 ``closeEvent``；那时**不许**弹任何模态框（关机流程会被一个没人看的框挂
住），也不重试、不碰系统登录项。判据用现成的进程级标记 ``pet.webm_clip.session_ending``
（issue #111 的"关机窗口里不派生、不阻塞"纪律同一个标记），走 :func:`_session_end_close`：
最大 best-effort 试一次落盘、失败只**如实记日志**、然后放行关窗——不假装保存成功，也不
把旧快照盖到别人新值上（落盘仍走 ``Config.save`` 的目录锁 + 锁内重读递归合并）。
"""

from __future__ import annotations

import logging

from PySide6.QtWidgets import QMessageBox, QPushButton

from .webm_clip import session_ending
from .settings_slot_config import (
    SAVE_FAILURE_AUTOSTART,
    SAVE_FAILURE_DISK,
    SAVE_FAILURE_LAYOUT,
    SAVE_FAILURE_PROFILE,
    SAVE_FAILURE_STALE,
    SAVE_FAILURE_UNKNOWN,
    SaveFailure,
    save_failure_of,
)

__all__ = [
    "CLOSE_DISCARD",
    "CLOSE_KEEP_EDITING",
    "CLOSE_SAVED",
    "DISCARD_LABEL",
    "DISCARD_UNVERIFIED_TITLE",
    "KEEP_LABEL",
    "build_save_failure_box",
    "confirm_discard_unsaved",
    "discard_unsaved_drafts",
    "discard_unverified_copy",
    "failure_of",
    "layout_failure",
    "notify_discard_unverified",
    "save_before_close",
]

#: 三条结局：已落盘 / 用户放弃未保存的改动 / 用户要留在窗口里继续改。
CLOSE_SAVED = "saved"
CLOSE_DISCARD = "discard"
CLOSE_KEEP_EDITING = "keep-editing"

SAVE_TITLE = "保存失败"
STALE_TITLE = "设置未保存：这只桌宠已重新生成"
KEEP_LABEL = "继续编辑"
#: 破坏性动作的按钮文案自写而不用 StandardButton：产品没装 Qt 中文翻译（仓库内无
#: QTranslator），标准按钮在中文界面上仍显示 "Yes"/"Discard"，破坏性操作不该让人猜。
DISCARD_LABEL = "放弃未保存的改动并关闭"
#: 两条出口的语义写在正文里（按钮文案只够说"放弃什么"）：默认那支永远是保留草稿。
KEEP_HINT = "选择「继续编辑」则窗口保留原样，本次改动一个字节都不会丢；"

STALE_HEADLINE = ("这只桌宠已被重新生成（或它的设置已在别处更新），本窗口里的改动属于上一代，"
                  "没有写入它的档案。")
STALE_NEXT = "关闭本窗口后重新打开「桌宠设置」，看到的就是最新一代的设置。"
#: 坏档案这一句**只说这只桌宠的档案**：全局那一半此时可能已经写成功（``save()`` 先写档案
#: 再写主配置），"本次一个字节都没写"只对档案成立、对全局不成立——写没写由
#: ``SaveFailure.slot_written`` / ``global_written`` 在正文里如实说（实机审查发现的自相矛盾）。
PROFILE_HEADLINE = ("这只桌宠的设置档案不可信（已损坏或版本不受支持）。为避免覆盖它，"
                    "未写入这只桌宠的档案。")
DISK_HEADLINE = "配置未能写入磁盘，改动可能在重启后丢失。"
UNKNOWN_HEADLINE = "保存时出现未预期的错误，改动可能没有完整写入，也可能一项都没写。"
#: 开机自启是**落盘之后**的 OS 侧收尾：它出错时设置本身已经保存，所以既不能说"没写成"，
#: 也不能让异常穿出关闭路径（那时窗口会变成"点 X 没反应"）。
AUTOSTART_TITLE = "开机自启未生效"
AUTOSTART_HEADLINE = "设置已经保存，但应用「开机自启」时出错：登录项可能没有生效。"
LAYOUT_TITLE = "菜单布局未保存"
LAYOUT_HEADLINE = "菜单布局无效，本次改动没有保存："
#: 放弃完成了、但"共享视图就是磁盘现状"这句话**核对不上**时的可见诊断标题。
DISCARD_UNVERIFIED_TITLE = "改动已放弃，但没能核对磁盘"


def layout_failure(diagnostics) -> SaveFailure:
    """表单校验没过（``_write_config`` 的第一道门：菜单布局无法解析）。

    这不是写盘失败，但同样是"保存没成"——走同一条出口，用户照样能看到明确的
    「放弃未保存的改动并关闭」，而不是被一个弹完提示还关不掉的窗口困住。
    """
    return SaveFailure(kind=SAVE_FAILURE_LAYOUT, status="menu-layout",
                       reason="、".join(str(item) for item in (diagnostics or ()))
                       or "布局无法解析")


def failure_of(host) -> SaveFailure:
    """这次 ``_write_config`` 失败的如实描述。

    宿主自己记的那份优先（``host._write_failure``：菜单布局校验这类保存**之前**的闸门），
    其次是配置面记的那份（``SlotRoutedConfig.last_save_failure``）；两份都没有时按"未能
    写入磁盘"如实上报——**不猜**类别。
    """
    recorded = getattr(host, "_write_failure", None)
    if isinstance(recorded, SaveFailure):
        return recorded
    config = getattr(host, "config", None)
    return (save_failure_of(config)
            or SaveFailure(kind=SAVE_FAILURE_DISK,
                           path=str(getattr(config, "path", "") or "")))


def _headline(failure: SaveFailure) -> str:
    if failure.kind == SAVE_FAILURE_STALE:
        return STALE_HEADLINE
    if failure.kind == SAVE_FAILURE_PROFILE:
        return PROFILE_HEADLINE
    if failure.kind == SAVE_FAILURE_LAYOUT:
        return LAYOUT_HEADLINE + (failure.reason or "布局无法解析")
    if failure.kind == SAVE_FAILURE_UNKNOWN:
        return UNKNOWN_HEADLINE
    if failure.kind == SAVE_FAILURE_AUTOSTART:
        return AUTOSTART_HEADLINE
    return DISK_HEADLINE


def _title(failure: SaveFailure) -> str:
    if failure.kind == SAVE_FAILURE_STALE:
        return STALE_TITLE
    if failure.kind == SAVE_FAILURE_LAYOUT:
        return LAYOUT_TITLE
    if failure.kind == SAVE_FAILURE_AUTOSTART:
        return AUTOSTART_TITLE
    return SAVE_TITLE


def _discard_note(failure: SaveFailure) -> str:
    """「放弃」到底放弃了什么：写成的部分如实点名，写不成的如实说，不许讲"已回滚"。"""
    if failure.kind == SAVE_FAILURE_UNKNOWN:
        return ("「放弃未保存的改动并关闭」只关窗口、不再写盘：本次是否已有部分写入"
                "无法确认。")
    if failure.kind == SAVE_FAILURE_AUTOSTART:
        # 设置已经落盘：这里没有"未保存的改动"可放弃，如实说清按钮在这里的含义。
        return ("「放弃未保存的改动并关闭」在这里等于直接关窗口：本次设置已经写入、不会撤回，"
                "只是开机自启仍是没生效的状态。")
    written = []
    if failure.slot_written:
        written.append("这只桌宠的独立设置")
    if failure.global_written:
        written.append("全局设置")
    if not written:
        return "「放弃未保存的改动并关闭」只关窗口、不再写盘：本次改动一项都没写入。"
    return ("「放弃未保存的改动并关闭」只关窗口、不再写盘：尚未写入的改动会丢掉，"
            "本次已经写入的部分（" + "、".join(written) + "）不会撤回。")


def _detail(failure: SaveFailure) -> str:
    """正文：先给恢复路径（代次落后要重开窗口），再回答"放弃放弃了什么"，末附路径。"""
    parts = []
    if failure.kind == SAVE_FAILURE_STALE:
        parts.append(STALE_NEXT)
    parts.append(KEEP_HINT + _discard_note(failure))
    if failure.path:
        parts.append("配置路径：" + failure.path)
    return "\n\n".join(parts)


def build_save_failure_box(parent, failure) -> tuple[QMessageBox, QPushButton, QPushButton]:
    """构造保存失败框（不 exec，便于断言文案与按钮语义）。

    按钮语义固定：``keep``（继续编辑）是默认钮**且**是 Esc 钮——非破坏性动作占默认位，
    误触回车/Esc 只会留在窗口里；``discard`` 是明确的破坏性出口。两者都不含糊，用户
    不必靠猜哪个是"安全"的。
    """
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(_title(failure))
    box.setText(_headline(failure))
    box.setInformativeText(_detail(failure))
    keep = box.addButton(KEEP_LABEL, QMessageBox.ButtonRole.AcceptRole)
    discard = box.addButton(DISCARD_LABEL, QMessageBox.ButtonRole.DestructiveRole)
    box.setDefaultButton(keep)
    box.setEscapeButton(keep)
    return box, keep, discard


def confirm_discard_unsaved(parent, failure) -> bool:
    """弹失败框并返回用户是否选择「放弃未保存的改动并关闭」（继续编辑/关框一律 False）。

    框用完即排删除：用户在同一个窗口里反复点「保存并退出」时不该攒下多个隐藏的
    QMessageBox 子对象（``clickedButton()`` 在 ``deleteLater`` 排队期间仍然有效）。
    """
    box, _keep, discard = build_save_failure_box(parent, failure)
    try:
        box.exec()
        return box.clickedButton() is discard
    finally:
        box.deleteLater()


def _decide(host, failure: SaveFailure) -> str:
    """按用户的选择收口：放弃 → **作废还没写成功的草稿**并记下决定（调用方随即关窗）。

    放弃必须是"真的放弃"：关窗只关掉窗口，草稿还留在共享的配置面上（进程内 fallback
    下那份视图在设置窗关掉之后仍被运行期使用，见 ``discard_unsaved_drafts``）。作废
    要与磁盘核对，核对不上就**可见地**说明"这次没能核对磁盘"——不许只写日志、更不许
    假称放弃完全成功。
    """
    if not confirm_discard_unsaved(host, failure):
        return CLOSE_KEEP_EDITING
    host._close_decided = True
    verified, path = discard_unsaved_drafts(host)
    if not verified:
        notify_discard_unverified(host, path)
    return CLOSE_DISCARD


def _host_failure(host, status: str, reason: str, *, kind: str) -> SaveFailure:
    """界面层自己发现的失败（保存/收尾步骤抛异常）：路径取宿主配置面，类别由调用方给。"""
    config = getattr(host, "config", None)
    return SaveFailure(kind=kind, status=status, reason=reason,
                       path=str(getattr(config, "path", "") or ""))


def discard_unsaved_drafts(host) -> tuple[bool, str]:
    """把"这次关闭里还没写成功"的草稿真的作废掉（配置面那一层见两个同名方法）。

    宿主配置面没有这套 API（测试替身 / 老宿主）时返回 ``(True, "")``：那些宿主本来
    就没有"留在内存里、会被后续无关保存顺手 flush 的待写草稿"这一层。

    配置面抛未预期异常时返回 ``(False, path)``：草稿可能只作废了一半，按"没能核对"
    如实上报，绝不静默当作成功。
    """
    config = getattr(host, "config", None)
    discard = getattr(config, "discard_unsaved_drafts", None)
    path = str(getattr(config, "path", "") or "")
    if not callable(discard):
        return True, ""
    try:
        verified, failed_path = discard()
    except Exception:
        logging.exception("放弃未保存的改动：作废草稿 / 重读磁盘失败")
        return False, path
    return bool(verified), str(failed_path or path)


def discard_unverified_copy(path: str) -> str:
    """"草稿作废了、但没能核对磁盘"的正文（先分开给测试读，避免只能靠弹真框断言）。"""
    text = ("本窗口里尚未保存成功的改动已经作废，它们不会再被写回磁盘。\n\n"
            "但放弃时重读配置以核对磁盘现状这一步失败了（目录锁被占用，或配置文件读不到 / "
            "内容不可信——具体原因在日志里），所以这次**没能核对磁盘**现状。")
    if path:
        text += "\n\n配置路径：" + path
    return text


def notify_discard_unverified(parent, path: str) -> None:
    """放弃完成、但没能与磁盘核对的**可见**诊断（不允许用一条日志代替）。

    用 ``QMessageBox.warning``：通知型、返回值无分支，与 ``_apply_autostart`` 的
    「开机自启设置失败」同一族；测试里这条静态方法被 conftest 换成 no-op，真框的
    可见性由 ``test_discard_unverified_notice_is_a_real_visible_box`` 用真 exec 证明。
    """
    QMessageBox.warning(parent, DISCARD_UNVERIFIED_TITLE, discard_unverified_copy(path))


def _session_end_close(host) -> str:
    """会话结束（注销/关机）时的关闭：最大 best-effort，**绝不**弹框、不重试、不延迟。

    Qt 在 ``aboutToQuit`` 之后还会 ``closeAllWindows``，所以这条路径照样会进设置窗的
    ``closeEvent``。普通闸门在这里是危险的：失败框 ``exec()`` 是模态循环，会把关机流程
    挂在一个没人看的框上（系统最终强杀进程，用户看到的只是"关机卡住"）；重试同理。

    做法：**试一次**落盘（仍然走 ``Config.save`` 的目录锁 + 锁内重读递归合并——不会把旧
    快照盖到别人刚写的新值上），失败就如实记日志。既不假装保存成功，也不阻塞关机。
    **跳过开机自启收尾**：那是 OS 侧写入，关机窗口里既无意义又可能卡住（`autostart.set_enabled`
    失败时还会弹框），配置值本身已落盘，下次启动的 ``_check_autostart_wanted`` 会按新值收敛。

    返回 :data:`CLOSE_SAVED` = "这次关闭已经做过决定，调用方照常关窗"（成功与否由日志如实
    记录；关机后界面已不存在，能留下证据的只有日志）。
    """
    host._close_decided = True
    logging.info("会话结束：设置窗按最大 best-effort 收口（不弹框、不重试、跳过开机自启收尾）")
    try:
        saved = bool(host._write_config())
    except Exception:
        logging.exception("会话结束：关闭设置时保存配置抛出未预期异常")
        saved = False
    if saved:
        logging.info("会话结束：设置已写入磁盘（开机自启按新值在下次启动收敛）")
    else:
        logging.warning("会话结束：设置未能写入磁盘，本次不阻塞关机（未保存的改动会丢）")
    return CLOSE_SAVED


def save_before_close(host) -> str:
    """关闭前的统一闸门：落盘 → 失败时弹「继续编辑（默认）/ 放弃未保存的改动并关闭」。

    返回 :data:`CLOSE_SAVED` / :data:`CLOSE_DISCARD` / :data:`CLOSE_KEEP_EDITING`。前两者
    都表示"这次关闭已经做过决定"（``host._close_decided`` 记一次，重入只关窗），调用方
    照常 ``accept()`` / ``reject()`` 即可；``CLOSE_KEEP_EDITING`` 时调用方**必须**不关窗
    （草稿留在控件里）。

    **会话结束（注销/关机）先分支**：那时 Qt 正在 ``closeAllWindows``，本闸门的失败框是
    模态循环——不许弹，走 :func:`_session_end_close` 的最大 best-effort 出口。
    """
    if getattr(host, "_close_decided", False) or getattr(host, "_saved_via_button", False):
        # 本次关闭已经做过决定；或落盘已由窗口外的入口完成（「恢复默认设置」）——后者
        # 再走一遍 ``_write_config`` 就是拿控件里那份**重置前**的旧值覆盖刚写下的默认值。
        return CLOSE_SAVED
    if session_ending():
        # 会话结束：不弹框、不重试、不延迟（见 ``_session_end_close``）。
        return _session_end_close(host)
    try:
        ok = bool(host._write_config())
    except Exception:
        # 未预期异常：不知道写到哪一步了。**不许**记一笔日志就当成功放行（那是静默丢草稿），
        # 也不许自动重试/自动回滚——把决定交给用户，同时把"是否已部分写入无法确认"说清楚。
        logging.exception("关闭设置时保存配置失败")
        # 窗口化 exe 的 stderr 无人接收（独立设置进程也没配文件日志），堆栈会直接
        # 丢失——落一份到配置目录，用户报障时能拿到真实异常（覆盖写，只留最近一次）。
        try:
            import traceback as _tb

            crash_path = getattr(getattr(host, "config", None), "dir", None)
            if crash_path is not None:
                crash_path.mkdir(parents=True, exist_ok=True)
                (crash_path / "settings-save-crash.log").write_text(
                    _tb.format_exc(), encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 诊断落盘失败不能影响主流程分流
            pass
        return _decide(host, _host_failure(host, "unexpected-exception",
                                           "保存过程中抛出未预期异常",
                                           kind=SAVE_FAILURE_UNKNOWN))
    if not ok:
        return _decide(host, failure_of(host))
    try:
        host._apply_autostart()
    except Exception:
        # 开机自启在**落盘之后**，所以它抛异常时设置已经在盘上（草稿不丢）；但异常若穿出
        # 关闭路径，Qt 槽这一拍就永远没有结果——窗口又变成"点 X 没反应"。同样走这一组出口，
        # 文案如实说"设置已保存、自启没成"（不能沿用"改动可能没写入"那套）。
        # 边界核实：``autostart.enable/disable`` 只吞 ``OSError``（pet/autostart.py:231/248/271），
        # ``plistlib.dump`` / ``_win_command`` / ``_linux_desktop_content`` 抛的其它异常、以及
        # 宿主或测试注入的 ``set_enabled`` 替身都会原样冒出来——**没有**"永不抛"的保证。
        logging.exception("关闭设置时应用开机自启失败")
        return _decide(host, _host_failure(host, "autostart-exception",
                                           "设置已写入磁盘，但应用「开机自启」时抛出未预期异常",
                                           kind=SAVE_FAILURE_AUTOSTART))
    host._close_decided = True
    return CLOSE_SAVED
