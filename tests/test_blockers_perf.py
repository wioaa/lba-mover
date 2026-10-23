"""1.0.0D 性能回归：挡路者定位（``packer._clip_extent`` 二分化）。

两个用例：

1. **行为不变性**：用测试内嵌的暴力参考实现对拍 —— 优化前后
   ``_clip_extent`` / ``find_blockers`` 的产出必须逐字段一致；
2. **性能**：10 万 extent × 5 万占用区间规模下 ``find_blockers`` 必须在
   500 ms 内完成（旧实现为线性遍历，秒级），并打印实测值。

纯内存运算，不碰任何设备。
"""

from __future__ import annotations

import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.models import Extent, FileEntry, TargetWindow  # noqa: E402
from app.core.ownership import OwnershipIndex  # noqa: E402
from app.core.packer import _clip_extent, find_blockers  # noqa: E402
from app.core.ranges import merge_runs  # noqa: E402


def _brute_clip(extent: Extent, runs, _starts=None):
    """优化前的暴力参考实现（每个 extent 线性遍历全部 runs）。"""
    pieces = []
    for run_start, run_end in runs:
        left = max(extent.lcn, run_start)
        right = min(extent.lcn_end, run_end)
        if right > left:
            pieces.append(
                Extent(vcn=extent.vcn + (left - extent.lcn), lcn=left, clusters=right - left)
            )
    return pieces


def _blockers_signature(blockers):
    return [
        (
            b.path,
            b.clusters_in_window,
            b.fragments_in_window,
            [(e.vcn, e.lcn, e.clusters) for e in b.extents_in_window],
            b.reason,
        )
        for b in blockers
    ]


def test_clip_extent_matches_bruteforce() -> None:
    """随机小规模数据：二分实现与暴力实现对拍必须逐段一致。"""
    rng = random.Random(42)
    for _ in range(300):
        total = rng.randrange(50, 500)
        # 生成 1~20 个互不相交、升序的 run（等价 merge_runs 产物）
        runs = []
        cursor = rng.randrange(0, 20)
        while cursor < total and len(runs) < 20:
            length = rng.randrange(1, 30)
            if rng.random() < 0.5:
                runs.append((cursor, min(total, cursor + length)))
            cursor += length + rng.randrange(0, 15)
        starts = [r[0] for r in runs]
        lcn = rng.randrange(-5, total + 5)
        clusters = rng.randrange(0, 40)
        extent = Extent(vcn=0, lcn=lcn, clusters=clusters)
        assert _clip_extent(extent, runs, starts) == _brute_clip(extent, runs), (
            extent,
            runs,
        )


def test_find_blockers_matches_bruteforce() -> None:
    """``find_blockers`` 整链路对拍：换上暴力裁剪后产出必须一致。"""
    import app.core.packer as packer_mod

    rng = random.Random(7)
    total = 200_000
    entries = []
    for index in range(200):
        start = rng.randrange(0, total - 40)
        entry = FileEntry(
            path=f"R:\\f{index}.bin",
            name=f"f{index}.bin",
            size=16 * 4096,
            extents=[Extent(vcn=0, lcn=start, clusters=16)],
        )
        entries.append(entry)

    selected = {entries[0].path}
    occupied_runs = merge_runs(
        [
            (e.lcn, e.lcn_end)
            for entry in entries[1::2]
            for e in entry.allocated_extents
        ]
    )
    ownership = OwnershipIndex.build(entries)
    by_path = {entry.path: entry for entry in entries}
    window = TargetWindow(0, total // 2)

    fast = find_blockers(window, selected, ownership, occupied_runs, by_path)
    original = packer_mod._clip_extent
    try:
        packer_mod._clip_extent = _brute_clip
        slow = find_blockers(window, selected, ownership, occupied_runs, by_path)
    finally:
        packer_mod._clip_extent = original
    assert _blockers_signature(fast) == _blockers_signature(slow)


def test_find_blockers_scale_is_sublinear() -> None:
    """10 万 extent × 5 万+ 占用区间：``find_blockers`` < 500 ms。

    旧实现 O(E×R)（每个 extent 线性遍历全部占用区间）在这个规模要跑数分钟；
    二分 + 内联后应进入数百毫秒级。场景构造：

    * 10 万个文件条目（各 1 个 extent，交替「仍在占用位图里」/「索引已陈旧」）
      —— 陈旧条目必须被裁剪为空并走廉价路径（这正是二次过滤的本职工作）；
    * 5 万个真实挡路者 + 5 万个额外的独立占用区间（合计占用区间 ≥ 10 万，
      加重二分定位的 R 维度）。

    剖析备注：剩余耗时的主体是「10 万个 Blocker 对象的构造 + 排序」（O(E)
    的固有输出成本），不是被修复的 E×R 裁剪热点。
    """
    rng = random.Random(2024)
    total_clusters = 240_000_000          # ≈ 977 GB @ 4 KiB
    count = 100_000

    entries = []
    cursor = 0
    for index in range(count):
        entry = FileEntry(
            path=f"R:\\big\\file{index:06d}.bin",
            name=f"file{index:06d}.bin",
            size=4 * 4096,
            extents=[Extent(vcn=0, lcn=cursor, clusters=4)],
        )
        entries.append(entry)
        cursor += 8                       # 文件与空隙交错

    # 只有偶数号文件仍在占用位图里（奇数号 = 陈旧索引，裁剪后应被丢弃）
    occupied_runs = merge_runs(
        [
            (e.lcn, e.lcn_end)
            for entry in entries[::2]
            for e in entry.allocated_extents
        ]
    )
    # 再加 5 万个独立占用区间（占用区间总数 ≥ 10 万）
    extra_start = cursor + 1000
    occupied_runs = merge_runs(
        occupied_runs + [(extra_start + i * 16, extra_start + i * 16 + 8) for i in range(50_000)]
    )

    ownership = OwnershipIndex.build(entries)
    by_path = {entry.path: entry for entry in entries}
    window = TargetWindow(0, total_clusters)

    started = time.perf_counter()
    blockers = find_blockers(window, [], ownership, occupied_runs, by_path)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    print(
        f"\n[perf] find_blockers：{count:,} extent × {len(occupied_runs):,} 占用区间 "
        f"→ {len(blockers):,} 个挡路者，耗时 {elapsed_ms:.1f} ms"
    )
    assert elapsed_ms < 500.0, f"find_blockers 耗时 {elapsed_ms:.1f} ms 超过 500 ms 预算"
    # 偶数号（仍在占用）= 挡路者；奇数号（陈旧索引）必须被位图二次过滤掉
    assert len(blockers) == count // 2, (
        f"应恰好 50,000 个挡路者（陈旧索引要被过滤），实际 {len(blockers):,}"
    )


if __name__ == "__main__":
    from tests._harness import exit_with

    exit_with(globals())
