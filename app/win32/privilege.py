"""管理员检测与 ``SE_MANAGE_VOLUME_NAME`` 特权提升。

``FSCTL_MOVE_FILE`` 若缺少该特权一定返回 ``ERROR_ACCESS_DENIED``，
因此执行模式前必须先成功提权。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Optional

from app.win32 import api

SE_MANAGE_VOLUME_NAME = "SeManageVolumePrivilege"


class LUID(ctypes.Structure):
    """本地唯一标识符。"""

    _fields_ = [
        ("LowPart", wintypes.DWORD),
        ("HighPart", wintypes.LONG),
    ]


class LUID_AND_ATTRIBUTES(ctypes.Structure):
    """LUID + 属性位。"""

    _fields_ = [
        ("Luid", LUID),
        ("Attributes", wintypes.DWORD),
    ]


class TOKEN_PRIVILEGES(ctypes.Structure):
    """单个特权的 ``TOKEN_PRIVILEGES``。"""

    _fields_ = [
        ("PrivilegeCount", wintypes.DWORD),
        ("Privileges", LUID_AND_ATTRIBUTES * 1),
    ]


class TOKEN_ELEVATION(ctypes.Structure):
    """``TokenElevation`` 查询结果。"""

    _fields_ = [("TokenIsElevated", wintypes.DWORD)]


def is_admin() -> bool:
    """当前进程是否以管理员（已提升）身份运行。"""
    try:
        if bool(api.shell32.IsUserAnAdmin()):
            return True
    except (AttributeError, OSError):
        pass  # 回退到令牌检查

    token: Optional[int] = None
    try:
        token = _open_process_token()
        if token is None:
            return False
        elevation = TOKEN_ELEVATION()
        returned = wintypes.DWORD(0)
        ok = api.advapi32.GetTokenInformation(
            token,
            api.TokenElevation,
            ctypes.byref(elevation),
            ctypes.sizeof(elevation),
            ctypes.byref(returned),
        )
        return bool(ok) and bool(elevation.TokenIsElevated)
    finally:
        api.close_handle(token)


def _open_process_token() -> Optional[int]:
    """打开当前进程令牌（``TOKEN_QUERY | TOKEN_ADJUST_PRIVILEGES``）。"""
    token = wintypes.HANDLE(0)
    ok = api.advapi32.OpenProcessToken(
        api.kernel32.GetCurrentProcess(),
        api.TOKEN_QUERY | api.TOKEN_ADJUST_PRIVILEGES,
        ctypes.byref(token),
    )
    if not ok:
        return None
    value = token.value
    return int(value) if value else None


def enable_manage_volume_privilege() -> bool:
    """提升 ``SeManageVolumePrivilege``。

    Returns:
        ``True`` 表示特权已启用；``False`` 表示未能启用（通常是权限不足）。
    """
    token = _open_process_token()
    if token is None:
        return False

    try:
        luid = LUID()
        if not api.advapi32.LookupPrivilegeValueW(
            None, SE_MANAGE_VOLUME_NAME, ctypes.byref(luid)
        ):
            return False

        privileges = TOKEN_PRIVILEGES()
        privileges.PrivilegeCount = 1
        privileges.Privileges[0].Luid = luid
        privileges.Privileges[0].Attributes = api.SE_PRIVILEGE_ENABLED

        ctypes.set_last_error(0)
        ok = api.advapi32.AdjustTokenPrivileges(
            token, False, ctypes.byref(privileges), 0, None, None
        )
        if not ok:
            return False
        # AdjustTokenPrivileges 即使返回成功也可能什么都没做：
        # 必须再检查 GetLastError 是否为 ERROR_NOT_ALL_ASSIGNED
        return ctypes.get_last_error() != api.ERROR_NOT_ALL_ASSIGNED
    finally:
        api.close_handle(token)


def privilege_report() -> str:
    """返回给日志用的中文权限状态说明。"""
    if is_admin():
        if enable_manage_volume_privilege():
            return "管理员权限已就绪，SE_MANAGE_VOLUME_NAME 已启用"
        return "管理员权限可用，但 SE_MANAGE_VOLUME_NAME 提权失败（执行将被拒绝）"
    return "当前为普通用户权限：仅可扫描与分析，执行迁移需要以管理员身份重启"
