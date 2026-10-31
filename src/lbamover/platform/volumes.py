"""卷枚举：把「本机有哪些可整理的卷」变成 :class:`VolumeInfo` 列表。

只做**只读**探测：``GetVolumeInformation`` / ``GetDiskFreeSpaceEx`` 都不
需要管理员权限，因此在普通权限下 UI 也能把目标列表画出来，用户点"开始"
时才因为写权限不足被拦下——比空列表让人猜要友好。

不在 Windows 上时返回空列表并带 note，不抛异常：UI 在没有卷的情况下
仍然要能启动并给出解释。
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

from ..contract.enums import VolumeKind
from ..contract.models import Geometry, VolumeInfo

__all__ = [
    "DRIVE_FIXED",
    "DRIVE_REMOVABLE",
    "enumerate_volumes",
    "probe_volume",
    "system_drive",
]

DRIVE_UNKNOWN = 0
DRIVE_NO_ROOT_DIR = 1
DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
DRIVE_REMOTE = 4
DRIVE_CDROM = 5
DRIVE_RAMDISK = 6

_KIND_MAP = {
    DRIVE_FIXED: VolumeKind.FIXED,
    DRIVE_REMOVABLE: VolumeKind.REMOVABLE,
    DRIVE_REMOTE: VolumeKind.NETWORK,
    DRIVE_RAMDISK: VolumeKind.RAMDISK,
}

IS_WINDOWS = os.name == "nt"


def system_drive() -> str:
    """系统盘挂载点，形如 ``C:\\``。非 Windows 返回空串。"""
    return os.environ.get("SystemDrive", "") + "\\" if IS_WINDOWS else ""


def _kernel32():
    lib = ctypes.WinDLL("kernel32", use_last_error=True)
    lib.GetLogicalDriveStringsW.restype = wintypes.DWORD
    lib.GetLogicalDriveStringsW.argtypes = [wintypes.DWORD, wintypes.LPWSTR]
    lib.GetDriveTypeW.restype = wintypes.UINT
    lib.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    lib.GetVolumeInformationW.restype = wintypes.BOOL
    lib.GetVolumeInformationW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.LPDWORD,
        wintypes.LPDWORD,
        wintypes.LPDWORD,
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    lib.GetDiskFreeSpaceExW.restype = wintypes.BOOL
    lib.GetDiskFreeSpaceExW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_ulonglong),
        ctypes.POINTER(ctypes.c_ulonglong),
        ctypes.POINTER(ctypes.c_ulonglong),
    ]
    lib.GetDiskFreeSpaceW.restype = wintypes.BOOL
    lib.GetDiskFreeSpaceW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPDWORD,
        wintypes.LPDWORD,
        wintypes.LPDWORD,
        wintypes.LPDWORD,
    ]
    return lib


def _mount_points(lib) -> list[str]:
    size = lib.GetLogicalDriveStringsW(0, None)
    if not size:
        return []
    buf = ctypes.create_unicode_buffer(size)
    lib.GetLogicalDriveStringsW(size, buf)
    return [p for p in buf[:].split("\x00") if p.strip()]


def probe_volume(mount_point: str, *, writable_hint: bool | None = None) -> VolumeInfo | None:
    """探测单个卷。失败返回 ``None``（调用方决定是否记日志）。"""
    if not IS_WINDOWS:
        return None
    lib = _kernel32()
    root = mount_point if mount_point.endswith("\\") else mount_point + "\\"

    dtype = lib.GetDriveTypeW(root)
    kind = _KIND_MAP.get(dtype, VolumeKind.UNKNOWN)
    if kind in (VolumeKind.UNKNOWN, VolumeKind.NETWORK):
        # 光驱 / 未就绪 / 网络映射盘：无法用 `\\.\X:` 打开，列出来只会误导
        return None

    label_buf = ctypes.create_unicode_buffer(261)
    fs_buf = ctypes.create_unicode_buffer(261)
    serial = wintypes.DWORD(0)
    max_len = wintypes.DWORD(0)
    flags = wintypes.DWORD(0)
    ok = lib.GetVolumeInformationW(
        root,
        label_buf,
        261,
        ctypes.byref(serial),
        ctypes.byref(max_len),
        ctypes.byref(flags),
        fs_buf,
        261,
    )
    label = label_buf.value if ok else ""
    fs = fs_buf.value if ok else ""

    free = ctypes.c_ulonglong(0)
    total = ctypes.c_ulonglong(0)
    avail = ctypes.c_ulonglong(0)
    if not lib.GetDiskFreeSpaceExW(root, ctypes.byref(free), ctypes.byref(total), ctypes.byref(avail)):
        return None

    spc = wintypes.DWORD(0)
    bps = wintypes.DWORD(0)
    free_clusters = wintypes.DWORD(0)
    total_clusters = wintypes.DWORD(0)
    cluster_bytes = 4096
    if lib.GetDiskFreeSpaceW(
        root,
        ctypes.byref(spc),
        ctypes.byref(bps),
        ctypes.byref(free_clusters),
        ctypes.byref(total_clusters),
    ):
        cluster_bytes = max(512, int(spc.value) * int(bps.value))

    total_bytes = int(total.value) or int(total_clusters.value) * cluster_bytes
    if total_bytes <= 0 or cluster_bytes <= 0:
        return None
    block_count = total_bytes // cluster_bytes

    is_sys = root.upper().startswith(system_drive().upper()) and system_drive() != ""
    device_path = "\\\\.\\" + root.rstrip("\\")

    return VolumeInfo(
        device_path=device_path,
        mount_point=root,
        label=label,
        filesystem=fs,
        kind=kind,
        geometry=Geometry(block_size=cluster_bytes, block_count=block_count),
        free_bytes=int(free.value) or int(free_clusters.value) * cluster_bytes,
        is_system=is_sys,
        is_mounted=True,
        readable=True,
        writable=(
            writable_hint if writable_hint is not None else kind.writable_by_default
        ),
        note="" if is_sys else "",
    )


def enumerate_volumes() -> list[VolumeInfo]:
    """列出所有可整理的卷，系统卷排最前，其余按盘符排序。"""
    if not IS_WINDOWS:
        return []
    lib = _kernel32()
    out: list[VolumeInfo] = []
    for mp in sorted(_mount_points(lib)):
        try:
            info = probe_volume(mp)
        except OSError:
            info = None
        if info is not None:
            out.append(info)
    out.sort(key=lambda v: (not v.is_system, v.mount_point))
    return out
