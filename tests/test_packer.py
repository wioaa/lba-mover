"""T03 验收：装箱与驱逐引擎（``packer`` / ``ownership``）。

覆盖设计文档 §6 T03 验收 2–8：INV-1~INV-4、INV-8 的随机化用例，``assess`` 三档，
``fdd_pack`` 确定性与 unplaced 原因，``find_blockers`` 的挡路者清单，
``OwnershipIndex`` 与朴素扫描的一致性 + 复杂度表现。
"""

from __future__ import annotations

import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.models import (  # noqa: E402
    VERDICT_DIRECT,
    VERDICT_EVICT,
    VERDICT_IMPOSSIBLE,
    Extent,
    FileEntry,
    PlanItem,
    TargetWindow,
)
from app.core.ownership import OwnershipIndex  # noqa: E402
from app.core.packer import (  # noqa: E402
    SpaceLedger,
    analyze,
    assess,
    build_evictions,
    build_segments,
    detect_self_overlap,
    fdd_pack,
    find_blockers,
    is_in_place,
    window_from_length,
    window_from_tail,
    window_text,
)
from app.core.ranges import merge_runs  # noqa: E402
from app.core.safety import SafetyPolicy  # noqa: E402


# --------------------------------------------------------------------------- #
# 测试数据构造
# --------------------------------------------------------------------------- #
def make_entry(path: str, clusters: int, lcn: int, pieces: int = 1, gap: int = 100) -> FileEntry:
    """构造一个占 ``clusters`` 簇、可选碎片的文件。"""
    entry = FileEntry(path=path, name=path.rsplit("\\", 1)[-1], size=clusters * 4096)
    per = max(1, clusters // max(1, pieces))
    remaining = clusters
    cursor = lcn
    for index in range(max(1, pieces)):
        take = per if index < pieces - 1 else remaining
        if take <= 0:
            break
        entry.extents.append(Extent(vcn=sum(e.clusters for e in entry.extents), lcn=cursor, clusters=take))
        cursor += take + gap
        remaining -= take
    return entry


def free_runs_from(total: int, occupied: list) -> list:
    """由占用簇列表算出空闲区间。"""
    flags = [False] * total
    for lcn in occupied:
        if 0 <= lcn < total:
            flags[lcn] = True
    runs = []
    start = None
    for lcn, used in enumerate(flags):
        if used:
            if start is not None:
                runs.append((start, lcn))
                start = None
        elif start is None:
            start = lcn
    if start is not None:
        runs.append((start, total))
    return runs


def occupied_from(entries: list) -> list:
    lcns = []
    for entry in entries:
        for extent in entry.allocated_extents:
            lcns.extend(range(extent.lcn, extent.lcn_end))
    return lcns


def _no_forbidden(total: int, bpc: int = 4096) -> SafetyPolicy:
    return SafetyPolicy(total_clusters=total, bytes_per_cluster=bpc)


def _intersects(a: tuple, b: tuple) -> bool:
    return a[0] < b[1] and b[0] < a[1]


# --------------------------------------------------------------------------- #
# SpaceLedger
# --------------------------------------------------------------------------- #
def test_ledger_first_fit() -> None:
    ledger = SpaceLedger([(100, 200), (300, 350)])
    assert ledger.free_clusters() == 150
    assert ledger.allocate(50) == 100          # 段首
    assert ledger.runs() == [(150, 200), (300, 350)]
    assert ledger.allocate(50) == 150          # 同一段剩余里 first-fit
    assert ledger.runs() == [(300, 350)]
    assert ledger.allocate(60) is None         # 只剩 50 簇，放不下 60
    assert ledger.allocate(50) == 300
    assert ledger.free_clusters() == 0


def test_ledger_near_prefers_after() -> None:
    ledger = SpaceLedger([(100, 200), (500, 600)])
    assert ledger.allocate(50, near=400) == 500     # 优先 >= near 的段（段首）
    assert ledger.allocate(50, near=0) == 100       # 没有 >= 0 之后…… 取最低段首
    ledger2 = SpaceLedger([(100, 200)])
    assert ledger2.allocate(50, near=1000) == 150   # 次选 < near 的最高段（段尾对齐）


def test_ledger_take_and_release() -> None:
    ledger = SpaceLedger([(0, 100)])
    assert ledger.take(10, 20) is True
    assert ledger.runs() == [(0, 10), (30, 100)]
    assert ledger.take(11, 5) is False           # 跨界
    ledger.release(10, 20)
    assert ledger.runs() == [(0, 100)]
    ledger.release(10, 20)                       # 重复释放是幂等的
    assert ledger.runs() == [(0, 100)]


def test_ledger_allocate_rejects_bad_input() -> None:
    ledger = SpaceLedger([(0, 100)])
    assert ledger.allocate(0) is None
    assert ledger.allocate(-5) is None
    assert ledger.take(0, 0) is False


# --------------------------------------------------------------------------- #
# fdd_pack：确定性 + unplaced 原因
# --------------------------------------------------------------------------- #
def test_fdd_pack_deterministic() -> None:
    entries = [make_entry(f"F{i}", 10 + i, 0) for i in range(10)]
    allowed = [(1000, 2000)]
    first = fdd_pack(list(entries), SpaceLedger(allowed), allowed)
    second = fdd_pack(list(entries), SpaceLedger(allowed), allowed)
    assert first[0] == second[0]
    assert [(e.path, r) for e, r in first[1]] == [(e.path, r) for e, r in second[1]]


def test_fdd_pack_large_file_reason() -> None:
    allowed = [(1000, 1100)]
    entries = [make_entry("huge.bin", 500, 0)]
    placed, unplaced = fdd_pack(entries, SpaceLedger(allowed), allowed)
    assert placed == {}
    assert len(unplaced) == 1
    entry, reason = unplaced[0]
    assert entry.path == "huge.bin"
    assert "500" in reason and "簇" in reason


def test_fdd_pack_skips_zero_cluster() -> None:
    empty = FileEntry(path="empty", name="empty", size=0)
    placed, unplaced = fdd_pack([empty], SpaceLedger([(0, 100)]), [(0, 100)])
    assert placed == {}
    assert "无物理分配" in unplaced[0][1]


# --------------------------------------------------------------------------- #
# detect_self_overlap 四种形态
# --------------------------------------------------------------------------- #
def test_self_overlap_before_and_after() -> None:
    entry = make_entry("a", 100, 500)
    assert detect_self_overlap(entry, 1000) is False     # 目标完全在源之后
    assert detect_self_overlap(entry, 100) is False      # 目标完全在源之前
    assert detect_self_overlap(entry, 9500) is False


def test_self_overlap_partial() -> None:
    entry = make_entry("a", 100, 500)                    # 源 500..600
    assert detect_self_overlap(entry, 550) is True       # 部分相交
    assert detect_self_overlap(entry, 450) is True


def test_self_overlap_exact_match_is_not_overlap() -> None:
    entry = make_entry("a", 100, 500)
    assert is_in_place(entry, 500) is True
    assert detect_self_overlap(entry, 500) is False      # 完全重合 = 不需移动
    assert is_in_place(entry, 501) is False


def test_build_segments_maps_contiguously() -> None:
    entry = make_entry("a", 100, 500, pieces=2, gap=50)   # 源不连续
    segments = build_segments(entry, 2000)
    assert [s.dst_lcn for s in segments] == [2000, 2000 + segments[0].clusters]
    assert sum(s.clusters for s in segments) == 100


# --------------------------------------------------------------------------- #
# INV-1 / INV-2 / INV-3 / INV-4 / INV-8（随机化 1000 次）
# --------------------------------------------------------------------------- #
def test_invariants_randomized() -> None:
    """1000 次随机区间 / 随机文件集，断言：

    INV-1 目标块两两不相交；INV-2 目标块 ⊆ allowed；INV-3 目标块连续且长度精确；
    INV-4 目标块与禁区无交集（allowed 已扣禁区，故等价）；INV-8 驱逐目标不在区间内、
    不在禁区、不与任何 src/dst 相交。
    """
    rng = random.Random(20261024)
    checked = 0
    for _ in range(1000):
        total = rng.randint(200, 3000)
        # 随机禁区
        head = rng.choice([0, 1, 20, 60])
        tail = rng.choice([0, 1, 20, 60])
        policy = SafetyPolicy(
            total_clusters=total,
            bytes_per_cluster=4096,
            head_reserve_clusters=head,
            tail_reserve_clusters=tail,
            mft_zone_start=0,
            mft_zone_end=0,
            guard_metadata=False,
        )
        forbidden = policy.forbidden_runs()
        # 随机占用：构造若干占用文件
        occupied: list = []
        others: list = []
        for index in range(rng.randint(0, 6)):
            size = rng.randint(1, max(1, total // 6))
            start = rng.randint(0, max(0, total - size))
            if any(l in occupied for l in range(start, start + size)):
                continue
            occupied.extend(range(start, start + size))
            others.append(make_entry(f"other{index}", size, start))
        # 随机选中文件
        selection: list = []
        for index in range(rng.randint(1, 4)):
            size = rng.randint(1, max(1, total // 8))
            start = rng.randint(0, max(0, total - size))
            if any(l in occupied for l in range(start, start + size)):
                continue
            occupied.extend(range(start, start + size))
            selection.append(make_entry(f"sel{index}", size, start))
        if not selection:
            continue

        from app.core.safety import subtract_forbidden as _sub

        # 契约：free_runs 必须是**已扣除禁区**的空闲区间
        free = _sub(free_runs_from(total, occupied), forbidden)
        # 随机区间
        w_start = rng.randint(0, max(0, total - 10))
        w_end = rng.randint(w_start + 1, min(total, w_start + total // 2 + 1))
        window = TargetWindow(w_start, w_end)
        allowed = [
            (max(s, w_start), min(e, w_end))
            for s, e in [(0, total)]
            if True
        ]
        allowed = [(max(0, w_start), min(total, w_end))]
        allowed = [
            (max(s, r[0]), min(e, r[1]))
            for s, e in allowed
            for r in [(s, e)]
        ]
        # 扣禁区
        from app.core.safety import subtract_forbidden

        allowed = subtract_forbidden(allowed, forbidden)
        if not allowed:
            continue

        index_obj = OwnershipIndex.build(others + selection)
        analysis = analyze(
            selection, window, allowed, free, index_obj,
            merge_runs([(a, b) for a, b in _runs_from_lcns(occupied)]),
            (),
            4096,
        )
        if analysis.verdict == VERDICT_IMPOSSIBLE:
            continue
        checked += 1

        # 组装 items（与 build_range_plan 相同的映射规则）
        dst_ranges = []
        src_ranges = []
        for path, target in analysis.placement.items():
            entry = next(e for e in selection if e.path == path)
            segments = build_segments(entry, target)
            dst = [(s.dst_lcn, s.dst_end) for s in segments]
            dst_ranges.extend(dst)
            src_ranges.extend((s.src_lcn, s.src_lcn + s.clusters) for s in segments)
            # INV-3：连续且长度精确
            assert sum(s.clusters for s in segments) == entry.allocated_clusters
            cursor = target
            for seg in segments:
                assert seg.dst_lcn == cursor
                cursor += seg.clusters

        # INV-1
        for i in range(len(dst_ranges)):
            for j in range(i + 1, len(dst_ranges)):
                assert not _intersects(dst_ranges[i], dst_ranges[j]), (i, j, dst_ranges)
        # INV-2 / INV-4
        for dst in dst_ranges:
            assert any(a[0] <= dst[0] and dst[1] <= a[1] for a in allowed), (dst, allowed)
            for run in forbidden:
                assert not _intersects(dst, run)

        # INV-8：驱逐目标
        ledger_out = SpaceLedger(analysis.evict_space)
        evictions, _fails = build_evictions(analysis.blockers, ledger_out, window)
        evict_dst = []
        for item in evictions:
            for seg in item.segments:
                evict_dst.append((seg.dst_lcn, seg.dst_end))
        for dst in evict_dst:
            assert not _intersects(dst, (window.start_lcn, window.end_lcn)), dst
            for run in forbidden:
                assert not _intersects(dst, run)
            for src in src_ranges:
                assert not _intersects(dst, src)
            for other in dst_ranges:
                assert not _intersects(dst, other)
        for i in range(len(evict_dst)):
            for j in range(i + 1, len(evict_dst)):
                assert not _intersects(evict_dst[i], evict_dst[j])

    assert checked > 300, f"随机用例覆盖不足：只验证了 {checked} 组"


def _runs_from_lcns(lcns: list) -> list:
    if not lcns:
        return []
    ordered = sorted(set(lcns))
    runs = []
    start = ordered[0]
    previous = ordered[0]
    for lcn in ordered[1:]:
        if lcn != previous + 1:
            runs.append((start, previous + 1))
            start = lcn
        previous = lcn
    runs.append((start, previous + 1))
    return runs


# --------------------------------------------------------------------------- #
# assess 三档
# --------------------------------------------------------------------------- #
def test_assess_direct_cases() -> None:
    total = 1000
    selection = [make_entry("a", 100, 10), make_entry("b", 50, 200)]
    for occupied in ([], [(0, 100)]):
        occ_lcns = [l for s, e in occupied for l in range(s, e)]
        free = free_runs_from(total, occ_lcns)
        window = TargetWindow(600, 900)
        report = assess(
            selection, window, [(600, 900)], free, None, occupied, (), 4096
        )
        assert report.verdict == VERDICT_DIRECT, report.reasons
        assert report.blockers == []
        assert report.ok is True


def test_assess_needs_eviction() -> None:
    """区间内只剩 10 簇空闲，但腾走两个相邻挡路文件后可放下 150 簇连续块。"""
    total = 1000
    blocker_a = make_entry("D:\\blocker_a.bin", 100, 700)
    blocker_b = make_entry("D:\\blocker_b.bin", 90, 800)
    selection = [make_entry("D:\\sel.bin", 150, 50)]
    occupied = list(range(610, 700))                 # 归属未知（不在索引里）
    occupied.extend(occupied_from([blocker_a, blocker_b, selection[0]]))
    free = free_runs_from(total, occupied)
    window = TargetWindow(600, 900)
    index = OwnershipIndex.build([blocker_a, blocker_b, selection[0]])
    occupied_runs = _runs_from_lcns(occupied)
    report = assess(selection, window, [(600, 900)], free, index, occupied_runs, (), 4096)
    assert report.verdict == VERDICT_EVICT, (report.verdict, report.reasons)
    assert len([b for b in report.blockers if b.movable]) == 2
    assert report.evictable_clusters == 190
    assert report.free_in_window == 20        # [600,610) + [890,900)
    assert report.blocked_clusters == 280
    assert report.unknown_owner_clusters == 90
    assert report.headline.startswith("需驱逐 2 个文件")


def test_assess_impossible_shortfall_exact() -> None:
    """差 1000 簇时 ``shortfall_clusters`` 必须恰好是 1000，且建议里出现「放大到」。"""
    total = 5000
    window = TargetWindow(1000, 2000)      # 可用 1000 簇
    selection = [make_entry("D:\\big.bin", 2000, 10)]
    report = assess(selection, window, [(1000, 2000)], [(0, 5000)], None, [], (), 4096)
    assert report.verdict == VERDICT_IMPOSSIBLE
    assert report.shortfall_clusters == 1000
    assert any("放大到" in s for s in report.suggestions)
    assert any("2,000" in r for r in report.reasons)


def test_assess_impossible_unknown_owner_hint() -> None:
    """区间被未知归属簇占满且索引为空 → 必须提示开启全卷索引。"""
    total = 3000
    occupied = list(range(1000, 2000))
    free = free_runs_from(total, occupied)
    selection = [make_entry("D:\\sel.bin", 500, 10)]
    report = assess(
        selection, TargetWindow(1000, 2000), [(1000, 2000)], free, None,
        _runs_from_lcns(occupied), (), 4096,
    )
    assert report.verdict == VERDICT_IMPOSSIBLE
    assert report.unknown_owner_clusters == 1000
    assert any("全卷归属索引" in s for s in report.suggestions)


def test_assess_impossible_empty_window() -> None:
    report = assess([], TargetWindow(0, 0), [], [], None, [], (), 4096)
    assert report.verdict == VERDICT_IMPOSSIBLE
    assert any("为空" in r for r in report.reasons)


def test_assess_impossible_all_forbidden() -> None:
    policy = SafetyPolicy(
        total_clusters=1000, bytes_per_cluster=4096,
        head_reserve_clusters=500, tail_reserve_clusters=500,
    )
    report = assess(
        [make_entry("a", 10, 10)], TargetWindow(0, 1000), [], [(0, 0)], None,
        policy.forbidden_runs(), (), 4096,
    )
    assert report.verdict == VERDICT_IMPOSSIBLE
    assert any("保留区" in r for r in report.reasons)


# --------------------------------------------------------------------------- #
# find_blockers
# --------------------------------------------------------------------------- #
def test_find_blockers_three_files() -> None:
    total = 4000
    window = TargetWindow(1000, 2000)
    targets = [
        make_entry("D:\\x.bin", 100, 1100),
        make_entry("D:\\y.bin", 60, 1400, pieces=2, gap=20),
        make_entry("D:\\z.bin", 40, 1900),
    ]
    selected = [make_entry("D:\\sel.bin", 200, 10)]
    index = OwnershipIndex.build(targets + selected)
    occupied_lcns = occupied_from(targets + selected)
    blockers = find_blockers(
        window,
        [selected[0].path],
        index,
        _runs_from_lcns(occupied_lcns),
        {e.path: e for e in targets},
    )
    assert len(blockers) == 3
    by_path = {b.path: b for b in blockers}
    assert by_path["D:\\x.bin"].clusters_in_window == 100
    assert by_path["D:\\z.bin"].clusters_in_window == 40
    # y 是两段 30 簇，都在区间内
    assert by_path["D:\\y.bin"].clusters_in_window == 60
    assert by_path["D:\\y.bin"].fragments_in_window == 2
    # 成本：小且连续者优先
    assert blockers[0].path == "D:\\z.bin"
    assert blockers[0].cost == 40


def test_find_blockers_skips_selected_and_stale() -> None:
    total = 4000
    window = TargetWindow(1000, 2000)
    selected = [make_entry("D:\\sel.bin", 100, 1100)]
    index = OwnershipIndex.build(selected + [make_entry("D:\\gone.bin", 100, 1500)])
    # gone.bin 的簇在位图里已经不算是占用（陈旧索引）
    occupied = occupied_from(selected)
    blockers = find_blockers(
        window, [selected[0].path], index, _runs_from_lcns(occupied), {}
    )
    assert blockers == []


def test_find_blockers_marks_metadata_not_movable() -> None:
    total = 4000
    window = TargetWindow(1000, 2000)
    meta = make_entry("D:\\$MFT", 100, 1100)
    index = OwnershipIndex.build([meta])
    blockers = find_blockers(
        window, [], index, _runs_from_lcns(occupied_from([meta])), {meta.path: meta}
    )
    assert len(blockers) == 1
    assert "元数据" in blockers[0].reason
    assert blockers[0].movable is False


def test_find_blockers_excluded_flag() -> None:
    total = 4000
    window = TargetWindow(1000, 2000)
    target = make_entry("D:\\t.bin", 100, 1100)
    index = OwnershipIndex.build([target])
    blockers = find_blockers(
        window, [], index, _runs_from_lcns(occupied_from([target])),
        {target.path: target}, excluded_paths=[target.path],
    )
    assert blockers[0].excluded is True
    assert blockers[0].movable is False


# --------------------------------------------------------------------------- #
# build_evictions
# --------------------------------------------------------------------------- #
def test_build_evictions_prefers_after_window() -> None:
    from app.core.models import Blocker

    blocker = Blocker(
        path="D:\\b.bin", name="b.bin", size=4096 * 10, total_clusters=10,
        clusters_in_window=10,
        extents_in_window=[Extent(vcn=0, lcn=1100, clusters=10)],
        fragments_in_window=1, cost=10.0,
    )
    ledger = SpaceLedger([(0, 1000), (2000, 3000)])
    items, failures = build_evictions([blocker], ledger, TargetWindow(1000, 2000))
    assert not failures
    assert len(items) == 1
    assert items[0].target_lcn == 2000          # 优先放到区间之后
    assert items[0].role == "evict"


def test_build_evictions_failure_reason() -> None:
    from app.core.models import Blocker

    blocker = Blocker(
        path="D:\\b.bin", name="b.bin", size=4096 * 100, total_clusters=100,
        clusters_in_window=100,
        extents_in_window=[Extent(vcn=0, lcn=1100, clusters=100)],
        fragments_in_window=1, cost=100.0,
    )
    ledger = SpaceLedger([(0, 50)])
    items, failures = build_evictions([blocker], ledger, TargetWindow(1000, 2000))
    assert items == []
    assert failures and "无法迁出" in failures[0]


# --------------------------------------------------------------------------- #
# OwnershipIndex
# --------------------------------------------------------------------------- #
def _naive_query(entries, start, end):
    hits = []
    for entry in entries:
        for extent in entry.allocated_extents:
            left = max(extent.lcn, start)
            right = min(extent.lcn_end, end)
            if right > left:
                hits.append((entry.path, left, right))
    return sorted(hits)


def test_ownership_matches_naive() -> None:
    rng = random.Random(4242)
    entries = []
    for index in range(300):
        clusters = rng.randint(1, 40)
        lcn = rng.randint(0, 50_000)
        entries.append(make_entry(f"f{index}", clusters, lcn, pieces=rng.randint(1, 3), gap=500))
    index = OwnershipIndex.build(entries)
    for _ in range(200):
        start = rng.randint(0, 50_000)
        end = start + rng.randint(1, 5_000)
        got = sorted((path, e.lcn, e.lcn_end) for path, e in index.query(start, end))
        assert got == _naive_query(entries, start, end)


def test_ownership_covered_and_paths() -> None:
    entries = [make_entry("a", 100, 1000), make_entry("b", 50, 1100)]
    index = OwnershipIndex.build(entries)
    assert index.covered(1000, 1200) == 150
    assert index.paths_in(1000, 1200) == ["a", "b"]
    assert index.extent_count == 2
    assert index.file_count == 2
    assert index.is_empty() is False
    assert OwnershipIndex.build([]).is_empty() is True


def test_ownership_query_scales_sublinear() -> None:
    """10k 与 100k extent 的单次查询耗时比 < 3（不随 n 线性增长）。"""
    def build(count: int) -> OwnershipIndex:
        rng = random.Random(count)
        entries = []
        for position in range(count):
            entries.append(
                make_entry(f"f{position}", 4, rng.randint(0, 4_000_000) * 8)
            )
        return OwnershipIndex.build(entries)

    def measure(index: OwnershipIndex) -> float:
        best = 1e9
        for _ in range(20):
            start = time.perf_counter()
            for _ in range(50):
                index.query(1_000_000, 1_000_100)
            elapsed = time.perf_counter() - start
            best = min(best, elapsed)
        return best

    small = build(10_000)
    large = build(100_000)
    ratio = measure(large) / max(1e-9, measure(small))
    assert ratio < 3.0, f"查询耗时比 {ratio:.2f} 不满足 < 3"


def test_ownership_empty_query() -> None:
    index = OwnershipIndex.build([])
    assert index.query(0, 100) == []
    assert index.covered(0, 100) == 0


# --------------------------------------------------------------------------- #
# window 工具
# --------------------------------------------------------------------------- #
def test_window_helpers() -> None:
    window = window_from_tail(1000, 200, 50)
    assert (window.start_lcn, window.end_lcn) == (750, 950)
    assert window.clusters == 200
    length = window_from_length(300, 100, 5000)
    assert (length.start_lcn, length.end_lcn) == (300, 400)
    assert window_text(window, 4096).startswith("LCN 750 – 949")
    assert "共" in window_text(window, 4096)
    assert window_text(TargetWindow(0, 0), 4096) == "未设置目标区间"


if __name__ == "__main__":
    from tests._harness import exit_with

    exit_with(globals())
