"""错误分类表：把平台错误码翻译成「重试还是放弃」。

这是**唯一**允许决定重试语义的地方。任何在别处写 ``if winerror == 32``
的代码都是重复实现，早晚与这张表不一致。
"""

from __future__ import annotations

from ..contract.enums import ErrorClass

__all__ = [
    "RETRYABLE_WINERRORS",
    "FATAL_WINERRORS",
    "DeviceUnavailable",
    "classify_winerror",
    "classify_oserror",
]

#: 瞬时错误：设备忙、共享冲突、超时、介质抖动。重试通常能过。
RETRYABLE_WINERRORS: frozenset[int] = frozenset(
    {
        21,    # ERROR_NOT_READY            设备未就绪（U 盘拔出瞬间）
        23,    # ERROR_CRC                  校验错，坏块多为随机
        32,    # ERROR_SHARING_VIOLATION    别的进程占着
        33,    # ERROR_LOCK_VIOLATION       区域被锁
        1117,  # ERROR_IO_DEVICE            设备 I/O 错误
        121,   # ERROR_SEM_TIMEOUT          信号量超时
        170,   # ERROR_BUSY
        1237,  # ERROR_RETRY
        1460,  # ERROR_TIMEOUT
        1450,  # ERROR_NO_SYSTEM_RESOURCES
        8,     # ERROR_NOT_ENOUGH_MEMORY
        234,   # ERROR_MORE_DATA            IOCTL 缓冲不足
        1201,  # ERROR_UNRECOVERABLE_READ   硬件层尝试恢复失败
    }
)

#: 不可恢复：参数错、越界、只读、权限、空间不足。重试只会浪费时间。
FATAL_WINERRORS: frozenset[int] = frozenset(
    {
        5,     # ERROR_ACCESS_DENIED        非管理员
        87,    # ERROR_INVALID_PARAMETER    参数非法（几乎总是对齐/越界）
        19,    # ERROR_WRITE_PROTECT        写保护
        38,    # ERROR_HANDLE_EOF           读到文件尾
        27,    # ERROR_SECTOR_NOT_FOUND     LBA 不存在
        112,   # ERROR_DISK_FULL
        1167,  # ERROR_DEVICE_NOT_CONNECTED
        1314,  # ERROR_PRIVILEGE_NOT_HELD
    }
)


class DeviceUnavailable(RuntimeError):
    """设备打不开（不存在、被独占、权限不足）。属于**环境**问题，不是代码 bug。"""

    def __init__(self, message: str, winerror: int | None = None) -> None:
        super().__init__(message)
        self.winerror = winerror


def classify_winerror(code: int | None) -> ErrorClass:
    """按错误码返回重试语义。未知码一律 FATAL。

    为什么未知码不重试：一个没见过的错误码意味着我们不知道重试会不会
    造成重复写入。在块设备上"多做一次"的代价可能是数据损坏，而"少做一次"
    的代价只是任务失败——后者可恢复得多。
    """
    if code is None:
        return ErrorClass.FATAL
    if code in RETRYABLE_WINERRORS:
        return ErrorClass.RETRYABLE
    return ErrorClass.FATAL


def classify_oserror(exc: OSError) -> ErrorClass:
    """POSIX 侧分类（Linux CI / 镜像文件路径）。"""
    import errno

    retryable = {
        errno.EAGAIN,
        errno.EBUSY,
        errno.EINTR,
        errno.ETIMEDOUT,
    }
    if exc.errno in retryable:
        return ErrorClass.RETRYABLE
    return ErrorClass.FATAL
