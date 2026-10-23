"""迁移规划（**纯函数**，无任何副作用，可直接单元测试）。

两个入口：

* :func:`build_plan` —— **v1 末端模式**，语义与输出逐字段保持不变（回归硬指标）；
* :func:`build_range_plan` —— v2 主入口，目标区间模式：内部 = ``packer``（装箱 /
  挡路者 / 驱逐）+ ``scheduler``（依赖排序 / 环断裂）的编排。

v1 策略：从卷尾向前（LCN 递减）放置文件，每个文件尽量落在**连续**空闲区间内；
文件之间不重叠；跳过被占用簇与 MFT 保留区。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from app.core.guard import SafetyGuard  # noqa: F401  (对外复用，保持单一导出点)
from app.core.io_profile import IoProfile
from app.core.models import (
    MEDIA_UNKNOWN,
    VERDICT_DIRECT,
    VERDICT_EVICT,
    VERDICT_IMPOSSIBLE,
    FileEntry,
    MovePlan,
    MoveSegment,
    PlanFailure,
    PlanItem,
    PlanStats,
    TargetWindow,
    blocker_cost,
    human_bytes,
)
from app.core.ownership import OwnershipIndex
from app.core.packer import (
    SpaceLedger,
    analyze,
    build_evictions,
    build_segments,
    detect_self_overlap,
    is_in_place,
    place_with_relay,
    to_report,
    window_from_tail,
)
from app.core.ranges import merge_runs, run_length
from app.core.safety import SafetyPolicy, SafetyViolation, assert_target_allowed, subtract_forbidden
from app.core.scheduler import (
    MAX_RELAY_ROUNDS,
    break_cycles,
    build_dependencies,
    build_tasks,
    merge_task_segments_for_execution,
    order_elevator,
)

# 预留区最小放大系数（用户默认值 = 选中总量 * 1.2）
DEFAULT_RESERVE_FACTOR = 1.2

# 预留区至少保留的簇数，避免 0 值导致规划退化
MIN_RESERVE_CLUSTERS = 1


@dataclass(slots=True)
class PlanParams:
    """规划输入参数（全部为纯数据）。"""

    total_clusters: int = 0
    bytes_per_cluster: int = 0
    reserve_clusters: int = 0
    free_runs: List[Tuple[int, int]] = field(default_factory=list)
    mft_zone_start: int = 0
    mft_zone_end: int = 0
    # ---- v2 新增（默认 0 = v1 行为，位置参数顺序不变）----
    head_reserve_clusters: int = 0
    tail_guard_clusters: int = 0
    mft_start_lcn: int = 0
    mft2_start_lcn: int = 0


def default_reserve_clusters(
    entries: Iterable[FileEntry],
    bytes_per_cluster: int,
    factor: float = DEFAULT_RESERVE_FACTOR,
) -> int:
    """按选中文件总字节数 * 系数，换算成簇数（向上取整）。"""
    if bytes_per_cluster <= 0:
        return 0
    total_bytes = sum(e.size for e in entries)
    clusters = int(total_bytes * factor / bytes_per_cluster) + 1
    return max(clusters, MIN_RESERVE_CLUSTERS)


def tail_floor_lcn(total_clusters: int, reserve_clusters: int) -> int:
    """末端预留区的起始 LCN（含）。"""
    return max(0, total_clusters - max(0, reserve_clusters))


class _TailPacker:
    """从卷尾向前寻找连续空位的小型状态机（纯内存计算）。"""

    def __init__(
        self,
        runs: Sequence[Tuple[int, int]],
        total_clusters: int,
        floor_lcn: int,
    ) -> None:
        self._runs: List[Tuple[int, int]] = [
            (max(start, floor_lcn), end) for start, end in runs if end > floor_lcn
        ]
        self._runs = [(s, e) for s, e in self._runs if e - s > 0]
        self._runs.sort()
        self._index: int = len(self._runs) - 1
        self._cursor: int = int(total_clusters)
        self._floor: int = int(floor_lcn)

    @property
    def cursor(self) -> int:
        return self._cursor

    def place(self, need: int) -> Optional[int]:
        """尝试为 ``need`` 个簇找到连续空位，返回目标起始 LCN。"""
        if need <= 0:
            return None
        while self._index >= 0:
            run_start, run_end = self._runs[self._index]
            usable_end = min(run_end, self._cursor)
            if usable_end - run_start < need:
                # 该 run 在游标以下的空间不够，继续找更靠前的 run
                self._index -= 1
                continue
            start = usable_end - need
            if start < self._floor:
                # 再往前只会更低，直接失败
                return None
            self._cursor = start
            return start
        return None


def build_plan(
    entries: Sequence[FileEntry],
    params: PlanParams,
) -> MovePlan:
    """生成迁移计划。

    Args:
        entries: 用户勾选的文件（顺序无关）。
        params: 卷参数与末端空闲区间。

    Returns:
        :class:`MovePlan`；无法安排的文件进入 ``failures`` 并带有中文原因。
    """
    total = max(0, int(params.total_clusters))
    reserve = max(0, int(params.reserve_clusters))
    floor = tail_floor_lcn(total, reserve)

    plan = MovePlan(
        total_clusters=total,
        reserve_clusters=reserve,
        tail_floor_lcn=floor,
    )
    # v1 末端模式产物（区间模式为 2，见 build_range_plan）
    plan.version = 1

    if total <= 0:
        plan.note = "卷簇数为 0，无法规划"
        return plan
    if reserve <= 0:
        plan.note = "末端预留区为 0 簇，无法规划"
        return plan

    # MFT 保留区始终视为不可用（即使位图显示空闲）
    runs = _subtract_mft_zone(params.free_runs, params.mft_zone_start, params.mft_zone_end)
    packer = _TailPacker(runs, total, floor)

    # 大文件优先，减少尾部碎片
    ordered = sorted(entries, key=lambda e: (-e.allocated_clusters, e.path))

    for entry in ordered:
        need = entry.allocated_clusters
        if not entry.movable:
            plan.failures.append(
                PlanFailure(entry=entry, reason=entry.reason or "标记为不可移动")
            )
            continue
        if need <= 0:
            plan.failures.append(PlanFailure(entry=entry, reason="文件无物理分配，无需移动"))
            continue
        if need > reserve:
            plan.failures.append(
                PlanFailure(
                    entry=entry,
                    reason=f"文件占用 {need:,} 簇，大于末端预留区 {reserve:,} 簇",
                )
            )
            continue

        # 已经完全落在末端预留区内的文件：不再搬动（不影响卷前部空间）
        if all(extent.lcn >= floor for extent in entry.allocated_extents):
            plan.items.append(
                PlanItem(
                    entry=entry,
                    target_lcn=entry.first_lcn,
                    clusters=need,
                    segments=[],
                    skipped_reason="已位于末端预留区，无需搬动",
                )
            )
            continue

        target = packer.place(need)
        if target is None:
            plan.failures.append(
                PlanFailure(
                    entry=entry,
                    reason=(
                        f"末端已无足够连续空间（需要 {need:,} 簇，"
                        f"剩余连续空间不足）"
                    ),
                )
            )
            continue

        segments = build_segments(entry, target)
        item = PlanItem(
            entry=entry,
            target_lcn=target,
            clusters=need,
            segments=segments,
        )
        if not any(seg.src_lcn != seg.dst_lcn for seg in segments):
            item.skipped_reason = "已位于目标位置"
        plan.items.append(item)

    # 让 UI 能区分「已就位」
    plan.items.sort(key=lambda i: i.target_lcn)
    if not plan.items:
        plan.note = "没有可迁移的文件"
    elif not plan.movable_items:
        plan.note = "所选文件已全部位于末端预留区，无需迁移"
    else:
        plan.note = (
            f"计划搬运 {len(plan.movable_items)} 个文件 / "
            f"{plan.total_move_clusters:,} 簇，目标区 [{floor:,}, {total:,})"
        )
    return plan


def _subtract_mft_zone(
    runs: Sequence[Tuple[int, int]],
    mft_start: int,
    mft_end: int,
) -> List[Tuple[int, int]]:
    """把 MFT 保留区从空闲区间中扣除（安全硬约束）。"""
    if mft_end <= mft_start:
        return list(runs)
    result: List[Tuple[int, int]] = []
    for start, end in runs:
        if end <= mft_start or start >= mft_end:
            result.append((start, end))
            continue
        if start < mft_start:
            result.append((start, mft_start))
        if end > mft_end:
            result.append((mft_end, end))
    return result


def total_clusters_of(entries: Iterable[FileEntry]) -> int:
    """选中文件的物理簇总数。"""
    return sum(e.allocated_clusters for e in entries)


def mark_tail_presence(
    entries: Sequence[FileEntry],
    total_clusters: int,
    reserve_clusters: int,
) -> None:
    """就地标注哪些文件已完整落在末端预留区内。"""
    floor = tail_floor_lcn(total_clusters, reserve_clusters)
    for entry in entries:
        extents = entry.allocated_extents
        entry.status = "待迁移"
        if not extents:
            entry.status = "不可移动"
            continue
        if all(extent.lcn >= floor for extent in extents):
            entry.status = "已就位"


def plan_summary(plan: MovePlan, bytes_per_cluster: int) -> Dict[str, int]:
    """给 UI 用的统计字典。"""
    return {
        "files": len(plan.movable_items),
        "clusters": plan.total_move_clusters,
        "bytes": plan.total_move_clusters * max(0, bytes_per_cluster),
        "failures": len(plan.failures),
    }


# =========================================================================== #
# v2：目标区间模式（packer + scheduler 编排）
# =========================================================================== #
Run = Tuple[int, int]


@dataclass(slots=True)
class RangePlanParams:
    """``build_range_plan`` 的输入（全部为纯数据）。"""

    total_clusters: int = 0
    bytes_per_cluster: int = 0
    window: TargetWindow = field(default_factory=TargetWindow)
    policy: Optional[SafetyPolicy] = None
    #: 全卷空闲区间（**已扣除禁区**）
    free_runs: List[Run] = field(default_factory=list)
    #: 全卷占用区间（用于计算"归属未知簇"与二次过滤陈旧索引）
    occupied_runs: List[Run] = field(default_factory=list)
    ownership: Optional[OwnershipIndex] = None
    excluded_paths: Tuple[str, ...] = ()
    io_profile: Optional[IoProfile] = None
    max_relay_rounds: int = MAX_RELAY_ROUNDS


def tail_window_for(
    total_clusters: int,
    reserve_clusters: int,
    tail_guard_clusters: int = 0,
) -> TargetWindow:
    """「末端倒数 N 簇」→ :class:`TargetWindow`（自动避开卷尾保留区）。"""
    return window_from_tail(total_clusters, reserve_clusters, tail_guard_clusters)


def _append_impossible(plan: MovePlan, reasons: List[str], entries: Sequence[FileEntry]) -> None:
    """把不可行原因同时写进 ``failures`` 与 ``note``（UI 两处都能看到）。"""
    detail = "；".join(reasons) if reasons else "不可行"
    plan.note = "不可行：" + detail
    reason = detail if len(detail) <= 120 else detail[:117] + "…"
    for entry in entries[:32]:
        plan.failures.append(PlanFailure(entry=entry, reason=reason))
    if not entries:
        plan.failures.append(
            PlanFailure(entry=FileEntry(path="", name="目标区间"), reason=reason)
        )


def build_range_plan(
    entries: Sequence[FileEntry],
    params: RangePlanParams,
) -> MovePlan:
    """v2 主入口：把选中文件装进指定目标区间，必要时驱逐挡路者。

    Args:
        entries: 用户勾选的文件（顺序无关）。
        params: 卷参数、目标区间、安全策略、空闲/占用区间、归属索引、IO 策略。

    Returns:
        :class:`MovePlan`（``version=2``）；不可行时 ``feasibility.verdict`` 为
        ``"impossible"``，``note`` 与 ``failures`` 带**量化**原因。
    """
    total = max(0, int(params.total_clusters))
    bpc = max(0, int(params.bytes_per_cluster)) or 4096
    window = params.window.clipped(total)
    policy = params.policy or SafetyPolicy(total_clusters=total, bytes_per_cluster=bpc)
    io_profile = params.io_profile or IoProfile.build(
        MEDIA_UNKNOWN, 256, bpc, window_clusters=window.clusters or None
    )

    plan = MovePlan(
        total_clusters=total,
        reserve_clusters=0,
        tail_floor_lcn=window.start_lcn,
        window=window,
        safety=policy,
        io_profile=io_profile,
        version=2,
    )
    plan.stats = PlanStats()

    # ---- 1) 归一化选中集合：剔除不可搬运对象 ----
    selection: List[FileEntry] = []
    for entry in entries:
        if not entry.movable:
            plan.failures.append(
                PlanFailure(entry=entry, reason=entry.reason or "标记为不可移动")
            )
        elif entry.is_compressed:
            plan.failures.append(
                PlanFailure(entry=entry, reason="NTFS 压缩文件，无法按需搬移")
            )
        elif entry.is_encrypted:
            plan.failures.append(
                PlanFailure(entry=entry, reason="EFS 加密文件，FSCTL_MOVE_FILE 不支持")
            )
        elif entry.allocated_clusters <= 0:
            plan.failures.append(PlanFailure(entry=entry, reason="无物理分配，无需搬运"))
        else:
            selection.append(entry)

    if total <= 0:
        plan.note = "卷簇数为 0，无法规划"
        return plan
    if window.clusters <= 0:
        plan.note = "目标区间为空，无法规划"
        return plan
    if not selection:
        plan.note = "没有可迁移的文件"
        return plan

    # ---- 2) 区间扣除禁区 → allowed ----
    allowed = subtract_forbidden([(window.start_lcn, window.end_lcn)], policy.forbidden_runs())

    # ---- 3) 可行性评估（只算一次）----
    analysis = analyze(
        selection,
        window,
        allowed,
        params.free_runs,
        params.ownership,
        params.occupied_runs,
        tuple(params.excluded_paths),
        bpc,
    )
    report = to_report(analysis)
    plan.feasibility = report
    plan.stats.unknown_owner_clusters = report.unknown_owner_clusters

    if report.verdict == VERDICT_IMPOSSIBLE:
        _append_impossible(plan, list(report.reasons) or ["目标区间不可行"], selection)
        if report.unknown_owner_clusters:
            plan.note += f"（其中 {report.unknown_owner_clusters:,} 簇归属未知）"
        return plan

    # ---- 4) 区间外空间账本：先放驱逐，再借给 relay ----
    ledger_out = SpaceLedger(analysis.evict_space)
    evictions, evict_failures = build_evictions(analysis.blockers, ledger_out, window)
    if evict_failures:
        _append_impossible(
            plan,
            ["区间外连续空闲不足，挡路文件无法迁出：" + evict_failures[0]]
            + evict_failures[1:4],
            selection,
        )
        return plan

    # ---- 5) 逐文件生成装载项（自重叠 → relay 两阶段）----
    items: List[PlanItem] = []
    relay_errors: List[str] = []
    by_path = {entry.path: entry for entry in selection}
    for path, target in sorted(analysis.placement.items(), key=lambda kv: (kv[1], kv[0])):
        entry = by_path.get(path)
        if entry is None:
            continue
        if is_in_place(entry, target):
            items.append(
                PlanItem(
                    entry=entry,
                    target_lcn=target,
                    clusters=entry.allocated_clusters,
                    segments=[],
                    skipped_reason="已在目标位置",
                )
            )
            continue
        if detect_self_overlap(entry, target):
            segments, error = place_with_relay(entry, target, ledger_out)
            if error:
                relay_errors.append(f"{entry.name}：{error}")
                continue
            # 分界点 = 第一阶段（src → relay）的段数 = 该文件的非稀疏 extent 数。
            # 早期写成 ``len(segments) // 2 if 偶数 else 0``：extent 数为奇数时
            # 分界点退化成 0，`build_tasks` 因此不会把中转拆成两条任务。
            split = len(entry.allocated_extents)
            if not 0 < split < len(segments):
                relay_errors.append(
                    f"{entry.name}：中转计划段数异常（{len(segments)} 段，分界 {split}）"
                )
                continue
            items.append(
                PlanItem(
                    entry=entry,
                    target_lcn=target,
                    clusters=entry.allocated_clusters,
                    segments=segments,
                    role="relay",
                    relay_split=split,
                )
            )
            continue
        items.append(
            PlanItem(
                entry=entry,
                target_lcn=target,
                clusters=entry.allocated_clusters,
                segments=build_segments(entry, target),
            )
        )

    if relay_errors:
        _append_impossible(plan, relay_errors, selection)
        return plan

    plan.items = sorted(items, key=lambda i: i.target_lcn)
    plan.evictions = evictions

    # ---- 6) 任务化 + 依赖排序（Kahn + 电梯）----
    tasks = build_tasks(plan.items, plan.evictions)
    sweep = io_profile.sweep
    edges = build_dependencies(tasks)
    ordered, remaining = order_elevator(tasks, edges, sweep)
    if remaining:
        relay_result = break_cycles(
            tasks,
            edges,
            ledger_out,
            window,
            params.max_relay_rounds,
            sweep,
        )
        if relay_result.errors:
            _append_impossible(plan, relay_result.errors, selection)
            return plan
        ordered = relay_result.tasks

    # ---- 7) 段合并（批次窗口）----
    if io_profile.coalesce:
        ordered = merge_task_segments_for_execution(
            ordered, io_profile.batch_clusters, coalesce=True
        )
    plan.tasks = ordered

    # ---- 8) 规划期安全复查（第一道校验）----
    try:
        for task in plan.tasks:
            for segment in task.segments:
                assert_target_allowed(policy, segment.dst_lcn, segment.clusters, "规划")
    except SafetyViolation as exc:
        _append_impossible(plan, [str(exc)], selection)
        return plan

    # ---- 9) 统计与文案 ----
    place_clusters = sum(i.move_clusters for i in plan.items)
    evict_clusters = sum(i.move_clusters for i in plan.evictions)
    relay_files = len({t.path for t in plan.tasks if t.phase == "relay_in"})
    plan.stats = PlanStats(
        place_files=len([i for i in plan.items if i.needs_move]),
        place_clusters=place_clusters,
        evict_files=len({i.entry.path for i in plan.evictions}),
        evict_clusters=evict_clusters,
        relay_files=relay_files,
        relay_clusters=plan.relay_clusters,
        unknown_owner_clusters=report.unknown_owner_clusters,
    )

    parts = ["装载 " + f"{place_clusters:,} 簇"]
    if evict_clusters:
        parts.append(f"驱逐 {evict_clusters:,} 簇")
    if plan.relay_clusters:
        parts.append(f"中转 {plan.relay_clusters:,} 簇")
    total_bytes = place_clusters * bpc
    verdict_text = "直接可行" if report.verdict == VERDICT_DIRECT else "需先驱逐挡路文件"
    plan.note = (
        f"{verdict_text}：{plan.stats.place_files} 个文件 / "
        + " + ".join(parts)
        + f"（装载合计 {human_bytes(total_bytes)}），目标区间 LCN "
        f"{window.start_lcn:,} – {window.end_lcn - 1:,}"
    )
    if report.unknown_owner_clusters:
        plan.note += f"；区间内 {report.unknown_owner_clusters:,} 簇归属未知"
    return plan
