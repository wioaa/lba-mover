"""目标区预占：占位文件的调度逻辑。

只测**调度**，不测 Win32 原语 —— 后者需要管理员权限与真实 NTFS 卷，本机
不具备。所以这里用一个会记录调用序列的假后端，验证的是"占哪些区间、什么
时候释放、失败怎么回滚"。

一个前提必须记住：预占是**降低风险**的增强，不是正确性前提。所以它失败时
搬运必须照常进行（布局复核与执行前审计才是把关的），只在日志里说清楚。
"""

from __future__ import annotations

import pytest

from lbamover.contract.enums import ExecMode
from lbamover.contract.models import Extent, JobParams, MoveOp, Plan, Strategy
from lbamover.devices.base import DeviceError
from lbamover.engine.preoccupy import PreoccupyGuard

BLOCK = 4096


class FakeMover:
    """记录调用的假后端。``fail_at`` 用来模拟某个 op 占位失败。"""

    def __init__(self, fail_at: int | None = None, fail_create: bool = False):
        self.calls: list[tuple] = []
        self.fail_at = fail_at
        self.fail_create = fail_create
        self.live: set[str] = set()
        self._n = 0

    def create_placeholder(self, path: str, size_bytes: int):
        self.calls.append(("create", path, size_bytes))
        if self.fail_create:
            raise DeviceError("没有 SeManageVolumePrivilege")
        if self.fail_at is not None and self._n == self.fail_at:
            raise DeviceError("占位失败（模拟）")
        self._n += 1
        self.live.add(path)
        return 1000 + self._n

    def move(self, handle, start_vcn: int, start_lcn: int, count: int) -> None:
        self.calls.append(("move", handle, start_vcn, start_lcn, count))

    def close_file(self, handle) -> None:
        self.calls.append(("close", handle))

    def delete_file(self, path: str) -> None:
        self.calls.append(("delete", path))
        self.live.discard(path)


def op(op_id: int, blocks: int, dst: int) -> MoveOp:
    return MoveOp(
        op_id=op_id,
        file_id=f"f{op_id}",
        path=f"/f{op_id}",
        src_extents=(Extent(1000 + op_id * 100, blocks),),
        dst_start=dst,
        verify=False,
    )


class TestOccupying:
    def test_one_placeholder_per_op_at_its_target(self):
        mover = FakeMover()
        guard = PreoccupyGuard(mover, "F:\\", BLOCK)
        report = guard.occupy([op(0, 10, 500), op(1, 20, 800)])

        assert report.all_ok and report.occupied == 2 and report.blocks == 30
        moves = [c for c in mover.calls if c[0] == "move"]
        assert moves[0][3] == 500 and moves[0][4] == 10, "第一个占位落在 op0 的目标区"
        assert moves[1][3] == 800 and moves[1][4] == 20

    def test_size_is_blocks_times_cluster(self):
        mover = FakeMover()
        PreoccupyGuard(mover, "F:\\", 4096).occupy([op(0, 8, 100)])
        create = [c for c in mover.calls if c[0] == "create"][0]
        assert create[2] == 8 * 4096, "占位文件大小必须是块数×簇大小"

    def test_placeholder_sits_on_the_volume_root(self):
        """占位文件必须在目标卷上 —— MOVE_FILE 只在同卷内搬簇。"""
        mover = FakeMover()
        guard = PreoccupyGuard(mover, "F:\\", BLOCK)
        guard.occupy([op(0, 1, 10)])
        path = [c for c in mover.calls if c[0] == "create"][0][1]
        assert path.startswith("F:\\")
        assert "placeholder" in path

    def test_handles_are_closed_after_placing(self):
        mover = FakeMover()
        PreoccupyGuard(mover, "F:\\", BLOCK).occupy([op(0, 4, 10)])
        kinds = [c[0] for c in mover.calls]
        assert kinds.index("close") > kinds.index("move"), "摆好位置就关句柄"

    def test_empty_plan_is_not_an_error(self):
        guard = PreoccupyGuard(FakeMover(), "F:\\", BLOCK)
        report = guard.occupy([])
        assert report.requested == 0 and report.all_ok


class TestReleasing:
    def test_release_for_deletes_only_that_op(self):
        mover = FakeMover()
        guard = PreoccupyGuard(mover, "F:\\", BLOCK)
        guard.occupy([op(0, 4, 10), op(1, 4, 20)])
        guard.release_for(0)

        deleted = [c[1] for c in mover.calls if c[0] == "delete"]
        assert len(deleted) == 1 and "000000" in deleted[0]
        assert len(mover.live) == 1, "另一个 op 的占位必须还在"

    def test_release_twice_is_harmless(self):
        mover = FakeMover()
        guard = PreoccupyGuard(mover, "F:\\", BLOCK)
        guard.occupy([op(0, 4, 10)])
        guard.release_for(0)
        guard.release_for(0)  # 不该再删一次
        assert len([c for c in mover.calls if c[0] == "delete"]) == 1

    def test_close_cleans_everything_left(self):
        """取消/异常路径下不会逐个释放 —— 收尾必须兜住。"""
        mover = FakeMover()
        guard = PreoccupyGuard(mover, "F:\\", BLOCK)
        guard.occupy([op(0, 4, 10), op(1, 4, 20), op(2, 4, 30)])
        guard.close()
        assert mover.live == set(), "卷根上不能留 .lbamover-placeholder-*.tmp"


class TestFailureRollsBack:
    def test_partial_failure_releases_what_was_taken(self):
        """占了一半就失败时必须整体回滚。

        留一半"已占住"比完全不占更危险：执行时那半段的窗口没了，另一半
        的窗口还在，而用户看到"已占住 2/3"会以为整体受保护。
        """
        mover = FakeMover(fail_at=2)
        guard = PreoccupyGuard(mover, "F:\\", BLOCK)
        report = guard.occupy([op(0, 4, 10), op(1, 4, 20), op(2, 4, 30)])

        assert not report.all_ok
        assert report.occupied == 2
        assert report.failures and "op2" in report.failures[0]
        assert mover.live == set(), "失败后必须把已占的也放掉"

    def test_unsupported_backend_is_reported_not_crashed(self):
        class Bare:
            pass

        guard = PreoccupyGuard(Bare(), "F:\\", BLOCK)
        report = guard.occupy([op(0, 4, 10)])
        assert not report.all_ok
        assert "不支持" in report.summary()

    def test_summary_is_readable(self):
        mover = FakeMover()
        guard = PreoccupyGuard(mover, "F:\\", BLOCK)
        assert "已占住 2" in guard.occupy([op(0, 4, 10), op(1, 4, 20)]).summary()


class TestExecutorIntegration:
    """执行器侧：prepare 占全部，run_op 前释放自己那一段。"""

    def _executor(self, guard, mover=None):
        """注意 mover 必须与 guard 用**同一个**：否则测的是两个互不相干的
        后端，序列看起来对不上的时候会误判成执行器的问题。"""
        from lbamover.engine.executor import NtfsMoveExecutor

        mover = mover or FakeMover()
        mover.open_file = lambda path, writable=True: 777
        mover.query = lambda h: ((0, 1000, 4),)
        return NtfsMoveExecutor(
            mover,
            JobParams(verify=False, max_retries=0),
            _Control(),
            cluster_size=BLOCK,
            preoccupy=guard,
        ), mover

    def test_release_happens_before_op_runs(self):
        mover = FakeMover()
        guard = PreoccupyGuard(mover, "F:\\", BLOCK)
        ex, _ = self._executor(guard, mover)
        guard.occupy([op(0, 4, 1000)])
        mover.calls.clear()  # 只关心"执行这个 op 时"发生了什么

        ex.run_op(op(0, 4, 1000))

        kinds = [c[0] for c in mover.calls]
        assert kinds and kinds[0] == "delete", (
            f"占位必须在搬运动作之前释放，实际序列：{kinds}"
        )
        assert "move" in kinds, "释放之后才搬"

    def test_no_guard_means_no_calls(self):
        mover = FakeMover()
        ex, _ = self._executor(None, mover)
        ex.prepare([op(0, 4, 1000)])
        assert mover.calls == [], "没开预占就一次都不该调"

    def test_prepare_failure_does_not_block(self):
        """占不住也要继续搬 —— 预占是增强，不是正确性前提。"""
        mover = FakeMover(fail_create=True)
        guard = PreoccupyGuard(mover, "F:\\", BLOCK)
        ex, _ = self._executor(guard, mover)
        ex.prepare([op(0, 4, 1000)])  # 不抛
        assert ex.lock_failures >= 1, "失败要留痕，否则用户以为开了就受保护"


class _Control:
    cancelled = False

    def checkpoint(self) -> None:
        return None
