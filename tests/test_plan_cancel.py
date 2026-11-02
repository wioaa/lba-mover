"""规划阶段的可中断性。

**缺陷原样**（2.0.0I 及之前）：真实卷上点「扫描 / 预演」之后，规划要跑几十秒；
这段时间里「停止」按钮是**禁用的**（``ControlBar.sync_state`` 只认 RUNNING/
PAUSED），就算绕过界面把 ``CancelJob`` 塞进队列，规划循环里也一次取消检查都
没有 —— 命令队列由正在规划的那条线程消费，它没空去读队列。用户看到的是
"按钮灰着，点了没反应，只能等"。

修法有两处，缺一不可，所以两组测试都要有：

1. 入口（UI）：扫描/规划期间「停止」可用；
2. 出口（引擎）：规划/筛选的循环内部按节奏查取消标志，查到就抛
   :class:`PlanningCancelled`，由 ``EngineCore`` 翻成 CANCELLED 状态。

只改一处都是假修：只开按钮 → 按了仍然没反应；只改循环 → 按钮还是灰的。
"""

from __future__ import annotations

import pytest

from lbamover.contract.enums import JobState, Strategy
from lbamover.contract.models import Extent, FileFrag, Geometry, JobParams
from lbamover.contract.ports import ListSink
from lbamover.engine.catalog import StaticCatalog
from lbamover.engine.control import CANCEL_CHECK_EVERY, check_cancel
from lbamover.engine.engine import EngineCore
from lbamover.engine.errors import CancelledError, PlanningCancelled
from lbamover.engine.frag import analyze, free_extents_from_files
from lbamover.engine.planner import build_plan
from lbamover.engine.safety import DEFAULT_POLICY

BLOCK = 4096


def make_report(count: int, total: int = 4096):
    """``count`` 个散开的双段文件 —— 每个都要一次分配，循环足够长。"""
    files = tuple(
        FileFrag(f"f{i}", f"f{i}.bin", (Extent(i * 3, 1), Extent(total - 1 - i, 1)))
        for i in range(count)
    )
    geo = Geometry(BLOCK, total)
    return analyze(files, free_extents_from_files(geo, files), geo, bucket_count=8)


def _always(_index: int = 0) -> bool:
    return True


class TestThrottle:
    def test_check_runs_on_interval_boundaries(self):
        """节流口径：第 0 个元素必查（否则短循环永远不查），之后每 N 个一次。"""
        calls = []

        def cancel() -> bool:
            calls.append(1)
            return False

        total = CANCEL_CHECK_EVERY * 3
        for i in range(total):
            check_cancel(cancel, i)
        assert len(calls) == 3, f"应只查 {total // CANCEL_CHECK_EVERY} 次，实际 {len(calls)}"

    def test_zero_index_always_checked(self):
        """``0 % N == 0`` 是有意的：短循环（只有一两个文件）也要能被停。"""
        with pytest.raises(PlanningCancelled):
            check_cancel(_always, 0)

    def test_none_cancel_is_noop(self):
        check_cancel(None, 0)  # 不抛即通过

    def test_raises_planning_cancelled(self):
        with pytest.raises(PlanningCancelled):
            check_cancel(_always, CANCEL_CHECK_EVERY)


class TestPlannerCancels:
    @pytest.mark.parametrize(
        "strategy",
        [Strategy.COALESCE, Strategy.COMPACT, Strategy.RELOCATE_TAIL, Strategy.RECLAIM],
    )
    def test_every_strategy_honours_cancel(self, strategy):
        report = make_report(64)
        params = JobParams(reclaim_blocks=64, reclaim_at=params_side())
        with pytest.raises(PlanningCancelled):
            build_plan("t", strategy, report, params=params, cancel=_always)

    def test_pack_tail_honours_cancel(self):
        report = make_report(64)
        with pytest.raises(PlanningCancelled):
            build_plan("t", Strategy.PACK_TAIL, report, cancel=_always)

    def test_no_cancel_still_plans(self):
        """取消检查不能改变正常结果 —— 没取消时计划必须照常产出。"""
        report = make_report(42)
        plan = build_plan("t", Strategy.COALESCE, report)
        assert plan.ops, "正常情况下应该有 op"

    def test_analyze_only_never_checks(self):
        """只分析模式不产生计划，也就没有循环可中断 —— 不该抛。"""
        report = make_report(8)
        plan = build_plan("t", Strategy.ANALYZE_ONLY, report, cancel=_always)
        assert plan.ops == ()


class TestScreenCancels:
    def test_screen_honours_cancel(self):
        """过滤整卷文件是规划里最贵的一段，必须能中断。"""
        report = make_report(64)
        with pytest.raises(PlanningCancelled):
            DEFAULT_POLICY.screen(report.files, cancel=_always)

    def test_screen_without_cancel_unchanged(self):
        report = make_report(16)
        allowed, blocked = DEFAULT_POLICY.screen(report.files)
        assert len(allowed) == len(report.files)
        assert blocked == []


class TestCore:
    @pytest.fixture()
    def core(self, tmp_path):
        report = make_report(96)
        geo = report.geometry
        from lbamover.contract.enums import VolumeKind
        from lbamover.contract.models import VolumeInfo

        vol = VolumeInfo(
            device_path="memory://cancel",
            mount_point="T:\\",
            label="t",
            filesystem="SYNTH",
            kind=VolumeKind.RAMDISK,
            geometry=geo,
            free_bytes=geo.total_bytes // 2,
            is_mounted=False,
            writable=True,
        )
        c = EngineCore(
            sink=ListSink(),
            catalog=StaticCatalog(report.files, report.free_extents, volume_key=vol.device_path),
            state_dir=tmp_path,
        )
        c.set_target(vol, JobParams())
        c.scan(vol)
        return c

    def test_cancelled_plan_raises_and_sets_state(self, core):
        core.control.request_cancel()
        core._set_state(JobState.STOPPING, "测试")
        with pytest.raises(CancelledError):
            core.make_plan(Strategy.COALESCE)
        assert core.state is JobState.CANCELLED, "取消规划必须落在 CANCELLED，不是 IDLE"
        assert core.plan is None, "取消不该留下一份空计划（界面会显示成'没事可做'）"

    def test_cancel_flag_is_not_sticky(self, core):
        """停止过一次之后，下一次规划必须能正常跑。

        这条是"改一下强度想重新预演，结果立刻又被取消"的回归测试：取消标志
        是一次性的，``make_plan`` 在非 STOPPING 状态会换一块新的 RunControl。
        """
        core.control.request_cancel()
        core._set_state(JobState.STOPPING, "测试")
        with pytest.raises(CancelledError):
            core.make_plan(Strategy.COALESCE)

        plan = core.make_plan(Strategy.COALESCE)
        assert plan.ops, "取消之后的第一次规划必须能产出计划"
        assert core.state is JobState.IDLE

    def test_plan_event_not_emitted_when_cancelled(self, core):
        from lbamover.contract.enums import EventKind

        sink = core.sink
        sink.events.clear()
        core.control.request_cancel()
        core._set_state(JobState.STOPPING, "测试")
        with pytest.raises(CancelledError):
            core.make_plan(Strategy.COALESCE)
        assert not [e for e in sink.events if e.kind is EventKind.PLAN], (
            "被打断的规划不该发 PlanEvent，否则界面会显示半份预演"
        )


def params_side():
    from lbamover.contract.enums import FreeSpaceSide

    return FreeSpaceSide.TAIL
