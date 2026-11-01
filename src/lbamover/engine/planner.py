"""整理策略与规划器：把「碎片报告」变成「一串可执行的文件搬迁」。

三个策略的行为差异是**可观测的**，不是命名游戏：

===================  ==========================================  ==================
策略                 选的哪些文件 / 什么顺序                       目标落点
===================  ==========================================  ==================
``COALESCE``         仅碎片文件，按「可回收碎片量」降序            卷首方向 first-fit
``COMPACT``          全部选中文件，按当前起始 LCN 升序            卷首方向 first-fit
``RELOCATE_TAIL``    仅碎片文件，按「可回收碎片量」降序            卷尾方向 first-fit
``ANALYZE_ONLY``     不产生任何 op                                —
===================  ==========================================  ==================

``COALESCE`` 与 ``COMPACT`` 的区别不在算法而在**目标**：前者只治单个文件
内部的碎片，后者顺手把一堆本来就连续的小文件也往前挤，换来更大的尾部
连续空间。
"""

from __future__ import annotations

import bisect
from typing import Iterable, Sequence

from ..contract.enums import Strategy
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

    selected, missing = select_files(report, strategy, file_ids)
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
    notes: list[str] = []
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
