"""T03 验收 1：``build_plan`` 与 v1 的**逐字段等价**回归。

做法：在测试里独立实现一份 v1 算法的「基线快照」（``_v1_baseline``，与 v1 发布
版本逐行对应），对 5 组样例比对 ``items`` / ``failures`` / ``note``，字段精确到
``(path, target_lcn, clusters, [(vcn, src_lcn, dst_lcn, clusters)])``。
"""

from __future__ import annotations

import os
import sys
from typing import List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.models import Extent, FileEntry, MoveSegment, PlanItem  # noqa: E402
from app.core.planner import PlanParams, build_plan, tail_floor_lcn  # noqa: E402


# --------------------------------------------------------------------------- #
# v1 基线（发布版逐行快照，勿修改）
# --------------------------------------------------------------------------- #
class _V1TailPacker:
    def __init__(self, runs, total_clusters, floor_lcn):
        self._runs = [(max(start, floor_lcn), end) for start, end in runs if end > floor_lcn]
        self._runs = [(s, e) for s, e in self._runs if e - s > 0]
        self._runs.sort()
        self._index = len(self._runs) - 1
        self._cursor = int(total_clusters)
        self._floor = int(floor_lcn)

    def place(self, need):
        if need <= 0:
            return None
        while self._index >= 0:
            run_start, run_end = self._runs[self._index]
            usable_end = min(run_end, self._cursor)
            if usable_end - run_start < need:
                self._index -= 1
                continue
            start = usable_end - need
            if start < self._floor:
                return None
            self._cursor = start
            return start
        return None


def _v1_subtract_mft_zone(runs, mft_start, mft_end):
    if mft_end <= mft_start:
        return list(runs)
    result = []
    for start, end in runs:
        if end <= mft_start or start >= mft_end:
            result.append((start, end))
            continue
        if start < mft_start:
            result.append((start, mft_start))
        if end > mft_end:
            result.append((mft_end, end))
    return result


def _v1_segments(entry: FileEntry, target_lcn: int) -> List[MoveSegment]:
    segments: List[MoveSegment] = []
    cursor = target_lcn
    for extent in entry.allocated_extents:
        segments.append(
            MoveSegment(
                vcn=extent.vcn,
                src_lcn=extent.lcn,
                dst_lcn=cursor,
                clusters=extent.clusters,
            )
        )
        cursor += extent.clusters
    return segments


def _v1_baseline(entries: Sequence[FileEntry], params: PlanParams):
    """返回 ``(note, items, failures)``，items 为 ``(path, target, clusters, segs)``。"""
    total = max(0, int(params.total_clusters))
    reserve = max(0, int(params.reserve_clusters))
    floor = tail_floor_lcn(total, reserve)
    if total <= 0:
        return "卷簇数为 0，无法规划", [], []
    if reserve <= 0:
        return "末端预留区为 0 簇，无法规划", [], []

    runs = _v1_subtract_mft_zone(params.free_runs, params.mft_zone_start, params.mft_zone_end)
    packer = _V1TailPacker(runs, total, floor)
    ordered = sorted(entries, key=lambda e: (-e.allocated_clusters, e.path))

    items: List[Tuple[str, int, int, Tuple]] = []
    failures: List[Tuple[str, str]] = []
    for entry in ordered:
        need = entry.allocated_clusters
        if not entry.movable:
            failures.append((entry.path, entry.reason or "标记为不可移动"))
            continue
        if need <= 0:
            failures.append((entry.path, "文件无物理分配，无需移动"))
            continue
        if need > reserve:
            failures.append(
                (entry.path, f"文件占用 {need:,} 簇，大于末端预留区 {reserve:,} 簇")
            )
            continue
        if all(extent.lcn >= floor for extent in entry.allocated_extents):
            items.append((entry.path, entry.first_lcn, need, ()))
            continue
        target = packer.place(need)
        if target is None:
            failures.append(
                (
                    entry.path,
                    f"末端已无足够连续空间（需要 {need:,} 簇，剩余连续空间不足）",
                )
            )
            continue
        segments = _v1_segments(entry, target)
        items.append(
            (
                entry.path,
                target,
                need,
                tuple((s.vcn, s.src_lcn, s.dst_lcn, s.clusters) for s in segments),
            )
        )
    items.sort(key=lambda item: item[1])
    if not items:
        note = "没有可迁移的文件"
    elif not [i for i in items if i[3]]:
        note = "所选文件已全部位于末端预留区，无需迁移"
    else:
        movable = [i for i in items if i[3]]
        clusters = sum(sum(seg[3] for seg in i[3]) for i in movable)
        note = (
            f"计划搬运 {len(movable)} 个文件 / {clusters:,} 簇，"
            f"目标区 [{floor:,}, {total:,})"
        )
    return note, items, failures


# --------------------------------------------------------------------------- #
# 比较工具
# --------------------------------------------------------------------------- #
def _normalize(plan) -> Tuple:
    items = tuple(
        (
            item.entry.path,
            item.target_lcn,
            item.clusters,
            tuple((s.vcn, s.src_lcn, s.dst_lcn, s.clusters) for s in item.segments),
        )
        for item in plan.items
    )
    failures = tuple((f.entry.path, f.reason) for f in plan.failures)
    return items, failures, plan.note


def _assert_same(entries, params, label: str) -> None:
    expected_note, expected_items, expected_failures = _v1_baseline(entries, params)
    plan = build_plan(entries, params)
    items, failures, note = _normalize(plan)
    assert list(items) == expected_items, (
        f"[{label}] items 不一致 | v1: {expected_items} | v2: {list(items)}"
    )
    assert list(failures) == expected_failures, (
        f"[{label}] failures 不一致 | v1: {expected_failures} | v2: {list(failures)}"
    )
    assert note == expected_note, f"[{label}] note 不一致 | v1: {expected_note} | v2: {note}"
    assert plan.version == 1, f"[{label}] version 应为 1"
    assert plan.tasks == [] and plan.evictions == [], f"[{label}] v1 计划不应有 tasks/evictions"


def _entry(path: str, clusters: int, lcn: int, pieces: int = 1, gap: int = 100, movable=True) -> FileEntry:
    entry = FileEntry(path=path, name=path, size=clusters * 4096, movable=movable)
    per = max(1, clusters // max(1, pieces))
    remaining = clusters
    cursor = lcn
    for index in range(max(1, pieces)):
        take = per if index < pieces - 1 else remaining
        if take <= 0:
            break
        entry.extents.append(
            Extent(vcn=sum(e.clusters for e in entry.extents), lcn=cursor, clusters=take)
        )
        cursor += take + gap
        remaining -= take
    return entry


# --------------------------------------------------------------------------- #
# 5 组样例
# --------------------------------------------------------------------------- #
def test_compat_empty_selection() -> None:
    params = PlanParams(total_clusters=1000, bytes_per_cluster=4096, reserve_clusters=200,
                        free_runs=[(0, 1000)])
    _assert_same([], params, "空集")


def test_compat_single_file() -> None:
    params = PlanParams(total_clusters=1000, bytes_per_cluster=4096, reserve_clusters=300,
                        free_runs=[(300, 1000)])
    _assert_same([_entry("A", 100, 10)], params, "单文件")


def test_compat_already_in_place() -> None:
    params = PlanParams(total_clusters=1000, bytes_per_cluster=4096, reserve_clusters=300,
                        free_runs=[(700, 1000)])
    entries = [_entry("tail", 100, 800), _entry("front", 50, 100)]
    _assert_same(entries, params, "已就位")


def test_compat_exceeds_reserve() -> None:
    params = PlanParams(total_clusters=1000, bytes_per_cluster=4096, reserve_clusters=100,
                        free_runs=[(0, 1000)])
    _assert_same([_entry("huge", 500, 10)], params, "超出预留")


def test_compat_mft_hole() -> None:
    params = PlanParams(
        total_clusters=2000, bytes_per_cluster=4096, reserve_clusters=600,
        free_runs=[(0, 2000)], mft_zone_start=1500, mft_zone_end=1600,
    )
    entries = [_entry("a", 100, 10), _entry("b", 200, 400, pieces=2), _entry("c", 50, 1900)]
    _assert_same(entries, params, "含 MFT 区洞")


def test_compat_multi_fragment_and_ordering() -> None:
    """多碎片 + 大文件优先 + 尾部碎片化，逐字段比对。"""
    params = PlanParams(
        total_clusters=5000, bytes_per_cluster=4096, reserve_clusters=1500,
        free_runs=[(0, 2000), (2600, 3600), (3800, 5000)],
    )
    entries = [
        _entry("x", 300, 10, pieces=3),
        _entry("y", 700, 100),
        _entry("z", 120, 600, pieces=2),
        _entry("w", 60, 50),
    ]
    _assert_same(entries, params, "多碎片")


def test_compat_immovable_and_zero_cluster() -> None:
    params = PlanParams(total_clusters=1000, bytes_per_cluster=4096, reserve_clusters=300,
                        free_runs=[(0, 1000)])
    entries = [
        _entry("locked", 100, 10, movable=False),
        FileEntry(path="empty", name="empty", size=0),
        _entry("ok", 50, 20),
    ]
    entries[0].reason = "被独占"
    _assert_same(entries, params, "不可移动 + 空文件")


def test_compat_new_params_defaults_are_zero() -> None:
    """``PlanParams`` 新增的 4 个字段默认必须是 0（v1 路径不读取它们）。"""
    params = PlanParams()
    assert params.head_reserve_clusters == 0
    assert params.tail_guard_clusters == 0
    assert params.mft_start_lcn == 0
    assert params.mft2_start_lcn == 0
    # 位置参数顺序不变（前 6 个）
    params2 = PlanParams(1000, 4096, 300, [(0, 1000)], 100, 200)
    assert params2.total_clusters == 1000 and params2.mft_zone_end == 200


def test_compat_new_fields_do_not_change_output() -> None:
    """即便把新字段填上，v1 路径输出也必须一字不变。"""
    base = PlanParams(total_clusters=1000, bytes_per_cluster=4096, reserve_clusters=300,
                      free_runs=[(0, 1000)])
    filled = PlanParams(
        total_clusters=1000, bytes_per_cluster=4096, reserve_clusters=300,
        free_runs=[(0, 1000)],
        head_reserve_clusters=64, tail_guard_clusters=64,
        mft_start_lcn=100, mft2_start_lcn=200,
    )
    entries = [_entry("A", 100, 10), _entry("B", 50, 400)]
    assert _normalize(build_plan(entries, base)) == _normalize(build_plan(entries, filled))


if __name__ == "__main__":
    from tests._harness import exit_with

    exit_with(globals())
