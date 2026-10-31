"""权限：检查是否管理员、按需提升特权。

原始卷写入需要 ``SeManageVolumePrivilege``（管理卷）以及管理员令牌本身。
这里**不做**自动 UAC 提权——静默弹 UAC 会让用户不知道自己在授权什么。
UI 的做法是：检测到非管理员时，把"以管理员身份重新启动"做成一个显式按钮。
"""

from __future__ import annotations

import ctypes
import os
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


def relaunch_as_admin(argv: list[str] | None = None) -> bool:
    """用 ``runas`` 重新拉起自己。返回是否成功发起。

    这是**显式动作**，只在用户点了按钮时调用，绝不在启动流程里自动执行。
    """
    if not IS_WINDOWS:
        return False
    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        params = " ".join(argv if argv is not None else sys.argv[1:])
        rc = shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
        return int(rc) > 32
    except OSError:
        return False
