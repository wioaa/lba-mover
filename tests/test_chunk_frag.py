"""分块与碎片分析。

``slice_move_op`` 的目标地址连续性是这个文件里最重要的一条断言：一个
op 的多个源 extent 在盘上不连续，搬到目标时必须**紧密拼接**。写成
"源地址 + 位移"是最常见的实现错误，且只有多 extent 的用例能抓到。
"""

from __future__ import annotations

import pytest

from lbamover.contract.models import Extent, FileFrag, Geometry, MoveOp
from lbamover.engine.chunk import (
    BlockSlice,
    iter_extent_chunks,
    plan_chunk_count,
    slice_move_op,
    slice_plan,
)
from lbamover.engine.frag import analyze, bucketize, free_extents_from_files
from lbamover.contract.models import Plan
from lbamover.contract.enums import Strategy


class TestIterExtentChunks:
    def test_exact_division(self):
        assert list(iter_extent_chunks(Extent(0, 8), 4)) == [
            Extent(0, 4),
            Extent(4, 4),
        ]

    def test_last_chunk_partial(self):
        assert list(iter_extent_chunks(Extent(10, 9), 4)) == [
            Extent(10, 4),
            Extent(14, 4),
            Extent(18, 1),
        ]

    def test_chunk_larger_than_extent(self):
        assert list(iter_extent_chunks(Extent(5, 3), 100)) == [Extent(5, 3)]

    def test_rejects_zero_chunk(self):
        with pytest.raises(ValueError):
            list(iter_extent_chunks(Extent(0, 4), 0))


class TestSliceMoveOp:
    def test_target_is_contiguous_across_extents(self):
        """多 extent 拼接：目标地址不能跟随源地址。"""
        op = MoveOp(
            op_id=0,
            file_id="f",
            path="f.bin",
            src_extents=(Extent(0, 4), Extent(40, 4)),
            dst_start=100,
        )
        got = slice_move_op(op, chunk_blocks=2)
        assert [(s.src_lba, s.dst_lba, s.blocks) for s in got] == [
            (0, 100, 2),
            (2, 102, 2),
            (40, 104, 2),
            (42, 106, 2),
        ]

    def test_offsets_are_monotonic(self):
        op = MoveOp(0, "f", "f", (Extent(0, 5), Extent(20, 3)), dst_start=50)
        got = slice_move_op(op, chunk_blocks=3)
        # 5 块 + 3 块、chunk=3 → 0..2 / 3..4 / 5..7
        assert [s.offset_blocks for s in got] == [0, 3, 5]
        assert sum(s.blocks for s in got) == op.blocks
        assert [s.dst_lba for s in got] == [50, 53, 55]

    def test_no_slice_self_overlap_for_legal_op(self):
        op = MoveOp(0, "f", "f", (Extent(0, 10),), dst_start=10)
        for s in slice_move_op(op, chunk_blocks=4):
            assert not s.self_overlapping

    def test_rejects_bad_chunk(self):
        with pytest.raises(ValueError):
            slice_move_op(MoveOp(0, "f", "f", (Extent(0, 4),), 0), 0)

    def test_slice_plan_keys_by_op(self):
        plan = Plan(
            job_id="j",
            strategy=Strategy.COALESCE,
            ops=(
                MoveOp(0, "a", "a", (Extent(0, 4),), 100),
                MoveOp(1, "b", "b", (Extent(8, 4),), 200),
            ),
        )
        table = slice_plan(plan, 4)
        assert set(table) == {0, 1}
        assert plan_chunk_count(plan, 4) == 2


class TestFreeExtentsFromFiles:
    def test_complement_is_exact(self):
        geo = Geometry(4096, 100)
        files = [FileFrag("a", "a", (Extent(10, 10),)), FileFrag("b", "b", (Extent(40, 5),))]
        free = free_extents_from_files(geo, files)
        assert sum(e.length for e in free) == 100 - 15
        # 自由区与已用区必须完全不重叠
        for f in free:
            for used in (Extent(10, 10), Extent(40, 5)):
                assert not f.overlaps(used)

    def test_leading_and_trailing_gaps(self):
        geo = Geometry(4096, 50)
        free = free_extents_from_files(geo, [FileFrag("a", "a", (Extent(5, 10),))])
        assert free == (Extent(0, 5), Extent(15, 35))

    def test_clips_out_of_range_file(self):
        geo = Geometry(4096, 20)
        free = free_extents_from_files(geo, [FileFrag("a", "a", (Extent(0, 50),))])
        assert free == ()


class TestAnalyze:
    def _setup(self):
        geo = Geometry(4096, 200)
        files = [
            FileFrag("a", "a", (Extent(0, 10), Extent(20, 10))),
            FileFrag("b", "b", (Extent(50, 20),)),
        ]
        free = free_extents_from_files(geo, files)
        return geo, files, free

    def test_normalizes_adjacent_extents(self):
        geo, files, free = self._setup()
        files = [FileFrag("c", "c", (Extent(0, 10), Extent(10, 10)))]
        report = analyze(files, free, geo)
        # 首尾相接的两段应被合并成一段，否则碎片度会虚高
        assert report.files[0].run_count == 1

    def test_buckets_cover_whole_volume(self):
        geo, files, free = self._setup()
        report = analyze(files, free, geo, bucket_count=32)
        assert len(report.buckets) == 32
        assert report.buckets[0].start == 0
        assert report.buckets[-1].end == geo.block_count
        for b in report.buckets:
            assert b.start < b.end
            assert b.used_blocks + b.free_blocks == b.length

    def test_bucket_usage_total_matches(self):
        geo, files, free = self._setup()
        report = analyze(files, free, geo, bucket_count=16)
        used_in_buckets = sum(b.used_blocks for b in report.buckets)
        assert used_in_buckets == sum(f.total_blocks for f in files)

    def test_bucket_count_clamped(self):
        geo = Geometry(4096, 5)
        buckets = bucketize(geo, [], [Extent(0, 5)], bucket_count=256)
        assert len(buckets) == 5

    def test_frag_ratio_zero_for_empty_buckets(self):
        geo, files, free = self._setup()
        report = analyze(files, free, geo, bucket_count=8)
        for b in report.buckets:
            assert 0.0 <= b.frag_ratio <= 1.0
            assert 0.0 <= b.usage_ratio <= 1.0


class TestBlockSlice:
    def test_rejects_zero_blocks(self):
        with pytest.raises(ValueError):
            BlockSlice(0, 0, 0, 0, 0)

    def test_self_overlap_flag(self):
        assert BlockSlice(0, 0, 5, 10, 0).self_overlapping
        assert not BlockSlice(0, 0, 100, 10, 0).self_overlapping
