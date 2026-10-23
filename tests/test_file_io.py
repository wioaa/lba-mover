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


def test_disk_cache_default_scratch_dir() -> None:
    """1.0.0D 方案 D：disk 模式 cache_dir=None → 默认暂存在源卷根，不再报错。"""
    with tempfile.TemporaryDirectory() as d:
        src = _write_src(d)
        dst = os.path.join(d, "dst.bin")
        # 把"卷根"指向测试临时目录，避免真的写 C:\ 根
        original_root = file_io._volume_root_of
        file_io._volume_root_of = lambda _path: d + os.sep
        try:
            written, nblocks = file_io.copy_stream(
                src, dst, mode="disk", cache_dir=None, buffer_blocks=2,
                bytes_per_cluster=_BPC, block_clusters=_BLOCK_CLUSTERS,
            )
        finally:
            file_io._volume_root_of = original_root
        with open(dst, "rb") as fh:
            assert fh.read() == _DATA
        assert written == len(_DATA)
        assert nblocks == _NBLOCKS
        # worker 语义：用完清理（copy_stream 只删暂存文件，目录由调用方删）
        scratch_dir = os.path.join(d, file_io.DEFAULT_SCRATCH_DIRNAME)
        assert os.path.isdir(scratch_dir)
        os.rmdir(scratch_dir)


def test_default_scratch_dir_falls_back_to_temp() -> None:
    """卷根不可写（候选路径落在一个**文件**上 → makedirs 抛 OSError）→ 回落 %TEMP%。"""
    with tempfile.TemporaryDirectory() as d:
        blocker = os.path.join(d, "not-a-dir")
        with open(blocker, "w", encoding="utf-8") as fh:
            fh.write("占位：候选目录的父路径是一个文件，makedirs 必失败")
        original_root = file_io._volume_root_of
        original_gettempdir = file_io.tempfile.gettempdir
        file_io._volume_root_of = lambda _path: blocker + os.sep
        file_io.tempfile.gettempdir = lambda: d
        try:
            result = file_io.default_scratch_dir("R:\\whatever.bin")
        finally:
            file_io._volume_root_of = original_root
            file_io.tempfile.gettempdir = original_gettempdir
        assert result == os.path.join(d, "lba-scratch")
        assert os.path.isdir(result)


def test_staged_copy_merges_scratch_writes() -> None:
    """暂存写合并：dst 与 memory 模式逐字节一致；暂存写次数显著少于块数。"""
    with tempfile.TemporaryDirectory() as work, tempfile.TemporaryDirectory() as cache:
        src = _write_src(work)
        dst = os.path.join(work, "dst.bin")
        # memory 基线
        memory_dst = os.path.join(work, "memory.bin")
        file_io.copy_stream(
            src, memory_dst, mode="memory", buffer_blocks=2,
            bytes_per_cluster=_BPC, block_clusters=_BLOCK_CLUSTERS,
        )
        with open(memory_dst, "rb") as fh:
            memory_bytes = fh.read()

        # 计数暂存盘写调用（打开 scratch 的句柄做标记）
        scratch_handles: set = set()
        original_open = file_io.open_readwrite
        original_write = file_io.write_at
        stats = {"calls": 0, "bytes": 0}

        def spy_open(path: str) -> int:
            handle = original_open(path)
            scratch_handles.add(handle)
            return handle

        def spy_write(handle: int, offset: int, data: bytes) -> int:
            if handle in scratch_handles:
                stats["calls"] += 1
                stats["bytes"] += len(data)
            return original_write(handle, offset, data)

        file_io.open_readwrite = spy_open
        file_io.write_at = spy_write
        try:
            written, nblocks = file_io.copy_stream(
                src, dst, mode="disk", cache_dir=cache, buffer_blocks=2,
                bytes_per_cluster=_BPC, block_clusters=_BLOCK_CLUSTERS,
            )
        finally:
            file_io.open_readwrite = original_open
            file_io.write_at = original_write

        # ① 功能不变：与 memory 模式逐字节一致
        with open(dst, "rb") as fh:
            assert fh.read() == memory_bytes == _DATA
        assert written == len(_DATA) and nblocks == _NBLOCKS
        # ② 写合并：暂存盘总字节不变，但写次数 < 块数（连续块拼成单次大写）
        assert stats["bytes"] == len(_DATA), "暂存盘写入总字节必须等于源大小"
        assert stats["calls"] < _NBLOCKS, (
            f"合并后暂存写 {stats['calls']} 次应少于块数 {_NBLOCKS}"
        )
        print(
            f"\n[perf] 暂存写合并：{_NBLOCKS} 块 → {stats['calls']} 次 WriteFile"
            f"（合并前 {_NBLOCKS} 次），总字节 {stats['bytes']:,} B"
        )


def test_staged_copy_merge_with_large_buffer() -> None:
    """buffer_blocks=1 / 3 等组合下合并写也不丢块（边界：单块组、跨组合并）。"""
    for buffer_blocks in (1, 2, 3, 5):
        with tempfile.TemporaryDirectory() as work, tempfile.TemporaryDirectory() as cache:
            src = _write_src(work)
            dst = os.path.join(work, "dst.bin")
            written, nblocks = file_io.copy_stream(
                src, dst, mode="disk", cache_dir=cache, buffer_blocks=buffer_blocks,
                bytes_per_cluster=_BPC, block_clusters=_BLOCK_CLUSTERS,
            )
            with open(dst, "rb") as fh:
                assert fh.read() == _DATA, f"buffer_blocks={buffer_blocks} 内容不一致"
            assert written == len(_DATA) and nblocks == _NBLOCKS


def test_block_stream_worker_default_scratch_dir() -> None:
    """BlockStreamWorker：disk + cache_dir=None → 自建默认暂存目录并用完清理。"""
    from PySide6.QtWidgets import QApplication

    from app.core.models import Extent, FileEntry
    from app.core.scanner import BlockStreamWorker

    _ = QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory() as d:
        data = os.urandom(5000)
        path = os.path.join(d, "a.bin")
        with open(path, "wb") as fh:
            fh.write(data)
        entries = [
            FileEntry(path=path, name="a.bin", size=len(data),
                      extents=[Extent(vcn=0, lcn=0, clusters=10)], movable=True),
        ]
        # 把"卷根"指向临时目录（避免真的在 C:\ 根建目录）
        original_root = file_io._volume_root_of
        file_io._volume_root_of = lambda _p: d + os.sep
        try:
            worker = BlockStreamWorker(
                entries, mode="disk", cache_dir=None,
                buffer_blocks=2, bytes_per_cluster=512, block_clusters=4,
            )
            worker.run()  # 同步执行
        finally:
            file_io._volume_root_of = original_root

        with open(path, "rb") as fh:
            assert fh.read() == data
        assert not os.path.exists(path + ".lbastream.tmp")
        # 默认暂存目录已被 worker 清理（空目录 rmdir）
        scratch_dir = os.path.join(d, file_io.DEFAULT_SCRATCH_DIRNAME)
        assert not os.path.isdir(scratch_dir), "worker 用完应清理自建的暂存目录"


def test_copy_stream_preallocate_overwrite() -> None:
    """预填充（SetEndOfFile 预分配连续簇）后再流式覆盖，内容与大小一致。"""
    with tempfile.TemporaryDirectory() as d:
        data = os.urandom(9000)
        src = os.path.join(d, "src.bin")
        with open(src, "wb") as fh:
            fh.write(data)
        dst = os.path.join(d, "dst.bin")
        written, nblocks = file_io.copy_stream(
            src, dst, mode="memory", buffer_blocks=2,
            bytes_per_cluster=512, block_clusters=4, preallocate_dst=True,
        )
        assert written == len(data)
        assert os.path.getsize(dst) == len(data)
        with open(dst, "rb") as fh:
            assert fh.read() == data


def test_block_stream_worker_end_to_end() -> None:
    """BlockStreamWorker 同卷内「前端→卷尾」：预填充 + 流式覆盖 + 源进回收站 + 改回原路径。"""
    from PySide6.QtWidgets import QApplication

    from app.core.models import Extent, FileEntry
    from app.core.scanner import BlockStreamWorker

    _ = QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as cache:
        d1, d2 = os.urandom(5000), os.urandom(3000)
        p1, p2 = os.path.join(d, "a.bin"), os.path.join(d, "b.bin")
        with open(p1, "wb") as fh:
            fh.write(d1)
        with open(p2, "wb") as fh:
            fh.write(d2)
        entries = [
            FileEntry(path=p1, name="a.bin", size=len(d1),
                      extents=[Extent(vcn=0, lcn=0, clusters=10)], movable=True),
            FileEntry(path=p2, name="b.bin", size=len(d2),
                      extents=[Extent(vcn=0, lcn=0, clusters=10)], movable=True),
        ]
        summary: dict = {}
        worker = BlockStreamWorker(
            entries, mode="disk", cache_dir=cache,
            buffer_blocks=2, bytes_per_cluster=512, block_clusters=4,
        )
        worker.completed.connect(lambda s: summary.update(s))
        worker.run()  # 同步执行（不 start，避免真实线程）

        # 同卷内重定位：改回原路径、内容一致
        with open(p1, "rb") as fh:
            assert fh.read() == d1
        with open(p2, "rb") as fh:
            assert fh.read() == d2
        # 无残留临时文件
        assert not os.path.exists(p1 + ".lbastream.tmp")
        assert not os.path.exists(p2 + ".lbastream.tmp")
        assert summary.get("moved") == 2 and summary.get("failed") == 0, summary


if __name__ == "__main__":
    from tests._harness import exit_with

    exit_with(globals())
