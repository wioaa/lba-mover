"""权限：检查是否管理员、按需提升特权。

原始卷写入需要 ``SeManageVolumePrivilege``（管理卷）以及管理员令牌本身。
这里**不做**自动 UAC 提权——静默弹 UAC 会让用户不知道自己在授权什么。
UI 的做法是：检测到非管理员时，把"以管理员身份重新启动"做成一个显式按钮。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from ctypes import wintypes

__all__ = [
    "is_admin",
    "enable_privilege",
    "relaunch_as_admin",
    "PRIVILEGES_FOR_RAW_VOLUME",
]

IS_WINDOWS = os.name == "nt"

#: 原始卷读写需要的特权
PRIVILEGES_FOR_RAW_VOLUME = (
    "SeManageVolumePrivilege",
    "SeBackupPrivilege",
    "SeRestorePrivilege",
)

TOKEN_ADJUST_PRIVILEGES = 0x0020
TOKEN_QUERY = 0x0008
SE_PRIVILEGE_ENABLED = 0x00000002


class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class _LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", _LUID), ("Attributes", wintypes.DWORD)]


class _TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [("PrivilegeCount", wintypes.DWORD), ("Privileges", _LUID_AND_ATTRIBUTES * 1)]


def is_admin() -> bool:
    """当前进程是否具备管理员权限。"""
    if not IS_WINDOWS:
        return os.geteuid() == 0 if hasattr(os, "geteuid") else False
    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        shell32.IsUserAnAdmin.restype = wintypes.BOOL
        return bool(shell32.IsUserAnAdmin())
    except OSError:
        return False


def enable_privilege(name: str) -> bool:
    """为当前进程启用一个特权。成功返回 True；失败返回 False（不抛）。

    失败是常态（普通用户），调用方据此决定给出什么提示，因此不用异常表达。
    """
    if not IS_WINDOWS:
        return False
    try:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except OSError:
        return False

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY, ctypes.byref(token)
    ):
        return False
    try:
        luid = _LUID()
        if not advapi32.LookupPrivilegeValueW(None, name, ctypes.byref(luid)):
            return False
        tp = _TOKEN_PRIVILEGES()
        tp.PrivilegeCount = 1
        tp.Privileges[0].Luid = luid
        tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
        if not advapi32.AdjustTokenPrivileges(
            token, False, ctypes.byref(tp), 0, None, None
        ):
            return False
        # AdjustTokenPrivileges 即使部分失败也返回 TRUE，必须看 LastError
        return ctypes.get_last_error() == 0
    finally:
        kernel32.CloseHandle(token)


ERROR_CANCELLED = 1223


def _relaunch_params(argv: list[str] | None) -> str:
    """拼出重新启动时要传给自己的参数串。

    开发态与打包态**不是一回事**，这里踩过一次：

    * 打包后 ``sys.executable`` 就是 ``LBAMover.exe``，参数原样传；
    * 开发态 ``sys.executable`` 是 ``python.exe``，只传 ``gui`` 之类的参数等于
      让 python 去执行一个叫 ``gui`` 的文件 —— 立刻失败，而 ``ShellExecuteW``
      只回一个笼统的错误码，看起来就像"提权没反应"。

    所以开发态必须把 ``sys.argv[0]``（脚本路径）一起带上。路径含空格时要用
    Windows 的引用规则，``list2cmdline`` 就是这件事的标准实现。
    """
    extra = list(sys.argv[1:] if argv is None else argv)
    if getattr(sys, "frozen", False):
        return subprocess.list2cmdline(extra)
    return subprocess.list2cmdline([sys.argv[0], *extra])


def _launch_error(code: int) -> str:
    """把 ``ShellExecuteW`` 的错误码翻译成人能照着处理的说明。"""
    if code == ERROR_CANCELLED:
        return "UAC 授权被取消（或系统策略不允许提权）"
    text = ctypes.FormatError(code).strip()
    return f"启动失败：{text or f'错误码 {code}'}"


def relaunch_as_admin(argv: list[str] | None = None) -> tuple[bool, str]:
    """用 ``runas`` 重新拉起自己。返回 ``(是否已发起, 失败说明)``。

    这是**显式动作**，只在用户点了按钮时调用，绝不在启动流程里自动执行。
    失败原因要能被人读懂：UAC 被取消、程序找不到、系统策略禁止，处理方式
    完全不同，笼统地说"提权失败"只会让人去猜。
    """
    if not IS_WINDOWS:
        return False, "只有 Windows 需要提权"
    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        # 不声明 restype 的话 HINSTANCE 会被当成 int 截断，返回值判断跟着失真
        shell32.ShellExecuteW.restype = ctypes.c_void_p
        shell32.ShellExecuteW.argtypes = [
            wintypes.HWND,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            ctypes.c_int,
        ]
        params = _relaunch_params(argv)
        rc = shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
    except OSError as exc:
        return False, f"无法调用 ShellExecuteW：{exc}"

    code = int(rc or 0)
    if code > 32:  # >32 才是"已发起"，其余是错误码
        return True, ""
    return False, _launch_error(code)
