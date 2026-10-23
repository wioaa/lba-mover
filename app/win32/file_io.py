"""用户态文件读写（ctypes）——分块流式搬迁模式的“手”（1.0.0C 特殊工作模式）。

只做普通 ``CreateFileW`` / ``ReadFile`` / ``WriteFile``，**不碰 ``FSCTL_MOVE_FILE``、
不需要管理卷特权**。与 ``app.core.block_stream``（分块计划 / 可调缓存 / 流水线调度）
配合，实现「源 → 暂存盘 → 目的」的双阶段分块流式搬迁：

* block = 65536 簇；缓冲按 block 倍数可调（``buffer_blocks``）；
* ``load``（读源块 → 写暂存盘）与 ``store``（读暂存块 → 写目的）按
  :func:`block_stream.stream_schedule` 重叠推进；
* 数据经文件系统落地，源簇读出后逻辑上即刻被重新占用。

这些原语**可对临时文件直接单测**（验证 ctypes 绑定与 staged_copy 正确性）；
真正的卷级 LBA 重排效果需在真实磁盘上验证。
"""

from __future__ import annotations

import ctypes
import os
from typing import Optional, Tuple

from app.core.block_stream import (
    BLOCK_CLUSTERS,
    plan_blocks,
    stream_schedule,
)
from app.win32 import api
from app.win32.api import kernel32, wintypes

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
CREATE_ALWAYS = 2
FILE_BEGIN = 0
_INVALID = ctypes.c_void_p(-1).value

# WriteFile 之前未绑定（读路径只用 ReadFile）；这里按 ReadFile 对称补上
kernel32.WriteFile.argtypes = [
    wintypes.HANDLE,
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    wintypes.LPVOID,
]
kernel32.WriteFile.restype = wintypes.BOOL


def _ok_handle(handle: int, context: str) -> int:
    if handle is None or handle == _INVALID:
        raise api.Win32Error(kernel32.GetLastError(), context)
    return handle


def open_read(path: str) -> int:
    """以只读 + 共享方式打开已存在文件。"""
    handle = kernel32.CreateFileW(
        str(path), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
        None, OPEN_EXISTING, 0, None,
    )
    return _ok_handle(handle, f"open_read({path})")


def open_write(path: str) -> int:
    """创建/覆盖一个只写文件。"""
    handle = kernel32.CreateFileW(
        str(path), GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE,
        None, CREATE_ALWAYS, 0, None,
    )
    return _ok_handle(handle, f"open_write({path})")


def open_readwrite(path: str) -> int:
    """创建/覆盖一个可读写文件（用作暂存盘）。"""
    handle = kernel32.CreateFileW(
        str(path), GENERIC_READ | GENERIC_WRITE, 0,
        None, CREATE_ALWAYS, 0, None,
    )
    return _ok_handle(handle, f"open_readwrite({path})")


def close(handle: int) -> None:
    if handle and handle != _INVALID:
        kernel32.CloseHandle(handle)


def read_at(handle: int, offset: int, size: int) -> bytes:
    """从 ``offset`` 读 ``size`` 字节（定位 + ReadFile）。"""
    if not kernel32.SetFilePointerEx(handle, int(offset), None, FILE_BEGIN):
        raise api.Win32Error(kernel32.GetLastError(), f"SetFilePointerEx@{offset}")
    buf = ctypes.create_string_buffer(int(size))
    done = wintypes.DWORD(0)
    if not kernel32.ReadFile(handle, buf, int(size), ctypes.byref(done), None):
        raise api.Win32Error(kernel32.GetLastError(), f"ReadFile@{offset}")
    return buf.raw[: done.value]


def write_at(handle: int, offset: int, data: bytes) -> int:
    """把 ``data`` 写到 ``offset``（定位 + WriteFile），返回写入字节数。"""
    if not kernel32.SetFilePointerEx(handle, int(offset), None, FILE_BEGIN):
        raise api.Win32Error(kernel32.GetLastError(), f"SetFilePointerEx@{offset}")
    done = wintypes.DWORD(0)
    n = len(data)
    if n and not kernel32.WriteFile(handle, data, n, ctypes.byref(done), None):
        raise api.Win32Error(kernel32.GetLastError(), f"WriteFile@{offset}")
    return done.value


def copy_block(src: int, dst: int, offset: int, length: int) -> int:
    """单跳：把 ``[offset, offset+length)`` 从 ``src`` 读到 ``dst``。"""
    data = read_at(src, offset, length)
    return write_at(dst, offset, data)


def staged_copy(
    src_path: str,
    scratch_path: str,
    dst_path: str,
    *,
    buffer_blocks: int = 2,
    bytes_per_cluster: int = 4096,
    block_clusters: int = BLOCK_CLUSTERS,
) -> Tuple[int, int]:
    """双阶段分块流式搬迁：源 →(load) 暂存盘 →(store) 目的。

    按 :func:`block_stream.stream_schedule` 推进；暂存盘同时驻留块数 ≤
    ``buffer_blocks``。返回 ``(写入目的字节数, 块数)``。
    """
    total = os.path.getsize(src_path)
    blocks = plan_blocks(total, bytes_per_cluster, block_clusters)
    if not blocks:
        handle = open_write(dst_path)
        close(handle)
        return 0, 0

    src = open_read(src_path)
    scratch = open_readwrite(scratch_path)
    dst = open_write(dst_path)
    try:
        written = 0
        for kind, index in stream_schedule(len(blocks), buffer_blocks):
            block = blocks[index]
            if kind == "load":
                write_at(scratch, block.offset, read_at(src, block.offset, block.length))
            else:  # store
                written += write_at(dst, block.offset, read_at(scratch, block.offset, block.length))
        # 搬运完成：清理暂存盘（硬盘缓存用完即还）
        try:
            os.remove(scratch_path)
        except OSError:
            pass
        return written, len(blocks)
    finally:
        close(src)
        close(scratch)
        close(dst)


def memory_copy(
    src_path: str,
    dst_path: str,
    *,
    buffer_blocks: int = 2,
    bytes_per_cluster: int = 4096,
    block_clusters: int = BLOCK_CLUSTERS,
) -> Tuple[int, int]:
    """内存分配缓存模式：块在 RAM 缓冲里中转（无暂存盘）。

    ``load`` 读源块进 RAM、``store`` 从 RAM 写目的；RAM 同时驻留 ≤ ``buffer_blocks``
    个 block（可调）。最快，但占用内存 = ``buffer_blocks × block``。返回
    ``(写入目的字节数, 块数)``。
    """
    total = os.path.getsize(src_path)
    blocks = plan_blocks(total, bytes_per_cluster, block_clusters)
    if not blocks:
        handle = open_write(dst_path)
        close(handle)
        return 0, 0

    src = open_read(src_path)
    dst = open_write(dst_path)
    cache: dict = {}  # index -> bytes，RAM 驻留 ≤ buffer_blocks
    try:
        written = 0
        for kind, index in stream_schedule(len(blocks), buffer_blocks):
            block = blocks[index]
            if kind == "load":
                cache[index] = read_at(src, block.offset, block.length)
            else:  # store
                written += write_at(dst, block.offset, cache.pop(index))
        return written, len(blocks)
    finally:
        close(src)
        close(dst)


#: 内存分配缓存模式
CACHE_MODE_MEMORY = "memory"
#: 设定缓存目录的硬盘缓存模式
CACHE_MODE_DISK = "disk"


def copy_stream(
    src_path: str,
    dst_path: str,
    *,
    mode: str = CACHE_MODE_MEMORY,
    cache_dir: Optional[str] = None,
    buffer_blocks: int = 2,
    bytes_per_cluster: int = 4096,
    block_clusters: int = BLOCK_CLUSTERS,
) -> Tuple[int, int]:
    """统一入口：按缓存模式做分块流式搬运。

    * ``mode="memory"``：内存分配缓存——块在 RAM 中转，最快、占内存；
    * ``mode="disk"``：设定缓存目录的硬盘缓存——块落 ``cache_dir`` 暂存盘再读出，
      占磁盘、可缓冲更大（``buffer_blocks`` 仍按 65536 簇 block 的倍数可调）。

    Returns:
        ``(写入目的字节数, 块数)``。
    """
    if mode == CACHE_MODE_MEMORY:
        return memory_copy(
            src_path, dst_path,
            buffer_blocks=buffer_blocks,
            bytes_per_cluster=bytes_per_cluster,
            block_clusters=block_clusters,
        )
    if mode == CACHE_MODE_DISK:
        if not cache_dir:
            raise ValueError("disk 缓存模式需指定 cache_dir（缓存目录）")
        os.makedirs(cache_dir, exist_ok=True)
        scratch = os.path.join(cache_dir, os.path.basename(dst_path) + ".stage")
        return staged_copy(
            src_path, scratch, dst_path,
            buffer_blocks=buffer_blocks,
            bytes_per_cluster=bytes_per_cluster,
            block_clusters=block_clusters,
        )
    raise ValueError(f"未知缓存模式：{mode!r}（可选 {CACHE_MODE_MEMORY!r} / {CACHE_MODE_DISK!r}）")


# ---------------------------------------------------------------- 回站删除
_FO_DELETE = 3
_FOF_ALLOWUNDO = 0x40
_FOF_NOCONFIRMATION = 0x10
_FOF_SILENT = 0x4
_FOF_NOERRORUI = 0x400


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", ctypes.c_uint),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", wintypes.LPVOID),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


_shell32 = ctypes.windll.shell32
_shell32.SHFileOperationW.argtypes = [ctypes.POINTER(_SHFILEOPSTRUCTW)]
_shell32.SHFileOperationW.restype = ctypes.c_int


def recycle(path: str) -> bool:
    """把文件移到回收站（可恢复），而非永久删除。返回是否成功。"""
    op = _SHFILEOPSTRUCTW()
    op.wFunc = _FO_DELETE
    op.pFrom = f"{path}\0\0"   # pFrom 需以双空字符结尾
    op.fFlags = _FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_SILENT | _FOF_NOERRORUI
    return _shell32.SHFileOperationW(ctypes.byref(op)) == 0 and not op.fAnyOperationsAborted
