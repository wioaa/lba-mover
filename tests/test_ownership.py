"""T01 验收 5 · 全卷归属索引（``app/core/ownership.py``）。

``OwnershipIndex`` 放在底座（T01）是因为 N4（挡路者反查）与 N5（MFT 扫描顺带产出）
都要用它 —— 两边并行开发时接口必须先定死。
"""

from __future__ import annotations

import os
import random
import sys
import time
from typing import List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.models import Extent, FileEntry  # noqa: E402
from app.core.ownership import OwnershipIndex  # noqa: E402


def _make_entries(runs: List[Tuple[str, List[Tuple[int, int]]]]) -> List[FileEntry]:
    """``runs`` = [(path, [(lcn, clusters), ...])]。"""
    entries: List[FileEntry] = []
    for path, extents in runs:
        entries.append(
            FileEntry(
                path=path,
                name=os.path.basename(path),
                extents=[Extent(vcn=0, lcn=lcn, clusters=c) for lcn, c in extents],
            )
        )
    return entries


def _naive_query(
    entries: List[FileEntry], start: int, end: int
) -> List[Tuple[str, Tuple[int, int, int]]]:
    """朴素 O(n) 实现（对照基准）：返回 ``(path, (vcn, lcn, clusters))``。"""
    hits: List[Tuple[str, Tuple[int, int, int]]] = []
    for entry in entries:
        for extent in entry.allocated_extents:
            left = max(extent.lcn, start)
            right = min(extent.lcn_end, end)
            if right > left:
                hits.append(
                    (
                        entry.path,
                        (
                            extent.vcn + (left - extent.lcn),
                            left,
                            right - left,
                        ),
                    )
                )
    hits.sort(key=lambda item: (item[1][1], item[0]))
    return hits


def test_query_matches_naive_on_random_data() -> None:
    """1000 个随机文件 / 3000 个查询，与朴素扫描**完全一致**。"""
    rng = random.Random(20240607)
    entries: List[FileEntry] = []
    for index in range(1000):
        count = rng.randint(1, 4)
        starts = sorted(rng.randrange(0, 100_000) for _ in range(count))
        entries.append(
            FileEntry(
                path=f"R:\\f{index}.bin",
                name=f"f{index}.bin",
                extents=[
                    Extent(vcn=offset * 8, lcn=start, clusters=rng.randint(1, 200))
                    for offset, start in enumerate(starts)
                ],
            )
        )
    index = OwnershipIndex.build(entries)
    for _ in range(3000):
        start = rng.randrange(0, 100_000)
        end = start + rng.randint(1, 5000)
        got = sorted(
            ((path, (e.vcn, e.lcn, e.clusters)) for path, e in index.query(start, end)),
            key=lambda item: (item[1][1], item[0]),
        )
        assert got == _naive_query(entries, start, end)


def test_covered_matches_manual_count() -> None:
    entries = _make_entries(
        [("R:\\a", [(100, 10), (200, 5)]), ("R:\\b", [(105, 10)])]
    )
    index = OwnershipIndex.build(entries)
    # 语义：按"每个 extent 与区间的相交长度"累加（重叠部分各算一次，保持与
    # packer 里"占用簇数"口径一致）；调用方若需要去重后长度应自己 merge。
    assert index.covered(100, 110) == 10 + 5        # a:10 / b:5
    assert index.covered(105, 115) == 5 + 10         # a:5  / b:10
    assert index.covered(0, 100) == 0


def test_query_clips_extent_for_move() -> None:
    """裁剪后的切片必须能直接喂给移动调用（vcn 已按偏移推进）。"""
    entries = _make_entries([("R:\\big", [(1000, 100)])])
    index = OwnershipIndex.build(entries)
    hits = index.query(1050, 1060)
    assert len(hits) == 1
    path, extent = hits[0]
    assert path == "R:\\big"
    assert (extent.vcn, extent.lcn, extent.clusters) == (50, 1050, 10)


def test_paths_in_deduplicates() -> None:
    entries = _make_entries([("R:\\a", [(100, 10), (200, 10)]), ("R:\\b", [(110, 5)])])
    index = OwnershipIndex.build(entries)
    assert index.paths_in(100, 120) == ["R:\\a", "R:\\b"]
    assert index.paths_in(200, 210) == ["R:\\a"]


def test_sparse_and_empty_entries_are_skipped() -> None:
    entries = [
        FileEntry(path="R:\\sparse", extents=[Extent(vcn=0, lcn=-1, clusters=10)]),
        FileEntry(path="R:\\empty", extents=[]),
        FileEntry(path="R:\\ok", extents=[Extent(vcn=0, lcn=500, clusters=3)]),
    ]
    index = OwnershipIndex.build(entries)
    assert index.extent_count == 1
    assert index.file_count == 1
    assert index.paths_in(0, 10_000) == ["R:\\ok"]


def test_by_path_keeps_entry_reference() -> None:
    """挡路者需要条目本身（size / 属性 / 可搬性），不能只有路径字符串。"""
    entries = _make_entries([("R:\\a", [(100, 10)])])
    index = OwnershipIndex.build(entries)
    assert index.by_path["R:\\a"] is entries[0]


def test_empty_index_is_safe() -> None:
    index = OwnershipIndex.build([])
    assert index.extent_count == 0
    assert index.file_count == 0
    assert index.query(0, 100) == []
    assert index.covered(0, 100) == 0
    assert index.is_empty() is True
    assert index.total_clusters == 0


def test_query_cost_does_not_scale_linearly() -> None:
    """10k 与 100k extent 的单次查询耗时比 < 3（不呈线性）。"""
    rng = random.Random(7)

    def build(count: int) -> OwnershipIndex:
        entries: List[FileEntry] = []
        for index in range(count):
            start = rng.randrange(0, 10_000_000)
            entries.append(
                FileEntry(
                    path=f"R:\\f{index}",
                    extents=[Extent(vcn=0, lcn=start, clusters=rng.randint(1, 8))],
                )
            )
        return OwnershipIndex.build(entries)

    def measure(index: OwnershipIndex, rounds: int = 4000) -> float:
        rng_local = random.Random(11)
        queries = [
            (rng_local.randrange(0, 10_000_000), rng_local.randint(1, 64)) for _ in range(rounds)
        ]
        started = time.perf_counter()
        for start, length in queries:
            index.query(start, start + length)
        return time.perf_counter() - started

    small = build(10_000)
    large = build(100_000)
    measure(small, 500)          # 预热
    measure(large, 500)
    # 1.0.0D：计时类断言在整机负载下会偶发抖动（testflow 全量跑时一次
    # ratio 超 3 的假阳性），改取 3 轮最优 —— 意图不变（查的是复杂度，
    # 不是绝对速度），最优值对后台噪声最不敏感。
    ratios = []
    for _round in range(3):
        ratios.append(measure(large) / max(1e-9, measure(small)))
    ratio = min(ratios)
    assert ratio < 3.0, (
        f"查询耗时比 {ratio:.2f}（三轮最优；10× 数据不该近似 10× 耗时）"
    )


if __name__ == "__main__":
    from tests._harness import exit_with

    exit_with(globals())
