"""1.0.0C 密实化策略回归（``app/core/densify.py``，纯函数、零磁盘访问）。

覆盖三个策略：
1. ``auto_fill_tail``  —— 全池自动挑选填满末端空闲；
2. ``compact_range``   —— 选定区间预密实化（碎片空闲 coalesce 成连续块）；
3. ``plan_size_zones`` —— 按文件大小分区段、提高密实性。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.densify import (  # noqa: E402
    auto_fill_tail,
    compact_range,
    plan_size_zones,
)
from app.core.models import Extent, FileEntry  # noqa: E402
from app.core.planner import PlanParams  # noqa: E402
from app.core.ranges import merge_runs, subtract_runs  # noqa: E402

BPC = 4096


def _entry(path: str, runs, *, movable: bool = True, reason: str = "") -> FileEntry:
    extents = []
    vcn = 0
    for lcn, clusters in runs:
        extents.append(Extent(vcn=vcn, lcn=lcn, clusters=clusters))
        vcn += clusters
    return FileEntry(
        path=path,
        name=path.rsplit("\\", 1)[-1],
        size=sum(c for _l, c in runs) * BPC,
        extents=extents,
        movable=movable,
        reason=reason,
    )


def _free(total: int, *occupied) -> list:
    occ = merge_runs([(s, e) for s, e in occupied if e > s])
    return subtract_runs([(0, total)], occ)


def _contiguous(items) -> bool:
    ordered = sorted(items, key=lambda i: i.target_lcn)
    return all(
        a.target_lcn + a.clusters == b.target_lcn for a, b in zip(ordered, ordered[1:])
    )


# --------------------------------------------------------------------------- #
# 策略① auto_fill_tail
# --------------------------------------------------------------------------- #
def test_auto_fill_tail_fills_from_pool() -> None:
    """全池自动挑选：大文件优先，把末端空闲尽量填满。"""
    a = _entry("R:\\a.bin", [(100, 40)])
    b = _entry("R:\\b.bin", [(200, 30)])
    c = _entry("R:\\c.bin", [(300, 20)])
    free = _free(1000, (100, 140), (200, 230), (300, 320))
    params = PlanParams(
        total_clusters=1000, bytes_per_cluster=BPC, reserve_clusters=100,
        free_runs=free, mft_zone_start=0, mft_zone_end=0,
    )
    plan = auto_fill_tail([a, b, c], params)

    assert len(plan.items) == 3, plan.note
    assert _contiguous(plan.items), "自动填充应连续落位"
    assert all(900 <= i.target_lcn and i.target_lcn + i.clusters <= 1000 for i in plan.items)
    assert sum(i.clusters for i in plan.items) == 90
    assert "90 簇" in plan.note and "填充 90%" in plan.note, plan.note


def test_auto_fill_tail_skips_when_full() -> None:
    """放不下的文件跳过而非报错（自动策略的核心区别）。"""
    a = _entry("R:\\a.bin", [(100, 40)])
    b = _entry("R:\\b.bin", [(200, 30)])
    free = _free(1000, (100, 140), (200, 230))
    params = PlanParams(
        total_clusters=1000, bytes_per_cluster=BPC, reserve_clusters=50,
        free_runs=free, mft_zone_start=0, mft_zone_end=0,
    )
    plan = auto_fill_tail([a, b], params)
    assert len(plan.items) == 1, plan.note            # 只装下大的 a(40)
    assert plan.items[0].entry.path == "R:\\a.bin"
    assert "未纳入" in plan.note, plan.note            # b 因空间不足跳过
    assert not plan.failures, "放不下不是 failure"


def test_auto_fill_tail_skips_already_at_tail() -> None:
    """已整段位于末端预留区的文件不再纳入候选。"""
    front = _entry("R:\\front.bin", [(100, 20)])
    at_tail = _entry("R:\\tail.bin", [(920, 10)])
    free = _free(1000, (100, 120), (920, 930))
    params = PlanParams(
        total_clusters=1000, bytes_per_cluster=BPC, reserve_clusters=100,
        free_runs=free, mft_zone_start=0, mft_zone_end=0,
    )
    plan = auto_fill_tail([front, at_tail], params)
    paths = {i.entry.path for i in plan.items}
    assert "R:\\front.bin" in paths
    assert "R:\\tail.bin" not in paths, "已在末端的文件不应再搬"


def test_auto_fill_tail_immovable_is_failure() -> None:
    """不可移动文件进 failures（与 build_plan 一致）。"""
    good = _entry("R:\\g.bin", [(100, 20)])
    bad = _entry("R:\\bad.bin", [(200, 20)], movable=False, reason="被独占打开")
    free = _free(1000, (100, 120), (200, 220))
    params = PlanParams(
        total_clusters=1000, bytes_per_cluster=BPC, reserve_clusters=100,
        free_runs=free, mft_zone_start=0, mft_zone_end=0,
    )
    plan = auto_fill_tail([good, bad], params)
    assert {i.entry.path for i in plan.items} == {"R:\\g.bin"}
    assert [f.entry.path for f in plan.failures] == ["R:\\bad.bin"]
    assert "被独占" in plan.failures[0].reason


def test_auto_fill_tail_too_big_skipped() -> None:
    """超过预留区大小的文件跳过并在 note 说明。"""
    big = _entry("R:\\big.bin", [(100, 120)])   # 120 > reserve 100
    free = _free(1000, (100, 220))
    params = PlanParams(
        total_clusters=1000, bytes_per_cluster=BPC, reserve_clusters=100,
        free_runs=free, mft_zone_start=0, mft_zone_end=0,
    )
    plan = auto_fill_tail([big], params)
    assert plan.items == []
    assert "超过预留区" in plan.note, plan.note


# --------------------------------------------------------------------------- #
# 策略② compact_range
# --------------------------------------------------------------------------- #
def test_compact_range_densifies_and_coalesces() -> None:
    """区间内碎片文件密集重排，空闲 coalesce 成一个更大连续块。"""
    a = _entry("R:\\a.bin", [(110, 20), (150, 10)])   # 区间内碎片
    b = _entry("R:\\b.bin", [(180, 15)])
    free = _free(1000, (110, 130), (150, 160), (180, 195))
    # 密实前：窗口 [100,200) 内最大连续空闲仅 20（碎片：10/20/20/5）
    win_before = [(max(s, 100), min(e, 200)) for s, e in free if min(e, 200) > max(s, 100)]
    largest_before = max((e - s for s, e in win_before), default=0)
    assert largest_before == 20

    plan = compact_range([a, b], (100, 200), free, 1000)

    assert len(plan.items) == 2, plan.note
    ordered = sorted(plan.items, key=lambda i: i.target_lcn)
    assert ordered[0].target_lcn == 100 and ordered[1].target_lcn == 130
    assert _contiguous(plan.items), "密实应连续"
    # 密实后：空闲整合成 55 簇连续块（原 55 簇碎片 → 一整块）
    assert "最大连续块 55" in plan.note, plan.note


def test_compact_range_keeps_immovable_blockers() -> None:
    """区间内不可搬障碍保留原位，可移动文件绕着它密实。"""
    a = _entry("R:\\a.bin", [(110, 20)])
    meta = _entry("R:\\$meta", [(150, 20)], movable=False, reason="NTFS 元数据")
    free = _free(1000, (110, 130), (150, 170))
    plan = compact_range([a, meta], (100, 200), free, 1000)
    moved = {i.entry.path for i in plan.items}
    assert moved == {"R:\\a.bin"}
    assert plan.items[0].target_lcn == 100
    assert "不可搬障碍" in plan.note, plan.note


def test_compact_range_no_movable() -> None:
    """区间内没有可移动文件时给出明确 note。"""
    meta = _entry("R:\\$meta", [(150, 20)], movable=False, reason="NTFS 元数据")
    free = _free(1000, (150, 170))
    plan = compact_range([meta], (100, 200), free, 1000)
    assert plan.items == []
    assert "没有可密实化" in plan.note, plan.note


# --------------------------------------------------------------------------- #
# 策略③ plan_size_zones
# --------------------------------------------------------------------------- #
def test_plan_size_zones_segregates_by_size() -> None:
    """按大小分区段：大 / 中 / 小各自成段，段内紧密排布。"""
    files = [
        _entry("R:\\L1", [(0, 100)]), _entry("R:\\L2", [(0, 90)]), _entry("R:\\L3", [(0, 80)]),
        _entry("R:\\M1", [(0, 50)]), _entry("R:\\M2", [(0, 45)]), _entry("R:\\M3", [(0, 40)]),
        _entry("R:\\S1", [(0, 10)]), _entry("R:\\S2", [(0, 8)]), _entry("R:\\S3", [(0, 5)]),
    ]
    placed, unplaced, zones = plan_size_zones(files, (0, 500), class_count=3)

    assert not unplaced, f"区域足够，应全部放入：{[e.path for e in unplaced]}"
    assert len(placed) == 9
    # 区段按尺寸从大到小：大段 [0,270)、中段 [270,405)、小段 [405,500)
    assert zones == [(0, 270), (270, 405), (405, 500)], zones
    assert all(placed[p] < 270 for p in ("R:\\L1", "R:\\L2", "R:\\L3"))
    assert all(270 <= placed[p] < 405 for p in ("R:\\M1", "R:\\M2", "R:\\M3"))
    assert all(placed[p] >= 405 for p in ("R:\\S1", "R:\\S2", "R:\\S3"))


def test_plan_size_zones_oversubscribed_prioritizes_large() -> None:
    """区域超发时优先保住大文件，小类被截断。"""
    files = [
        _entry("R:\\a", [(0, 40)]), _entry("R:\\b", [(0, 35)]),
        _entry("R:\\c", [(0, 30)]), _entry("R:\\d", [(0, 25)]),
        _entry("R:\\e", [(0, 20)]),
    ]
    placed, unplaced, zones = plan_size_zones(files, (0, 100), class_count=3)
    # 大尺寸类（40,35）优先拿够并放入
    assert "R:\\a" in placed and "R:\\b" in placed
    assert unplaced, "超发应有文件落选"
    assert all(p in placed for p in placed)  # 放入的都必须真的在段内
    for p, t in placed.items():
        assert 0 <= t < 100


def test_plan_size_zones_single_class_is_plain_pack() -> None:
    """class_count=1 时退化为普通大文件优先装箱。"""
    files = [_entry("R:\\a", [(0, 30)]), _entry("R:\\b", [(0, 20)])]
    placed, unplaced, zones = plan_size_zones(files, (0, 100), class_count=1)
    assert zones == [(0, 100)]
    assert placed == {"R:\\a": 0, "R:\\b": 30}
    assert not unplaced


if __name__ == "__main__":
    from tests._harness import exit_with

    exit_with(globals())
