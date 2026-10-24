"""用户态分块流式搬迁的 I/O 回归（``app/win32/file_io.py``）。

对**临时文件**做真实 CreateFileW/ReadFile/WriteFile 往返（零磁盘风险），验证：
* 读写原语（open/read_at/write_at/close）；
* 两种可调缓存模式经 :func:`copy_stream` 都能无损搬运：
  - ``memory``：内存分配缓存（块在 RAM 中转）；
  - ``disk``：设定缓存目录的硬盘缓存（块落暂存盘再读出）。

缺陷回归钉（Debug 文档第六节）：
* D-003 短读截断 → ``test_read_at_*``；
* D-004 暂存名撞车 → ``test_scratch_name_*`` / ``test_copy_stream_disk_scratch_*``；
* D-008 攒批 join 峰值 → ``test_staged_copy_segmented_write_*``。
"""

from __future__ import annotations

import ctypes
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.win32 import api, file_io  # noqa: E402

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


def test_memory_copy_rejects_short_block_write() -> None:
    """D-011 memory 侧：实写字节数 != 块长度 → 抛错，且异常路径不泄漏句柄。"""
    with tempfile.TemporaryDirectory() as d:
        src = _write_src(d)
        dst = os.path.join(d, "dst.bin")
        original_write = file_io.write_at

        def short_write(handle: int, offset: int, data: bytes) -> int:
            """每次都少写 1 字节（模拟 write_at 契约被破坏 / 底层短写）。"""
            return len(data) - 1 if len(data) > 1 else len(data)

        file_io.write_at = short_write
        try:
            try:
                file_io.copy_stream(
                    src, dst, mode="memory", buffer_blocks=2,
                    bytes_per_cluster=_BPC, block_clusters=_BLOCK_CLUSTERS,
                )
            except api.Win32Error as exc:
                assert exc.winerror == api.ERROR_DISK_FULL, exc
                assert "短写" in str(exc) and "目的块" in str(exc), exc
            else:
                raise AssertionError("memory 模式块级短写必须报错，不能被 written 静默累加")
        finally:
            file_io.write_at = original_write

        # 异常路径资源不泄漏：句柄若在 finally 里没关掉，Windows 上这两个文件
        # 会因缺少 FILE_SHARE_DELETE 而删不掉（PermissionError）。
        os.remove(dst)
        os.remove(src)
        assert not os.path.exists(dst) and not os.path.exists(src)


def test_memory_copy_lossless_under_short_writes() -> None:
    """D-011 memory 侧：底层 WriteFile 持续短写时，write_at 重试后 dst 仍无损。"""
    with tempfile.TemporaryDirectory() as d:
        src = _write_src(d)
        dst = os.path.join(d, "dst.bin")
        original = file_io.kernel32
        fake = _ShortWriteKernel32(original, chunk=7)
        file_io.kernel32 = fake
        try:
            written, nblocks = file_io.copy_stream(
                src, dst, mode="memory", buffer_blocks=2,
                bytes_per_cluster=_BPC, block_clusters=_BLOCK_CLUSTERS,
            )
        finally:
            file_io.kernel32 = original
        with open(dst, "rb") as fh:
            assert fh.read() == _DATA, "底层持续短写下 memory 模式落盘必须无损"
        assert written == len(_DATA) and nblocks == _NBLOCKS
        assert fake.calls > _NBLOCKS, "应发生多次 WriteFile（确实在循环重试续写）"


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


def test_staged_copy_removes_stage_on_failure() -> None:
    """C-1：dst 写入中途抛异常 → stage 文件**仍被清理**，异常原样上抛。"""
    with tempfile.TemporaryDirectory() as work, tempfile.TemporaryDirectory() as cache:
        src = _write_src(work)
        dst = os.path.join(work, "dst.bin")
        scratch_path = os.path.join(cache, os.path.basename(dst) + ".stage")

        original_write = file_io.write_at
        state = {"calls": 0}

        def failing_write(handle: int, offset: int, data: bytes) -> int:
            state["calls"] += 1
            if state["calls"] >= 2:      # 第 2 次写（dst 的第一块）中途炸
                raise OSError("模拟 dst 写入失败")
            return original_write(handle, offset, data)

        file_io.write_at = failing_write
        try:
            try:
                file_io.staged_copy(
                    src, scratch_path, dst, buffer_blocks=2,
                    bytes_per_cluster=_BPC, block_clusters=_BLOCK_CLUSTERS,
                )
            except OSError as exc:
                assert "模拟 dst 写入失败" in str(exc)
            else:
                raise AssertionError("注入的写入异常应当上抛")
        finally:
            file_io.write_at = original_write

        assert not os.path.exists(scratch_path), (
            "异常路径 stage 文件也必须被清理（C-1：remove 在 finally 里）"
        )


def test_block_stream_worker_cleans_stage_residue() -> None:
    """C-1：worker 收尾先删目录内全部 `.stage` 残留，清空后才 rmdir；
    非 stage 文件不动（目录保留）。"""
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
        scratch_dir = os.path.join(d, file_io.DEFAULT_SCRATCH_DIRNAME)
        os.makedirs(scratch_dir, exist_ok=True)
        # 预置两类残留：本 worker 的 `.stage` 残留（模拟进程曾被杀）+ 别人的文件
        stale_stage = os.path.join(scratch_dir, "b.bin.stage")
        with open(stale_stage, "wb") as fh:
            fh.write(b"stale")
        foreign = os.path.join(scratch_dir, "keep.txt")
        with open(foreign, "w", encoding="utf-8") as fh:
            fh.write("not ours")

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

        # `.stage` 残留（含预置的）全部被清
        assert not os.path.exists(stale_stage), "目录内的 .stage 残留应被收尾清理"
        stages = [n for n in os.listdir(scratch_dir) if n.endswith(".stage")]
        assert stages == []
        # 非 stage 文件不动 → 目录未空 → rmdir 失败 → 目录保留
        assert os.path.exists(foreign), "非 .stage 文件不许动"
        assert os.path.isdir(scratch_dir), "目录非空时应保留（不 rmdir）"


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


class _ShortReadKernel32:
    """代理真实 ``kernel32``，只把 ``ReadFile`` 换成「每次最多返回 chunk 字节」。

    D-003 的模拟手段：本地 NTFS 上真实 ``ReadFile`` 几乎总是读满，无法复现短读，
    因此把 ``file_io.kernel32`` 临时换成这个代理（其余函数原样转发给真实 DLL）。
    """

    def __init__(self, real, content: bytes, chunk: int) -> None:
        self._real = real
        self._content = content
        self._chunk = max(1, int(chunk))
        self._pos = 0
        self.calls = 0

    def __getattr__(self, name: str):
        return getattr(self._real, name)

    def ReadFile(self, handle, buffer, size, done_ref, overlapped):  # noqa: N802
        self.calls += 1
        take = max(0, min(int(size), self._chunk, len(self._content) - self._pos))
        if take:
            ctypes.memmove(buffer, self._content[self._pos:self._pos + take], take)
            self._pos += take
        target = getattr(done_ref, "_obj", None)
        if target is None:  # pragma: no cover - 只防实现改用非 byref 传参
            raise AssertionError("ReadFile 的 done 参数必须是 ctypes.byref(...)")
        target.value = take
        return 1


class _ShortWriteKernel32:
    """代理真实 ``kernel32``，把 ``WriteFile`` 限制成短写 / 写不动两种病态行为。

    D-011 的模拟手段：本地磁盘上真实 ``WriteFile`` 几乎总是写满，无法复现短写，
    因此把 ``file_io.kernel32`` 临时换成这个代理（其余函数原样转发）。

    * ``chunk``：每次最多写这么多字节（短写，触发循环续写）；
    * ``stall_after``：累计写到这么多字节后，``WriteFile`` 返回 0 字节（写不动）。
    """

    def __init__(self, real, chunk: "int | None" = None, stall_after: "int | None" = None) -> None:
        self._real = real
        self._chunk = chunk
        self._stall_at = stall_after
        self._written = 0
        self.calls = 0

    def __getattr__(self, name: str):
        return getattr(self._real, name)

    def WriteFile(self, handle, buffer, size, done_ref, overlapped):  # noqa: N802
        self.calls += 1
        size = int(size)
        if self._chunk is not None:
            size = min(size, int(self._chunk))
        if self._stall_at is not None and self._written >= int(self._stall_at):
            done_ref._obj.value = 0          # 真写不进去了
            return 1
        if self._stall_at is not None:
            size = min(size, int(self._stall_at) - self._written)
        ok = self._real.WriteFile(handle, buffer, size, done_ref, overlapped)
        self._written += int(done_ref._obj.value)
        return ok


def test_read_at_retries_until_full_size_on_short_reads() -> None:
    """D-003 ①：ReadFile 每次只返回部分字节（短读）时，read_at 必须循环读满 size。"""
    content = os.urandom(5000)
    for chunk in (1, 7, 512, 4999):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "src.bin")
            with open(path, "wb") as fh:
                fh.write(content)
            handle = file_io.open_read(path)
            original = file_io.kernel32
            fake = _ShortReadKernel32(original, content, chunk)
            file_io.kernel32 = fake
            try:
                got = file_io.read_at(handle, 0, len(content))
            finally:
                file_io.kernel32 = original
                file_io.close(handle)
            assert len(got) == len(content), (
                f"chunk={chunk}：短读后未读满（{len(got)} < {len(content)}）"
            )
            assert got == content, f"chunk={chunk}：短读拼出来的内容与源不一致"
            assert fake.calls > 1, f"chunk={chunk}：应发生多次 ReadFile（确实短读）"
    print("\n[D-003] 短读补齐：chunk=1/7/512/4999 四种短读步长均读满且内容一致")


def test_read_at_raises_on_premature_eof() -> None:
    """D-003 ②：未读满 size 就遇到真 EOF → 显式抛 Win32Error，绝不静默返回短数据。"""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "short.bin")
        with open(path, "wb") as fh:
            fh.write(b"0123456789")          # 只有 10 字节
        handle = file_io.open_read(path)
        try:
            # 请求量 = 文件长度：正常返回（不误报）
            assert file_io.read_at(handle, 0, 10) == b"0123456789"

            # 请求 100 字节：读到 10 字节后 EOF → 必须报错
            try:
                file_io.read_at(handle, 0, 100)
            except api.Win32Error as exc:
                assert exc.winerror == api.ERROR_HANDLE_EOF, exc
                assert isinstance(exc, OSError)  # 沿用项目既有异常类型（OSError 子类）
            else:
                raise AssertionError("提前 EOF 必须抛错，不能静默返回截断数据")

            # 起点就在 EOF 之后：同样报错（第一次 ReadFile 即 0 字节）
            try:
                file_io.read_at(handle, 10_000, 8)
            except api.Win32Error as exc:
                assert exc.winerror == api.ERROR_HANDLE_EOF, exc
            else:
                raise AssertionError("EOF 之后读取必须抛错")
        finally:
            file_io.close(handle)


def test_write_at_retries_until_full_length_on_short_writes() -> None:
    """D-011 ①：WriteFile 每次只写部分字节（短写）时，write_at 必须循环写满。"""
    payload = os.urandom(5000)
    for chunk in (1, 7, 512, 4999):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "out.bin")
            handle = file_io.open_write(path)
            original = file_io.kernel32
            fake = _ShortWriteKernel32(original, chunk=chunk)
            file_io.kernel32 = fake
            try:
                written = file_io.write_at(handle, 0, payload)
            finally:
                file_io.kernel32 = original
                file_io.close(handle)
            assert written == len(payload), (
                f"chunk={chunk}：短写后未写满（{written} < {len(payload)}）"
            )
            with open(path, "rb") as fh:
                assert fh.read() == payload, f"chunk={chunk}：落盘内容与源不一致"
            assert fake.calls > 1, f"chunk={chunk}：应发生多次 WriteFile（确实短写）"
    print("\n[D-011] 短写补齐：chunk=1/7/512/4999 四种短写步长均写满且落盘一致")


def test_write_at_raises_when_write_stalls() -> None:
    """D-011 ②：WriteFile 返回 0 字节（写不进去）→ 抛错，绝不把已写字节当成功。"""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "out.bin")
        handle = file_io.open_write(path)
        original = file_io.kernel32
        try:
            # 空数据：早返回 0，不进循环、不调 WriteFile
            file_io.kernel32 = _ShortWriteKernel32(original, stall_after=0)
            assert file_io.write_at(handle, 0, b"") == 0

            # 一个字节都写不进去 → 必须报错
            try:
                file_io.write_at(handle, 0, b"x" * 4096)
            except api.Win32Error as exc:
                assert exc.winerror == api.ERROR_DISK_FULL, exc
                assert isinstance(exc, OSError)   # 沿用既有异常词汇（OSError 子类）
            else:
                raise AssertionError("WriteFile 返回 0 字节必须抛错")

            # 中途写不动（写了 10 字节后停滞）→ 同样报错，不能返回 10 当成功
            file_io.kernel32 = _ShortWriteKernel32(original, stall_after=10)
            try:
                file_io.write_at(handle, 0, b"y" * 4096)
            except api.Win32Error as exc:
                assert exc.winerror == api.ERROR_DISK_FULL, exc
            else:
                raise AssertionError("中途写不动必须抛错，不能静默返回已写字节数")
        finally:
            file_io.kernel32 = original
            file_io.close(handle)


def test_staged_copy_rejects_short_block_write() -> None:
    """D-011 位置二：目的块实写字节数 != 块长度 → 显式报错，不静默累加 written。"""
    with tempfile.TemporaryDirectory() as work, tempfile.TemporaryDirectory() as cache:
        src = _write_src(work)
        dst = os.path.join(work, "dst.bin")
        scratch_path = os.path.join(cache, "s.stage")
        original_open_rw = file_io.open_readwrite
        original_write = file_io.write_at
        scratch_handles: set = set()

        def spy_open_rw(path: str) -> int:
            handle = original_open_rw(path)
            scratch_handles.add(handle)
            return handle

        def short_dst_write(handle: int, offset: int, data: bytes) -> int:
            """目的盘每次都少写 1 字节（模拟 write_at 契约被破坏 / 底层短写）。

            暂存盘句柄照常真写——否则块根本没落暂存盘，先炸的是读侧而非写侧。
            """
            if handle in scratch_handles:
                return original_write(handle, offset, data)
            return len(data) - 1 if len(data) > 1 else len(data)

        file_io.open_readwrite = spy_open_rw
        file_io.write_at = short_dst_write
        try:
            try:
                file_io.staged_copy(
                    src, scratch_path, dst, buffer_blocks=2,
                    bytes_per_cluster=_BPC, block_clusters=_BLOCK_CLUSTERS,
                )
            except api.Win32Error as exc:
                assert "短写" in str(exc), exc
                assert exc.winerror == api.ERROR_DISK_FULL, exc
            else:
                raise AssertionError("块级短写必须报错，不能被 written 静默累加吞掉")
        finally:
            file_io.open_readwrite = original_open_rw
            file_io.write_at = original_write
        assert not os.path.exists(scratch_path), "异常路径下 stage 文件仍须被清理"


def test_scratch_name_unique_across_directories() -> None:
    """D-004：暂存文件名按目标**完整路径**哈希 —— 跨目录唯一、同目标可复现。"""
    a = file_io._scratch_name(os.path.join("D:" + os.sep, "dir-a", "same.bin"))
    b = file_io._scratch_name(os.path.join("D:" + os.sep, "dir-b", "same.bin"))
    assert a != b, "不同目录下的同名目标不得复用同一个暂存文件"
    assert a.endswith(".stage") and b.endswith(".stage"), (
        "必须以 .stage 结尾（worker 按该后缀清理残留）"
    )
    # 同一目标（相对路径 / 大小写差异）→ 同一名字（可复现，残留能被再次清理）
    assert file_io._scratch_name(os.path.join("D:" + os.sep, "dir-a", "SAME.bin")) == a
    assert os.path.basename(a) == a, "纯摘要名：不含用户文件名（避免超长 / 非法字符）"


def test_copy_stream_disk_scratch_unique_per_directory() -> None:
    """D-004：同一 cache_dir 下，不同目录的**同名目标**各用各的暂存盘，内容不串。"""
    seen: list = []
    original_open = file_io.open_readwrite

    def spy_open(path: str) -> int:
        seen.append(path)
        return original_open(path)

    with tempfile.TemporaryDirectory() as cache:
        file_io.open_readwrite = spy_open
        try:
            for payload in (os.urandom(7000), os.urandom(3000)):
                with tempfile.TemporaryDirectory() as work:
                    src = os.path.join(work, "src.bin")
                    with open(src, "wb") as fh:
                        fh.write(payload)
                    dst = os.path.join(work, "same.bin")   # 两个目录里**同名**的目标
                    written, _nblocks = file_io.copy_stream(
                        src, dst, mode="disk", cache_dir=cache, buffer_blocks=2,
                        bytes_per_cluster=_BPC, block_clusters=_BLOCK_CLUSTERS,
                    )
                    with open(dst, "rb") as fh:
                        assert fh.read() == payload, "同名目标内容被覆盖 / 错乱"
                    assert written == len(payload)
        finally:
            file_io.open_readwrite = original_open

        assert len(seen) == 2, seen
        assert seen[0] != seen[1], "两次搬运必须走不同的暂存文件"
        assert os.path.basename(seen[0]) != os.path.basename(seen[1])
        assert os.listdir(cache) == [], "暂存文件必须都被清理干净"


def _run_disk_copy(cap: "int | None" = None, join: bool = False) -> tuple:
    """跑一次 disk 模式拷贝，返回 ``(dst 字节, 暂存写统计, written, nblocks)``。

    ``join=True`` 时把 ``_write_run`` 换回**整段 join** 的旧实现作基线；
    否则按 ``cap`` 设 ``MAX_MERGE_BYTES``（``None`` = 用模块默认 8 MiB）。
    """
    original_open = file_io.open_readwrite
    original_write = file_io.write_at
    original_run = file_io._write_run
    original_cap = file_io.MAX_MERGE_BYTES
    scratch_handles: set = set()
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

    def join_run(scratch, blocks, pending, run, max_merge=None) -> None:
        """旧版整段 join 基线（写入仍经 spy 计数，口径一致）。"""
        file_io.write_at(scratch, blocks[run[0]].offset, b"".join(pending[i] for i in run))

    file_io.open_readwrite = spy_open
    file_io.write_at = spy_write
    if join:
        file_io._write_run = join_run
    elif cap is not None:
        file_io.MAX_MERGE_BYTES = cap

    with tempfile.TemporaryDirectory() as work, tempfile.TemporaryDirectory() as cache:
        src = _write_src(work)
        dst = os.path.join(work, "dst.bin")
        try:
            written, nblocks = file_io.copy_stream(
                src, dst, mode="disk", cache_dir=cache, buffer_blocks=2,
                bytes_per_cluster=_BPC, block_clusters=_BLOCK_CLUSTERS,
            )
        finally:
            file_io.open_readwrite = original_open
            file_io.write_at = original_write
            file_io._write_run = original_run
            file_io.MAX_MERGE_BYTES = original_cap
        with open(dst, "rb") as fh:
            data = fh.read()
    return data, stats, written, nblocks


def test_staged_copy_write_guard_keeps_join_equivalence_and_merge_counts() -> None:
    """D-011 ③：写侧校验上线后，分段写仍与整段 join 版逐字节一致、合并次数不变。"""
    baseline, base_stats, written0, nblocks0 = _run_disk_copy(join=True)
    assert baseline == _DATA and written0 == len(_DATA) and nblocks0 == _NBLOCKS
    assert base_stats["bytes"] == len(_DATA)
    assert base_stats["calls"] == 3, f"join 基线暂存写次数应为 3，实为 {base_stats['calls']}"

    for cap in (1 << 20, 3000, 1):
        data, stats, written, nblocks = _run_disk_copy(cap)
        assert data == baseline, f"cap={cap}：与整段 join 版落盘字节不一致"
        assert stats["bytes"] == len(_DATA), f"cap={cap}：暂存写入总字节变了"
        assert written == len(_DATA) and nblocks == _NBLOCKS

    # 默认上限（8 MiB）下：分段不触发 → 合并次数与 join 版**完全一致**
    default_data, default_stats, _, _ = _run_disk_copy()
    assert default_data == baseline
    assert default_stats["calls"] == base_stats["calls"], (
        f"默认上限下合并次数应与 join 版一致（{base_stats['calls']}），"
        f"实为 {default_stats['calls']}"
    )
    assert default_stats["calls"] < _NBLOCKS, "默认路径仍保留连续块合并收益"

    # 端到端：让底层 WriteFile **持续短写**（每次 7 字节）跑完整 disk 流程 ——
    # 落盘字节仍须与 join 基线一致，且 write_at 调用次数（合并次数）不变
    # （重试发生在 write_at **内部**，不得改变上层的合并策略）。
    original_kernel32 = file_io.kernel32
    file_io.kernel32 = _ShortWriteKernel32(original_kernel32, chunk=7)
    try:
        data, stats, written, nblocks = _run_disk_copy()
    finally:
        file_io.kernel32 = original_kernel32
    assert data == baseline, "底层持续短写时，落盘字节必须与 join 版一致"
    assert stats["calls"] == base_stats["calls"] == 3, (
        f"短写重试不得改变合并次数（应 {base_stats['calls']}），实为 {stats['calls']}"
    )
    assert stats["bytes"] == len(_DATA)
    assert written == len(_DATA) and nblocks == _NBLOCKS


def test_staged_copy_segmented_write_matches_joined_bytes() -> None:
    """D-008：限制单次合并长度后分段写入，落盘字节与「整段 join」版逐字节一致。"""
    baseline: bytes = b""
    stats_by_cap: dict = {}

    for cap in (1 << 20, 3000, 1):     # 大=原 join 行为；3000=跨块拆两段；1=逐块写
        with tempfile.TemporaryDirectory() as work, tempfile.TemporaryDirectory() as cache:
            src = _write_src(work)
            dst = os.path.join(work, "dst.bin")

            scratch_handles: set = set()
            original_open = file_io.open_readwrite
            original_write = file_io.write_at
            original_cap = file_io.MAX_MERGE_BYTES
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
            file_io.MAX_MERGE_BYTES = cap
            try:
                written, nblocks = file_io.copy_stream(
                    src, dst, mode="disk", cache_dir=cache, buffer_blocks=2,
                    bytes_per_cluster=_BPC, block_clusters=_BLOCK_CLUSTERS,
                )
            finally:
                file_io.open_readwrite = original_open
                file_io.write_at = original_write
                file_io.MAX_MERGE_BYTES = original_cap

            with open(dst, "rb") as fh:
                data = fh.read()
            assert data == _DATA, f"cap={cap}：落盘内容与源不一致"
            assert written == len(_DATA) and nblocks == _NBLOCKS
            if not baseline:
                baseline = data
            assert data == baseline, f"cap={cap}：与整段 join 版落盘字节不一致"
            assert stats["bytes"] == len(_DATA), f"cap={cap}：暂存写入总字节变了"
            stats_by_cap[cap] = stats["calls"]

    # 分段确实生效：上限越小 → WriteFile 次数越多（但字节与内容完全不变）
    assert stats_by_cap[1] >= stats_by_cap[3000] > stats_by_cap[1 << 20], stats_by_cap
    assert stats_by_cap[1 << 20] < _NBLOCKS, "默认上限下仍保留连续块合并收益"
    print(
        f"\n[D-008] 分段写：{_NBLOCKS} 块 → cap=1MiB {stats_by_cap[1 << 20]} 次 / "
        f"cap=3000 {stats_by_cap[3000]} 次 / cap=1 {stats_by_cap[1]} 次 WriteFile，"
        "落盘字节三者一致"
    )


def test_write_run_splits_long_run_and_preserves_bytes() -> None:
    """D-008：超长连续段被拆成多段写 —— 每段 ≤ 上限、首尾相接、拼回与 join 版一致。"""
    blocks = file_io.plan_blocks(len(_DATA), _BPC, _BLOCK_CLUSTERS)
    pending = {b.index: _DATA[b.offset:b.offset + b.length] for b in blocks}
    run = list(range(len(blocks)))

    writes: list = []
    original_write = file_io.write_at

    def record(handle: int, offset: int, data: bytes) -> int:
        writes.append((offset, data))
        return len(data)

    file_io.write_at = record
    try:
        file_io._write_run(-1, blocks, pending, run, max_merge=3000)
    finally:
        file_io.write_at = original_write

    assert len(writes) > 1, "上限小于整段长度时必须拆成多段写"
    assert all(len(data) <= 3000 for _offset, data in writes), (
        "单次写入长度必须 ≤ 合并上限（内存峰值不再随段长线性翻倍）"
    )
    cursor = blocks[0].offset
    for offset, data in writes:          # 段首尾相接：无空洞、无重叠
        assert offset == cursor, (offset, cursor)
        cursor += len(data)
    assert cursor == blocks[0].offset + len(_DATA)
    assert b"".join(data for _offset, data in writes) == _DATA, (
        "分段写入拼回来必须与整段 join 逐字节一致"
    )


if __name__ == "__main__":
    from tests._harness import exit_with

    exit_with(globals())
