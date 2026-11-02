"""镜像文件设备：把普通文件当成一块磁盘来读写。

用途：在没有管理员权限的机器上做端到端验证、在 CI 里跑完整流水线、
以及在真机操作前先拿一份卷镜像演练。

实现要点：Windows 没有 ``os.pread``，所以用「加锁 + seek + 循环读到满」
的方式。循环读到满是必须的——文件对象在磁盘满、被截断等情况下会返回
短读，直接返回会给上游造成静默截断。
"""

from __future__ import annotations

import os
import threading

from ..contract.enums import ErrorClass
from ..contract.models import Geometry
from ..platform.errors import classify_oserror
from .base import BlockDeviceBase, DeviceError

__all__ = ["ImageDevice"]


class ImageDevice(BlockDeviceBase):
    """文件背衬的块设备。容量 = 文件大小向块大小向下取整。"""

    def __init__(
        self,
        path: str,
        block_size: int = 4096,
        writable: bool = False,
    ) -> None:
        if block_size <= 0:
            raise DeviceError(f"block_size 非法: {block_size}", error_class=ErrorClass.FATAL)
        self._path = os.path.abspath(path)
        mode = "r+b" if writable else "rb"
        try:
            # buffering=0 → 无缓冲，避免大块写时多一次内存拷贝
            self._fh = open(self._path, mode, buffering=0)
        except OSError as exc:
            raise DeviceError(
                f"打开镜像失败 {self._path}: {exc.strerror}",
                error_class=ErrorClass.FATAL,
                winerror=getattr(exc, "winerror", None),
            ) from exc

        size = os.fstat(self._fh.fileno()).st_size
        block_count = size // block_size
        if block_count <= 0:
            self._fh.close()
            raise DeviceError(
                f"镜像过小：{size} 字节不足一个块（{block_size}）",
                error_class=ErrorClass.FATAL,
            )
        geo = Geometry(block_size=block_size, block_count=block_count)
        super().__init__(name=self._path, geometry=geo, writable=writable)
        self._lock = threading.Lock()
        self._truncated_bytes = size - block_count * block_size

    @property
    def truncated_bytes(self) -> int:
        """文件末尾不足一个块而被忽略的字节数。"""
        return self._truncated_bytes

    @staticmethod
    def create(path: str, block_count: int, block_size: int = 4096) -> "ImageDevice":
        """创建（或清空）一个镜像文件并返回可写设备。"""
        size = block_count * block_size
        with open(path, "wb") as fh:
            fh.truncate(size)
        return ImageDevice(path, block_size=block_size, writable=True)

    # -- 半锁定（读取侧） --------------------------------------------------

    def lock_range(self, offset_bytes: int, length_bytes: int) -> bool:
        """给镜像文件的这段范围加共享锁（仅 Windows 有效）。

        ``msvcrt.get_osfhandle`` 把 Python 的文件描述符换成内核句柄，
        ``LockFileEx`` 要的是后者。

        非 Windows 直接返回 ``False``：POSIX 的 ``fcntl`` 锁是进程级
        建议锁，语义和 Windows 的字节范围锁不同，假装等价只会让人误信。
        """
        if os.name != "nt" or self._closed or length_bytes <= 0:
            return False
        try:
            import msvcrt

            from .win32 import Win32Native

            handle = msvcrt.get_osfhandle(self._fh.fileno())
            Win32Native.lock_file_range(handle, offset_bytes, length_bytes)
            return True
        except (OSError, DeviceError):
            # 锁不上不改变"能不能读"：降级为无保护读取，由调用方自己决定
            # 要不要把这件事告诉用户。
            return False

    def unlock_range(self, offset_bytes: int, length_bytes: int) -> None:
        if os.name != "nt" or self._closed or length_bytes <= 0:
            return
        try:
            import msvcrt

            from .win32 import Win32Native

            handle = msvcrt.get_osfhandle(self._fh.fileno())
            Win32Native.unlock_file_range(handle, offset_bytes, length_bytes)
        except (OSError, DeviceError):
            pass

    def _read(self, lba: int, count: int, want: int) -> bytes:
        offset = lba * self.block_size
        got = bytearray()
        with self._lock:
            try:
                self._fh.seek(offset)
            except OSError as exc:
                raise DeviceError(
                    f"seek 失败: {exc}",
                    error_class=classify_oserror(exc),
                    lba=lba,
                    count=count,
                ) from exc
            while len(got) < want:
                try:
                    piece = self._fh.read(want - len(got))
                except OSError as exc:
                    raise DeviceError(
                        f"读失败: {exc}",
                        error_class=classify_oserror(exc),
                        lba=lba,
                        count=count,
                    ) from exc
                if not piece:
                    raise DeviceError(
                        f"提前遇到 EOF：请求 {want} 字节，只读到 {len(got)}",
                        error_class=ErrorClass.FATAL,
                        lba=lba,
                        count=count,
                    )
                got += piece
        return bytes(got)

    def _write(self, lba: int, count: int, data: bytes) -> None:
        offset = lba * self.block_size
        with self._lock:
            try:
                self._fh.seek(offset)
                view = memoryview(data)
                written = 0
                while written < len(data):
                    n = self._fh.write(view[written:])
                    if not n:
                        raise DeviceError(
                            "写入返回 0 字节",
                            error_class=ErrorClass.FATAL,
                            lba=lba,
                            count=count,
                        )
                    written += n
            except DeviceError:
                # DeviceError 也是 OSError，必须原样抛出，不能被下面的分支二次包装
                raise
            except OSError as exc:
                raise DeviceError(
                    f"写失败: {exc}",
                    error_class=classify_oserror(exc),
                    lba=lba,
                    count=count,
                ) from exc

    def _flush(self) -> None:
        with self._lock:
            try:
                self._fh.flush()
                os.fsync(self._fh.fileno())
            except OSError as exc:
                raise DeviceError(f"flush 失败: {exc}", error_class=classify_oserror(exc)) from exc

    def _close(self) -> None:
        try:
            self._fh.close()
        except OSError:
            pass
