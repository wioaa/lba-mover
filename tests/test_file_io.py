"""用户态分块流式搬迁的 I/O 回归（``app/win32/file_io.py``）。

对**临时文件**做真实 CreateFileW/ReadFile/WriteFile 往返（零磁盘风险），验证：
* 读写原语（open/read_at/write_at/close）；
* 两种可调缓存模式经 :func:`copy_stream` 都能无损搬运：
  - ``memory``：内存分配缓存（块在 RAM 中转）；
  - ``disk``：设定缓存目录的硬盘缓存（块落暂存盘再读出）。
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.win32 import file_io  # noqa: E402

# 用小 block 让 10KB 数据跨多个块（block = 4 簇 × 512 B = 2048 B → 5 块）
_BPC = 512
_BLOCK_CLUSTERS = 4
_BLOCK_BYTES = _BPC * _BLOCK_CLUSTERS
_DATA = os.urandom(10_000)
_NBLOCKS = (len(_DATA) + _BLOCK_BYTES - 1) // _BLOCK_BYTES


def _write_src(directory: str) -> str:
    path = os.path.join(directory, "src.bin")
    with open(path, "wb") as fh:
        fh.write(_DATA)
    return path


def test_primitives_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "x.bin")
        handle = file_io.open_write(path)
        try:
            assert file_io.write_at(handle, 0, b"hello") == 5
            assert file_io.write_at(handle, 100, b"world") == 5
        finally:
            file_io.close(handle)
        handle = file_io.open_read(path)
        try:
            assert file_io.read_at(handle, 0, 5) == b"hello"
            assert file_io.read_at(handle, 100, 5) == b"world"
        finally:
            file_io.close(handle)


def test_memory_cache_mode_lossless() -> None:
    for buffer_blocks in (1, 2, 3):
        with tempfile.TemporaryDirectory() as d:
            src = _write_src(d)
            dst = os.path.join(d, "dst.bin")
            written, nblocks = file_io.copy_stream(
                src, dst, mode="memory", buffer_blocks=buffer_blocks,
                bytes_per_cluster=_BPC, block_clusters=_BLOCK_CLUSTERS,
            )
            with open(dst, "rb") as fh:
                assert fh.read() == _DATA
            assert written == len(_DATA)
            assert nblocks == _NBLOCKS


def test_disk_cache_mode_lossless() -> None:
    with tempfile.TemporaryDirectory() as work, tempfile.TemporaryDirectory() as cache:
        src = _write_src(work)
        dst = os.path.join(work, "dst.bin")
        written, nblocks = file_io.copy_stream(
            src, dst, mode="disk", cache_dir=cache, buffer_blocks=2,
            bytes_per_cluster=_BPC, block_clusters=_BLOCK_CLUSTERS,
        )
        with open(dst, "rb") as fh:
            assert fh.read() == _DATA
        assert written == len(_DATA)
        assert nblocks == _NBLOCKS


def test_copy_stream_disk_requires_cache_dir() -> None:
    with tempfile.TemporaryDirectory() as d:
        src = _write_src(d)
        try:
            file_io.copy_stream(src, os.path.join(d, "o.bin"), mode="disk", cache_dir=None)
        except ValueError:
            return
        raise AssertionError("disk 模式未给 cache_dir 应抛 ValueError")


if __name__ == "__main__":
    from tests._harness import exit_with

    exit_with(globals())
