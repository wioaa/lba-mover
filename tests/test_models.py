"""契约层：区间、碎片指标、计划指纹、参数校验。

这些是所有下游模块的地基。地基上的定义一旦模糊，上层怎么测都测不实。
"""

from __future__ import annotations

import pytest

from lbamover.contract.enums import ErrorClass, FreeSpaceSide, JobState, Strategy
from lbamover.contract.models import (
    Extent,
    FileFrag,
    FragReport,
    Geometry,
    JobParams,
    MoveOp,
    Plan,
    Progress,
    merge_extents,
)


class TestExtent:
    def test_rejects_negative_length(self):
        with pytest.raises(ValueError):
            Extent(0, -1)

    def test_rejects_negative_start(self):
        with pytest.raises(ValueError):
            Extent(-5, 3)

    def test_rejects_half_open_zero_length(self):
        # start=10,length=0 会算出一个假的右端点，必须拒绝
        with pytest.raises(ValueError):
            Extent(10, 0)

    def test_empty_extent_allowed(self):
        e = Extent(0, 0)
        assert e.is_empty and e.end == 0

    def test_overlap_and_adjacency(self):
        assert Extent(0, 10).overlaps(Extent(9, 5))
        assert not Extent(0, 10).overlaps(Extent(10, 5))
        assert Extent(0, 10).adjacent(Extent(10, 5))
        assert not Extent(0, 10).adjacent(Extent(11, 5))

    def test_merge_adjacent_and_overlapping(self):
        got = merge_extents([Extent(30, 4), Extent(0, 10), Extent(10, 5)])
        assert got == [Extent(0, 15), Extent(30, 4)]

    def test_merge_absorbs_contained(self):
        assert merge_extents([Extent(0, 100), Extent(10, 5)]) == [Extent(0, 100)]

    def test_merge_empty_input(self):
        assert merge_extents([]) == []
        assert merge_extents([Extent(0, 0)]) == []


class TestFileFrag:
    def test_contiguous_file_has_zero_frag(self):
        f = FileFrag("a", "a.bin", (Extent(0, 100),))
        assert f.run_count == 1 and f.frag_score == 0.0
        assert not f.is_fragmented

    def test_half_split_scores_half(self):
        f = FileFrag("a", "a.bin", (Extent(0, 50), Extent(100, 50)))
        assert f.run_count == 2
        assert f.frag_score == pytest.approx(0.5)

    def test_uneven_split_dominated_by_largest_run(self):
        # 90/10 切分：整理收益主要来自"再拼一次"，碎片度应偏低
        f = FileFrag("a", "a.bin", (Extent(0, 90), Extent(100, 10)))
        assert f.frag_score == pytest.approx(0.1)

    def test_requires_sorted_non_overlapping_extents(self):
        with pytest.raises(ValueError):
            FileFrag("a", "a.bin", (Extent(50, 10), Extent(0, 10)))
        with pytest.raises(ValueError):
            FileFrag("a", "a.bin", (Extent(0, 20), Extent(10, 20)))

    def test_roundtrip_dict(self):
        f = FileFrag("a", "a.bin", (Extent(0, 10), Extent(20, 5)))
        assert FileFrag.from_dict(f.to_dict()) == f


class TestPlanHash:
    def _plan(self, dst: int = 100) -> Plan:
        return Plan(
            job_id="j1",
            strategy=Strategy.COALESCE,
            ops=(MoveOp(0, "a", "a.bin", (Extent(0, 10),), dst),),
        )

    def test_hash_stable_across_instances(self):
        assert self._plan().plan_hash == self._plan().plan_hash

    def test_hash_changes_with_target(self):
        assert self._plan(100).plan_hash != self._plan(200).plan_hash

    def test_hash_changes_with_strategy(self):
        p = self._plan()
        other = Plan(job_id=p.job_id, strategy=Strategy.COMPACT, ops=p.ops)
        assert p.plan_hash != other.plan_hash

    def test_roundtrip_preserves_hash(self):
        p = self._plan()
        assert Plan.from_dict(p.to_dict()).plan_hash == p.plan_hash


class TestMoveOp:
    def test_blocks_and_dst_extent(self):
        op = MoveOp(0, "a", "a.bin", (Extent(0, 10), Extent(40, 6)), dst_start=100)
        assert op.blocks == 16
        assert op.dst_extent == Extent(100, 16)
        assert op.src_span == Extent(0, 46)

    def test_self_overlapping_detected(self):
        op = MoveOp(0, "a", "a.bin", (Extent(0, 10),), dst_start=5)
        assert op.self_overlapping

    def test_rejects_empty_sources(self):
        with pytest.raises(ValueError):
            MoveOp(0, "a", "a.bin", (), dst_start=0)


class TestJobParams:
    def test_defaults_valid(self):
        p = JobParams()
        assert p.chunk_bytes == 4096 * 256

    def test_buffer_budget_is_product(self):
        p = JobParams(block_size=4096, chunk_blocks=32, queue_depth=3, concurrency=2)
        assert p.buffer_budget_bytes == 4096 * 32 * 3 * 2

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"block_size": 1000},          # 非 2 的幂
            {"block_size": 256},           # 小于 512
            {"chunk_blocks": 0},
            {"queue_depth": 0},
            {"concurrency": 0},
            {"max_retries": -1},
            {"retry_base_ms": 100, "retry_max_ms": 50},  # max < base
            {"rate_limit_mbps": -1},
        ],
    )
    def test_invalid_params_rejected(self, kwargs):
        with pytest.raises(ValueError):
            JobParams(**kwargs)

    def test_roundtrip_dict(self):
        p = JobParams(chunk_blocks=64, concurrency=3)
        assert JobParams.from_dict(p.to_dict()) == p

    def test_roundtrip_covers_every_field(self):
        """每个字段都要往返 —— ``to_dict`` 手写键名的年代漏过四个字段。

        漏字段的表现不是报错，而是"往返后静默回到默认值"：用户在界面勾了
        「忽略错误运行」，参数一旦落盘/回放就变回保守收尾，日志里看不出区别。
        所以这里不只比相等，还先比**键集合**，让漏字段立刻指认到具体名字；
        再逐字段比，让值错时能指出是哪一项（``==`` 只给一个 False）。
        """
        from dataclasses import fields

        p = JobParams(
            block_size=8192,
            chunk_blocks=32,
            queue_depth=3,
            concurrency=2,
            max_retries=5,
            retry_base_ms=20,
            retry_max_ms=500,
            rate_limit_mbps=12.0,
            verify=False,
            checkpoint_every_chunks=8,
            allow_raw_write_on_mounted=True,
            reserve_tail_blocks=64,
            reclaim_blocks=4096,
            reclaim_at=FreeSpaceSide.HEAD,
            reclaim_avoid_protected=False,
            ignore_errors=True,
        )
        dumped = p.to_dict()

        declared = {f.name for f in fields(JobParams)}
        assert declared == set(dumped), (
            f"to_dict 漏字段：{sorted(declared - set(dumped))}；"
            f"多出未知键：{sorted(set(dumped) - declared)}"
        )

        back = JobParams.from_dict(dumped)
        assert back == p
        for f in fields(JobParams):
            assert getattr(back, f.name) == getattr(p, f.name), f"字段 {f.name} 往返后变了"

    def test_from_dict_restores_enum_from_plain_value(self):
        """``to_dict`` 输出 ``.value``，``from_dict`` 必须按注解转回枚举。

        不做这一步就是构造期直接炸：``reclaim_at 必须是 FreeSpaceSide``。
        """
        raw = JobParams(reclaim_at=FreeSpaceSide.TAIL).to_dict()
        assert raw["reclaim_at"] == "tail", "枚举应导出成可 JSON 化的 value"
        assert JobParams.from_dict(raw).reclaim_at is FreeSpaceSide.TAIL

    def test_from_dict_ignores_unknown_keys(self):
        """老断点文件里可能带着已删除的字段，不能因此构造失败。"""
        raw = JobParams(chunk_blocks=32).to_dict()
        raw["legacy_field_from_2_0_0a"] = "x"
        assert JobParams.from_dict(raw).chunk_blocks == 32


class TestProgressAndGeometry:
    def test_geometry_contains(self):
        g = Geometry(block_size=4096, block_count=100)
        assert g.contains(Extent(0, 100))
        assert not g.contains(Extent(99, 2))

    def test_geometry_rejects_bad_block_size(self):
        with pytest.raises(ValueError):
            Geometry(block_size=0, block_count=10)

    def test_progress_ratio_bounds(self):
        assert Progress(done_blocks=50, total_blocks=100).ratio == 0.5
        assert Progress(done_blocks=150, total_blocks=100).ratio == 1.0
        assert Progress().ratio == 0.0

    def test_job_state_terminal_flags(self):
        assert JobState.DONE.is_terminal and JobState.CANCELLED.is_terminal
        assert not JobState.RUNNING.is_terminal
        assert JobState.RUNNING.is_active and not JobState.IDLE.is_active


class TestFragReportMetrics:
    def _report(self) -> FragReport:
        files = (
            FileFrag("a", "a", (Extent(0, 10),)),
            FileFrag("b", "b", (Extent(10, 10), Extent(40, 10))),
        )
        return FragReport(
            geometry=Geometry(4096, 100),
            files=files,
            free_extents=(Extent(20, 20), Extent(50, 50)),
        )

    def test_weighted_file_score(self):
        r = self._report()
        # a(10 块, score 0) 与 b(20 块, score 0.5) 按块数加权 → 10/30
        assert r.file_frag_score == pytest.approx(10 / 30)

    def test_free_score(self):
        r = self._report()
        # 自由 70 块、最大连续 50 → 1 - 50/70
        assert r.free_frag_score == pytest.approx(1 - 50 / 70)

    def test_worst_sorted_by_benefit(self):
        r = self._report()
        assert [f.file_id for f in r.worst(5)] == ["b"]

    def test_error_class_retryable_flag(self):
        from lbamover.devices.base import DeviceError

        e = DeviceError("x", error_class=ErrorClass.RETRYABLE)
        assert e.retryable
        e2 = DeviceError("y", error_class=ErrorClass.FATAL)
        assert not e2.retryable
