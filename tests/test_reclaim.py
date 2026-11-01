"""「可用空间整理」（Strategy.RECLAIM）。

这一档和其它策略的判据根本不同：它不是"尽量整理得好一点"，而是**一个承诺** ——
这几块必须空出来。用户拿这个结果去装虚拟机、拷镜像，所以"差不多空了"等于没做。

因此这里的用例大多在验证「做不到时说清楚」而不是「尽力而为」：

* 目标区外的空闲不够搬出挡路的文件 → 报差额，不硬做；
* 卷首躺着 `$MFT` 这类搬不动的东西 → 整体让开（否则卷首模式永远失败）；
* 只要有一个文件搬不出去 → 整单不可行，不给"搬了一半"的计划。
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from lbamover.contract.enums import FreeSpaceSide, Strategy
from lbamover.contract.models import Extent, FileFrag, FragReport, Geometry, JobParams, Plan
from lbamover.engine.frag import free_extents_from_files
from lbamover.engine.planner import build_plan
from lbamover.engine.safety import audit_plan


def make_report(files, total=1000, protected=()) -> FragReport:
    geo = Geometry(4096, total)
    return FragReport(
        geometry=geo,
        files=files,
        free_extents=free_extents_from_files(geo, files),
        protected_extents=tuple(protected),
    )


def three_files(total=1000, protected=()) -> FragReport:
    files = (
        FileFrag("a", "A.bin", (Extent(0, 100),), 0),
        FileFrag("b", "B.bin", (Extent(150, 50),), 0),
        FileFrag("c", "C.bin", (Extent(500, 50),), 0),
    )
    return make_report(files, total=total, protected=protected)


def plan_reclaim(report, blocks, side=FreeSpaceSide.TAIL, **kw):
    return build_plan(
        "j",
        Strategy.RECLAIM,
        report,
        params=JobParams(reclaim_blocks=blocks, reclaim_at=side, **kw),
    )


class TestTargetRegion:
    def test_tail_target_sits_at_the_end(self):
        plan = plan_reclaim(three_files(), 100)
        assert plan.feasible
        assert plan.reclaim_extent == Extent(900, 100)

    def test_head_target_sits_at_the_start(self):
        plan = plan_reclaim(three_files(), 200, FreeSpaceSide.HEAD)
        assert plan.feasible
        assert plan.reclaim_extent == Extent(0, 200)

    def test_only_blocking_files_are_moved(self):
        plan = plan_reclaim(three_files(), 200, FreeSpaceSide.HEAD)
        assert {op.file_id for op in plan.ops} == {"a", "b"}, "没挡路的文件不该被搬"

    def test_moves_never_land_inside_target(self):
        """落点又压回目标区，等于搬了一整天、空间没到手。"""
        for side in (FreeSpaceSide.HEAD, FreeSpaceSide.TAIL):
            plan = plan_reclaim(three_files(), 200, side)
            assert plan.feasible
            target = plan.reclaim_extent
            for op in plan.ops:
                dst = op.dst_extent
                assert dst.end <= target.start or dst.start >= target.end, (
                    f"{side.label}: {op.path} 的落点 {dst} 压在目标区 {target} 上"
                )

    def test_already_empty_target_needs_no_moves(self):
        plan = plan_reclaim(three_files(), 50)
        assert plan.feasible and plan.ops == ()
        assert plan.reclaim_extent == Extent(950, 50)
        assert any("已经全部是空闲" in n for n in plan.notes)


class TestProtectedRegions:
    """搬不动的东西必须被尊重。

    卷首 1 GiB 内几乎必然躺着 ``$MFT`` / ``$LogFile``，而 ``pagefile.sys``
    也常在固定位置。把它们算成"能腾出来"，用户会得到一个永远达不到的承诺。
    """

    def test_target_steps_over_protected_region(self):
        report = three_files(protected=(Extent(0, 30),))
        plan = plan_reclaim(report, 200, FreeSpaceSide.HEAD)
        assert plan.feasible
        assert plan.reclaim_extent == Extent(30, 200), "目标区没有让开元文件"
        assert any("让开" in n for n in plan.notes), "让开了却没说，用户以为贴着卷首"

    def test_strict_mode_refuses_instead_of_stepping_over(self):
        report = three_files(protected=(Extent(0, 30),))
        plan = plan_reclaim(report, 200, FreeSpaceSide.HEAD, reclaim_avoid_protected=False)
        assert not plan.feasible
        assert plan.ops == ()
        assert any("搬不动" in r for r in plan.reasons)

    def test_protected_at_the_edge_pushes_target_inward(self):
        """卷尾最后一段搬不动时，目标区往前挪，而不是直接报错。

        真实卷上尾部常有 ``$Secure`` / ``$Extend`` 这类元文件。死守"必须贴到
        最后一个块"会让卷尾模式在这些卷上永远失败，所以这里选择让开 ——
        但必须在 notes 里说明"没贴着边"以及原因。
        """
        report = three_files(protected=(Extent(900, 40),))
        plan = plan_reclaim(report, 100)
        assert plan.feasible
        assert plan.reclaim_extent.end <= 900, "目标区压进了搬不动的区域"
        assert any("让开" in n for n in plan.notes)


class TestInfeasibleIsHonest:
    """做不到的时候，要说清楚差多少、该怎么办。"""

    def test_insufficient_outside_free_reports_the_gap(self):
        files = (FileFrag("big", "big.bin", (Extent(0, 900),), 0),)
        plan = plan_reclaim(make_report(files, total=1000), 300, FreeSpaceSide.HEAD)
        assert not plan.feasible
        assert any("差" in r for r in plan.reasons)

    def test_zero_size_is_rejected_not_guessed(self):
        plan = plan_reclaim(three_files(), 0)
        assert not plan.feasible
        assert any("多少" in r or "0" in r for r in plan.reasons)

    def test_oversized_request_is_rejected(self):
        plan = plan_reclaim(three_files(), 5000)
        assert not plan.feasible

    def test_one_unmovable_file_kills_the_whole_plan(self):
        """只要有一个挡路的文件搬不出去，目标区就不可能全空。

        这时候交出一份"搬了一半"的计划是最坏的结果：用户会以为空间到手了。
        """
        report = three_files(protected=(Extent(120, 20),))
        plan = plan_reclaim(report, 200, FreeSpaceSide.HEAD)
        # b 横跨 150..200，而 120..140 是搬不动的 —— 目标区里仍有占用
        assert not plan.feasible or plan.reclaim_extent != Extent(0, 200)


class TestManualScopeInteraction:
    def test_manual_selection_is_ignored_but_announced(self):
        """目标区腾空必须搬走所有挡路的文件，和"只动这几个"天然冲突。"""
        plan = build_plan(
            "j",
            Strategy.RECLAIM,
            three_files(),
            file_ids=("c",),
            params=JobParams(reclaim_blocks=200, reclaim_at=FreeSpaceSide.HEAD),
        )
        assert plan.feasible
        assert {op.file_id for op in plan.ops} == {"a", "b"}
        assert any("忽略手动勾选" in n for n in plan.notes)


class TestSerialization:
    def test_reclaim_extent_survives_roundtrip(self):
        plan = plan_reclaim(three_files(), 100)
        again = Plan.from_dict(json.loads(json.dumps(plan.to_dict())))
        assert again.reclaim_extent == plan.reclaim_extent

    def test_old_plan_without_field_still_loads(self):
        # 老断点文件里没有这个字段，不能因此加载失败
        raw = plan_reclaim(three_files(), 100).to_dict()
        raw.pop("reclaim_extent")
        assert Plan.from_dict(raw).reclaim_extent is None


class TestAudit:
    """执行前复核：目标区最后是不是真的全空。

    规划器算过一遍，这里必须再算一遍 —— "规划时查了"依赖规划器不出错，
    而执行前复核是最后一道闸门。
    """

    def test_clean_plan_passes(self):
        report = three_files()
        plan = plan_reclaim(report, 200, FreeSpaceSide.HEAD)
        audit = audit_plan(plan, report)
        assert audit.allowed, audit.reasons

    def test_missing_op_is_caught(self):
        report = three_files()
        plan = plan_reclaim(report, 200, FreeSpaceSide.HEAD)
        crippled = replace(plan, ops=plan.ops[:1])  # 偷偷少搬一个
        audit = audit_plan(crippled, report)
        assert not audit.allowed
        assert any("没搬走" in r for r in audit.reasons)

    def test_moved_op_landing_in_target_is_caught(self):
        report = three_files()
        plan = plan_reclaim(report, 200, FreeSpaceSide.HEAD)
        op = plan.ops[0]
        bad = replace(op, dst_start=20)  # 落点被改回目标区里
        audit = audit_plan(replace(plan, ops=(bad,)), report)
        assert not audit.allowed

    def test_reclaim_does_not_require_free_space_to_shrink(self):
        """搬运只挪位置，空闲总量不变 —— 不能因此被"搬完没空间了"误判。"""
        report = three_files()
        plan = plan_reclaim(report, 200, FreeSpaceSide.HEAD)
        audit = audit_plan(plan, report)
        assert audit.free_after_blocks == report.free_blocks
