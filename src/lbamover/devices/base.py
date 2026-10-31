"""设备抽象与工厂。

核心契约只有一条，但它决定了整个引擎的正确性：

    **``read_blocks`` 绝不返回短数据。**

短读返回的是「看起来成功、实际截断」的块，下游会把它当成完整数据写进
目标区，于是复制出来的盘表面正常、实际内容缺失。旧版项目为此付出过一次
完整的数据损坏排查（缺陷 D-003）。所以这里宁可抛异常，也不容忍短读。
"""

from __future__ import annotations

import abc
from dataclasses import dataclass

from ..contract.enums import ErrorClass
from ..contract.models import Geometry

__all__ = [
    "DeviceError",
    "DeviceSpec",
    "BlockDeviceBase",
    "check_range",
    "open_device",
]


class DeviceError(OSError):
    """块设备操作失败。

    继承 ``OSError`` 以便上层用统一的异常处理；``error_class`` 是重试策略
    的唯一输入，**不要**在调用点重新判断类型。
    """

    def __init__(
        self,
        message: str,
        *,
        error_class: ErrorClass = ErrorClass.FATAL,
        lba: int | None = None,
        count: int = 0,
        winerror: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_class = error_class
        self.lba = lba
        self.count = count
        self.winerror = winerror

    @property
    def retryable(self) -> bool:
        return self.error_class in (ErrorClass.RETRYABLE, ErrorClass.VERIFY_MISMATCH)

    def __str__(self) -> str:
        loc = "" if self.lba is None else f" @LBA {self.lba}+{self.count}"
        code = "" if self.winerror is None else f" [winerror={self.winerror}]"
        return f"{self.message}{loc}{code}"


def check_range(geometry: Geometry, lba: int, count: int) -> None:
    """越界检查。所有实现都必须先调它，再做真实 IO。"""
    if count < 0:
        raise DeviceError(f"块数不能为负: {count}", lba=lba, count=count)
    if lba < 0:
        raise DeviceError(f"LBA 不能为负: {lba}", lba=lba, count=count)
    if count == 0:
        return
    if lba + count > geometry.block_count:
        raise DeviceError(
            f"越界访问：请求 [{lba}, {lba + count}) 超出设备范围 {geometry.block_count} 块",
            lba=lba,
            count=count,
        )


class BlockDeviceBase(abc.ABC):
    """块设备实现的公共骨架。

    子类只需实现 ``_read`` / ``_write`` / ``_flush`` / ``_close``，越界检查、
    参数校验、统计由基类统一完成——避免每个实现各写一遍、各漏一处。
    """

    def __init__(self, name: str, geometry: Geometry, writable: bool) -> None:
        self._name = name
        self._geometry = geometry
        self._writable = writable
        self._closed = False
        self.read_calls = 0
        self.write_calls = 0
        self.bytes_read = 0
        self.bytes_written = 0

    # -- 属性 -------------------------------------------------------------

    @property
    def geometry(self) -> Geometry:
        return self._geometry

    @property
    def name(self) -> str:
        return self._name

    @property
    def writable(self) -> bool:
        return self._writable

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def block_size(self) -> int:
        return self._geometry.block_size

    # -- 公共入口 ---------------------------------------------------------

    def read_blocks(self, lba: int, count: int) -> bytes:
        if self._closed:
            raise DeviceError("设备已关闭", error_class=ErrorClass.FATAL, lba=lba, count=count)
        if count <= 0:
            return b""
        check_range(self._geometry, lba, count)
        want = count * self._geometry.block_size
        data = self._read(lba, count, want)
        if len(data) != want:
            # 这里是契约的执行点：不补零、不裁剪，直接失败。
            raise DeviceError(
                f"短读：请求 {want} 字节，实际 {len(data)} 字节（实现层必须补齐或抛错）",
                error_class=ErrorClass.RETRYABLE,
                lba=lba,
                count=count,
            )
        self.read_calls += 1
        self.bytes_read += want
        return data

    def write_blocks(self, lba: int, data: bytes) -> None:
        if self._closed:
            raise DeviceError("设备已关闭", error_class=ErrorClass.FATAL, lba=lba)
        if not self._writable:
            raise DeviceError("设备只读", error_class=ErrorClass.FATAL, lba=lba)
        bs = self._geometry.block_size
        if len(data) % bs != 0:
            raise DeviceError(
                f"写入长度 {len(data)} 不是块大小 {bs} 的整数倍",
                error_class=ErrorClass.FATAL,
                lba=lba,
            )
        count = len(data) // bs
        if count == 0:
            return
        check_range(self._geometry, lba, count)
        self._write(lba, count, data)
        self.write_calls += 1
        self.bytes_written += len(data)

    def flush(self) -> None:
        if not self._closed:
            self._flush()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._close()

    def __enter__(self) -> "BlockDeviceBase":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- 子类实现 ---------------------------------------------------------

    @abc.abstractmethod
    def _read(self, lba: int, count: int, want: int) -> bytes:
        """返回**恰好** ``want`` 字节，或抛 :class:`DeviceError`。"""

    @abc.abstractmethod
    def _write(self, lba: int, count: int, data: bytes) -> None: ...

    def _flush(self) -> None:
        return None

    def _close(self) -> None:
        return None

    def __repr__(self) -> str:  # pragma: no cover
        g = self._geometry
        return (
            f"{type(self).__name__}(name={self._name!r}, "
            f"blocks={g.block_count}, block_size={g.block_size}, writable={self._writable})"
        )


@dataclass(frozen=True, slots=True)
class DeviceSpec:
    """设备构造参数。UI 的「目标选择」翻译成它，工厂据此造设备。"""

    #: ``"memory"`` / ``"image"`` / ``"win32"``
    kind: str
    #: 镜像文件路径或卷设备路径（``\\.\D:``）
    path: str = ""
    block_size: int = 4096
    block_count: int = 0
    writable: bool = False
    #: 真机后端是否启用无缓冲 IO（要求对齐缓冲，吞吐更稳）。
    unbuffered: bool = True
    #: 是否在打开时锁定卷（写入必须锁，否则 FS 会缓存脏页导致写坏）。
    lock_volume: bool = False


def open_device(spec: DeviceSpec) -> BlockDeviceBase:
    """按 spec 造设备。真机后端按需懒导入，非 Windows 上不会炸。"""
    kind = spec.kind
    if kind == "memory":
        from .memory import MemoryDevice

        return MemoryDevice(
            block_count=spec.block_count,
            block_size=spec.block_size,
            writable=spec.writable,
            name=spec.path or "memory",
        )
    if kind == "image":
        from .image import ImageDevice

        return ImageDevice(
            path=spec.path,
            block_size=spec.block_size,
            writable=spec.writable,
        )
    if kind == "win32":
        from .win32 import Win32VolumeDevice

        return Win32VolumeDevice(
            device_path=spec.path,
            block_size=spec.block_size,
            writable=spec.writable,
            unbuffered=spec.unbuffered,
            lock_volume=spec.lock_volume,
        )
    raise DeviceError(f"未知的设备类型: {kind!r}", error_class=ErrorClass.FATAL)
