"""半锁定（簇区间共享锁）：范围计算、与执行流程的衔接、异常释放。

锁这件事最容易"看起来接上了、其实没接"：调用了 lock 却没在 finally 里
unlock，或者锁着空 range 还自认为安全。所以这里的断言分两类 ——
**算出来的范围对不对**，以及**拿到的锁有没有在被释放**（含异常路径）。
"""

from __future__ import annotations

import pytest

from lbamover.contract.models import Extent, JobParams, MoveOp
from lbamover.devices.base import DeviceError
from lbamover.devices.memory import MemoryDevice
from lbamover.engine.control import RunControl
from lbamover.engine.executor import NtfsMoveExecutor, lock_ranges

CLUSTER = 4096


def op_for(path: str, extents, dst: int = 4096, op_id: int = 1) -> MoveOp:
    return MoveOp(
        op_id=op_id,
        file_id=path,
        path=path,
        src_extents=tuple(extents),
        dst_start=dst,
        verify=False,
    )


def params(**kw) -> JobParams:
    kw.setdefault("chunk_blocks", 8)
    kw.setdefault("max_retries", 0)
    kw.setdefault("verify", False)
    return JobParams(**kw)


class RecordingMover:
    """按「每簇一个 LCN」建模，并记录每一次加锁/解锁。"""

    def __init__(self, mapping: list[int], *, fail_lock: bool = False, with_lock: bool = True) -> None:
        self.mapping = list(mapping)
        self.calls: list[tuple] = []
        self.fail_lock = fail_lock
        if not with_lock:
            # 模拟"不提供锁能力的后端"：不能有这个属性，执行器要能跳过
            self.lock_range = None  # type: ignore[assignment]
            self.unlock_range = None  # type: ignore[assignment]

    def open_file(self, path: str, writable: bool = True):
        self.calls.append(("open", path))
        return path

    def query(self, handle):
        self.calls.append(("query",))
        runs: list[tuple[int, int, int]] = []
        vcn = 0
        i = 0
        mapping = self.mapping
        while i < len(mapping):
            start = mapping[i]
            j = i
            while j + 1 < len(mapping) and mapping[j + 1] == mapping[j] + 1:
                j += 1
            runs.append((vcn, start, j - i + 1))
            vcn += j - i + 1
            i = j + 1
        return tuple(runs)

    def move(self, handle, start_vcn: int, lcn: int, count: int) -> None:
        self.calls.append(("move", start_vcn, lcn, count))
        for k in range(count):
            self.mapping[start_vcn + k] = lcn + k

    def lock_range(self, handle, offset: int, length: int) -> None:
        if self.fail_lock:
            raise DeviceError("另一个进程持有的锁挡住了")
        self.calls.append(("lock", offset, length))

    def unlock_range(self, handle, offset: int, length: int) -> None:
        self.calls.append(("unlock", offset, length))

    def close_file(self, handle) -> None:
        self.calls.append(("close",))


class NoLockMover:
    """只有四个必需动作的后端 —— 锁是可选能力，不能因此报错。"""

    def __init__(self, mapping: list[int]) -> None:
        self.mapping = list(mapping)
        self.moves = 0

    def open_file(self, path: str, writable: bool = True):
        return path

    def query(self, handle):
        return ((0, self.mapping[0], len(self.mapping)),)

    def move(self, handle, start_vcn: int, lcn: int, count: int) -> None:
        self.moves += 1
        for k in range(count):
            self.mapping[start_vcn + k] = lcn + k

    def close_file(self, handle) -> None:
        return None


class TestLockRanges:
    """「部分簇范围」到底指哪几段 —— 这条错了，锁就等于白加。"""

    def test_single_extent_covers_whole_range(self):
        op = op_for("f", [Extent(10, 4)])
        assert lock_ranges(op, CLUSTER) == [(0, 4 * CLUSTER)]

    def test_multi_extent_is_offset_by_vcn(self):
        """第二段的偏移必须是**前面所有段长度之和**，不是它的 LCN。"""
        op = op_for("f", [Extent(10, 2), Extent(50, 3)])
        assert lock_ranges(op, CLUSTER) == [(0, 2 * CLUSTER), (2 * CLUSTER, 3 * CLUSTER)]

    def test_gap_between_extents_is_not_locked(self):
        """段与段之间的空洞属于别的文件，锁它等于替别人上锁。"""
        op = op_for("f", [Extent(10, 2), Extent(90, 2)])
        ranges = lock_ranges(op, CLUSTER)
        assert len(ranges) == 2, "空洞不该被并进范围"
        # 两段之间隔着 0 长度的重叠：第二段起点 = 第一段终点
        assert ranges[1][0] == ranges[0][0] + ranges[0][1]

    def test_empty_extents_give_no_ranges(self):
        """MoveOp 本身禁止空 src_extents，这里只验函数对退化的输入不炸。"""
        from types import SimpleNamespace

        assert lock_ranges(SimpleNamespace(src_extents=()), CLUSTER) == []

    def test_offset_beyond_4gib(self):
        """超过 2^32 的偏移不能回绕 —— 32 位截断会把锁加到文件头上。"""
        op = op_for("f", [Extent(0, 1), Extent(0, 1)])
        ranges = lock_ranges(op, 4 * 1024 * 1024)
        assert ranges[1][0] == 4 * 1024 * 1024
        op2 = op_for("f", [Extent(0, 1)])
        big = lock_ranges(op2, 5 * 1024 * 1024 * 1024)
        assert big[0][1] == 5 * 1024 * 1024 * 1024


class TestExecutorLocking:
    def test_locks_before_query_and_unlocks_before_close(self):
        """顺序是有意义的：先锁再 query，窗口才关得上。"""
        mover = RecordingMover([10, 50, 51])
        ex = NtfsMoveExecutor(mover, params(partial_lock=True), RunControl(), cluster_size=CLUSTER)
        res = ex.run_op(op_for("f", [Extent(10, 1), Extent(50, 2)], dst=200))

        assert res.ok
        order = [c[0] for c in mover.calls]
        assert order.index("lock") < order.index("query"), "锁必须在读布局之前"
        assert order.index("unlock") < order.index("close"), "解锁必须在关句柄之前"

    def test_lock_covers_every_extent(self):
        mover = RecordingMover([10, 50, 51])
        ex = NtfsMoveExecutor(mover, params(partial_lock=True), RunControl(), cluster_size=CLUSTER)
        ex.run_op(op_for("f", [Extent(10, 1), Extent(50, 2)], dst=200))

        locked = [c for c in mover.calls if c[0] == "lock"]
        unlocked = [c for c in mover.calls if c[0] == "unlock"]
        assert len(locked) == 2 and locked == [("lock", 0, CLUSTER), ("lock", CLUSTER, 2 * CLUSTER)]
        assert [("unlock", o, ln) for _, o, ln in locked] == unlocked, "锁与解锁必须逐段成对"

    def test_switch_off_means_no_lock_calls(self):
        mover = RecordingMover([10, 50, 51])
        ex = NtfsMoveExecutor(mover, params(partial_lock=False), RunControl(), cluster_size=CLUSTER)
        res = ex.run_op(op_for("f", [Extent(10, 1), Extent(50, 2)], dst=200))

        assert res.ok
        assert not [c for c in mover.calls if c[0] in ("lock", "unlock")]

    def test_lock_failure_does_not_fail_the_file(self):
        """锁拿不到是"这次没保护"，不是"这个文件不能搬"。"""
        mover = RecordingMover([10, 11], fail_lock=True)
        ex = NtfsMoveExecutor(mover, params(partial_lock=True), RunControl(), cluster_size=CLUSTER)
        res = ex.run_op(op_for("f", [Extent(10, 2)], dst=300))

        assert res.ok, "锁失败不该让文件失败 —— 布局复核仍然会把关"
        assert [c for c in mover.calls if c[0] == "move"], "该搬的还是要搬"
        assert ex.lock_failures == 1
        assert ex.last_lock_error

    def test_backend_without_lock_capability_is_skipped(self):
        mover = NoLockMover([10, 11])
        ex = NtfsMoveExecutor(mover, params(partial_lock=True), RunControl(), cluster_size=CLUSTER)
        res = ex.run_op(op_for("f", [Extent(10, 2)], dst=300))

        assert res.ok
        assert mover.moves >= 1
        assert ex.lock_failures == 0, "后端没有锁能力 ≠ 锁失败"

    def test_lock_released_when_move_raises(self):
        """搬簇中途抛错时锁也必须释放，否则整卷整理会留下孤锁。"""

        class ExplodingMover(RecordingMover):
            def move(self, handle, start_vcn, lcn, count):
                raise DeviceError("注入的搬簇失败")

        mover = ExplodingMover([10, 11])
        ex = NtfsMoveExecutor(mover, params(partial_lock=True), RunControl(), cluster_size=CLUSTER)
        res = ex.run_op(op_for("f", [Extent(10, 2)], dst=300))

        assert not res.ok
        order = [c[0] for c in mover.calls]
        assert "lock" in order and "unlock" in order
        assert order.index("unlock") > order.index("lock")


class TestReadSideLock:
    """读取路径的同一把锁：读一段之前锁住，读完立刻放。"""

    def test_memory_device_is_not_lockable(self):
        dev = MemoryDevice(64, CLUSTER)
        assert dev.lock_range(0, CLUSTER) is False, "没有外部写入者的设备不该假装能锁"
        dev.unlock_range(0, CLUSTER)  # 不该抛

    def test_pipeline_locks_each_read_in_pairs(self):
        from lbamover.contract.models import Geometry
        from lbamover.devices.base import BlockDeviceBase
        from lbamover.engine.pipeline import ChunkPipeline

        class LockRecordingDevice(BlockDeviceBase):
            def __init__(self) -> None:
                super().__init__("rec", Geometry(block_size=CLUSTER, block_count=64), writable=True)
                self.events: list[tuple] = []

            def lock_range(self, offset_bytes: int, length_bytes: int) -> bool:
                self.events.append(("lock", offset_bytes, length_bytes))
                return True

            def unlock_range(self, offset_bytes: int, length_bytes: int) -> None:
                self.events.append(("unlock", offset_bytes, length_bytes))

            def _read(self, lba: int, count: int, want: int) -> bytes:
                return b"\x00" * want

            def _write(self, lba: int, count: int, data: bytes) -> None:
                return None

            def _flush(self) -> None:
                return None

            def _close(self) -> None:
                return None

        src = LockRecordingDevice()
        dst = LockRecordingDevice()
        pipe = ChunkPipeline(
            src, dst, params(partial_lock=True, chunk_blocks=2), RunControl(), sleep=lambda _s: None
        )
        res = pipe.run_op(op_for("f", [Extent(10, 4)], dst=32))

        assert res.ok
        lock_events = [e for e in src.events if e[0] == "lock"]
        unlock_events = [e for e in src.events if e[0] == "unlock"]
        assert lock_events, "读取侧开关打开时必须真的加锁"
        assert len(lock_events) == len(unlock_events), "每次加锁都要有对应的解锁"
        assert [("unlock", e[1], e[2]) for e in lock_events] == unlock_events

    def test_pipeline_does_not_lock_when_switch_off(self):
        from lbamover.contract.models import Geometry
        from lbamover.devices.base import BlockDeviceBase
        from lbamover.engine.pipeline import ChunkPipeline

        class CountingDevice(BlockDeviceBase):
            def __init__(self) -> None:
                super().__init__("cnt", Geometry(block_size=CLUSTER, block_count=64), writable=True)
                self.lock_calls = 0

            def lock_range(self, offset_bytes: int, length_bytes: int) -> bool:
                self.lock_calls += 1
                return True

            def unlock_range(self, offset_bytes: int, length_bytes: int) -> None:
                return None

            def _read(self, lba: int, count: int, want: int) -> bytes:
                return b"\x00" * want

            def _write(self, lba: int, count: int, data: bytes) -> None:
                return None

            def _flush(self) -> None:
                return None

            def _close(self) -> None:
                return None

        src, dst = CountingDevice(), CountingDevice()
        pipe = ChunkPipeline(
            src, dst, params(partial_lock=False, chunk_blocks=2), RunControl(), sleep=lambda _s: None
        )
        assert pipe.run_op(op_for("f", [Extent(10, 4)], dst=32)).ok
        assert src.lock_calls == 0


@pytest.mark.skipif(__import__("os").name != "nt", reason="LockFileEx 只在 Windows 上存在")
class TestWin32LockArguments:
    def test_zero_length_is_rejected(self):
        """长度 0 在 LockFileEx 里是「锁到文件末尾」，不能放过去。"""
        from lbamover.devices.win32 import Win32Native

        with pytest.raises(DeviceError) as info:
            Win32Native.lock_file_range(1, 0, 0)
        assert "长度" in str(info.value)

    def test_negative_offset_is_rejected(self):
        from lbamover.devices.win32 import Win32Native

        with pytest.raises(DeviceError):
            Win32Native.lock_file_range(1, -1, CLUSTER)

    def test_real_file_roundtrip(self, tmp_path):
        """对一个真实临时文件加锁再解锁：证明 ctypes 声明与结构体大小是对的。"""
        from lbamover.devices.win32 import Win32Native

        target = tmp_path / "lock_target.bin"
        target.write_bytes(b"\x00" * (CLUSTER * 4))
        handle = Win32Native.open_file(str(target), writable=True)
        try:
            Win32Native.lock_file_range(handle, 0, CLUSTER)
            Win32Native.unlock_file_range(handle, 0, CLUSTER)
            Win32Native.lock_file_range(handle, CLUSTER, CLUSTER * 2)
            Win32Native.unlock_file_range(handle, CLUSTER, CLUSTER * 2)
        finally:
            Win32Native.close(handle)

    def test_shared_lock_blocks_exclusive_taker(self, tmp_path):
        """共享锁之间可以共存（两个读者），但挡住想独占这一段的人 —— 也就是写者。

        这正是「半锁定」要证明的那一条：我们锁住之后，别的进程**写**这段会
        失败，而**读**仍然可以，另一个只想读的人加共享锁也不该被我们挡住。
        """
        from lbamover.devices.win32 import Win32Native

        target = tmp_path / "lock_conflict.bin"
        target.write_bytes(b"\x00" * (CLUSTER * 2))
        first = Win32Native.open_file(str(target), writable=True)
        reader = Win32Native.open_file(str(target), writable=False)
        writer = Win32Native.open_file(str(target), writable=True)
        try:
            Win32Native.lock_file_range(first, 0, CLUSTER)
            # 另一个读者：共享锁可以共存
            Win32Native.lock_file_range(reader, 0, CLUSTER)
            Win32Native.unlock_file_range(reader, 0, CLUSTER)
            # 想独占的人被拒 —— 写者走的就是这条路
            with pytest.raises(DeviceError):
                Win32Native.lock_file_range(writer, 0, CLUSTER, exclusive=True)
        finally:
            Win32Native.unlock_file_range(first, 0, CLUSTER)
            Win32Native.close(first)
            Win32Native.close(reader)
            Win32Native.close(writer)
