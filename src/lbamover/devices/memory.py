"""内存块设备：用于测试、冒烟验证与「干跑」演示。

它刻意带**故障注入**能力。没有可注入故障的设备，重试策略、短读补齐、
断点续做这三块逻辑就只能靠"看代码觉得对"来验收，而它们恰好是最容易写错
的部分。
"""

from __future__ import annotations

import threading
import time
import zlib

from ..contract.enums import ErrorClass
from ..contract.models import Geometry
from .base import BlockDeviceBase, DeviceError

__all__ = ["MemoryDevice"]


class MemoryDevice(BlockDeviceBase):
    """以 ``bytearray`` 为背衬的块设备。

    故障注入字段（全部默认关闭，不影响正常路径）：

    ``fail_next_reads`` / ``fail_next_writes``
        接下来的 N 次读/写直接抛 ``DeviceError``。用于验证重试次数与
        退避节奏。
    ``fail_lba_set``
        命中这些 LBA 的读写失败。用于验证"局部坏块不影响其它 op"。
    ``short_read_blocks``
        读操作故意少返回这么多块。用于验证基类契约会把它变成异常，
        而不是让截断数据流到下游。
    ``corrupt_writes``
        写入后翻转首字节。用于验证回读校验能发现问题。
    ``latency_s``
        每次读写额外睡眠的秒数。用于验证并发/限速是否真的生效。
    """

    def __init__(
        self,
        block_count: int,
        block_size: int = 4096,
        writable: bool = True,
        name: str = "memory",
        data: bytes | None = None,
    ) -> None:
        geo = Geometry(block_size=block_size, block_count=block_count)
        super().__init__(name=name, geometry=geo, writable=writable)
        size = block_count * block_size
        if data is None:
            self._buf = bytearray(size)
        else:
            if len(data) != size:
                raise ValueError(f"初始数据长度 {len(data)} 与设备容量 {size} 不符")
            self._buf = bytearray(data)
        self._lock = threading.RLock()

        # -- 故障注入 --
        self.fail_next_reads = 0
        self.fail_next_writes = 0
        self.fail_lba_set: set[int] = set()
        self.short_read_blocks = 0
        self.corrupt_writes = False
        self.latency_s = 0.0
        self.read_only_from: int | None = None

        # -- 观测 --
        self.read_failures = 0
        self.write_failures = 0

    # -- 观测/构造辅助 -----------------------------------------------------

    def snapshot(self) -> bytes:
        with self._lock:
            return bytes(self._buf)

    def load(self, data: bytes) -> None:
        with self._lock:
            if len(data) != len(self._buf):
                raise ValueError("数据长度与容量不符")
            self._buf[:] = data

    def fill_pattern(self, seed: int = 0) -> None:
        """按块填充可校验的伪随机图案（每块前 4 字节写块号）。"""
        bs = self.block_size
        with self._lock:
            for i in range(self.geometry.block_count):
                chunk = zlib.crc32(f"{seed}:{i}".encode()).to_bytes(4, "little")
                self._buf[i * bs : i * bs + 4] = chunk
                # 其余字节用 seed 与块号派生，保证内容可区分
                fill = bytes([(seed + i) & 0xFF]) * (bs - 4)
                self._buf[i * bs + 4 : (i + 1) * bs] = fill

    def digest(self) -> str:
        with self._lock:
            return f"{zlib.crc32(self._buf):08x}"

    def nonzero_blocks(self) -> int:
        bs = self.block_size
        with self._lock:
            return sum(
                1
                for i in range(self.geometry.block_count)
                if any(self._buf[i * bs : (i + 1) * bs])
            )

    def reset_stats(self) -> None:
        self.read_calls = 0
        self.write_calls = 0
        self.bytes_read = 0
        self.bytes_written = 0
        self.read_failures = 0
        self.write_failures = 0

    # -- 读写实现 ---------------------------------------------------------

    def _maybe_delay(self) -> None:
        if self.latency_s > 0:
            time.sleep(self.latency_s)

    def _read(self, lba: int, count: int, want: int) -> bytes:
        self._maybe_delay()
        with self._lock:
            if self.fail_next_reads > 0:
                self.fail_next_reads -= 1
                self.read_failures += 1
                raise DeviceError(
                    "注入的读故障",
                    error_class=ErrorClass.RETRYABLE,
                    lba=lba,
                    count=count,
                )
            if self.fail_lba_set and any(
                lba + i in self.fail_lba_set for i in range(min(count, 64))
            ):
                self.read_failures += 1
                raise DeviceError(
                    "注入的 LBA 读故障",
                    error_class=ErrorClass.RETRYABLE,
                    lba=lba,
                    count=count,
                )
            if self.short_read_blocks:
                bs = self.block_size
                keep = max(0, count - self.short_read_blocks) * bs
                start = lba * bs
                return bytes(self._buf[start : start + keep])
            start = lba * self.block_size
            return bytes(self._buf[start : start + want])

    def _write(self, lba: int, count: int, data: bytes) -> None:
        self._maybe_delay()
        with self._lock:
            if self.fail_next_writes > 0:
                self.fail_next_writes -= 1
                self.write_failures += 1
                raise DeviceError(
                    "注入的写故障",
                    error_class=ErrorClass.RETRYABLE,
                    lba=lba,
                    count=count,
                )
            if self.fail_lba_set and any(
                lba + i in self.fail_lba_set for i in range(min(count, 64))
            ):
                self.write_failures += 1
                raise DeviceError(
                    "注入的 LBA 写故障",
                    error_class=ErrorClass.RETRYABLE,
                    lba=lba,
                    count=count,
                )
            if self.read_only_from is not None and lba >= self.read_only_from:
                self.write_failures += 1
                raise DeviceError(
                    "目标区处于模拟的写保护区",
                    error_class=ErrorClass.FATAL,
                    lba=lba,
                    count=count,
                )
            start = lba * self.block_size
            self._buf[start : start + len(data)] = data
            if self.corrupt_writes:
                self._buf[start] ^= 0xFF
