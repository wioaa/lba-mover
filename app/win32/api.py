"""Win32 API 绑定与通用辅助（唯一含 ``ctypes`` 细节的模块之一）。

设计要点：
* 所有函数都显式声明 ``argtypes`` / ``restype``，避免 64 位指针被截断。
* 出错的调用统一抛 :class:`Win32Error`（带 Win32 错误码 + 系统错误文本 + 上下文）。
* ``DeviceIoControl`` 封装允许返回部分数据，以支持 ``ERROR_MORE_DATA`` 语义。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Optional

# --------------------------------------------------------------------------- #
# DLL 句柄
# --------------------------------------------------------------------------- #
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
INVALID_HANDLE_VALUE: int = ctypes.c_void_p(-1).value or 0
NULL_HANDLE = 0

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x00000080
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
#: 顺序扫描提示：让内核加大预读窗口（预热与顺序读用）
FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000

# 文件属性位（与 core.models 中的同名常量保持一致）
FILE_ATTRIBUTE_READONLY = 0x00000001
FILE_ATTRIBUTE_HIDDEN = 0x00000002
FILE_ATTRIBUTE_SYSTEM = 0x00000004
FILE_ATTRIBUTE_DIRECTORY = 0x00000010
FILE_ATTRIBUTE_SPARSE_FILE = 0x00000200
FILE_ATTRIBUTE_COMPRESSED = 0x00000800
FILE_ATTRIBUTE_ENCRYPTED = 0x00004000
INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF

# 访问权限位（用于打开文件读取 retrieval pointers）
FILE_READ_ATTRIBUTES = 0x00000080
FILE_READ_DATA = 0x00000001

DRIVE_UNKNOWN = 0
DRIVE_NO_ROOT_DIR = 1
DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
DRIVE_REMOTE = 4
DRIVE_CDROM = 5
DRIVE_RAMDISK = 6

# 错误码
ERROR_SUCCESS = 0
ERROR_MORE_DATA = 234
ERROR_ACCESS_DENIED = 5
ERROR_NOT_ALL_ASSIGNED = 1300
ERROR_ALREADY_EXISTS = 183
ERROR_INVALID_PARAMETER = 87
ERROR_HANDLE_EOF = 38
ERROR_DISK_FULL = 112
ERROR_SHARING_VIOLATION = 32
ERROR_LOCK_VIOLATION = 33

# 文件系统控制码
FSCTL_GET_NTFS_VOLUME_DATA = 0x00090064
FSCTL_GET_VOLUME_BITMAP = 0x0009006F
FSCTL_GET_RETRIEVAL_POINTERS = 0x00090073
FSCTL_MOVE_FILE = 0x00090074

# 存储 / 卷映射相关 IOCTL（只读探测）
IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400
IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS = 0x00560000

# 令牌相关
TOKEN_QUERY = 0x0008
TOKEN_ADJUST_PRIVILEGES = 0x0020
SE_PRIVILEGE_ENABLED = 0x00000002
TokenElevation = 20


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #
class Win32Error(OSError):
    """一次失败的 Win32 调用。

    Attributes:
        winerror: Win32 错误码（``GetLastError``）。
        context: 出错的调用说明，便于日志定位。
    """

    def __init__(self, winerror: int, context: str = "") -> None:
        code = int(winerror or 0)
        self.context = context
        message = format_error(code)
        text = f"[{code}] {message}"
        if context:
            text = f"{context} 失败：{text}"
        super().__init__(code, text)
        # ⚠ OSError.__init__ 会重置 winerror/errno，必须在它之后再赋值
        self.winerror = code
        self.errno = code


def format_error(code: int) -> str:
    """把 Win32 错误码翻译成系统文本（失败时回退为 ``错误码 N``）。"""
    if code == ERROR_SUCCESS:
        return "操作成功"
    try:
        return ctypes.FormatError(code).strip() or f"错误码 {code}"
    except (ValueError, OSError):  # pragma: no cover - 极端情况下 FormatError 会抛错
        return f"错误码 {code}"


def raise_last_error(context: str) -> None:
    """读取线程级 last error 并抛出 :class:`Win32Error`。"""
    raise Win32Error(ctypes.get_last_error(), context)


def handle_is_valid(handle: Optional[int]) -> bool:
    """判断 ``CreateFile`` 返回的句柄是否有效。"""
    return bool(handle) and handle != INVALID_HANDLE_VALUE


# --------------------------------------------------------------------------- #
# 函数原型
# --------------------------------------------------------------------------- #
kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
]
kernel32.CreateFileW.restype = wintypes.HANDLE

kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

kernel32.DeviceIoControl.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    wintypes.LPVOID,
]
kernel32.DeviceIoControl.restype = wintypes.BOOL

kernel32.GetVolumeInformationW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.LPWSTR,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD),
    wintypes.LPWSTR,
    wintypes.DWORD,
]
kernel32.GetVolumeInformationW.restype = wintypes.BOOL

kernel32.GetDiskFreeSpaceExW.argtypes = [
    wintypes.LPCWSTR,
    ctypes.POINTER(ctypes.c_ulonglong),
    ctypes.POINTER(ctypes.c_ulonglong),
    ctypes.POINTER(ctypes.c_ulonglong),
]
kernel32.GetDiskFreeSpaceExW.restype = wintypes.BOOL

kernel32.GetLogicalDrives.argtypes = []
kernel32.GetLogicalDrives.restype = wintypes.DWORD

kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
kernel32.GetDriveTypeW.restype = wintypes.UINT

kernel32.GetCurrentProcess.argtypes = []
kernel32.GetCurrentProcess.restype = wintypes.HANDLE

kernel32.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
kernel32.GetFileAttributesW.restype = wintypes.DWORD

kernel32.SetFilePointerEx.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int64,
    ctypes.POINTER(ctypes.c_int64),
    wintypes.DWORD,
]
kernel32.SetFilePointerEx.restype = wintypes.BOOL

kernel32.ReadFile.argtypes = [
    wintypes.HANDLE,
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    wintypes.LPVOID,
]
kernel32.ReadFile.restype = wintypes.BOOL

kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE

kernel32.GetLastError.argtypes = []
kernel32.GetLastError.restype = wintypes.DWORD

advapi32.OpenProcessToken.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.HANDLE),
]
advapi32.OpenProcessToken.restype = wintypes.BOOL

advapi32.GetTokenInformation.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]
advapi32.GetTokenInformation.restype = wintypes.BOOL

advapi32.LookupPrivilegeValueW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.LPVOID,
]
advapi32.LookupPrivilegeValueW.restype = wintypes.BOOL

advapi32.AdjustTokenPrivileges.argtypes = [
    wintypes.HANDLE,
    wintypes.BOOL,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.LPVOID,
]
advapi32.AdjustTokenPrivileges.restype = wintypes.BOOL

shell32.IsUserAnAdmin.argtypes = []
shell32.IsUserAnAdmin.restype = wintypes.BOOL


# --------------------------------------------------------------------------- #
# 通用封装
# --------------------------------------------------------------------------- #
def open_handle(
    path: str,
    access: int,
    share: int = FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
    flags: int = FILE_ATTRIBUTE_NORMAL,
    context: str = "CreateFileW",
) -> int:
    """打开文件 / 卷句柄，失败时抛 :class:`Win32Error`。"""
    handle = kernel32.CreateFileW(path, access, share, None, OPEN_EXISTING, flags, None)
    if not handle_is_valid(handle):
        raise_last_error(f"{context}({path})")
    return int(handle)  # type: ignore[arg-type]


def close_handle(handle: Optional[int]) -> None:
    """安全关闭句柄（忽略无效值）。"""
    if handle and handle != INVALID_HANDLE_VALUE:
        kernel32.CloseHandle(handle)


def get_file_attributes(path: str) -> int:
    """``GetFileAttributesW``；失败返回 ``INVALID_FILE_ATTRIBUTES`` 而不抛异常。"""
    try:
        value = int(kernel32.GetFileAttributesW(path))
    except (OSError, ValueError):  # pragma: no cover - 极端情况
        return INVALID_FILE_ATTRIBUTES
    return value


FILE_BEGIN = 0


def read_at(handle: int, offset: int, length: int, context: str = "读取卷数据") -> bytes:
    """在**指定偏移**读取 ``length`` 字节（循环直到读满或遇到 EOF）。

    Raises:
        Win32Error: 定位或读取失败。
    """
    if length <= 0:
        return b""
    if not kernel32.SetFilePointerEx(handle, int(offset), None, FILE_BEGIN):
        raise_last_error(f"{context}（定位到 {offset:,}）")

    chunks: list = []
    remaining = int(length)
    while remaining > 0:
        block = min(remaining, 1 << 20)
        buffer = ctypes.create_string_buffer(block)
        read = wintypes.DWORD(0)
        if not kernel32.ReadFile(handle, buffer, block, ctypes.byref(read), None):
            raise_last_error(f"{context}（读取 {offset:,}+{length:,}）")
        if read.value == 0:
            break
        chunks.append(buffer.raw[: read.value])
        remaining -= read.value
    return b"".join(chunks)


def device_io_control_raw(
    handle: int,
    ioctl_code: int,
    in_bytes: bytes = b"",
    out_size: int = 0,
    context: str = "DeviceIoControl",
) -> tuple[bool, bytes, int, int]:
    """执行一次 ``DeviceIoControl``，**不抛异常**。

    某些 FSCTL（如 ``FSCTL_GET_VOLUME_BITMAP``）在输出缓冲区不足时
    会返回 ``ERROR_MORE_DATA`` 同时写入部分数据，调用方需要拿到这部分数据。

    Returns:
        ``(是否成功, 输出字节, 实际字节数, Win32 错误码)``。
    """
    in_buf = None
    in_len = 0
    if in_bytes:
        in_buf = ctypes.create_string_buffer(in_bytes, len(in_bytes))
        in_len = len(in_bytes)

    out_buf = ctypes.create_string_buffer(out_size) if out_size > 0 else None
    returned = wintypes.DWORD(0)

    ok = kernel32.DeviceIoControl(
        handle,
        ioctl_code,
        in_buf,
        in_len,
        out_buf,
        out_size,
        ctypes.byref(returned),
        None,
    )
    error = 0 if ok else ctypes.get_last_error()
    raw = b""
    if out_buf is not None and returned.value > 0:
        raw = out_buf.raw[: returned.value]
    _ = context
    return bool(ok), raw, int(returned.value), int(error)


def device_io_control(
    handle: int,
    ioctl_code: int,
    in_bytes: bytes = b"",
    out_size: int = 0,
    context: str = "DeviceIoControl",
) -> tuple[bytes, int]:
    """执行一次 ``DeviceIoControl``，失败时抛异常。

    Args:
        handle: 设备 / 文件句柄。
        ioctl_code: 控制码（FSCTL_*）。
        in_bytes: 输入缓冲区内容。
        out_size: 输出缓冲区字节数，可为 0。
        context: 出错时附加的上下文文案。

    Returns:
        ``(输出字节, 实际返回字节数)``。

    Raises:
        Win32Error: 调用失败（``ERROR_MORE_DATA`` 也会抛出，由调用方自行处理）。
    """
    ok, raw, returned_len, error = device_io_control_raw(
        handle, ioctl_code, in_bytes, out_size, context
    )
    if not ok:
        raise Win32Error(error, f"{context}(code=0x{ioctl_code:08X})")
    return raw, returned_len
