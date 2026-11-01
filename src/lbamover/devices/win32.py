"""Windows 真机后端：原始卷块读写 + NTFS 簇级搬迁原语。

===========================================================================
安全边界（读之前请先读这段）
===========================================================================

1. **原始块写只对「未挂载的卷」或「卷镜像」安全。**
   对一个**已挂载**的 NTFS 卷直接改写数据区，文件系统并不知道这些块换了
   主人，结果是元数据与数据不一致 —— 也就是文件系统损坏。这不是"可能"，
   是必然。因此 :class:`Win32VolumeDevice` 在 ``writable=True`` 时强制要求
   先 ``lock_and_dismount()``；只有显式传入 ``allow_raw_on_mounted=True``
   （由 UI 的双重确认闸门产生）才会绕过。

2. **对已挂载卷做「整理」，正确原语是 ``FSCTL_MOVE_FILE``，不是裸写。**
   它由 NTFS 自己搬簇并同步更新元数据，这才是安全路径。本模块提供
   :meth:`Win32Native.move_file_data` 这个原语，它需要调用方给出
   ``(file_handle, start_vcn, start_lcn, cluster_count)``。把文件映射到
   这些参数需要读取 NTFS 的 runlist（``FSCTL_GET_RETRIEVAL_POINTERS``），
   本版尚未实现该目录服务，见 ``docs/ARCHITECTURE.md`` 的「能力矩阵」。

3. **无缓冲 IO 必须对齐。** 缓冲地址、长度、偏移三者都要对齐到扇区，
   否则 ``ReadFile`` 直接返回 ``ERROR_INVALID_PARAMETER``。这里用
   ``VirtualAlloc``（页对齐，4096）来保证地址对齐。

本模块**未经管理员权限下的真机验证**。所有入口在非 Windows 上抛
:class:`DeviceUnavailable`；真机能力由 :func:`capabilities` 探测后上报 UI，
不会静默降级成"假装成功"。
"""

from __future__ import annotations

import ctypes
import os
import struct
import threading
from ctypes import wintypes
from dataclasses import dataclass

from ..contract.enums import ErrorClass
from ..contract.models import Geometry
from ..platform.errors import DeviceUnavailable, classify_winerror
from .base import BlockDeviceBase, DeviceError, check_range

__all__ = [
    "Win32VolumeDevice",
    "Win32Native",
    "capabilities",
    "is_windows",
]

IS_WINDOWS = os.name == "nt"

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_READ_ATTRIBUTES = 0x00000080
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3

FILE_FLAG_NO_BUFFERING = 0x20000000
FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
FILE_FLAG_WRITE_THROUGH = 0x80000000
#: 允许 CreateFileW 打开目录与特殊文件（$MFT、页面文件这类也要靠它）
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000

FILE_BEGIN = 0
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000
PAGE_READWRITE = 0x04

#: 卷必须被锁定才能原始写
FSCTL_LOCK_VOLUME = 0x00090018
FSCTL_UNLOCK_VOLUME = 0x0009001C
FSCTL_DISMOUNT_VOLUME = 0x00090020
#: NTFS 自管搬迁：由文件系统搬簇并更新元数据（安全路径）
FSCTL_MOVE_FILE = 0x00090074
#: 查文件的 VCN→LCN 映射（METHOD_NEITHER，必须用虚拟内存缓冲）
FSCTL_GET_RETRIEVAL_POINTERS = 0x00090073
IOCTL_DISK_GET_DRIVE_GEOMETRY = 0x00070000
IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C

ERROR_INSUFFICIENT_BUFFER = 122
ERROR_MORE_DATA = 234
ERROR_NOT_SUPPORTED = 50
ERROR_INVALID_FUNCTION = 1
#: retrieval pointers 在"文件不占任何簇"时返回它（内容常驻 MFT 的小文件）
ERROR_HANDLE_EOF = 38


# --------------------------------------------------------------------------
# 结构体
# --------------------------------------------------------------------------


class _DISK_GEOMETRY(ctypes.Structure):
    _fields_ = [
        ("Cylinders", ctypes.c_longlong),
        ("MediaType", wintypes.DWORD),
        ("TracksPerCylinder", wintypes.DWORD),
        ("SectorsPerTrack", wintypes.DWORD),
        ("BytesPerSector", wintypes.DWORD),
    ]


class _LENGTH_INFORMATION(ctypes.Structure):
    _fields_ = [("Length", ctypes.c_longlong)]


class _MOVE_FILE_DATA(ctypes.Structure):
    """``FSCTL_MOVE_FILE`` 的输入结构，全程簇粒度（不是块）。"""

    _fields_ = [
        ("FileHandle", wintypes.HANDLE),
        ("StartingVcn", ctypes.c_longlong),
        ("StartingLcn", ctypes.c_longlong),
        ("ClusterCount", wintypes.DWORD),
    ]


# --------------------------------------------------------------------------
# 动态库与对齐缓冲
# --------------------------------------------------------------------------

_k32 = None
_k32_lock = threading.Lock()


def _kernel32():
    global _k32
    if not IS_WINDOWS:
        raise DeviceUnavailable("非 Windows 平台，无法使用原始卷后端")
    if _k32 is None:
        with _k32_lock:
            if _k32 is None:
                lib = ctypes.WinDLL("kernel32", use_last_error=True)
                lib.CreateFileW.restype = wintypes.HANDLE
                lib.CreateFileW.argtypes = [
                    wintypes.LPCWSTR,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.LPVOID,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.HANDLE,
                ]
                lib.CloseHandle.restype = wintypes.BOOL
                lib.CloseHandle.argtypes = [wintypes.HANDLE]
                lib.DeviceIoControl.restype = wintypes.BOOL
                lib.DeviceIoControl.argtypes = [
                    wintypes.HANDLE,
                    wintypes.DWORD,
                    wintypes.LPVOID,
                    wintypes.DWORD,
                    wintypes.LPVOID,
                    wintypes.DWORD,
                    ctypes.POINTER(wintypes.DWORD),
                    wintypes.LPVOID,
                ]
                lib.SetFilePointerEx.restype = wintypes.BOOL
                lib.SetFilePointerEx.argtypes = [
                    wintypes.HANDLE,
                    ctypes.c_longlong,
                    ctypes.POINTER(ctypes.c_longlong),
                    wintypes.DWORD,
                ]
                lib.ReadFile.restype = wintypes.BOOL
                lib.ReadFile.argtypes = [
                    wintypes.HANDLE,
                    wintypes.LPVOID,
                    wintypes.DWORD,
                    ctypes.POINTER(wintypes.DWORD),
                    wintypes.LPVOID,
                ]
                lib.WriteFile.restype = wintypes.BOOL
                lib.WriteFile.argtypes = [
                    wintypes.HANDLE,
                    wintypes.LPVOID,
                    wintypes.DWORD,
                    ctypes.POINTER(wintypes.DWORD),
                    wintypes.LPVOID,
                ]
                lib.VirtualAlloc.restype = ctypes.c_void_p
                lib.VirtualAlloc.argtypes = [
                    ctypes.c_void_p,
                    ctypes.c_size_t,
                    wintypes.DWORD,
                    wintypes.DWORD,
                ]
                lib.VirtualFree.restype = wintypes.BOOL
                lib.VirtualFree.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD]
                lib.FlushFileBuffers.restype = wintypes.BOOL
                lib.FlushFileBuffers.argtypes = [wintypes.HANDLE]
                _k32 = lib
    return _k32


class AlignedBuffer:
    """页对齐缓冲。``VirtualAlloc`` 的返回值天然按页（4096）对齐，
    满足无缓冲 IO 对缓冲地址的对齐要求。"""

    __slots__ = ("_ptr", "_size")

    def __init__(self, size: int) -> None:
        if size <= 0:
            raise ValueError("缓冲大小必须为正")
        ptr = _kernel32().VirtualAlloc(None, size, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE)
        if not ptr:
            err = ctypes.get_last_error()
            raise DeviceError(
                f"VirtualAlloc 失败（{size} 字节）",
                error_class=classify_winerror(err),
                winerror=err,
            )
        self._ptr = ptr
        self._size = size

    @property
    def address(self) -> int:
        return self._ptr

    @property
    def size(self) -> int:
        return self._size

    def load(self, data: bytes) -> None:
        n = min(len(data), self._size)
        ctypes.memmove(self._ptr, data, n)

    def to_bytes(self, n: int) -> bytes:
        return ctypes.string_at(self._ptr, min(n, self._size))

    def free(self) -> None:
        if self._ptr:
            _kernel32().VirtualFree(self._ptr, 0, MEM_RELEASE)
            self._ptr = 0

    def __del__(self) -> None:  # pragma: no cover - 兜底释放
        try:
            self.free()
        except Exception:
            pass


# --------------------------------------------------------------------------
# 原生助手
# --------------------------------------------------------------------------


def is_windows() -> bool:
    return IS_WINDOWS


def _win_error(what: str, *, lba: int | None = None, count: int = 0) -> DeviceError:
    err = ctypes.get_last_error()
    return DeviceError(
        f"{what} 失败（{ctypes.FormatError(err).strip() if IS_WINDOWS else err}）",
        error_class=classify_winerror(err),
        lba=lba,
        count=count,
        winerror=err,
    )


class Win32Native:
    """一组无状态的 Win32 调用封装。所有方法都做错误码翻译。"""

    # -- 句柄 -------------------------------------------------------------

    @staticmethod
    def open_volume(device_path: str, writable: bool, unbuffered: bool = True) -> int:
        access = GENERIC_READ | (GENERIC_WRITE if writable else 0)
        flags = FILE_FLAG_SEQUENTIAL_SCAN
        if unbuffered:
            flags |= FILE_FLAG_NO_BUFFERING
        handle = _kernel32().CreateFileW(
            device_path,
            access,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            flags,
            None,
        )
        if handle == INVALID_HANDLE_VALUE or handle is None:
            err = ctypes.get_last_error()
            raise DeviceUnavailable(
                f"打开 {device_path} 失败：{ctypes.FormatError(err).strip()}"
                + ("（原始卷读写需要管理员权限）" if err == 5 else ""),
                winerror=err,
            )
        return int(handle)

    @staticmethod
    def close(handle: int) -> None:
        if handle:
            _kernel32().CloseHandle(wintypes.HANDLE(handle))

    # -- IOCTL ------------------------------------------------------------

    @staticmethod
    def ioctl(handle: int, code: int, in_buf: ctypes.Structure | None, out_size: int) -> bytes:
        out = ctypes.create_string_buffer(out_size) if out_size > 0 else None
        returned = wintypes.DWORD(0)
        ok = _kernel32().DeviceIoControl(
            wintypes.HANDLE(handle),
            code,
            ctypes.byref(in_buf) if in_buf is not None else None,
            ctypes.sizeof(in_buf) if in_buf is not None else 0,
            out,
            out_size,
            ctypes.byref(returned),
            None,
        )
        if not ok:
            err = ctypes.get_last_error()
            if err in (ERROR_MORE_DATA, ERROR_INSUFFICIENT_BUFFER) and out is not None:
                return bytes(out.raw[: returned.value])
            raise _win_error(f"IOCTL 0x{code:08X}")
        return bytes(out.raw[: returned.value]) if out is not None else b""

    @staticmethod
    def device_length(handle: int) -> int:
        raw = Win32Native.ioctl(handle, IOCTL_DISK_GET_LENGTH_INFO, None, 8)
        if len(raw) < 8:
            raise DeviceError("IOCTL_DISK_GET_LENGTH_INFO 返回数据不足", error_class=ErrorClass.FATAL)
        info = _LENGTH_INFORMATION.from_buffer_copy(raw[:8])
        return int(info.Length)

    @staticmethod
    def sector_size(handle: int) -> int:
        raw = Win32Native.ioctl(handle, IOCTL_DISK_GET_DRIVE_GEOMETRY, None, ctypes.sizeof(_DISK_GEOMETRY))
        geo = _DISK_GEOMETRY.from_buffer_copy(raw[: ctypes.sizeof(_DISK_GEOMETRY)])
        return int(geo.BytesPerSector) or 512

    # -- 锁卷 -------------------------------------------------------------

    @staticmethod
    def lock_volume(handle: int, dismount: bool = True, timeout_s: float = 5.0) -> None:
        """锁定（并可选卸载）卷。写入原始扇区前必须做，否则缓存脏页会覆盖修改。"""
        import time

        deadline = time.monotonic() + timeout_s
        last: DeviceError | None = None
        while True:
            try:
                Win32Native.ioctl(handle, FSCTL_LOCK_VOLUME, None, 0)
                break
            except DeviceError as exc:
                last = exc
            if time.monotonic() > deadline:
                raise DeviceError(
                    "锁定卷失败：卷正被占用（请关闭该卷上的所有程序后重试）",
                    error_class=ErrorClass.RETRYABLE,
                    winerror=getattr(last, "winerror", None),
                )
            time.sleep(0.2)
        if dismount:
            try:
                Win32Native.ioctl(handle, FSCTL_DISMOUNT_VOLUME, None, 0)
            except DeviceError as exc:
                # 卸载失败不致命：卷已锁定时写入仍然安全
                raise DeviceError(
                    "卷已锁定但卸载失败，写入仍可继续（部分系统卷不允许卸载）",
                    error_class=ErrorClass.RETRYABLE,
                    winerror=exc.winerror,
                ) from None

    @staticmethod
    def unlock_volume(handle: int) -> None:
        try:
            Win32Native.ioctl(handle, FSCTL_UNLOCK_VOLUME, None, 0)
        except DeviceError:
            pass  # 句柄关闭时系统会自动解锁

    # -- NTFS 安全搬迁 ----------------------------------------------------

    @staticmethod
    def move_file_data(
        volume_handle: int,
        file_handle: int,
        start_vcn: int,
        start_lcn: int,
        cluster_count: int,
    ) -> None:
        """把文件的 ``cluster_count`` 个簇从 VCN 起搬到 LCN 起的位置。

        这是唯一被 NTFS 认可的"搬块"方式：元数据由文件系统自己更新，
        不会造成不一致。调用方必须保证 ``[start_lcn, start_lcn+count)``
        落在自由空间内。
        """
        if cluster_count <= 0:
            raise DeviceError("cluster_count 必须为正", error_class=ErrorClass.FATAL)
        if start_lcn < 0:
            raise DeviceError("start_lcn 不能为负", error_class=ErrorClass.FATAL)
        data = _MOVE_FILE_DATA(
            FileHandle=wintypes.HANDLE(file_handle),
            StartingVcn=start_vcn,
            StartingLcn=start_lcn,
            ClusterCount=cluster_count,
        )
        Win32Native.ioctl(volume_handle, FSCTL_MOVE_FILE, data, 0)

    @staticmethod
    def ioctl_neither(handle: int, code: int, address: int, in_size: int, out_size: int) -> int:
        """METHOD_NEITHER 的 IOCTL：驱动直接拿裸指针读写同一块缓冲。

        这类 IOCTL（如 retrieval pointers）不能走 ``byref(结构体)`` 那套，
        缓冲必须是我们自己分配的、地址稳定的内存（``VirtualAlloc``）。
        返回实际写入字节数。
        """
        returned = wintypes.DWORD(0)
        ok = _kernel32().DeviceIoControl(
            wintypes.HANDLE(handle),
            code,
            ctypes.c_void_p(address),
            in_size,
            ctypes.c_void_p(address),
            out_size,
            ctypes.byref(returned),
            None,
        )
        if not ok:
            err = ctypes.get_last_error()
            # 缓冲不足时驱动会把已填好的部分写回来，调用方可以先用着
            if err != ERROR_MORE_DATA and err != ERROR_INSUFFICIENT_BUFFER:
                raise _win_error(f"IOCTL 0x{code:08X}")
        return int(returned.value)

    @staticmethod
    def query_retrieval_pointers(handle: int) -> tuple[tuple[int, int, int], ...]:
        """返回 ``((start_vcn, start_lcn, count), ...)``；``lcn == -1`` 表示稀疏。

        这是判断"文件现在到底占哪些簇"的唯一可靠来源，也是搬簇之前**必须**
        重新查一次的校验依据——扫描到执行之间文件可能已经变了。

        ``ERROR_HANDLE_EOF``（38）表示**这个文件不占任何簇**（内容全在 MFT
        记录里的小文件、或零长度分配），返回空元组即可。把它当错误处理会让
        未提权扫描把小文件全记成"打不开"——实测能占 20% 以上。
        """
        size = 64 * 1024
        for _attempt in range(6):
            buf = AlignedBuffer(size)
            try:
                # 入参是 STARTING_VCN_INPUT_BUFFER，与输出共用同一块缓冲
                ctypes.memmove(buf.address, struct.pack("<q", 0), 8)
                try:
                    got = Win32Native.ioctl_neither(
                        handle, FSCTL_GET_RETRIEVAL_POINTERS, buf.address, 8, size
                    )
                except DeviceError as exc:
                    if getattr(exc, "winerror", 0) == ERROR_HANDLE_EOF:
                        return ()
                    raise
                raw = buf.to_bytes(got) if got else buf.to_bytes(size)
                parsed = _parse_retrieval(raw)
                if parsed is not None:
                    return parsed
            finally:
                buf.free()
            size *= 4  # 碎片极多的文件需要更大缓冲，重试一次就够
        raise DeviceError("查询文件簇映射失败：缓冲反复不足", error_class=ErrorClass.FATAL)

    @staticmethod
    def open_file(path: str, writable: bool = True, attributes_only: bool = False) -> int:
        """打开文件拿句柄。

        * 搬簇（``FSCTL_MOVE_FILE``）需要 ``GENERIC_READ|GENERIC_WRITE``；
        * 只查簇映射（``FSCTL_GET_RETRIEVAL_POINTERS``）只要 ``FILE_READ_ATTRIBUTES``。

        第二种很关键：页面文件、注册表 hive、别人独占打开的大文件，用
        ``GENERIC_READ`` 一律被拒，但**属性级打开通常能成功**，而查簇映射
        正需要它。不区分这两种权限，未提权扫描会漏掉一大批文件。
        """
        if attributes_only:
            access = FILE_READ_ATTRIBUTES
        else:
            access = GENERIC_READ | (GENERIC_WRITE if writable else 0)
        handle = _kernel32().CreateFileW(
            long_path(path),
            access,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS,  # 允许打开目录/特殊文件
            None,
        )
        if handle == INVALID_HANDLE_VALUE or handle is None:
            err = ctypes.get_last_error()
            raise DeviceError(
                f"打开文件失败 {path}：{ctypes.FormatError(err).strip()}",
                error_class=classify_winerror(err),
                winerror=err,
            )
        return int(handle)

    @staticmethod
    def query_cluster_geometry(handle: int) -> tuple[int, int]:
        """返回 ``(bytes_per_sector, sectors_per_cluster)``。"""
        sector = Win32Native.sector_size(handle)
        raw = Win32Native.ioctl(handle, IOCTL_DISK_GET_DRIVE_GEOMETRY, None, ctypes.sizeof(_DISK_GEOMETRY))
        geo = _DISK_GEOMETRY.from_buffer_copy(raw[: ctypes.sizeof(_DISK_GEOMETRY)])
        return sector, max(1, int(geo.SectorsPerTrack))


def long_path(path: str) -> str:
    """超过 248 字符的路径要加长路径前缀，否则 CreateFileW 直接失败。

    前缀是 Windows 的「双反斜杠 + ? + 反斜杠」约定。这里用 ``chr(92)`` 拼，
    是因为这一串反斜杠在多层转义里极易被吃掉一位；少一个的后果是
    CreateFileW 报"文件名语法不正确"，而且**只在超长路径上出现**。
    """
    prefix = "\\\\?\\"
    if path.startswith(prefix):
        return path
    p = path if os.path.isabs(path) else os.path.abspath(path)
    if len(p) >= 248:
        return prefix + p
    return p


def _parse_retrieval(raw: bytes) -> tuple[tuple[int, int, int], ...] | None:
    """解析 RETRIEVAL_POINTERS_BUFFER。

    布局：``DWORD ExtentCount; LARGE_INTEGER StartingVcn; {LARGE_INTEGER NextVcn,
    LARGE_INTEGER Lcn}[]``。每个 extent 的长度由下一个 NextVcn 推出来。
    """
    if len(raw) < 16:
        return None
    extent_count = struct.unpack_from("<I", raw, 0)[0]
    starting_vcn = struct.unpack_from("<q", raw, 8)[0]
    if extent_count == 0:
        return ()
    avail = (len(raw) - 16) // 16
    use = min(extent_count, avail)
    out: list[tuple[int, int, int]] = []
    prev_vcn = starting_vcn
    for i in range(use):
        next_vcn, lcn = struct.unpack_from("<qq", raw, 16 + i * 16)
        count = next_vcn - prev_vcn
        if count > 0:
            out.append((prev_vcn, lcn, count))
        prev_vcn = next_vcn
    return tuple(out)


# --------------------------------------------------------------------------
# 卷设备
# --------------------------------------------------------------------------


class Win32VolumeDevice(BlockDeviceBase):
    """原始卷块设备（``\\\\.\\D:`` 或 ``\\\\.\\PhysicalDrive0``）。

    打开即完成三步安全检查：句柄可写 → 扇区对齐 → 是否需要锁卷。
    """

    def __init__(
        self,
        device_path: str,
        block_size: int = 4096,
        writable: bool = False,
        unbuffered: bool = True,
        lock_volume: bool = False,
        allow_raw_on_mounted: bool = False,
    ) -> None:
        if not IS_WINDOWS:
            raise DeviceUnavailable("Win32VolumeDevice 只能在 Windows 上使用")
        self._device_path = device_path
        self._lock = threading.RLock()
        self._aligned = unbuffered
        self._buffer: AlignedBuffer | None = None
        self._locked = False

        handle = Win32Native.open_volume(device_path, writable, unbuffered)
        try:
            sector = Win32Native.sector_size(handle)
            if block_size % sector != 0:
                raise DeviceError(
                    f"block_size {block_size} 不是扇区大小 {sector} 的整数倍，"
                    "无缓冲 IO 会直接失败",
                    error_class=ErrorClass.FATAL,
                )
            length = Win32Native.device_length(handle)
            block_count = length // block_size
            if block_count <= 0:
                raise DeviceError("设备长度为 0", error_class=ErrorClass.FATAL)

            if writable and not lock_volume:
                # 写入原始扇区不锁卷 = 缓存脏页随时覆盖我们的修改
                raise DeviceError(
                    "对原始卷写入必须先锁定卷（lock_volume=True）",
                    error_class=ErrorClass.FATAL,
                )
            if writable and lock_volume:
                try:
                    Win32Native.lock_volume(handle, dismount=True)
                    self._locked = True
                except DeviceError:
                    if not allow_raw_on_mounted:
                        raise
                    # 显式放行：仅用于离线镜像/可放弃数据的场景
        except Exception:
            Win32Native.close(handle)
            raise

        self._handle = handle
        super().__init__(
            name=device_path,
            geometry=Geometry(block_size=block_size, block_count=block_count),
            writable=writable,
        )

    # -- 缓冲管理 ---------------------------------------------------------

    def _ensure_buffer(self, size: int) -> AlignedBuffer:
        buf = self._buffer
        if buf is None or buf.size < size:
            if buf is not None:
                buf.free()
            buf = AlignedBuffer(size)
            self._buffer = buf
        return buf

    # -- 读写 -------------------------------------------------------------

    def _read(self, lba: int, count: int, want: int) -> bytes:
        with self._lock:
            if self._aligned:
                buf = self._ensure_buffer(want)
                self._read_into(lba, want, buf.address)
                return buf.to_bytes(want)
            tmp = ctypes.create_string_buffer(want)
            self._read_into(lba, want, ctypes.cast(tmp, ctypes.c_void_p).value)
            return bytes(tmp.raw)

    def _read_into(self, lba: int, want: int, address: int) -> None:
        """循环读到满。``ReadFile`` 在设备层会返回短读，必须续读。"""
        k32 = _kernel32()
        offset = lba * self.geometry.block_size
        got = 0
        pos = ctypes.c_longlong(0)
        if not k32.SetFilePointerEx(wintypes.HANDLE(self._handle), offset, ctypes.byref(pos), FILE_BEGIN):
            raise _win_error("SetFilePointerEx（读）", lba=lba)
        while got < want:
            n = wintypes.DWORD(0)
            ok = k32.ReadFile(
                wintypes.HANDLE(self._handle),
                ctypes.c_void_p(address + got),
                want - got,
                ctypes.byref(n),
                None,
            )
            if not ok:
                raise _win_error("ReadFile", lba=lba, count=want // self.geometry.block_size)
            if n.value == 0:
                # 真 EOF：绝不能返回短数据，必须显式失败
                raise DeviceError(
                    f"读取遇到 EOF（已读 {got}/{want} 字节）",
                    error_class=ErrorClass.FATAL,
                    lba=lba,
                )
            got += n.value

    def _write(self, lba: int, count: int, data: bytes) -> None:
        with self._lock:
            k32 = _kernel32()
            offset = lba * self.geometry.block_size
            pos = ctypes.c_longlong(0)
            if not k32.SetFilePointerEx(
                wintypes.HANDLE(self._handle), offset, ctypes.byref(pos), FILE_BEGIN
            ):
                raise _win_error("SetFilePointerEx（写）", lba=lba, count=count)
            if self._aligned:
                buf = self._ensure_buffer(len(data))
                buf.load(data)
                address = buf.address
            else:
                tmp = ctypes.create_string_buffer(data, len(data))
                address = ctypes.cast(tmp, ctypes.c_void_p).value
            done = 0
            total = len(data)
            while done < total:
                n = wintypes.DWORD(0)
                ok = k32.WriteFile(
                    wintypes.HANDLE(self._handle),
                    ctypes.c_void_p(address + done),
                    total - done,
                    ctypes.byref(n),
                    None,
                )
                if not ok:
                    raise _win_error("WriteFile", lba=lba, count=count)
                if n.value == 0:
                    raise DeviceError(
                        "WriteFile 写入 0 字节（设备可能已满或只读）",
                        error_class=ErrorClass.FATAL,
                        lba=lba,
                        count=count,
                    )
                done += n.value

    def _flush(self) -> None:
        if self._handle and not _kernel32().FlushFileBuffers(wintypes.HANDLE(self._handle)):
            raise _win_error("FlushFileBuffers")

    def _close(self) -> None:
        if self._buffer is not None:
            self._buffer.free()
            self._buffer = None
        if getattr(self, "_handle", None):
            if self._locked:
                Win32Native.unlock_volume(self._handle)
            Win32Native.close(self._handle)
            self._handle = 0

    # -- 附加能力 ---------------------------------------------------------

    def bytes_range(self, ext) -> tuple[int, int]:
        return ext.start * self.geometry.block_size, ext.length * self.geometry.block_size


class Win32ClusterMover:
    """``FSCTL_MOVE_FILE`` 的最小封装：开卷、开文件、查映射、搬簇。

    注意两处与 :class:`Win32VolumeDevice` 相反的地方：

    * 卷句柄**不锁**（锁了就打不开卷上的文件，MOVE_FILE 也就无从谈起）；
    * 不用无缓冲 IO（搬簇是文件系统元数据操作，跟裸扇区读写不是一回事）。
    """

    def __init__(self, device_path: str, *, writable: bool = True) -> None:
        if not IS_WINDOWS:
            raise DeviceUnavailable("Win32ClusterMover 只能在 Windows 上使用")
        self._device_path = device_path
        self._handle = Win32Native.open_volume(device_path, writable=writable, unbuffered=False)
        self._writable = writable

    def open_file(self, path: str, writable: bool = True) -> int:
        return Win32Native.open_file(path, writable=writable and self._writable)

    def query(self, handle: int) -> tuple[tuple[int, int, int], ...]:
        return Win32Native.query_retrieval_pointers(handle)

    def move(self, handle: int, start_vcn: int, start_lcn: int, count: int) -> None:
        Win32Native.move_file_data(self._handle, handle, start_vcn, start_lcn, count)

    def close_file(self, handle: int) -> None:
        Win32Native.close(handle)

    def close(self) -> None:
        if getattr(self, "_handle", 0):
            Win32Native.close(self._handle)
            self._handle = 0

    def __enter__(self) -> "Win32ClusterMover":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# --------------------------------------------------------------------------
# 能力探测（给 UI 展示，不做静默降级）
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Capabilities:
    raw_read: bool
    raw_write: bool
    lock_volume: bool
    move_file: bool
    is_admin: bool
    reason: str = ""


def capabilities(device_path: str = r"\\.\C:") -> Capabilities:
    """探测真机后端能力。全部失败也**不抛异常**，只如实上报。

    UI 拿这个结果显示在目标选择页底部：用户有权知道"为什么点了没反应"。
    """
    if not IS_WINDOWS:
        return Capabilities(False, False, False, False, False, "非 Windows 平台")

    from ..platform.privilege import is_admin

    admin = is_admin()
    raw_read = raw_write = lock_ok = move_ok = False
    reason = ""
    try:
        h = Win32Native.open_volume(device_path, writable=False, unbuffered=True)
        try:
            Win32Native.device_length(h)
            raw_read = True
            try:
                Win32Native.ioctl(h, FSCTL_LOCK_VOLUME, None, 0)
                Win32Native.ioctl(h, FSCTL_UNLOCK_VOLUME, None, 0)
                lock_ok = True
            except DeviceError as exc:
                reason = exc.message
        finally:
            Win32Native.close(h)
    except (DeviceUnavailable, DeviceError) as exc:
        reason = str(exc)

    if admin and raw_read:
        try:
            h = Win32Native.open_volume(device_path, writable=True, unbuffered=True)
            Win32Native.close(h)
            raw_write = True
            move_ok = True  # FSCTL_MOVE_FILE 与写句柄同权限
        except (DeviceUnavailable, DeviceError) as exc:
            reason = str(exc)

    return Capabilities(
        raw_read=raw_read,
        raw_write=raw_write,
        lock_volume=lock_ok,
        move_file=move_ok,
        is_admin=admin,
        reason=reason,
    )
