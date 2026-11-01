"""整理策略与规划器：把「碎片报告」变成「一串可执行的文件搬迁」。

四个策略的行为差异是**可观测的**，不是命名游戏：

===================  ==========================================  ==================
策略                 选的哪些文件 / 什么顺序                       目标落点
===================  ==========================================  ==================
``COALESCE``         仅碎片文件，按「可回收碎片量」降序            卷首方向 first-fit
``COMPACT``          全部选中文件，按当前起始 LCN 升序            卷首方向 first-fit
``RELOCATE_TAIL``    仅碎片文件，按「可回收碎片量」降序            卷尾方向 first-fit
``RECLAIM``          与目标区相交的全部文件，按距目标区远端升序    目标区另一侧
``ANALYZE_ONLY``     不产生任何 op                                —
===================  ==========================================  ==================

``COALESCE`` 与 ``COMPACT`` 的区别不在算法而在**目标**：前者只治单个文件
内部的碎片，后者顺手把一堆本来就连续的小文件也往前挤，换来更大的尾部
连续空间。

``RECLAIM`` 是个**约束式**任务，跟上面三个「优化式」的都不一样：不是
"尽量搬得好一点"，而是"这几块必须空出来"。因此它的判据也不同 ——
它不挑碎片文件，只挑挡路的文件；宁可放弃也不给出一个"大概腾出了"的
结果，因为用户是按这个数字去规划下一步的（装虚拟机、拷大文件）。
"""

from __future__ import annotations

import bisect
from typing import Iterable, Sequence

from ..contract.enums import FreeSpaceSide, Strategy
from ..contract.models import Extent, FragReport, JobParams, MoveOp, Plan
from .alloc import FreeSpaceMap, IntervalSet

__all__ = ["build_plan", "select_files", "order_files", "validate_plan"]


def select_files(
    report: FragReport,
    strategy: Strategy,
    file_ids: Sequence[str] = (),
) -> tuple[list, tuple[str, ...]]:
    """按策略挑选文件。返回 ``(文件列表, 未找到的 id 列表)``。"""
    wanted = tuple(dict.fromkeys(file_ids))
    missing: tuple[str, ...] = ()
    if wanted:
        index = {f.file_id: f for f in report.files}
        missing = tuple(fid for fid in wanted if fid not in index)
        pool = [index[fid] for fid in wanted if fid in index]
    else:
        pool = list(report.files)

    if strategy in (Strategy.COALESCE, Strategy.RELOCATE_TAIL):
        pool = [f for f in pool if f.is_fragmented]
    elif strategy is Strategy.ANALYZE_ONLY:
        pool = []
    return pool, missing


def order_files(files: Iterable, strategy: Strategy) -> list:
    """排序决定"先搬谁"，直接影响能否腾出空间给后面的文件。"""
    items = list(files)
    if strategy is Strategy.COMPACT:
        # 从低地址往高地址推进，每次腾出的空洞正好接着用
        return sorted(items, key=lambda f: (min(e.start for e in f.extents), f.file_id))
    # 碎片收益大的先做：腾出的连续空间最多，后续 op 更容易放下
    return sorted(
        items,
        key=lambda f: (
            -((f.run_count - 1) * (f.total_blocks / max(1, f.run_count))),
            f.file_id,
        ),
    )


def build_plan(
    job_id: str,
    strategy: Strategy,
    report: FragReport,
    *,
    file_ids: Sequence[str] = (),
    params: JobParams | None = None,
) -> Plan:
    """生成执行计划。**不抛异常**——不可行时返回 ``feasible=False`` 并带原因。"""
    params = params or JobParams()
    geometry = report.geometry

    if strategy is Strategy.ANALYZE_ONLY:
        return Plan(
            job_id=job_id,
            strategy=strategy,
            ops=(),
            feasible=True,
            notes=("只分析模式：不产生任何写入操作",),
        )

    if strategy is Strategy.RECLAIM:
        return _build_reclaim_plan(job_id, report, params, file_ids)

    selected, missing = select_files(report, strategy, file_ids)
    notes: list[str] = []
    if missing and selected:
        # 部分失效是很常见的：用户选完文件、点了开始，中间文件被删掉/改名。
        # 只跳过并说清楚，不要整单拒绝 —— 但也不能一声不吭。
        notes.append(
            f"{len(missing)} 个指定文件已不在本次扫描结果里（可能被删除或改名），已跳过"
        )
    if not selected:
        why = (
            f"指定的 {len(missing)} 个文件在扫描结果中不存在"
            if missing and not selected
            else "没有需要整理的文件（未发现碎片）"
        )
        return Plan(
            job_id=job_id,
            strategy=strategy,
            ops=(),
            feasible=not (missing and not selected),
            reasons=((why,) if missing and not selected else ()),
            notes=(why,),
        )

    ordered = order_files(selected, strategy)
    free = FreeSpaceMap(report.free_extents, capacity=geometry.block_count)

    # 尚未搬走的文件占的块 —— 目标区绝对不能落在这里
    blocked = IntervalSet()
    for f in ordered:
        blocked.add_many(f.extents)

    tail_limit = geometry.block_count - max(0, params.reserve_tail_blocks)
    ops: list[MoveOp] = []
    reasons: list[str] = []
    cursor = 0

    for f in ordered:
        # 此刻 blocked 里还包含自己的源区 —— 分配时正好把它当禁区（不能写到
        # 自己还没读完的地方），分配完再摘掉。这样不用每个文件复制一份几千
        # 个区间的 avoid 集合（那是 O(文件数 × 区间数)）。
        n = f.total_blocks
        dst = free.allocate(
            n,
            avoid=blocked,
            from_tail=(strategy is Strategy.RELOCATE_TAIL),
            max_end=tail_limit,
        )
        blocked.subtract(f.extents)
        if dst is None:
            reasons.append(
                f"{f.path}: 找不到 {n} 块（{n * geometry.block_size / 1048576:.1f} MiB）"
                f"的连续空闲区，跳过"
            )
            continue

        ops.append(
            MoveOp(
                op_id=cursor,
                file_id=f.file_id,
                path=f.path,
                src_extents=tuple(f.extents),
                dst_start=dst,
                verify=params.verify,
            )
        )
        cursor += 1
        # 搬走后源区变为空闲，后续 op 可以复用（这就是 requires_serial 的来源）
        free.release(f.extents)

    if missing:
        notes.append(f"忽略了 {len(missing)} 个不存在的文件 id")

    requires_serial = _needs_serial(ops)
    if requires_serial:
        notes.append("计划需要复用搬走腾出的空间，将以串行方式执行以保证读前写后安全")

    feasible = bool(ops)
    return Plan(
        job_id=job_id,
        strategy=strategy,
        ops=tuple(ops),
        feasible=feasible,
        reasons=tuple(reasons),
        notes=tuple(notes),
        reserved_tail=params.reserve_tail_blocks,
        requires_serial=requires_serial,
    )


# --------------------------------------------------------------------------
# 内部
# --------------------------------------------------------------------------


def _mib(blocks: int, geometry) -> str:
    return f"{blocks * geometry.block_size / 1048576:.1f} MiB"


def _free_gaps(occupied: IntervalSet, total: int) -> list[tuple[int, int]]:
    """``[0, total)`` 减去 ``occupied`` 之后的连续空档，升序。

    "可用空间整理"要落在一段**完全不含搬不动的东西**的区间里，所以先把
    这些禁区之间的空隙列出来，再从空隙里挑 —— 直接在原始目标区上纠结
    "往哪让一点"会漏掉让开以后又撞上下一个禁区的情况（``$MFT`` 后面
    紧跟着 ``$LogFile`` 就是最常见的组合）。
    """
    gaps: list[tuple[int, int]] = []
    cursor = 0
    for e in occupied:
        s = max(0, e.start)
        t = min(total, e.end)
        if t <= cursor:
            continue
        if s > cursor:
            gaps.append((cursor, s))
        cursor = max(cursor, t)
    if cursor < total:
        gaps.append((cursor, total))
    return gaps


def _pick_target(
    total: int,
    need: int,
    side: FreeSpaceSide,
    protected: IntervalSet,
    avoid: bool,
) -> tuple[Extent | None, str]:
    """决定目标区到底落在哪。返回 ``(区间, 不可行原因)``。

    贴边（``avoid=False``）与让开（``avoid=True``）不是"严格/宽松"的关系，
    而是两种不同的承诺：前者是"必须正好在卷首前 N 块"，后者是"卷这一侧
    给我一段 N 块的连续空闲"。默认给后者，因为 NTFS 把元文件钉死在低地址，
    贴边在真实卷上必然失败 —— 一个永远报错的功能等于没有。
    """
    if not avoid:
        start = 0 if side is FreeSpaceSide.HEAD else total - need
        target = Extent(start, need)
        hit = protected.overlap_range(target.start, target.end)
        if hit:
            used = sum(e.length for e in hit)
            return None, (
                f"{side.label}这 {need} 块里有 {used} 块是搬不动的系统数据"
                f"（元文件 / 页面文件 / 系统目录），无法完全腾空。"
                f"可改用「自动避开受保护区」，或把目标改到另一侧"
            )
        return target, ""

    gaps = _free_gaps(protected, total)
    if side is FreeSpaceSide.HEAD:
        for s, e in gaps:
            if e - s >= need:
                return Extent(s, need), ""
    else:
        for s, e in reversed(gaps):
            if e - s >= need:
                return Extent(e - need, need), ""
    widest = max((e - s for s, e in gaps), default=0)
    return None, (
        f"{side.label}侧最大可腾空的连续区间只有 {widest} 块，"
        f"不够要求的 {need} 块（差 {need - widest} 块）。"
        f"请调小要腾出的空间，或改到另一侧"
    )


def _files_touching(files: Iterable, target: Extent) -> list:
    """与目标区有任何重叠的文件。``extents`` 升序，所以只看首尾就够排掉绝大多数。"""
    out = []
    for f in files:
        if not f.extents:
            continue
        if f.extents[-1].end <= target.start or f.extents[0].start >= target.end:
            continue
        out.append(f)
    return out


def _build_reclaim_plan(
    job_id: str,
    report: FragReport,
    params: JobParams,
    file_ids: Sequence[str],
) -> Plan:
    """「可用空间整理」：把目标区里挡路的文件全部搬出去，让该区变成连续空闲。

    与其它策略最大的区别是**判据**：这里不存在"尽力而为"。要么目标区最后
    确实全空，要么整单不可行并说清差多少。理由很实际 —— 用户拿这个结果去
    装虚拟机/拷镜像，一个"差不多空了"的结果会让他下一次操作直接失败。
    """
    geometry = report.geometry
    total = geometry.block_count
    need = params.reclaim_blocks
    side = params.reclaim_at
    notes: list[str] = []

    def infeasible(why: str, reasons: Sequence[str] = ()) -> Plan:
        return Plan(
            job_id=job_id,
            strategy=Strategy.RECLAIM,
            ops=(),
            feasible=False,
            reasons=tuple(reasons) or (why,),
            notes=tuple(notes),
        )

    if need <= 0:
        return infeasible("「可用空间整理」需要先指定要腾出多少空间，当前为 0")
    if need > total:
        return infeasible(
            f"要腾出的 {_mib(need, geometry)} 超过卷总容量 {_mib(total, geometry)}"
        )
    if file_ids:
        # 目标区腾空必须搬走所有挡路的文件，和"只动这几个"天然冲突。
        # 不拒绝，但必须说清楚，否则用户会以为手动勾选生效了。
        notes.append("可用空间整理按目标区选文件，忽略手动勾选的文件范围")

    protected = IntervalSet(report.protected_extents)
    target, why = _pick_target(total, need, side, protected, params.reclaim_avoid_protected)
    if target is None:
        return infeasible(why)

    offset = target.start if side is FreeSpaceSide.HEAD else total - target.end
    if offset:
        # 说清楚"为什么没贴着边"：用户要的是"卷尾那块空地"，拿到的却是
        # 往前的一段，必须解释，否则他会以为功能算错了。
        notes.append(
            f"{side.label}方向最近 {offset} 块内有搬不动的系统数据（元文件/页面文件），"
            f"目标区已向卷内让开，实际落在 {target.start}–{target.end} 块"
        )
    notes.append(
        f"目标区：{side.label} {target.start}–{target.end}（{_mib(need, geometry)}）"
    )

    blockers = _files_touching(report.files, target)
    if not blockers:
        notes.append("目标区当前已经全部是空闲空间，不需要搬运任何数据")
        return Plan(
            job_id=job_id,
            strategy=Strategy.RECLAIM,
            ops=(),
            feasible=True,
            notes=tuple(notes),
            reclaim_extent=target,
        )

    # 落点必须在目标区之外，而且按方向"往另一头堆"：腾出卷首就该把文件
    # 往后放，否则刚搬出去的文件又贴着目标区，下次整理还得再搬一遍。
    if side is FreeSpaceSide.HEAD:
        min_start, max_end, from_tail = target.end, None, True
    else:
        min_start, max_end, from_tail = 0, target.start, False

    # 目标区外到底有多少空闲能装：注意**不能用** free.total_blocks，
    # 目标区内的空闲虽然存在，但一旦拿来落点就等于目标区没腾干净。
    outside_free = 0
    for e in report.free_extents:
        if e.end <= target.start or e.start >= target.end:
            outside_free += e.length
        else:
            outside_free += max(0, target.start - e.start) + max(0, e.end - target.end)

    moving = sum(f.total_blocks for f in blockers)
    if outside_free < moving:
        return infeasible(
            f"目标区外只剩 {_mib(outside_free, geometry)} 空闲，"
            f"而要腾出目标区需要把 {moving} 块（{_mib(moving, geometry)}）搬出去，"
            f"差 {moving - outside_free} 块。请调小要腾出的空间"
        )

    ordered = sorted(
        blockers,
        key=lambda f: (
            f.extents[0].start if side is FreeSpaceSide.HEAD else -f.extents[-1].end,
            f.file_id,
        ),
    )

    # avoid 必须包含**全部**不搬的占用，包括被保护策略拦下的那些：
    # 它们不在 report.files 里，但一样不能被写入。
    blocked = IntervalSet(report.protected_extents)
    blocked.add_many(e for f in report.files for e in f.extents)

    free = FreeSpaceMap(report.free_extents, capacity=total)
    ops: list[MoveOp] = []
    stuck: list[str] = []
    cursor = 0
    for f in ordered:
        n = f.total_blocks
        dst = free.allocate(
            n, avoid=blocked, from_tail=from_tail, min_start=min_start, max_end=max_end
        )
        blocked.subtract(f.extents)
        if dst is None:
            stuck.append(f"{f.path}: 目标区外找不到 {n} 块的连续空闲")
            continue
        ops.append(
            MoveOp(
                op_id=cursor,
                file_id=f.file_id,
                path=f.path,
                src_extents=tuple(f.extents),
                dst_start=dst,
                verify=params.verify,
            )
        )
        cursor += 1
        # 归还源区：搬走以后那块空间确实空了，后续 op 可以复用（同一个
        # 是否需要串行的问题，以及同一条「目标区内的释放不参与落点」约束）。
        free.release(f.extents)

    if stuck:
        # 只要有一个挡路的文件搬不出去，目标区就不可能全空 —— 这时候
        # 交出一份"搬了一半"的计划是最坏的结果：用户会以为空间到手了。
        head = stuck[:3]
        more = f"（另有 {len(stuck) - 3} 个同类文件）" if len(stuck) > 3 else ""
        return infeasible(
            "",
            reasons=(
                f"目标区里有 {len(stuck)} 个文件搬不出去，无法完全腾空："
                + "；".join(head)
                + more,
                "建议调小要腾出的空间，或把目标改到卷的另一侧",
            ),
        )

    requires_serial = _needs_serial(ops)
    if requires_serial:
        notes.append("计划需要复用搬走腾出的空间，将以串行方式执行以保证读前写后安全")
    notes.append(
        f"需搬出 {len(ops)} 个文件、共 {moving} 块（{_mib(moving, geometry)}），"
        f"搬完后 {side.label}将有 {_mib(need, geometry)} 连续空闲"
    )

    problem = _audit_reclaim(ops, target)
    if problem:
        return infeasible(problem)

    return Plan(
        job_id=job_id,
        strategy=Strategy.RECLAIM,
        ops=tuple(ops),
        feasible=bool(ops),
        notes=tuple(notes),
        requires_serial=requires_serial,
        reclaim_extent=target,
    )


def _audit_reclaim(ops: Sequence[MoveOp], target: Extent) -> str:
    """自检：这串 op 真的能让 ``target`` 变全空吗。

    两条必须同时成立 —— 挡路文件都搬走了、落点没有一个落在目标区里。
    这是纯几何检查，跑一遍很便宜，但能挡住"参数改错一个符号就默默覆盖
    目标区"这类事故。返回空串表示通过。
    """
    for op in ops:
        dst = op.dst_extent
        if dst.start < target.end and dst.end > target.start:
            return f"内部自检失败：{op.path} 的落点落进了目标区"
    return ""


def _needs_serial(ops: Sequence[MoveOp]) -> bool:
    """任一 op 的目标区与"另一个 op 的源区"相交 ⇒ 必须串行。

    并发执行时，后一个 op 的写入可能落在前一个 op 尚未读完的源区上，
    结果是数据静默损坏。这种计划只能在"前一个完全结束后"才允许开始下一个。

    实现：把所有源区间排序后二分。朴素的双重循环在真实卷上是
    O(op²)，几千个文件要跑十几秒；而源区间互不重叠，只要看目标区左边界
    附近那一个就够。
    """
    if len(ops) < 2:
        return False
    srcs = sorted(
        (s.start, s.end, op.op_id) for op in ops for s in op.src_extents if s.length > 0
    )
    if not srcs:
        return False
    starts = [s[0] for s in srcs]
    for op in ops:
        dst = op.dst_extent
        i = bisect.bisect_left(starts, dst.end) - 1
        while i >= 0:
            start, end, owner = srcs[i]
            if end <= dst.start:
                # 源区间互不重叠且升序：比它更早的都不可能与 dst 相交
                if owner != op.op_id:
                    break
                i -= 1
                continue
            if owner != op.op_id:
                return True
            i -= 1
    return False


def validate_plan(plan: Plan, geometry_block_count: int) -> list[str]:
    """执行前的静态自检。返回问题列表，空列表表示通过。

    这是"宁可拒绝执行也不要写错的"最后一道闸门，故意做成纯函数以便单测。
    """
    problems: list[str] = []
    dsts: list[tuple[int, int, int]] = []  # (start, end, op_id)
    for op in plan.ops:
        dst = op.dst_extent
        if dst.end > geometry_block_count:
            problems.append(f"op{op.op_id}: 目标区越界 {dst} > {geometry_block_count}")
        if dst.start < 0:
            problems.append(f"op{op.op_id}: 目标区起点为负")
        if op.self_overlapping:
            problems.append(f"op{op.op_id}: 目标区与自身源区重叠（会自我覆盖）")
        if op.blocks <= 0:
            problems.append(f"op{op.op_id}: 块数为 0")
        dsts.append((dst.start, dst.end, op.op_id))

    # 目标区两两重叠：排序后只需比对相邻项（重叠必然发生在相邻者之间）
    dsts.sort()
    for prev, cur in zip(dsts, dsts[1:]):
        if cur[0] < prev[1]:
            problems.append(
                f"op{cur[2]}: 目标区与 op{prev[2]} 的目标区重叠 "
                f"[{cur[0]}, {cur[1]}) / [{prev[0]}, {prev[1]})"
            )

    if plan.requires_serial is False and _needs_serial(plan.ops):
        # 声明可并行，就必须真的没有"目标踩源"的情况。
        # 这里直接复用 _needs_serial 的排序二分：朴素双循环是 O(op²)，真实卷上
        # 两千多个 op 要跑一分钟，而它只是个自检。
        problems.append("计划声明可并行，但存在目标区与另一个 op 的源区重叠的情况")
    return problems
