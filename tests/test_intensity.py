"""整理强度档位：模式决定"往哪搬"，强度决定"动多少文件"。

这组用例守的是 2.0.0I 的解耦本身，而不只是五个分支各跑一遍：

* 策略不再筛文件 —— 同一种模式下换档位，文件集必须跟着变；
* ``STANDARD`` 必须与旧行为完全一致 —— 升级不该让任何人手上的任务
  突然多搬或少搬文件；
* 约束式任务（``RECLAIM``）和无操作任务（``ANALYZE_ONLY``）不认档位 ——
  前者挑三拣四就会给出"大概腾出了"的假结果，后者根本不产生操作。
"""

from __future__ import annotations

import pytest

from lbamover.contract.enums import Intensity, Strategy
from lbamover.contract.models import Extent, FileFrag, FragReport, Geometry, JobParams
from lbamover.engine.frag import analyze, free_extents_from_files
from lbamover.engine.planner import (
    DEEP_SMALL_FILE_MIB,
    FAST_MAX_FILES,
    FAST_RATIO,
    build_plan,
    order_files,
    select_files,
)

BLOCK = 4096


def make_report(files, total=200_000, block_size=BLOCK) -> FragReport:
    geo = Geometry(block_size, total)
    return analyze(files, free_extents_from_files(geo, files), geo, bucket_count=8)


def frag(file_id: str, blocks: int, run_count: int = 2, start: int = 0) -> FileFrag:
    """造一个碎片文件：``blocks`` 个块均分成 ``run_count`` 段，段间留空隙。"""
    per = max(1, blocks // run_count)
    extents = []
    cursor = start
    left = blocks
    for _ in range(run_count):
        take = min(per, left)
        extents.append(Extent(cursor, take))
        cursor += take + 7  # 7 块的洞，保证段不连续
        left -= take
    if left > 0:
        extents.append(Extent(cursor, left))
    return FileFrag(file_id, f"{file_id}.bin", tuple(extents))


def cont(file_id: str, blocks: int, start: int) -> FileFrag:
    return FileFrag(file_id, f"{file_id}.bin", (Extent(start, blocks),))


class TestScopeIsDecidedByIntensity:
    """同一模式、不同档位 → 文件集必须不同。"""

    def _mixed(self) -> FragReport:
        return make_report(
            [
                frag("frag_a", 100, 4, start=1000),
                frag("frag_b", 80, 2, start=2000),
                cont("cont_small", 40, start=3000),  # 160 KiB，远小于 1 MiB
                cont("cont_big", 4000, start=10_000),  # 约 15.6 MiB
            ]
        )

    def _ids(self, report: FragReport, intensity: Intensity) -> set[str]:
        picked, _ = select_files(report, Strategy.COALESCE, (), intensity)
        return {f.file_id for f in picked}

    def test_fast_only_takes_top_share(self):
        """两个碎片文件时"前 20%"取一个 —— 取收益最大的那个。"""
        report = self._mixed()
        picked, _ = select_files(report, Strategy.COALESCE, (), Intensity.FAST)
        assert [f.file_id for f in picked] == ["frag_a"], (
            "frag_a 是 4 段 100 块（收益 75），frag_b 是 2 段 80 块（收益 40）"
        )

    def test_standard_is_fragmented_only(self):
        assert self._ids(self._mixed(), Intensity.STANDARD) == {"frag_a", "frag_b"}

    def test_deep_adds_small_contiguous(self):
        picked = self._ids(self._mixed(), Intensity.DEEP)
        assert picked == {"frag_a", "frag_b", "cont_small"}, (
            "深度档把卷首零散小文件也纳入 —— 它们散着会把连续空间切成小洞"
        )
        assert "cont_big" not in picked, "深度档不该动大文件"

    def test_thorough_takes_everything(self):
        assert len(self._ids(self._mixed(), Intensity.THOROUGH)) == 4

    def test_extreme_is_volume_based_not_fragmentation_based(self):
        """极端档只按体积切，与"是否碎片"无关 —— 这是刻意的另一种切法。"""
        report = self._mixed()
        params = JobParams(intensity=Intensity.EXTREME, extreme_min_mib=1)  # 1 MiB
        picked, _ = select_files(
            report, Strategy.COALESCE, (), Intensity.EXTREME, params
        )
        assert {f.file_id for f in picked} == {"cont_big"}, (
            "15.6 MiB 的连续文件要进，100 块的碎片文件（约 400 KiB）不该进"
        )

    def test_strategy_is_not_used_for_scope(self):
        """四种模式在标准档下选出同一批文件 —— 策略管落点，不管范围。"""
        report = self._mixed()
        for strategy in (
            Strategy.COALESCE,
            Strategy.COMPACT,
            Strategy.RELOCATE_TAIL,
        ):
            picked, _ = select_files(report, strategy, (), Intensity.STANDARD)
            assert {f.file_id for f in picked} == {"frag_a", "frag_b"}, strategy


class TestFastShare:
    def test_ratio_applies_below_ceiling(self):
        n = 1000  # 20% = 200 个，还没撞上限
        report = make_report([frag(f"f{i}", 20, 2, start=i * 40) for i in range(n)])
        picked, _ = select_files(report, Strategy.COALESCE, (), Intensity.FAST)
        assert len(picked) == min(FAST_MAX_FILES, round(n * FAST_RATIO))

    def test_ceiling_caps_huge_volumes(self):
        """几十万文件的卷上纯比例会挑出几万个，那不是"快速"了。"""
        n = int(FAST_MAX_FILES / FAST_RATIO) + 200  # 比例算出 > 上限
        report = make_report([frag(f"f{i}", 20, 2, start=i * 40) for i in range(n)])
        picked, _ = select_files(report, Strategy.COALESCE, (), Intensity.FAST)
        assert len(picked) == FAST_MAX_FILES

    def test_small_volumes_take_twenty_percent(self):
        report = make_report([frag(f"f{i}", 20, 2, start=i * 40) for i in range(10)])
        picked, _ = select_files(report, Strategy.COALESCE, (), Intensity.FAST)
        assert len(picked) == round(10 * FAST_RATIO)

    def test_single_fragmented_file_still_selected(self):
        """只有一个碎片文件时"前 20%"不该算成 0 —— 点快速是想做点事，不是想什么都不做。"""
        report = make_report([frag("only", 100, 3, start=500)])
        picked, _ = select_files(report, Strategy.COALESCE, (), Intensity.FAST)
        assert [f.file_id for f in picked] == ["only"]

    def test_fast_repairs_biggest_benefit_first(self):
        files = [
            frag("tiny", 20, 2, start=100),
            frag("huge", 2000, 4, start=5000),
            frag("mid", 400, 3, start=9000),
        ]
        # 再凑 7 个无关文件，让 20% 恰好是 2 个（10 × 0.2）
        files += [frag(f"pad{i}", 30, 2, start=20_000 + i * 80) for i in range(7)]
        report = make_report(files)
        picked, _ = select_files(report, Strategy.COALESCE, (), Intensity.FAST)
        assert [f.file_id for f in picked] == ["huge", "mid"], (
            "收益 = (段数-1) × 每段平均块数：huge 1500、mid 267、pad 15、tiny 10"
        )


class TestExtremeOrdering:
    def test_largest_first(self):
        """最大的先搬：它最难找落点，最后放会剩一块谁都塞不进的洞。"""
        report = make_report(
            [
                cont("small", 300, 1000),
                cont("big", 9000, 20_000),
                cont("mid", 2000, 40_000),
            ]
        )
        params = JobParams(intensity=Intensity.EXTREME, extreme_min_mib=1)
        picked, _ = select_files(
            report, Strategy.RELOCATE_TAIL, (), Intensity.EXTREME, params
        )
        ordered = order_files(picked, Strategy.RELOCATE_TAIL, Intensity.EXTREME)
        assert [f.file_id for f in ordered] == ["big", "mid", "small"]

    def test_threshold_is_respected(self):
        report = make_report([cont("small", 100, 1000), cont("big", 100_000, 20_000)])
        params = JobParams(intensity=Intensity.EXTREME, extreme_min_mib=64)  # 64 MiB
        picked, _ = select_files(
            report, Strategy.RELOCATE_TAIL, (), Intensity.EXTREME, params
        )
        assert [f.file_id for f in picked] == ["big"]


class TestExtremeProducesTailPlan:
    """用户原话：「极端模式可以做成全盘大文件分析，然后移动到卷尾」。"""

    def test_extreme_relocate_tail_moves_big_files_toward_tail(self):
        files = [cont(f"big{i}", 5000, 1000 + i * 6000) for i in range(4)]
        files.append(cont("small", 20, 30_000))
        report = make_report(files, total=200_000)
        params = JobParams(intensity=Intensity.EXTREME, extreme_min_mib=1)
        plan = build_plan(
            "t", Strategy.RELOCATE_TAIL, report, params=params
        )
        assert plan.feasible
        assert {op.file_id for op in plan.ops} == {"big0", "big1", "big2", "big3"}
        # 卷尾方向 first-fit：第一个 op 就该落在高地址一侧
        assert plan.ops[0].dst_start > report.geometry.block_count // 2

    def test_no_big_file_says_why(self):
        """卷里没有大文件时必须说清是门槛挡住了，而不是"没发现碎片"。

        "未发现碎片"在这里是谎话：卷里可能有几万个碎片文件，只是都不到
        256 MiB。用户看到那句话会以为盘很健康，然后放弃这个功能。
        """
        report = make_report([frag(f"f{i}", 40, 2, start=i * 60) for i in range(20)])
        params = JobParams(intensity=Intensity.EXTREME, extreme_min_mib=256)
        plan = build_plan("t", Strategy.RELOCATE_TAIL, report, params=params)
        assert not plan.ops
        text = " ".join(plan.notes)
        assert "256 MiB" in text and "最大的文件" in text
        assert "未发现碎片" not in text


class TestConstraintTasksIgnoreIntensity:
    def test_reclaim_ignores_intensity(self):
        """约束式任务必须搬开所有挡路的，不能因为选了"快速"就只搬 20%。"""
        # 卷首一段空闲目标区，前后都被文件占着
        files = [cont(f"f{i}", 100, 1000 + i * 200) for i in range(6)]
        report = make_report(files, total=100_000)
        params = JobParams(
            intensity=Intensity.FAST, reclaim_blocks=50_000, reclaim_avoid_protected=False
        )
        picked, _ = select_files(report, Strategy.RECLAIM, (), Intensity.FAST, params)
        assert len(picked) == len(files), "RECLAIM 不看强度，挡路的全部进候选"

    def test_analyze_only_selects_nothing_at_any_intensity(self):
        report = make_report([frag("a", 100, 3, start=100)])
        for intensity in Intensity:
            picked, _ = select_files(report, Strategy.ANALYZE_ONLY, (), intensity)
            assert picked == [], intensity


class TestEveryCombinationPlans:
    @pytest.mark.parametrize("strategy", [s for s in Strategy if s.writes_blocks])
    @pytest.mark.parametrize("intensity", list(Intensity))
    def test_combination_does_not_crash(self, strategy, intensity):
        """5 模式 × 5 档位全部组合都必须能出计划 —— 组合爆炸是这次改动的主要风险。"""
        files = [
            frag("frag", 300, 4, start=1000),
            cont("small", 30, 5000),
            cont("big", 20_000, 60_000),
        ]
        report = make_report(files, total=400_000)
        params = JobParams(
            intensity=intensity,
            extreme_min_mib=1,
            reclaim_blocks=1_000 if strategy is Strategy.RECLAIM else 0,
        )
        plan = build_plan("t", strategy, report, params=params)
        assert plan.strategy is strategy
        # 有 ops 就必须通过校验（目标区不覆盖未搬走的源区）
        if plan.ops:
            from lbamover.engine.planner import validate_plan

            assert not validate_plan(plan, report.geometry.block_count)


class TestParams:
    def test_defaults_keep_old_behaviour(self):
        p = JobParams()
        assert p.intensity is Intensity.STANDARD
        assert p.extreme_min_mib == 256

    def test_roundtrip(self):
        p = JobParams(intensity=Intensity.EXTREME, extreme_min_mib=1024)
        assert JobParams.from_dict(p.to_dict()) == p

    def test_invalid_intensity_rejected(self):
        with pytest.raises(ValueError):
            JobParams(intensity="extreme")  # type: ignore[arg-type]

    def test_zero_threshold_rejected(self):
        with pytest.raises(ValueError):
            JobParams(extreme_min_mib=0)


class TestDeepBoundary:
    def test_small_file_boundary_is_inclusive_below(self):
        """DEEP 的门槛是"不足 1 MiB"，恰好 1 MiB 的连续文件不该被纳入。"""
        limit_blocks = DEEP_SMALL_FILE_MIB * 1024 * 1024 // BLOCK
        report = make_report(
            [
                cont("just_under", limit_blocks - 1, 1000),
                cont("exactly", limit_blocks, 5000),
            ]
        )
        picked, _ = select_files(report, Strategy.COALESCE, (), Intensity.DEEP)
        assert [f.file_id for f in picked] == ["just_under"]
