"""区间装箱引擎（N2 + N4）—— **纯算法层**，无 Qt / 无 ctypes / 无 IO。

四件事：

1. :class:`TargetWindow` 的各种构造与展示（``window_from_tail`` / ``window_text``）；
2. :class:`SpaceLedger` 可分配空间账本（first-fit，确定性，可 release）；
3. :func:`fdd_pack` First-Fit-Decreasing 装箱 + :func:`detect_self_overlap`；
4. :func:`find_blockers` / :func:`build_evictions` 挡路者识别与驱逐子计划；
5. :func:`assess` 三档可行性评估（先算术剪枝，再装箱验证）。

所有区间都是 **半开区间** ``(start, end)``，单位统一为簇。
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from app.core.io_profile import IoProfile
from app.core.models import (
    GB,
    MEDIA_UNKNOWN,
    VERDICT_DIRECT,
    VERDICT_EVICT,
    VERDICT_IMPOSSIBLE,
    Blocker,
    Extent,
    FeasibilityReport,
    FileEntry,
    MoveSegment,
    PlanItem,
    TargetWindow,
    blocker_cost,
    human_bytes,
)
from app.core.ownership import OwnershipIndex
from app.core.ranges import merge_runs, run_length, subtract_runs

Run = Tuple[int, int]


# --------------------------------------------------------------------------- #
# TargetWindow 构造 / 展示
# --------------------------------------------------------------------------- #
def window_from_tail(total_clusters: int, tail_clusters: int, tail_guard: int) -> TargetWindow:
    """「从末端倒数 N 簇」：终点停在卷尾保留区之前。"""
    end = max(0, int(total_clusters) - max(0, int(tail_guard)))
    start = max(0, end - max(0, int(tail_clusters)))
    return TargetWindow(start, end)


def window_from_length(start_lcn: int, clusters: int, total_clusters: int) -> TargetWindow:
    """「从某 LCN 起连续 N 簇」。"""
    start = max(0, int(start_lcn))
    return TargetWindow(start, min(max(0, int(total_clusters)), start + max(0, int(clusters))))


def window_text(window: TargetWindow, bytes_per_cluster: int) -> str:
    """UI 展示：``LCN 1,048,576 – 3,145,727（偏移 4.0 GB – 12.0 GB，共 8.0 GB / 2,097,152 簇）``。"""
    if window.is_empty:
        return "未设置目标区间"
    start_offset = window.start_lcn * max(0, bytes_per_cluster)
    end_offset = window.end_lcn * max(0, bytes_per_cluster)
    size = window.clusters * max(0, bytes_per_cluster)
    return (
        f"LCN {window.start_lcn:,} – {window.end_lcn - 1:,}"
        f"（偏移 {human_bytes(start_offset)} – {human_bytes(end_offset)}，"
        f"共 {human_bytes(size)} / {window.clusters:,} 簇）"
    )


def gb_to_clusters(gigabytes: float, bytes_per_cluster: int) -> int:
    """GB → 簇（向上取整）。"""
    if bytes_per_cluster <= 0:
        return 0
    return int(max(0.0, float(gigabytes)) * GB // bytes_per_cluster)


# --------------------------------------------------------------------------- #
# 空间账本
# --------------------------------------------------------------------------- #
class SpaceLedger:
    """可分配空间账本：纯内存、确定性、支持 release（供 relay / 试算回滚）。

    ``runs`` 始终保持升序、互不相交。
    """

    def __init__(self, runs: Sequence[Run]) -> None:
        self._runs: List[Run] = merge_runs([(int(s), int(e)) for s, e in runs if e > s])

    # ---------------- 查询 ---------------- #
    def runs(self) -> List[Run]:
        return list(self._runs)

    def free_clusters(self) -> int:
        return sum(e - s for s, e in self._runs)

    def largest_free(self) -> int:
        return max((e - s for s, e in self._runs), default=0)

    # ---------------- 分配 ---------------- #
    def allocate(self, clusters: int, near: Optional[int] = None) -> Optional[int]:
        """first-fit 分配 ``clusters`` 个**连续**簇，返回起始 LCN；失败返回 ``None``。

        Args:
            clusters: 需要的连续簇数。
            near: 不为 ``None`` 时按「优先 ≥ near 的最低段（段首）→ 次选 < near 的
                最高段（段尾对齐）」挑选，用于把驱逐目标优先放到区间之后。
        """
        if clusters <= 0:
            return None

        if near is None:
            for index, (start, end) in enumerate(self._runs):
                if end - start >= clusters:
                    self._consume(index, start, start + clusters)
                    return start
            return None

        # 1) 优先「起点 >= near 的最低段」，按段首落位
        for index, (start, end) in enumerate(self._runs):
            if start >= near and end - start >= clusters:
                self._consume(index, start, start + clusters)
                return start
        # 2) 次选「终点 <= near 的最高段」，按段尾对齐（尽量贴近 near）
        best: Optional[Tuple[int, int]] = None
        for index, (start, end) in enumerate(self._runs):
            if end <= near and end - start >= clusters:
                if best is None or end > best[1]:
                    best = (index, end)
        if best is not None:
            index, end = best
            self._consume(index, end - clusters, end)
            return end - clusters
        # 3) 跨 near 的段（near 落在段内部）：按段首落位
        for index, (start, end) in enumerate(self._runs):
            if start < near < end and end - start >= clusters:
                self._consume(index, start, start + clusters)
                return start
        return None

    def take(self, start: int, clusters: int) -> bool:
        """在**指定起点**占用（打包时"先选位置再提交"用）。"""
        if clusters <= 0:
            return False
        end = start + clusters
        for index, (run_start, run_end) in enumerate(self._runs):
            if run_start <= start and end <= run_end:
                self._consume(index, start, end)
                return True
        return False

    def release(self, start: int, clusters: int) -> None:
        """归还一段空间（幂等、自动合并）。"""
        if clusters <= 0:
            return
        self._runs = merge_runs(self._runs + [(int(start), int(start) + int(clusters))])

    def _consume(self, index: int, start: int, end: int) -> None:
        run_start, run_end = self._runs[index]
        replacement: List[Run] = []
        if start > run_start:
            replacement.append((run_start, start))
        if end < run_end:
            replacement.append((end, run_end))
        self._runs[index : index + 1] = replacement

    def __len__(self) -> int:
        return len(self._runs)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"SpaceLedger(runs={len(self._runs)}, free={self.free_clusters()})"


# --------------------------------------------------------------------------- #
# 自重叠 / 段构造
# --------------------------------------------------------------------------- #
def build_segments(entry: FileEntry, target_lcn: int) -> List[MoveSegment]:
    """把文件的各 extent 依序映射到目标连续区间（v1 逻辑，逐字节保持不变）。"""
    segments: List[MoveSegment] = []
    cursor = int(target_lcn)
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


def is_in_place(entry: FileEntry, target_lcn: int) -> bool:
    """文件是否已经**恰好**占据目标块（无需搬运）。"""
    extents = entry.allocated_extents
    if not extents:
        return False
    cursor = int(target_lcn)
    for extent in extents:
        if extent.lcn != cursor:
            return False
        cursor += extent.clusters
    return True


def detect_self_overlap(entry: FileEntry, target_lcn: int) -> bool:
    """目标块与该文件**当前** src 是否相交（完全重合视为"已在原位"，不算重叠）。"""
    total = entry.allocated_clusters
    if total <= 0:
        return False
    if is_in_place(entry, target_lcn):
        return False
    end = int(target_lcn) + total
    return any(extent.overlaps(target_lcn, end) for extent in entry.allocated_extents)


def _runs_of(entry: FileEntry) -> List[Run]:
    return [(e.lcn, e.lcn_end) for e in entry.allocated_extents]


# --------------------------------------------------------------------------- #
# FDD 装箱
# --------------------------------------------------------------------------- #
def fdd_pack(
    entries: Sequence[FileEntry],
    ledger: SpaceLedger,
    allowed: Sequence[Run],
    *,
    avoid_self_overlap: bool = True,
) -> Tuple[Dict[str, int], List[Tuple[FileEntry, str]]]:
    """按「簇数降序、路径升序」为每个文件寻找**连续**目标块。

    Returns:
        ``(path -> target_lcn, [(未能放置的文件, 中文原因)])``
    """
    order = sorted(entries, key=lambda e: (-e.allocated_clusters, e.path.lower()))
    placed: Dict[str, int] = {}
    unplaced: List[Tuple[FileEntry, str]] = []
    allowed_runs = list(allowed)

    for entry in order:
        need = entry.allocated_clusters
        if need <= 0:
            unplaced.append((entry, "无物理分配，无需搬运"))
            continue
        target = _place_one(entry, need, ledger, allowed_runs, avoid_self_overlap)
        if target is None:
            unplaced.append(
                (entry, f"剩余连续空间不足（需 {need:,} 簇，最大连续空闲 {ledger.largest_free():,} 簇）")
            )
            continue
        placed[entry.path] = target
    return placed, unplaced


def _place_one(
    entry: FileEntry,
    need: int,
    ledger: SpaceLedger,
    allowed: Sequence[Run],
    avoid_self_overlap: bool,
) -> Optional[int]:
    """为单个文件挑落点（a 段首 → b 自身 extent 之后的段首 → c 段尾对齐）。

    ★ **返回值语义（FIX-4）**

    * 优先返回一个**不与自身 src 重叠**的落点；
    * 若该文件在区间内**所有**可放位置都与自身 src 重叠，则返回**第一个满足
      范围/允许区约束的落点**（即使自重叠），由上层
      （:func:`app.core.planner.build_range_plan`）识别 ``detect_self_overlap``
      后生成 ``role="relay"`` 的两阶段中转计划；
    * 只有连"能放下的位置"都不存在时才返回 ``None``。

    早期实现的 ``fallback`` 分支是**死代码**：``run_end - need`` 永远落在该 run
    内、``ledger.take`` 必成功，而自重叠检查在 ``take`` 之前就 ``continue`` 了，
    于是 ``fallback`` 永远不会被赋值 → 返回 ``None`` → 上层报"即使腾走全部
    可驱逐簇，仍差 N 簇（impossible）"，而不是 relay 中转。设计 §459 明确要求
    这里应当让上层标 relay。
    """
    own_runs = _runs_of(entry)
    fallback: Optional[int] = None

    for run_start, run_end in ledger.runs():
        if run_end - run_start < need:
            continue
        candidates: List[int] = [run_start]
        # b) 紧贴该文件自身最大 extent 末尾之后
        if own_runs and avoid_self_overlap:
            widest = max(own_runs, key=lambda r: r[1] - r[0])
            candidates.append(widest[1])
        # c) 段尾对齐（目标块贴着段尾）
        candidates.append(run_end - need)

        for candidate in candidates:
            if candidate < run_start or candidate + need > run_end:
                continue
            if not _within_allowed(allowed, candidate, need):
                continue
            if avoid_self_overlap and _overlaps_any(own_runs, candidate, candidate + need):
                # 自重叠：记下"能放得下的位置"，等整轮都没找到干净落点时再用
                if fallback is None:
                    fallback = candidate
                continue
            if ledger.take(candidate, need):
                return candidate
            if fallback is None:
                fallback = candidate

    # 没有任何"避开自身"的落点，但存在可放位置：交给上层标记 relay
    if fallback is not None and ledger.take(fallback, need):
        return fallback
    return None


def _overlaps_any(runs: Sequence[Run], start: int, end: int) -> bool:
    for run_start, run_end in runs:
        if run_start < end and start < run_end:
            return True
    return False


def _within_allowed(allowed: Sequence[Run], start: int, clusters: int) -> bool:
    """``[start, start+clusters)`` 是否完整落在某个 allowed 段内。"""
    end = start + clusters
    for run_start, run_end in allowed:
        if run_start <= start and end <= run_end:
            return True
    return False


def place_with_relay(
    entry: FileEntry,
    target_lcn: int,
    relay_ledger: SpaceLedger,
) -> Tuple[List[MoveSegment], str]:
    """目标块与自身 src 相交时，生成「src → relay → dst」两阶段段列表。

    Returns:
        ``(segments, error)``；``error`` 非空表示中转空间不足。
    """
    need = entry.allocated_clusters
    relay = relay_ledger.allocate(need)
    if relay is None:
        return [], f"缺少 {need:,} 簇中转空间"
    first = build_segments(entry, relay)
    # 第二段：从中转位置搬回最终目标（VCN 连续，src=relay）
    second: List[MoveSegment] = []
    cursor = int(target_lcn)
    for extent in first:
        second.append(
            MoveSegment(
                vcn=extent.vcn,
                src_lcn=extent.dst_lcn,
                dst_lcn=cursor,
                clusters=extent.clusters,
            )
        )
        cursor += extent.clusters
    return first + second, ""


# --------------------------------------------------------------------------- #
# 挡路者识别（N4）
# --------------------------------------------------------------------------- #
def find_blockers(
    window: TargetWindow,
    selected_paths: Sequence[str],
    ownership: Optional[OwnershipIndex],
    occupied_runs: Sequence[Run],
    entries_by_path: Dict[str, FileEntry],
    excluded_paths: Sequence[str] = (),
) -> List[Blocker]:
    """找出目标区间内「不属于选中集合」的占用文件。

    用位图占用区间做**二次过滤**：索引可能是旧的（文件已删除/已移动），
    只有真正仍被占用的簇才算挡路者，避免拿陈旧索引生成幽灵驱逐任务。
    """
    if ownership is None or window.clusters <= 0:
        return []

    selected = set(selected_paths)
    excluded = set(excluded_paths)
    occupied = merge_runs([(int(s), int(e)) for s, e in occupied_runs if e > s])
    # 1.0.0D 性能：run 起点列表只算一次，供 `_clip_extent` 做二分定位
    occupied_starts = [run[0] for run in occupied]

    buckets: Dict[str, List[Extent]] = {}
    # 1.0.0D：把 `_clip_extent` 的二分裁剪**内联**进主循环 —— 10 万 extent 级别时
    # 每.extent 一次函数调用的开销在剖析里是可测的；算法不变，只是少一层调用。
    extent_cls = Extent
    for path, extent in ownership.query(window.start_lcn, window.end_lcn):
        if path in selected:
            continue
        lcn = extent.lcn
        lcn_end = extent.lcn_end
        # 候选占用区间是一段连续下标：
        #   first = 最后一个 run_start <= lcn 的下标（可能包含 extent 起点）
        #   last  = 最后一个 run_start <  lcn_end 的下标
        first = bisect.bisect_right(occupied_starts, lcn) - 1
        if first < 0:
            first = 0
        last = bisect.bisect_left(occupied_starts, lcn_end) - 1
        pieces: Optional[List[Extent]] = None
        for index in range(first, last + 1):
            run_start, run_end = occupied[index]
            left = lcn if lcn > run_start else run_start
            right = run_end if run_end < lcn_end else lcn_end
            if right > left:
                piece = extent_cls(
                    vcn=extent.vcn + (left - lcn), lcn=left, clusters=right - left
                )
                if pieces is None:
                    pieces = [piece]
                else:
                    pieces.append(piece)
        if pieces is None:
            continue
        bucket = buckets.get(path)
        if bucket is None:
            buckets[path] = pieces
        else:
            bucket.extend(pieces)

    blockers: List[Blocker] = []
    for path, extents in buckets.items():
        clusters = sum(e.clusters for e in extents)
        if clusters <= 0:
            continue
        entry = entries_by_path.get(path)
        fragments = len(extents)
        reason = ""
        name = entry.name if entry is not None else path.rsplit("\\", 1)[-1]
        if name.startswith("$"):
            reason = "NTFS 元数据文件，禁止搬运"
        elif entry is None:
            reason = "缺少文件信息（索引不完整），无法安全迁出"
        elif entry.is_compressed:
            reason = "NTFS 压缩文件，无法按需搬移（只能在压缩单元边界移动）"
        elif entry.is_encrypted:
            reason = "EFS 加密文件，FSCTL_MOVE_FILE 不支持"
        elif not entry.movable:
            reason = entry.reason or "标记为不可移动"
        blockers.append(
            Blocker(
                path=path,
                name=name,
                size=entry.size if entry is not None else clusters * 4096,
                total_clusters=entry.allocated_clusters if entry is not None else clusters,
                clusters_in_window=clusters,
                extents_in_window=extents,
                fragments_in_window=fragments,
                cost=blocker_cost(clusters, fragments),
                excluded=path in excluded,
                reason=reason,
            )
        )
    blockers.sort(key=lambda b: (b.cost, b.path.lower()))
    return blockers


def _clip_extent(
    extent: Extent,
    runs: Sequence[Run],
    starts: Optional[Sequence[int]] = None,
) -> List[Extent]:
    """把 extent 裁剪到给定区间集合上（可能产生多段）。

    ``runs`` 必须是 :func:`merge_runs` 的产物（升序、互不相交）——
    1.0.0D 起：用 ``bisect`` 按 run 起点定位首个可能相交的 run，向后推进到
    不相交为止。复杂度 O(E×R) → O(E log R + 命中数)；规划 10 万 extent ×
    5 万占用区间时这是 :func:`find_blockers` 的绝对热点。
    ``starts`` 是 ``[r[0] for r in runs]``，由调用方（``find_blockers``）
    预计算一次复用；不给则内部现算（O(R)，仅小规模场景这么用）。
    """
    pieces: List[Extent] = []
    if not runs:
        return pieces
    if starts is None:
        starts = [run[0] for run in runs]

    # 候选区间是一段连续下标：
    #   first = 最后一个 run_start <= extent.lcn 的下标（它可能包含 extent 起点）
    #   last  = 最后一个 run_start <  extent.lcn_end 的下标
    first = bisect.bisect_right(starts, extent.lcn) - 1
    if first < 0:
        first = 0
    last = bisect.bisect_left(starts, extent.lcn_end) - 1
    for index in range(first, last + 1):
        run_start, run_end = runs[index]
        left = max(extent.lcn, run_start)
        right = min(extent.lcn_end, run_end)
        if right > left:
            pieces.append(
                Extent(
                    vcn=extent.vcn + (left - extent.lcn),
                    lcn=left,
                    clusters=right - left,
                )
            )
    return pieces


def build_evictions(
    blockers: Sequence[Blocker],
    ledger_out: SpaceLedger,
    window: TargetWindow,
) -> Tuple[List[PlanItem], List[str]]:
    """按成本升序为每个挡路切片分配**区间外**的连续目标。"""
    failures: List[str] = []
    items: List[PlanItem] = []
    for blocker in sorted(blockers, key=lambda b: (b.cost, b.path.lower())):
        if blocker.excluded:
            continue
        if blocker.reason:
            failures.append(f"{blocker.name}：{blocker.reason}")
            continue
        entry = FileEntry(
            path=blocker.path,
            name=blocker.name,
            size=blocker.size,
            movable=True,
        )
        for extent in blocker.extents_in_window:
            target = ledger_out.allocate(extent.clusters, near=window.end_lcn)
            if target is None:
                failures.append(
                    f"{blocker.name}：区间外无 {extent.clusters:,} 簇连续空闲，无法迁出"
                )
                break
            items.append(
                PlanItem(
                    entry=entry,
                    target_lcn=target,
                    clusters=extent.clusters,
                    segments=[
                        MoveSegment(
                            vcn=extent.vcn,
                            src_lcn=extent.lcn,
                            dst_lcn=target,
                            clusters=extent.clusters,
                        )
                    ],
                    role="evict",
                )
            )
    return items, failures


# --------------------------------------------------------------------------- #
# 可行性评估
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Analysis:
    """内部评估结果（``assess`` 与 ``build_range_plan`` 共用，只算一次）。"""

    verdict: str = VERDICT_IMPOSSIBLE
    window: TargetWindow = field(default_factory=TargetWindow)
    allowed: List[Run] = field(default_factory=list)
    usable_clusters: int = 0
    selected_clusters: int = 0
    free_in_window: int = 0
    blocked_clusters: int = 0
    evictable_clusters: int = 0
    unknown_owner_clusters: int = 0
    #: 选中文件**自身**占用的簇（第二轮可复用：它们搬走之后就空出来了）
    reclaimable_clusters: int = 0
    largest_file_clusters: int = 0
    shortfall_clusters: int = 0
    placement: Dict[str, int] = field(default_factory=dict)
    unplaced: List[Tuple[FileEntry, str]] = field(default_factory=list)
    blockers: List[Blocker] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)
    evict_space: List[Run] = field(default_factory=list)


def analyze(
    entries: Sequence[FileEntry],
    window: TargetWindow,
    allowed: Sequence[Run],
    free_runs: Sequence[Run],
    ownership: Optional[OwnershipIndex],
    bitmap_occupied: Sequence[Run],
    excluded_paths: Sequence[str] = (),
    bytes_per_cluster: int = 4096,
) -> Analysis:
    """三档可行性评估（先算术剪枝，再装箱验证）。

    Args:
        entries: 选中文件（已归一化：剔除不可移动项由调用方负责）。
        window: 目标区间（已裁剪到卷内）。
        allowed: 区间扣除禁区后的**全部**簇范围（含被占用部分）。
        free_runs: 全卷空闲区间（已扣除禁区）。
        ownership: 归属索引；``None`` 表示归属完全未知。
        bitmap_occupied: 全卷占用区间（用于计算归属未知簇）。
        excluded_paths: 用户排除的挡路者（不参与驱逐，但仍算挡路）。
    """
    result = Analysis(window=window)
    result.allowed = list(allowed)
    result.usable_clusters = run_length(allowed)
    result.selected_clusters = sum(e.allocated_clusters for e in entries)
    result.largest_file_clusters = max((e.allocated_clusters for e in entries), default=0)

    free_in_window = window.intersect(free_runs)
    result.free_in_window = run_length(free_in_window)
    result.blocked_clusters = max(0, window.clusters - result.free_in_window)

    if window.clusters <= 0:
        result.reasons.append("目标区间为空")
        result.suggestions.append("请先设置目标区间（末端倒数 N GB 或绝对 LCN 区间）")
        return result
    if result.usable_clusters <= 0:
        result.reasons.append("目标区间全部落在安全保留区内（卷首 / 卷尾 / MFT）")
        result.suggestions.append("把区间移出保留区，或先调小卷首 / 卷尾保留区")
        return result

    # ---- 算术剪枝 ----
    if result.largest_file_clusters > result.usable_clusters:
        gap = result.largest_file_clusters - result.usable_clusters
        result.shortfall_clusters = gap
        biggest = max(entries, key=lambda e: e.allocated_clusters)
        result.reasons.append(
            f"{biggest.name} 需 {biggest.allocated_clusters:,} 簇，"
            f"大于区间可用 {result.usable_clusters:,} 簇（差 {gap:,} 簇）"
        )
        result.suggestions.append(
            f"把区间放大到至少 {result.largest_file_clusters:,} 簇"
            f"（≈ {human_bytes(result.largest_file_clusters * max(1, bytes_per_cluster))} 以上）"
        )
        return result
    if result.selected_clusters > result.usable_clusters:
        gap = result.selected_clusters - result.usable_clusters
        result.shortfall_clusters = gap
        result.reasons.append(
            f"选中文件共需 {result.selected_clusters:,} 簇，"
            f"区间可用仅 {result.usable_clusters:,} 簇（差 {gap:,} 簇）"
        )
        result.suggestions.append("把区间放大到至少 " + f"{result.selected_clusters:,} 簇")
        return result

    # ---- 第一轮：只用区间内当前空闲簇 ----
    ledger1 = SpaceLedger(free_in_window)
    placed1, unplaced1 = fdd_pack(entries, ledger1, allowed)
    if not unplaced1:
        result.verdict = VERDICT_DIRECT
        result.placement = placed1
        result.unplaced = []
        return result

    # ---- 挡路者识别 + 归属未知统计 ----
    entries_by_path: Dict[str, FileEntry] = dict(ownership.by_path) if ownership is not None else {}
    entries_by_path.update({e.path: e for e in entries})
    blockers = find_blockers(
        window,
        [e.path for e in entries],
        ownership,
        bitmap_occupied,
        entries_by_path,
        excluded_paths,
    )
    result.blockers = blockers
    covered = ownership.covered(window.start_lcn, window.end_lcn) if ownership is not None else 0
    result.unknown_owner_clusters = max(0, result.blocked_clusters - covered)
    evict_runs: List[Run] = []
    for blocker in blockers:
        if blocker.movable:
            evict_runs.extend((e.lcn, e.lcn_end) for e in blocker.extents_in_window)
    evict_runs = merge_runs(evict_runs)
    result.evictable_clusters = run_length(evict_runs)

    # ---- 第二轮：虚拟腾空（空闲 ∪ 将被驱逐 ∪ 选中文件自身将腾出）----
    # ★ FIX-4 集成：自重叠文件的唯一落点是「区间外中转」，而中转空间来自区间外，
    # 同时选中文件搬走后其在窗口内的簇也会空出。这两笔账若不计入，自重叠/环场景
    # 会被第 622 行误判成 impossible（"即使腾走全部可驱逐簇，仍差 N 簇"），
    # 实际应当交给 build_range_plan 的 relay 两阶段处理。见 tests/test_relay.py。
    selected_in_window: List[Run] = []
    for entry in entries:
        for extent in entry.allocated_extents:
            s = max(int(extent.lcn), window.start_lcn)
            e = min(int(extent.lcn_end), window.end_lcn)
            if e > s:
                selected_in_window.append((s, e))
    selected_in_window = merge_runs(selected_in_window)
    virtual_free = merge_runs(
        free_in_window + window.intersect(evict_runs) + selected_in_window
    )
    ledger2 = SpaceLedger(virtual_free)
    placed2, unplaced2 = fdd_pack(entries, ledger2, allowed)

    if unplaced2:
        shortfall = sum(e.allocated_clusters for e, _reason in unplaced2)
        result.shortfall_clusters = shortfall
        result.unplaced = unplaced2
        result.reasons.append(
            f"即使腾走全部可驱逐簇，仍差 {shortfall:,} 簇（{len(unplaced2)} 个文件放不下）"
        )
        if result.unknown_owner_clusters > 0:
            result.reasons.append(
                f"其中 {result.unknown_owner_clusters:,} 簇归属未知"
                "（可能是未扫描的小文件 / 目录 / 元数据）"
            )
            result.suggestions.append(
                "开启「全卷归属索引」（MFT 通道，一次遍历即完成）后重新评估"
            )
        result.suggestions.append(
            f"或把区间放大到至少 {result.selected_clusters + result.blocked_clusters:,} 簇"
        )
        return result

    # ---- 驱逐目标是否放得下（区间外空闲）----
    # 区间外空间 = 全卷空闲 − 区间（**按区间裁剪，不能整段丢弃**）
    evict_space = subtract_runs(free_runs, [(window.start_lcn, window.end_lcn)])
    result.evict_space = evict_space
    ledger_out = SpaceLedger(evict_space)
    evictions, evict_failures = build_evictions(blockers, ledger_out, window)
    if evict_failures:
        gap = sum(
            b.clusters_in_window
            for b in blockers
            if b.movable and not any(i.entry.path == b.path for i in evictions)
        )
        result.shortfall_clusters = max(0, gap)
        result.unplaced = unplaced2
        result.reasons.append(
            f"区间外连续空闲不足，还有 {len(evict_failures)} 个挡路文件无法迁出"
        )
        result.reasons.extend(evict_failures[:5])
        result.suggestions.append("把区间改小一些，或先手动清理区间外的碎片空间")
        return result

    result.verdict = VERDICT_EVICT
    result.placement = placed2
    result.unplaced = []
    return result


def to_report(analysis: Analysis) -> FeasibilityReport:
    """把内部 :class:`Analysis` 转成 UI 可直接绑定的报告。"""
    report = FeasibilityReport(
        verdict=analysis.verdict,
        window_clusters=analysis.window.clusters,
        usable_clusters=analysis.usable_clusters,
        selected_clusters=analysis.selected_clusters,
        free_in_window=analysis.free_in_window,
        blocked_clusters=analysis.blocked_clusters,
        evictable_clusters=analysis.evictable_clusters,
        unknown_owner_clusters=analysis.unknown_owner_clusters,
        largest_file_clusters=analysis.largest_file_clusters,
        shortfall_clusters=analysis.shortfall_clusters,
        blockers=list(analysis.blockers),
        reasons=list(analysis.reasons),
        suggestions=list(analysis.suggestions),
    )
    return report


def assess(
    entries: Sequence[FileEntry],
    window: TargetWindow,
    allowed: Sequence[Run],
    free_runs: Sequence[Run],
    ownership: Optional[OwnershipIndex],
    bitmap_occupied: Sequence[Run],
    excluded_paths: Sequence[str] = (),
    bytes_per_cluster: int = 4096,
) -> FeasibilityReport:
    """可行性的公开入口（返回 :class:`FeasibilityReport`）。"""
    analysis = analyze(
        entries,
        window,
        allowed,
        free_runs,
        ownership,
        bitmap_occupied,
        excluded_paths,
        bytes_per_cluster,
    )
    return to_report(analysis)


def default_io_profile(
    media: str = MEDIA_UNKNOWN,
    buffer_mb: int = 256,
    bytes_per_cluster: int = 4096,
) -> IoProfile:
    """便捷构造（planner 在调用方未提供 IoProfile 时使用）。"""
    return IoProfile.build(media, buffer_mb, bytes_per_cluster)
