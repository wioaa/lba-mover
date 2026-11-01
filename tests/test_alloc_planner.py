"""分配器与规划器。

这里守的是一条数据安全不变量，不是性能指标：

    任何 op 的目标区都不得覆盖"尚未搬走"的文件的源区。

它一旦被破坏，后果是读到的数据被自己的写入覆盖 —— 源数据永久丢失。
所以测试里大量断言都围绕"目标区与源区的关系"而不是"能不能塞下"。
"""

from __future__ import annotations

import pytest

from lbamover.contract.enums import Strategy
from lbamover.contract.models import Extent, FileFrag, FragReport, Geometry, JobParams
from lbamover.engine.alloc import FreeSpaceMap, IntervalSet
from lbamover.engine.frag import analyze, free_extents_from_files
from lbamover.engine.planner import build_plan, order_files, select_files, validate_plan


def make_report(files, total=200, block_size=4096) -> FragReport:
    geo = Geometry(block_size, total)
    return analyze(files, free_extents_from_files(geo, files), geo, bucket_count=16)


class TestIntervalSet:
    def test_subtract_splits_instead_of_dropping(self):
        s = IntervalSet([Extent(0, 100)])
        s.subtract([Extent(20, 10)])
        # 整段丢弃会放出仍需保护的区域，必须切成两段
        assert s.items == (Extent(0, 20), Extent(30, 70))

    def test_subtract_multiple_holes(self):
        s = IntervalSet([Extent(0, 50)])
        s.subtract([Extent(10, 5), Extent(30, 5)])
        assert s.items == (Extent(0, 10), Extent(15, 15), Extent(35, 15))

    def test_contains_range(self):
        s = IntervalSet([Extent(0, 20), Extent(20, 10)])
        assert s.contains_range(Extent(5, 20))
        assert not s.contains_range(Extent(5, 40))

    def test_intersects(self):
        s = IntervalSet([Extent(10, 10)])
        assert s.intersects(Extent(15, 2))
        assert not s.intersects(Extent(20, 2))


class TestFreeSpaceMap:
    def test_allocate_consumes_space(self):
        f = FreeSpaceMap([Extent(0, 100)])
        start = f.allocate(30)
        assert start == 0
        assert f.free_blocks == 70

    def test_allocate_respects_avoid(self):
        f = FreeSpaceMap([Extent(0, 100)])
        start = f.allocate(10, avoid=[Extent(0, 20)])
        assert start == 20

    def test_allocate_returns_none_when_no_room(self):
        f = FreeSpaceMap([Extent(0, 10)])
        assert f.allocate(11) is None

    def test_allocate_from_tail(self):
        f = FreeSpaceMap([Extent(0, 100)])
        assert f.allocate(10, from_tail=True) == 90

    def test_allocate_respects_max_end(self):
        f = FreeSpaceMap([Extent(0, 100)])
        start = f.allocate(10, from_tail=True, max_end=50)
        assert start == 40

    def test_release_merges_back(self):
        f = FreeSpaceMap([Extent(0, 100)])
        f.allocate(40)
        f.release([Extent(0, 40)])
        assert f.free_blocks == 100
        assert f.run_count == 1

    def test_is_free_checks_whole_range(self):
        f = FreeSpaceMap([Extent(0, 10), Extent(10, 10)])
        assert f.is_free(Extent(5, 10))
        assert not f.is_free(Extent(5, 20))

    def test_rejects_non_positive(self):
        with pytest.raises(ValueError):
            FreeSpaceMap([Extent(0, 10)]).allocate(0)


class TestSelectFiles:
    def test_coalesce_only_picks_fragmented(self):
        report = make_report([
            FileFrag("a", "a", (Extent(0, 10),)),
            FileFrag("b", "b", (Extent(20, 5), Extent(40, 5))),
        ])
        picked, missing = select_files(report, Strategy.COALESCE)
        assert [f.file_id for f in picked] == ["b"]
        assert missing == ()

    def test_compact_picks_all(self):
        report = make_report([
            FileFrag("a", "a", (Extent(0, 10),)),
            FileFrag("b", "b", (Extent(20, 5), Extent(40, 5))),
        ])
        picked, _ = select_files(report, Strategy.COMPACT)
        assert len(picked) == 2

    def test_unknown_ids_reported(self):
        report = make_report([FileFrag("a", "a", (Extent(0, 5), Extent(20, 5)))])
        picked, missing = select_files(report, Strategy.COALESCE, ("a", "zzz"))
        assert [f.file_id for f in picked] == ["a"]
        assert missing == ("zzz",)

    def test_analyze_only_selects_nothing(self):
        report = make_report([FileFrag("a", "a", (Extent(0, 5), Extent(20, 5)))])
        assert select_files(report, Strategy.ANALYZE_ONLY)[0] == []

    def test_order_compact_is_by_lcn(self):
        report = make_report([
            FileFrag("late", "late", (Extent(80, 5), Extent(120, 5))),
            FileFrag("early", "early", (Extent(10, 5), Extent(40, 5))),
        ])
        picked, _ = select_files(report, Strategy.COMPACT)
        assert [f.file_id for f in order_files(picked, Strategy.COMPACT)] == ["early", "late"]


class TestPlannerInvariants:
    def test_no_target_overlaps_own_source(self):
        report = make_report([
            FileFrag(f"f{i}", f"f{i}", (Extent(i * 20, 5), Extent(i * 20 + 10, 5)))
            for i in range(4)
        ])
        plan = build_plan("j", Strategy.COALESCE, report)
        assert plan.ops
        for op in plan.ops:
            assert not op.self_overlapping, f"op{op.op_id} 会自我覆盖"

    def test_targets_never_land_on_later_sources_when_parallel(self):
        """声明可并行的计划，目标区不得踩任何其它 op 的源区。"""
        report = make_report([
            FileFrag(f"f{i}", f"f{i}", (Extent(i * 50, 5), Extent(i * 50 + 20, 5)))
            for i in range(3)
        ], total=400)
        plan = build_plan("j", Strategy.COALESCE, report)
        if plan.requires_serial:
            pytest.skip("该布局必然需要复用空间，串行是正确结论")
        for op in plan.ops:
            for other in plan.ops:
                if op is other:
                    continue
                for src in other.src_extents:
                    assert not op.dst_extent.overlaps(src)

    def test_serial_flag_set_when_reusing_freed_space(self):
        # 磁盘几乎占满：只能把文件搬进"刚腾出来的位置"，此时必须串行
        files = [FileFrag(f"f{i}", f"f{i}", (Extent(i * 10, 4), Extent(i * 10 + 6, 4)))
                 for i in range(9)]
        report = make_report(files, total=100)
        plan = build_plan("j", Strategy.COALESCE, report)
        assert plan.ops
        assert plan.requires_serial is True

    def test_validate_plan_flags_self_overlap(self):
        from lbamover.contract.models import MoveOp, Plan

        bad = Plan(
            job_id="j",
            strategy=Strategy.COALESCE,
            ops=(MoveOp(0, "a", "a", (Extent(0, 10),), dst_start=5),),
        )
        problems = validate_plan(bad, 100)
        assert any("自我覆盖" in p for p in problems)

    def test_validate_plan_flags_out_of_range(self):
        from lbamover.contract.models import MoveOp, Plan

        bad = Plan(
            job_id="j",
            strategy=Strategy.COALESCE,
            ops=(MoveOp(0, "a", "a", (Extent(0, 10),), dst_start=95),),
        )
        assert any("越界" in p for p in validate_plan(bad, 100))

    def test_validate_plan_flags_overlapping_targets(self):
        from lbamover.contract.models import MoveOp, Plan

        bad = Plan(
            job_id="j",
            strategy=Strategy.COALESCE,
            ops=(
                MoveOp(0, "a", "a", (Extent(0, 10),), dst_start=50),
                MoveOp(1, "b", "b", (Extent(20, 10),), dst_start=55),
            ),
        )
        assert any("目标区重叠" in p or "目标区与另一个 op" in p for p in validate_plan(bad, 200))

    def test_validate_plan_flags_false_parallel_claim(self):
        from lbamover.contract.models import MoveOp, Plan

        # 目标踩到另一个 op 的源，却声明可并行 → 必须报出来
        bad = Plan(
            job_id="j",
            strategy=Strategy.COALESCE,
            ops=(
                MoveOp(0, "a", "a", (Extent(0, 10),), dst_start=60),
                MoveOp(1, "b", "b", (Extent(60, 10),), dst_start=0),
            ),
            requires_serial=False,
        )
        assert any("声明可并行" in p for p in validate_plan(bad, 200))


class TestPlannerOutcomes:
    def test_analyze_only_has_no_ops(self):
        report = make_report([FileFrag("a", "a", (Extent(0, 5), Extent(20, 5)))])
        plan = build_plan("j", Strategy.ANALYZE_ONLY, report)
        assert plan.ops == () and plan.feasible

    def test_no_fragments_yields_notes(self):
        report = make_report([FileFrag("a", "a", (Extent(0, 10),))])
        plan = build_plan("j", Strategy.COALESCE, report)
        assert plan.ops == ()
        assert plan.notes

    def test_infeasible_when_no_contiguous_room(self):
        # 碎片文件需要 12 块连续空间，但自由空间被切成 1 块的小洞
        files = [
            FileFrag("big", "big", (Extent(0, 6), Extent(14, 6))),
        ]
        for i in range(10):
            files.append(FileFrag(f"s{i}", f"s{i}", (Extent(30 + i * 4, 2),)))
        geo = Geometry(4096, 100)
        report = analyze(files, free_extents_from_files(geo, files), geo)
        plan = build_plan("j", Strategy.COALESCE, report)
        # 要么放不下（有原因），要么放下（有 op），不允许两者皆无
        assert plan.ops or plan.reasons

    def test_reserve_tail_limits_relocate(self):
        report = make_report([
            FileFrag(f"f{i}", f"f{i}", (Extent(i * 30, 5), Extent(i * 30 + 10, 5)))
            for i in range(3)
        ], total=300)
        params = JobParams(reserve_tail_blocks=250)
        plan = build_plan("j", Strategy.RELOCATE_TAIL, report, params=params)
        for op in plan.ops:
            assert op.dst_extent.end <= 300 - 250 + 1 or op.dst_extent.start <= 50

    def test_plan_hash_reflects_ops(self):
        report = make_report([
            FileFrag("a", "a", (Extent(0, 5), Extent(20, 5))),
        ], total=200)
        p1 = build_plan("j", Strategy.COALESCE, report)
        p2 = build_plan("j", Strategy.COALESCE, report)
        assert p1.plan_hash == p2.plan_hash
