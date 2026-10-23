"""依赖排序与环断裂 —— **纯算法层**。

两个约束必须同时满足：

* **依赖序**：若 ``a`` 的目标块覆盖了 ``b`` 的源块，则 ``b`` 必须先执行（否则会把
  还没搬走的数据覆盖掉）；
* **电梯序**：按目标 LCN 单调推进，减少磁头换向。

二者会互相冲突（拓扑要求 B 先，而 A 的目标 LCN 更小），不能串行拼接。
本模块用 **Kahn 拓扑 + 最小堆就绪队列**：在每个拓扑层内自由选择时贪心地走同方向，
**永不违反依赖** —— 这是唯一能同时满足两条约束的多项式解法（设计 §3.6）。

剩余的强连通分量（环）用 **relay 中转**打断：把环上一个文件先搬到区间外的空闲处，
再搬向最终目标，从而把环拆成链。
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from app.core.models import MoveSegment, MoveTask, PlanItem, TargetWindow
from app.core.packer import SpaceLedger

PHASE_EVICT = "evict"
PHASE_PLACE = "place"
PHASE_RELAY_OUT = "relay_out"
PHASE_RELAY_IN = "relay_in"

MAX_RELAY_ROUNDS: int = 3


# --------------------------------------------------------------------------- #
# 任务构造
# --------------------------------------------------------------------------- #
def build_tasks(
    items: Sequence[PlanItem],
    evictions: Sequence[PlanItem] = (),
    relay_items: Sequence[PlanItem] = (),
) -> List[MoveTask]:
    """把计划项摊平成可执行任务序列（``evict`` → ``place`` → ``relay``）。

    ``role == "relay"`` 的计划项会被拆成 ``relay_out`` / ``relay_in`` 两条任务，
    分界点由 ``PlanItem.relay_split`` 指明（planner 生成时填好）。

    ``order`` 只是**初始顺序**与堆的同序 tie-breaker；真正的执行序由
    :func:`order_elevator` 决定。
    """
    tasks: List[MoveTask] = []

    for index, item in enumerate(evictions):
        if not item.segments:
            continue
        tasks.append(
            MoveTask(
                order=len(tasks),
                phase=PHASE_EVICT,
                item_index=index,
                path=item.entry.path,
                path_known=item.entry.path_known,
                segments=list(item.segments),
            )
        )

    for index, item in enumerate(items):
        if not item.segments:
            continue
        if item.role == "relay" and 0 < item.relay_split < len(item.segments):
            split = item.relay_split
            tasks.append(
                MoveTask(
                    order=len(tasks),
                    phase=PHASE_RELAY_OUT,
                    item_index=index,
                    path=item.entry.path,
                    path_known=item.entry.path_known,
                    segments=list(item.segments[:split]),
                )
            )
            tasks.append(
                MoveTask(
                    order=len(tasks),
                    phase=PHASE_RELAY_IN,
                    item_index=index,
                    path=item.entry.path,
                    path_known=item.entry.path_known,
                    segments=list(item.segments[split:]),
                )
            )
            continue
        tasks.append(
            MoveTask(
                order=len(tasks),
                phase=PHASE_PLACE,
                item_index=index,
                path=item.entry.path,
                path_known=item.entry.path_known,
                segments=list(item.segments),
            )
        )

    for index, item in enumerate(relay_items):
        if not item.segments:
            continue
        tasks.append(
            MoveTask(
                order=len(tasks),
                phase=PHASE_RELAY_OUT,
                item_index=index,
                path=item.entry.path,
                path_known=item.entry.path_known,
                segments=list(item.segments),
            )
        )
    return tasks


# --------------------------------------------------------------------------- #
# 依赖图
# --------------------------------------------------------------------------- #
def ranges_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """两个半开区间是否相交。"""
    return a_start < b_end and b_start < a_end


def any_range_overlap(a_ranges: Sequence[Tuple[int, int]], b_ranges: Sequence[Tuple[int, int]]) -> bool:
    for a_start, a_end in a_ranges:
        for b_start, b_end in b_ranges:
            if ranges_overlap(a_start, a_end, b_start, b_end):
                return True
    return False


def build_dependencies(tasks: Sequence[MoveTask]) -> Dict[int, Set[int]]:
    """构建依赖边：``edges[a] ∋ b`` 表示「b 必须先于 a 执行」。

    规则：**a 的目标块覆盖了 b 的源块 ⇒ b 必须先搬走**，否则 a 会把 b 的数据覆盖掉。

    唯一例外是 ``relay_in``：它的"源"是 relay_out 刚写出的**中转拷贝**，不是需要
    保护的原数据。因此

    * ``relay_in`` 不作为任何任务的"前置条件"（它的源可以被随意覆盖）；
    * 反而要为它加一条生产者边：``relay_out``（同路径、同中转块）必须先执行。

    这条例外是环断裂能收敛的关键 —— 否则 relay_out(→relay) 与 relay_in(relay→dst)
    会互相依赖，形成永远解不开的新环。
    """
    edges: Dict[int, Set[int]] = {t.order: set() for t in tasks}
    prepared = [(t.order, t.dst_ranges, t.src_ranges, t.phase) for t in tasks]

    for a_order, a_dst, _a_src, _a_phase in prepared:
        for b_order, _b_dst, b_src, b_phase in prepared:
            if a_order == b_order:
                continue
            if b_phase == PHASE_RELAY_IN:
                # relay_in 的源是"中转拷贝"，不构成"必须先搬走"的约束
                continue
            if any_range_overlap(a_dst, b_src):
                # a 会压到 b 的源 ⇒ b 是 a 的前置
                edges[a_order].add(b_order)

    # 生产者边：relay_out → relay_in（同路径且中转块相交）
    relay_outs = [t for t in prepared if t[3] == PHASE_RELAY_OUT]
    for b_order, _b_dst, b_src, b_phase in prepared:
        if b_phase != PHASE_RELAY_IN:
            continue
        for a_order, a_dst, _a_src, _a_phase in relay_outs:
            if a_order == b_order:
                continue
            if any_range_overlap(a_dst, b_src):
                # 生产者必须先写进中转块
                edges[b_order].add(a_order)
    return edges


def reverse_edges(edges: Dict[int, Set[int]]) -> Dict[int, Set[int]]:
    """反向图（Tarjan 用）。"""
    reversed_edges: Dict[int, Set[int]] = {node: set() for node in edges}
    for node, targets in edges.items():
        for target in targets:
            reversed_edges.setdefault(target, set()).add(node)
    return reversed_edges


# --------------------------------------------------------------------------- #
# Kahn + 最小堆 = 拓扑约束下的电梯单向序
# --------------------------------------------------------------------------- #
def order_elevator(
    tasks: Sequence[MoveTask],
    edges: Dict[int, Set[int]],
    sweep: str = "ascending",
) -> Tuple[List[MoveTask], List[int]]:
    """Kahn 拓扑排序，就绪集合用**堆按 dst_lcn 排序**（电梯单向）。

    Args:
        tasks: 任务列表（其 ``order`` 必须唯一）。
        edges: ``build_dependencies`` 的产物。
        sweep: ``ascending``（目标 LCN 从小到大）或 ``descending``。

    Returns:
        ``(执行序, 未能排序的剩余节点)``。剩余节点非空即表示存在依赖环。
    """
    by_order: Dict[int, MoveTask] = {t.order: t for t in tasks}
    indegree: Dict[int, int] = {node: 0 for node in by_order}
    dependents: Dict[int, List[int]] = {node: [] for node in by_order}
    for node, prerequisites in edges.items():
        for prerequisite in prerequisites:
            if prerequisite in indegree and node in indegree:
                indegree[node] += 1
                dependents[prerequisite].append(node)

    descending = sweep == "descending"

    def _key(order: int) -> Tuple[int, int]:
        task = by_order[order]
        dst = task.first_dst
        return (-dst if descending else dst, order)

    heap: List[Tuple[Tuple[int, int], int]] = []
    for node, degree in indegree.items():
        if degree == 0:
            heapq.heappush(heap, (_key(node), node))

    ordered: List[MoveTask] = []
    while heap:
        _priority, node = heapq.heappop(heap)
        ordered.append(by_order[node])
        # node 执行完毕后，解除所有"依赖 node"的边
        for dependent in dependents.get(node, ()):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                heapq.heappush(heap, (_key(dependent), dependent))

    remaining = sorted(node for node, degree in indegree.items() if degree > 0)
    return ordered, remaining


def execution_positions(ordered: Sequence[MoveTask]) -> Dict[int, int]:
    """``order -> 执行位次``（测试断言用）。"""
    return {task.order: position for position, task in enumerate(ordered)}


def violates_dependency(ordered: Sequence[MoveTask], edges: Dict[int, Set[int]]) -> Optional[Tuple[int, int]]:
    """返回第一处违反依赖的 ``(任务, 应当先执行的任务)``；无违规则 ``None``。"""
    positions = execution_positions(ordered)
    for node, prerequisites in edges.items():
        for prerequisite in prerequisites:
            if prerequisite not in positions or node not in positions:
                continue
            if positions[prerequisite] >= positions[node]:
                return node, prerequisite
    return None


# --------------------------------------------------------------------------- #
# Tarjan SCC（迭代实现，避免大图递归爆栈）
# --------------------------------------------------------------------------- #
def tarjan_scc(nodes: Iterable[int], edges: Dict[int, Set[int]]) -> List[List[int]]:
    """Tarjan 强连通分量（迭代版）。返回分量列表（每个分量内节点升序）。"""
    index_counter = 0
    indices: Dict[int, int] = {}
    lowlink: Dict[int, int] = {}
    on_stack: Set[int] = set()
    stack: List[int] = []
    result: List[List[int]] = []

    graph: Dict[int, List[int]] = {node: sorted(edges.get(node, ())) for node in nodes}
    for node in graph:
        if node in indices:
            continue
        # 迭代 DFS：栈里放 (节点, 下一个待访问邻居的下标)
        work: List[Tuple[int, int]] = [(node, 0)]
        while work:
            current, child_index = work.pop()
            if child_index == 0:
                indices[current] = index_counter
                lowlink[current] = index_counter
                index_counter += 1
                stack.append(current)
                on_stack.add(current)

            children = graph.get(current, [])
            recursed = False
            for position in range(child_index, len(children)):
                child = children[position]
                if child not in indices:
                    work.append((current, position + 1))
                    work.append((child, 0))
                    recursed = True
                    break
                if child in on_stack:
                    lowlink[current] = min(lowlink[current], indices[child])
            if recursed:
                continue

            if lowlink[current] == indices[current]:
                component: List[int] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == current:
                        break
                result.append(sorted(component))

            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[current])
    return result


def cyclic_components(nodes: Iterable[int], edges: Dict[int, Set[int]]) -> List[List[int]]:
    """只返回**真环**（大小 > 1，或存在自环）的强连通分量。"""
    cyclic: List[List[int]] = []
    for component in tarjan_scc(nodes, edges):
        if len(component) > 1:
            cyclic.append(component)
        elif component and component[0] in edges.get(component[0], set()):
            cyclic.append(component)
    return cyclic


# --------------------------------------------------------------------------- #
# 环断裂（relay 中转）
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class RelayResult:
    """``break_cycles`` 的结果。"""

    tasks: List[MoveTask] = field(default_factory=list)
    rounds: int = 0
    errors: List[str] = field(default_factory=list)
    inserted: List[int] = field(default_factory=list)


def split_task_for_relay(
    task: MoveTask,
    relay_start: int,
) -> Tuple[MoveTask, MoveTask]:
    """把一条任务拆成 ``relay_out``（src→relay）与 ``relay_in``（relay→dst）两条。"""
    cursor = relay_start
    out_segments: List[MoveSegment] = []
    for segment in task.segments:
        out_segments.append(
            MoveSegment(
                vcn=segment.vcn,
                src_lcn=segment.src_lcn,
                dst_lcn=cursor,
                clusters=segment.clusters,
            )
        )
        cursor += segment.clusters

    in_segments: List[MoveSegment] = []
    for segment in out_segments:
        in_segments.append(
            MoveSegment(
                vcn=segment.vcn,
                src_lcn=segment.dst_lcn,
                dst_lcn=next(
                    original.dst_lcn
                    for original in task.segments
                    if original.vcn == segment.vcn
                ),
                clusters=segment.clusters,
            )
        )

    relay_out = MoveTask(
        order=task.order,
        phase=PHASE_RELAY_OUT,
        item_index=task.item_index,
        path=task.path,
        # ★ FIX-5：`path_known` 必须随任务一起继承。
        # 漏传会退回默认 True，`execute_task` 的"路径未知必须拒绝"防线
        # 就只剩 `path.startswith("(record ")` 这条字符串兜底。
        path_known=task.path_known,
        segments=out_segments,
    )
    relay_in = MoveTask(
        order=-1,          # 由调用方分配
        phase=PHASE_RELAY_IN,
        item_index=task.item_index,
        path=task.path,
        path_known=task.path_known,
        segments=in_segments,
    )
    return relay_out, relay_in


def break_cycles(
    tasks: Sequence[MoveTask],
    edges: Dict[int, Set[int]],
    ledger_relay: SpaceLedger,
    window: TargetWindow,
    max_rounds: int = MAX_RELAY_ROUNDS,
    sweep: str = "ascending",
) -> RelayResult:
    """反复用 relay 打断依赖环，直到可拓扑排序或用尽中转轮数。

    Returns:
        :class:`RelayResult`；``errors`` 非空表示不可行（不抛异常）。
    """
    result = RelayResult(tasks=list(tasks))

    current_edges = dict(edges)
    rounds = 0
    while True:
        ordered, remaining = order_elevator(result.tasks, current_edges, sweep)
        if not remaining:
            result.tasks = ordered
            result.rounds = rounds
            return result

        components = cyclic_components(remaining, current_edges)
        if not components:
            # 理论上不该发生（有环必有真 SCC）；保守报错
            result.errors.append(
                f"存在无法拓扑排序的任务（{len(remaining)} 个），但未检出依赖环"
            )
            result.rounds = rounds
            return result

        if rounds >= max_rounds:
            result.errors.append(
                f"依赖环过多（>{max_rounds} 层中转），建议改小目标区间或分批执行"
            )
            result.rounds = rounds
            return result

        # 选「中转成本最小」的成员：簇数最少者（它要搬两遍）
        by_order = {t.order: t for t in result.tasks}
        candidate: Optional[int] = None
        candidate_cost = None
        for component in components:
            for member in component:
                task = by_order.get(member)
                if task is None:
                    continue
                cost = task.clusters
                if candidate_cost is None or cost < candidate_cost:
                    candidate, candidate_cost = member, cost
        if candidate is None:
            result.errors.append("检出依赖环但找不到可中转的任务")
            result.rounds = rounds
            return result

        task = by_order[candidate]
        relay_start = ledger_relay.allocate(task.clusters, near=window.end_lcn)
        if relay_start is None:
            result.errors.append(
                f"缺少 {task.clusters:,} 簇中转空间（文件 {task.path}）；"
                "建议扩大空闲区或减少选中文件"
            )
            result.rounds = rounds
            return result

        relay_out, relay_in = split_task_for_relay(task, relay_start)
        relay_in.order = max(t.order for t in result.tasks) + 1
        replaced: List[MoveTask] = []
        for item in result.tasks:
            if item.order == candidate:
                replaced.append(relay_out)
                replaced.append(relay_in)
            else:
                replaced.append(item)
        result.tasks = replaced
        result.inserted.append(candidate)
        rounds += 1
        result.rounds = rounds
        current_edges = build_dependencies(result.tasks)


def sort_tasks(
    tasks: Sequence[MoveTask],
    sweep: str = "ascending",
) -> Tuple[List[MoveTask], List[int], Dict[int, Set[int]]]:
    """便捷组合：建依赖图 + 拓扑电梯排序。"""
    edges = build_dependencies(tasks)
    ordered, remaining = order_elevator(tasks, edges, sweep)
    return ordered, remaining, edges


def merge_task_segments_for_execution(
    tasks: Sequence[MoveTask],
    batch_clusters: int,
    coalesce: bool = True,
) -> List[MoveTask]:
    """按任务合并段（保持任务顺序），返回**新的**任务列表（不修改入参）。

    ★ ``path_known`` 必须逐条继承（FIX-5）：重建 ``MoveTask`` 时漏传会退回
    默认 ``True``，于是"路径未知的文件不得执行"这道防线在段合并之后失效。
    """
    from app.core.io_profile import merge_segments

    merged: List[MoveTask] = []
    for task in tasks:
        segments = (
            merge_segments(task.segments, batch_clusters)
            if coalesce
            else [seg for seg in task.segments]
        )
        merged.append(
            MoveTask(
                order=task.order,
                phase=task.phase,
                item_index=task.item_index,
                path=task.path,
                path_known=task.path_known,
                segments=segments,
            )
        )
    return merged
