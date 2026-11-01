"""真机执行器：布局复核、分块搬簇、回读校验、取消。

用一个**会真正改变映射**的假后端来测，而不是断言"某个函数被调了几次"：
搬簇这件事的全部风险在于"搬完之后文件是不是还在原来的位置"，只有让假后端
真的改映射，才能验证执行器没有搬错。

这些用例不需要 Windows，也不需要管理员权限——真实后端
（``Win32ClusterMover``）与本文件被测的逻辑之间只有一个三方法的接口。
"""

from __future__ import annotations

import pytest

from lbamover.contract.models import Extent, JobParams, MoveOp
from lbamover.devices.base import DeviceError
from lbamover.engine.control import RunControl
from lbamover.engine.executor import NtfsMoveExecutor, layout_mismatch


class FakeMover:
    """用「每个 VCN 一个 LCN」的映射模拟一个卷。

    把簇展开成逐簇数组，``move`` 就是逐簇改写——虽然朴素，但语义与
    ``FSCTL_MOVE_FILE`` 一致：搬完那段就落在新位置，其余不动。
    """

    def __init__(self, files: dict[str, list[int]]) -> None:
        self.files = {k: list(v) for k, v in files.items()}
        self.moves: list[tuple[int, int, int]] = []
        self.open_count = 0
        self.closed = False
        self.fail_next_move = 0
        self.missing: set[str] = set()

    # -- ClusterMover 协议 -------------------------------------------------

    def open_file(self, path: str, writable: bool = True):
        if path in self.missing:
            raise DeviceError(f"文件不存在：{path}")
        self.open_count += 1
        return path

    def query(self, handle):
        if handle not in self.files:
            raise DeviceError(f"句柄无效：{handle}")
        return _compress(self.files[handle])

    def move(self, handle, start_vcn: int, lcn: int, count: int) -> None:
        if self.fail_next_move > 0:
            self.fail_next_move -= 1
            raise DeviceError("注入的搬簇失败")
        mapping = self.files[handle]
        for k in range(count):
            mapping[start_vcn + k] = lcn + k
        self.moves.append((start_vcn, lcn, count))

    def close_file(self, handle) -> None:
        return None

    def close(self) -> None:
        self.closed = True


def _compress(mapping: list[int]) -> tuple[tuple[int, int, int], ...]:
    """逐簇映射压缩成 (start_vcn, start_lcn, count) 的 run 序列。"""
    runs: list[tuple[int, int, int]] = []
    i = 0
    n = len(mapping)
    while i < n:
        j = i
        while j + 1 < n and mapping[j + 1] == mapping[j] + 1:
            j += 1
        runs.append((i, mapping[i], j - i + 1))
        i = j + 1
    return tuple(runs)


def op_for(path: str, extents: tuple[Extent, ...], dst: int, op_id: int = 0) -> MoveOp:
    return MoveOp(op_id, "16", path, extents, dst_start=dst)


def params(**kw) -> JobParams:
    base = dict(block_size=4096, chunk_blocks=2, max_retries=0, retry_base_ms=1)
    base.update(kw)
    return JobParams(**base)


class TestLayoutMismatch:
    def test_identical_layout_passes(self):
        live = ((0, 10, 2), (2, 50, 3))
        assert layout_mismatch(live, [(0, 10, 2), (2, 50, 3)]) is None

    def test_merged_runs_are_equivalent(self):
        """物理上连续的两段被文件系统合并成一段，是等价的，不该拒绝执行。"""
        live = ((0, 10, 5),)
        assert layout_mismatch(live, [(0, 10, 2), (2, 12, 3)]) is None

    def test_moved_clusters_detected(self):
        live = ((0, 10, 2), (2, 99, 3))
        assert "LCN" in (layout_mismatch(live, [(0, 10, 2), (2, 50, 3)]) or "")

    def test_shrunk_file_detected(self):
        live = ((0, 10, 2),)
        assert layout_mismatch(live, [(0, 10, 2), (2, 50, 3)]) is not None

    def test_sparse_detected(self):
        live = ((0, 10, 2), (2, -1, 3))
        assert "稀疏" in (layout_mismatch(live, [(0, 10, 2), (2, 50, 3)]) or "")

    def test_empty_detected(self):
        assert layout_mismatch((), [(0, 10, 2)]) is not None


class TestNtfsMoveExecutor:
    def test_moves_clusters_and_verifies(self):
        mover = FakeMover({"D:\\a.bin": [10, 11, 50, 51, 52]})
        ex = NtfsMoveExecutor(mover, params(), RunControl(), cluster_size=4096)
        op = op_for("D:\\a.bin", (Extent(10, 2), Extent(50, 3)), dst=100)

        res = ex.run_op(op)

        assert res.ok, res.error
        assert res.blocks_done == 5
        assert res.verified
        # 搬完之后应该是从 100 起的一段连续簇
        assert mover.files["D:\\a.bin"] == [100, 101, 102, 103, 104]
        assert sum(c for _, _, c in mover.moves) == 5

    def test_chunking_respects_params(self):
        mover = FakeMover({"D:\\b.bin": [10, 11, 12, 13, 14]})
        ex = NtfsMoveExecutor(mover, params(chunk_blocks=2), RunControl(), cluster_size=4096)
        op = op_for("D:\\b.bin", (Extent(10, 5),), dst=200)
        assert ex.run_op(op).ok
        assert [c for _, _, c in mover.moves] == [2, 2, 1]

    def test_refuses_when_layout_changed(self):
        """扫描之后文件被别人挪过：必须拒绝，而不是照着旧计划搬。"""
        mover = FakeMover({"D:\\c.bin": [10, 11, 70, 71, 72]})
        ex = NtfsMoveExecutor(mover, params(), RunControl(), cluster_size=4096)
        op = op_for("D:\\c.bin", (Extent(10, 2), Extent(50, 3)), dst=100)

        res = ex.run_op(op)

        assert not res.ok
        assert "布局复核" in res.error
        assert mover.moves == []

    def test_missing_file_is_reported_not_raised(self):
        mover = FakeMover({})
        mover.missing.add("D:\\gone.bin")
        ex = NtfsMoveExecutor(mover, params(), RunControl(), cluster_size=4096)
        res = ex.run_op(op_for("D:\\gone.bin", (Extent(10, 2),), dst=100))
        assert not res.ok and "不存在" in res.error

    def test_cancel_before_first_chunk(self):
        mover = FakeMover({"D:\\d.bin": [10, 11, 12, 13]})
        ctrl = RunControl()
        ctrl.request_cancel()
        ex = NtfsMoveExecutor(mover, params(), ctrl, cluster_size=4096)
        res = ex.run_op(op_for("D:\\d.bin", (Extent(10, 4),), dst=300))
        assert res.cancelled and not res.ok
        assert mover.moves == []

    def test_device_error_reported_after_partial_progress(self):
        mover = FakeMover({"D:\\e.bin": [10, 11, 12, 13]})
        mover.fail_next_move = 1
        ex = NtfsMoveExecutor(mover, params(chunk_blocks=2), RunControl(), cluster_size=4096)
        res = ex.run_op(op_for("D:\\e.bin", (Extent(10, 4),), dst=400))
        assert not res.ok
        assert "搬簇失败" in res.error

    def test_start_blocks_ignored_on_purpose(self):
        """op 内断点对 MOVE_FILE 没有意义：它一次搬一段，中途停下是混合状态。"""
        mover = FakeMover({"D:\\f.bin": [10, 11, 12, 13]})
        ex = NtfsMoveExecutor(mover, params(chunk_blocks=2), RunControl(), cluster_size=4096)
        res = ex.run_op(op_for("D:\\f.bin", (Extent(10, 4),), dst=500), start_blocks=2)
        assert res.blocks_done == 4
        assert mover.files["D:\\f.bin"] == [500, 501, 502, 503]

    def test_progress_callback_reports_blocks(self):
        mover = FakeMover({"D:\\g.bin": [10, 11, 12, 13]})
        seen: list[int] = []
        ex = NtfsMoveExecutor(
            mover, params(chunk_blocks=2), RunControl(), cluster_size=4096,
            on_chunk=lambda sl: seen.append(sl.blocks),
        )
        ex.run_op(op_for("D:\\g.bin", (Extent(10, 4),), dst=600))
        assert seen == [2, 2]

    def test_close_propagates_to_mover(self):
        mover = FakeMover({})
        ex = NtfsMoveExecutor(mover, params(), RunControl(), cluster_size=4096)
        ex.close()
        assert mover.closed


class TestFailureStage:
    """真机路径的失败阶段。

    这条路径上"打不开文件"远多于"搬簇失败"——被独占打开的文件在系统盘上
    成百上千。而"打不开"和"搬不动"的处置完全不同，所以阶段必须带出去。
    """

    def test_open_failure_records_stage(self):
        mover = FakeMover({"D:\\a.bin": [10, 11]})
        mover.missing = {"D:\\a.bin"}
        ex = NtfsMoveExecutor(mover, params(), RunControl(), cluster_size=4096)
        op = op_for("D:\\a.bin", (Extent(10, 2),), dst=100)

        res = ex.run_op(op)

        assert not res.ok
        assert res.stage == "打开文件"
        assert res.error_type == "DeviceError"
        assert res.error_class

    def test_layout_mismatch_records_stage(self):
        mover = FakeMover({"D:\\c.bin": [10, 11, 70, 71, 72]})
        ex = NtfsMoveExecutor(mover, params(), RunControl(), cluster_size=4096)
        op = op_for("D:\\c.bin", (Extent(10, 2), Extent(50, 3)), dst=100)

        res = ex.run_op(op)

        assert not res.ok
        assert res.stage == "复核布局"
