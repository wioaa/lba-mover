"""1.0.0C 密实化策略（纯函数、可离线单测，不碰任何磁盘 / 句柄）。

三个策略都围绕「提高密实性、整合碎片空闲」，复用 ``packer`` 里已验证的装箱原语
（``fdd_pack`` / ``SpaceLedger`` / ``build_segments``）：

1. :func:`auto_fill_tail`  —— 全池自动挑选可移动文件填入**末端预留区**的空闲空间，
   尽量填满（大文件优先、连续落位、放不下就跳过），最大化腾出前端连续空间；
2. :func:`compact_range`   —— **选定区间预密实化**：把区间内的可移动文件密集重排，
   可用空间 = 区间空闲 ∪ 这些文件自身将腾出的簇，从而把碎片空闲 coalesce 成连续块；
3. :func:`plan_size_zones` —— **按文件大小分区段**：把区间按尺寸类切成连续区段，
   同尺寸的文件聚在同一段里装箱，空隙更均匀、密实性更高。

约定：本模块只算「目标布局」，不决定搬运顺序 / 中转（那由 ``scheduler`` 负责）；
因此返回的结构与现有 ``MovePlan`` / ``{path: target}`` 一致，日后接 UI / 执行器时
可复用既有依赖排序与 relay 逻辑。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from app.core.models import (
    FileEntry,
    MovePlan,
    PlanFailure,
    PlanItem,
    TargetWindow,
)
from app.core.packer import SpaceLedger, build_segments, fdd_pack
from app.core.planner import PlanParams, tail_floor_lcn
from app.core.ranges import merge_runs, run_length, subtract_runs

Run = Tuple[int, int]


# ---------------------------------------------------------------- 内部工具


def _clip(runs: Sequence[Run], lo: int, hi: int) -> List[Run]:
    """把区间集合裁剪到 ``[lo, hi)`` 并合并。"""
    out: List[Run] = []
    for start, end in runs:
        s, e = max(int(start), lo), min(int(end), hi)
        if e > s:
            out.append((s, e))
    return merge_runs(out)


def _extent_runs(entries: Sequence[FileEntry]) -> List[Run]:
    return [(int(x.lcn), int(x.lcn_end)) for e in entries for x in e.allocated_extents]


def _overlaps_window(entry: FileEntry, lo: int, hi: int) -> bool:
    return any(not (int(x.lcn_end) <= lo or int(x.lcn) >= hi) for x in entry.allocated_extents)


def _split_groups(items: List[FileEntry], k: int) -> List[List[FileEntry]]:
    """把（已按尺寸排好序的）列表切成 ``k`` 个连续的、尽量等大的组。"""
    n = len(items)
    k = max(1, min(k, n))
    size, rem = divmod(n, k)
    groups: List[List[FileEntry]] = []
    idx = 0
    for i in range(k):
        cnt = size + (1 if i < rem else 0)
        groups.append(items[idx:idx + cnt])
        idx += cnt
    return groups


# ---------------------------------------------------------------- 策略①


def auto_fill_tail(entries: Sequence[FileEntry], params: PlanParams) -> MovePlan:
    """全池自动挑选文件填入末端预留区的空闲空间（策略①）。

    与 ``planner.build_plan`` 的区别：后者按**用户勾选**的文件逐个放置、放不下即记
    failure；本函数从**全部**可移动文件里自动挑（大文件优先、连续落位），放不下的
    **跳过**而非报错，目标是尽量把末端空闲填满、最大化腾出前端连续空间。

    Returns:
        :class:`MovePlan`（``version=1``，末端模式产物，结构与 v1 一致，可被同一
        执行器消费）；``note`` 报告填充统计。
    """
    total = max(0, int(params.total_clusters))
    reserve = max(0, int(params.reserve_clusters))
    floor = tail_floor_lcn(total, reserve)

    plan = MovePlan(total_clusters=total, reserve_clusters=reserve, tail_floor_lcn=floor)
    plan.version = 1

    if total <= 0:
        plan.note = "卷簇数为 0，无法规划"
        return plan
    if reserve <= 0:
        plan.note = "末端预留区为 0 簇，无法规划"
        return plan

    # 末端可用空闲 = 卷空闲 − MFT 保留区，再裁到 [floor, total)
    runs = list(params.free_runs)
    if params.mft_zone_end > params.mft_zone_start:
        runs = subtract_runs(runs, [(params.mft_zone_start, params.mft_zone_end)])
    tail_free = _clip(runs, floor, total)
    free_before = run_length(tail_free)

    # 候选：可移动、有物理分配、不超预留区、且并非已整段位于末端（无需搬）
    candidates: List[FileEntry] = []
    too_big = 0
    for entry in entries:
        if not entry.movable:
            plan.failures.append(PlanFailure(entry=entry, reason=entry.reason or "标记为不可移动"))
            continue
        need = entry.allocated_clusters
        if need <= 0:
            continue  # 无物理分配，自动策略忽略
        if need > reserve:
            too_big += 1
            continue
        if all(int(x.lcn) >= floor for x in entry.allocated_extents):
            continue  # 已在末端预留区
        candidates.append(entry)

    ledger = SpaceLedger(tail_free)
    placed, _unplaced = fdd_pack(candidates, ledger, [(floor, total)])
    by_path = {e.path: e for e in candidates}

    moved = 0
    for path, target in placed.items():
        entry = by_path[path]
        segments = build_segments(entry, target)
        item = PlanItem(
            entry=entry,
            target_lcn=target,
            clusters=entry.allocated_clusters,
            segments=segments,
        )
        if not any(seg.src_lcn != seg.dst_lcn for seg in segments):
            item.skipped_reason = "已位于目标位置"
        else:
            moved += 1
        plan.items.append(item)

    plan.items.sort(key=lambda i: i.target_lcn)

    free_after = run_length(ledger.runs())
    filled = free_before - free_after
    pct = (filled * 100 // free_before) if free_before else 0
    skipped = len(candidates) - len(placed)
    extra = f"；另 {skipped} 个因连续空间不足未纳入" if skipped else ""
    if too_big:
        extra += f"，{too_big} 个超过预留区大小"
    plan.note = (
        f"自动填充末端：搬移 {moved} 个文件 / {filled:,} 簇，"
        f"末端空闲 {free_before:,} → {free_after:,} 簇（填充 {pct}%）{extra}"
    )
    return plan


# ---------------------------------------------------------------- 策略②


def compact_range(
    entries: Sequence[FileEntry],
    window: Sequence[int],
    free_runs: Sequence[Run],
    total_clusters: int,
    mft_zone_start: int = 0,
    mft_zone_end: int = 0,
) -> MovePlan:
    """选定区间预密实化（策略②）：把区间内可移动文件密集重排，coalesce 出连续空闲。

    思路：区间内**可移动**文件全部保留、向区间一端密集装箱；可打包空间 = 区间内的
    空闲簇 ∪ 这些文件自身将腾出的簇（它们搬走即空出），再扣除区间内**不可搬**的障碍
    文件所占。搬完后碎片空闲被挤成一个（尽量大的）连续块。

    Args:
        window: 选定区间 ``[start, end)``。
        free_runs: 全卷空闲区间（来自位图）。
        total_clusters: 卷总簇数。
        mft_zone_start/end: MFT 保留区（安全硬约束，密实时同样避开）。

    Returns:
        :class:`MovePlan`（``version=2``，带 ``window``）；``note`` 报告密实效果。
        本函数只算目标布局，搬运顺序 / 中转交由 ``scheduler``。
    """
    lo, hi = int(window[0]), int(window[1])
    plan = MovePlan(
        total_clusters=max(0, int(total_clusters)),
        tail_floor_lcn=lo,
        window=TargetWindow(lo, hi),
    )
    plan.version = 2

    if hi <= lo:
        plan.note = "选定区间为空，无需密实"
        return plan

    to_compact: List[FileEntry] = []
    blockers: List[FileEntry] = []
    for entry in entries:
        if not _overlaps_window(entry, lo, hi):
            continue
        if not entry.movable or entry.allocated_clusters <= 0:
            blockers.append(entry)
            continue
        to_compact.append(entry)

    if not to_compact:
        plan.note = f"区间 [{lo:,}, {hi:,}) 内没有可密实化的可移动文件"
        return plan

    win_free = _clip(free_runs, lo, hi)
    if mft_zone_end > mft_zone_start:
        win_free = subtract_runs(win_free, [(mft_zone_start, mft_zone_end)])

    own = _clip(_extent_runs(to_compact), lo, hi)
    packable = merge_runs(win_free + own)
    blocker_runs = merge_runs(_clip(_extent_runs(blockers), lo, hi))
    packable = subtract_runs(packable, blocker_runs)

    free_before = run_length(win_free)
    ledger = SpaceLedger(packable)
    # avoid_self_overlap=False：密实本就常在区间内重排，允许落到自身将腾出的簇上；
    # 覆盖顺序 / 环由 scheduler 处理，本函数只产出目标布局。
    placed, _unplaced = fdd_pack(to_compact, ledger, [(lo, hi)], avoid_self_overlap=False)
    by_path = {e.path: e for e in to_compact}

    for path, target in placed.items():
        entry = by_path[path]
        segments = build_segments(entry, target)
        item = PlanItem(
            entry=entry,
            target_lcn=target,
            clusters=entry.allocated_clusters,
            segments=segments,
        )
        if not any(seg.src_lcn != seg.dst_lcn for seg in segments):
            item.skipped_reason = "已位于目标位置"
        plan.items.append(item)
    plan.items.sort(key=lambda i: i.target_lcn)

    # 密实后区间内剩余空闲（= 原窗口空闲 − 被装箱吃掉的部分），取最大连续块
    remaining = _clip(ledger.runs(), lo, hi)
    largest = max((e - s for s, e in remaining), default=0)
    moved = sum(1 for i in plan.items if not i.skipped_reason)
    plan.note = (
        f"区间预密实化：[{lo:,}, {hi:,}) 内 {moved} 个文件密集重排，"
        f"空闲 {free_before:,} 簇整合为最大连续块 {largest:,} 簇"
        + (f"（{len(blockers)} 个不可搬障碍保留原位）" if blockers else "")
    )
    return plan


# ---------------------------------------------------------------- 策略③


def plan_size_zones(
    entries: Sequence[FileEntry],
    region: Sequence[int],
    *,
    class_count: int = 3,
) -> Tuple[Dict[str, int], List[FileEntry], List[Run]]:
    """按文件大小把区间切成若干「区段」，同尺寸类聚在一起装箱（策略③）。

    同尺寸的文件在同一段里紧密排列，空隙更均匀，整体密实性高于混杂堆放。
    区段按「需求优先、从大到小」顺序切分：大尺寸类先拿够自己的量，区间超发时
    小类被截断（优先保住大文件——它们腾出的前端连续空间最多）。

    Args:
        region: 目标区间 ``[lo, hi)``（按单个连续 span 处理）。
        class_count: 尺寸类数量（默认 3：大 / 中 / 小）。

    Returns:
        ``({path: target_lcn}, [未能放入的文件], [各区段 (start, end)])``；
        区段按尺寸从大到小排列（索引 0 为最大尺寸类）。
    """
    lo, hi = int(region[0]), int(region[1])
    span = hi - lo
    candidates = [e for e in entries if e.movable and e.allocated_clusters > 0]
    candidates.sort(key=lambda e: (-e.allocated_clusters, e.path))
    if not candidates or span <= 0 or class_count < 1:
        return {}, list(candidates), []

    groups = _split_groups(candidates, class_count)
    totals = [sum(e.allocated_clusters for e in g) for g in groups]

    # 需求优先、顺序切段：大类先要够自己的量；放不下时后续（小）类被截断
    zones: List[Run] = []
    cursor = lo
    for i, tot in enumerate(totals):
        end = hi if i == len(totals) - 1 else min(hi, cursor + tot)
        end = max(cursor, end)
        zones.append((cursor, end))
        cursor = end

    placed: Dict[str, int] = {}
    unplaced: List[FileEntry] = []
    for group, (zs, ze) in zip(groups, zones):
        if ze <= zs:
            unplaced.extend(group)
            continue
        ledger = SpaceLedger([(zs, ze)])
        # avoid_self_overlap=False：本函数只算目标布局，文件当前在哪与目标无关；
        # 覆盖顺序 / 环由 scheduler 处理。
        got, miss = fdd_pack(group, ledger, [(zs, ze)], avoid_self_overlap=False)
        placed.update(got)
        unplaced.extend(entry for entry, _reason in miss)
    return placed, unplaced, zones


# ---------------------------------------------------------------- 卷前半大文件扫描


@dataclass(frozen=True)
class FrontHalfFile:
    """一个在卷前半有占用的大文件——搬到末端即可腾直前部连续空间的候选。"""

    entry: FileEntry
    front_clusters: int   # 位于前半 [0, boundary) 的簇数
    back_clusters: int    # 位于后半的簇数

    @property
    def total_clusters(self) -> int:
        return self.front_clusters + self.back_clusters

    @property
    def mostly_front(self) -> bool:
        """是否主要位于前半（≥ 一半簇在前半）。"""
        return self.front_clusters * 2 >= self.total_clusters


@dataclass
class FrontHalfReport:
    """卷前半大文件扫描结果。"""

    boundary_lcn: int                 # 前半 / 后半分界 = total // 2
    total_clusters: int
    files: List[FrontHalfFile]        # 前半有占用的大文件，按 front_clusters 降序
    front_occupied: int               # 这些文件占用的前半簇总数
    movable_front: int                # 其中可移动文件的前半簇数


def front_half_report(
    entries: Sequence[FileEntry],
    total_clusters: int,
    *,
    min_clusters: int = 0,
) -> FrontHalfReport:
    """卷前半大文件扫描：把扫描到的大文件按「前半卷」归类。

    前半 = ``[0, total_clusters // 2)``。一个文件只要在前半有占用簇即入选
    （它正是「应搬到末端以腾空前部连续空间」的候选）；跨中线的文件按前半占比
    计入 ``front_clusters``。``min_clusters`` 用于过滤小文件。纯函数，可离线单测。
    """
    boundary = max(0, int(total_clusters) // 2)
    files: List[FrontHalfFile] = []
    front_occupied = 0
    movable_front = 0
    for entry in entries:
        if entry.allocated_clusters <= 0 or entry.allocated_clusters < min_clusters:
            continue
        front = 0
        for extent in entry.allocated_extents:
            s = max(int(extent.lcn), 0)
            e = min(int(extent.lcn_end), boundary)
            if e > s:
                front += e - s
        if front <= 0:
            continue
        back = entry.allocated_clusters - front
        files.append(FrontHalfFile(entry=entry, front_clusters=front, back_clusters=back))
        front_occupied += front
        if entry.movable:
            movable_front += front
    files.sort(key=lambda f: (-f.front_clusters, f.entry.path))
    return FrontHalfReport(
        boundary_lcn=boundary,
        total_clusters=int(total_clusters),
        files=files,
        front_occupied=front_occupied,
        movable_front=movable_front,
    )
