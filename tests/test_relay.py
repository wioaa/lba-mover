"""FIX-4 / FIX-5 回归：relay 回退可达 + ``path_known`` 不丢。

FIX-4 的核心：``packer._place_one`` 的 ``fallback`` 分支曾经是**死代码**
（``run_end - need`` 永远落在 run 内、``ledger.take`` 必成功，而自重叠检查在
``take`` 之前就 ``continue`` 了），于是"所有落点都自重叠"的文件被报成
``impossible``（"即使腾走全部可驱逐簇，仍差 N 簇"），而不是 ``role="relay"``
的两阶段中转。设计 §459 明确要求这里让上层标 relay。

FIX-5：``merge_task_segments_for_execution`` / ``split_task_for_relay`` 重建
``MoveTask`` 时丢过 ``path_known``，导致 ``execute_task`` 的"路径未知必须拒绝"
防线只剩字符串兜底。本文件用零写盘的方式验证拒绝确实发生。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.guard import SafetyGuard  # noqa: E402
from app.core.io_profile import IoProfile  # noqa: E402
from app.core.models import (  # noqa: E402
    MEDIA_UNKNOWN,
    VERDICT_IMPOSSIBLE,
    BitmapSnapshot,
    Extent,
    FileEntry,
    MoveSegment,
    MoveTask,
    TargetWindow,
)
from app.core.ownership import OwnershipIndex  # noqa: E402
from app.core.packer import SpaceLedger, _place_one, fdd_pack  # noqa: E402
from app.core.planner import RangePlanParams, build_range_plan  # noqa: E402
from app.core.safety import SafetyPolicy  # noqa: E402
from app.core.scheduler import (  # noqa: E402
    build_dependencies,
    merge_task_segments_for_execution,
    split_task_for_relay,
    violates_dependency,
)
from app.win32 import mover  # noqa: E402

BPC = 4096


def _entry(path: str, runs) -> FileEntry:
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
    )


def _bits(total: int, occupied: set) -> bytes:
    bits = bytearray((total + 7) // 8)
    for lcn in occupied:
        if 0 <= lcn < total:
            bits[lcn >> 3] |= 1 << (lcn & 7)
    return bytes(bits)


def _runs(total: int, occupied: set, *, want_free: bool) -> list:
    runs = []
    start = None
    for lcn in range(total):
        hit = (lcn not in occupied) if want_free else (lcn in occupied)
        if hit:
            if start is None:
                start = lcn
        elif start is not None:
            runs.append((start, lcn))
            start = None
    if start is not None:
        runs.append((start, total))
    return runs


def _plan(entries, window, total: int = 512, extra_occupied=()):
    occupied: set = set(extra_occupied)
    for entry in entries:
        for extent in entry.allocated_extents:
            occupied.update(range(extent.lcn, extent.lcn + extent.clusters))
    policy = SafetyPolicy(total_clusters=total, bytes_per_cluster=BPC)
    params = RangePlanParams(
        total_clusters=total,
        bytes_per_cluster=BPC,
        window=TargetWindow(*window),
        policy=policy,
        free_runs=_runs(total, occupied, want_free=True),
        occupied_runs=_runs(total, occupied, want_free=False),
        ownership=OwnershipIndex.build(entries),
        io_profile=IoProfile.build(
            MEDIA_UNKNOWN, 64, BPC, window_clusters=max(1, window[1] - window[0])
        ),
    )
    return build_range_plan(entries, params), policy, occupied


# --------------------------------------------------------------------------- #
# 1) _place_one：所有落点都自重叠时必须返回落点（不是 None）
# --------------------------------------------------------------------------- #
def test_place_one_returns_self_overlapping_candidate() -> None:
    """单处自重叠：区间 [100,115) 内每个落点都压到自身 extent。"""
    entry = _entry("R:\\A.bin", [(100, 5), (110, 5)])
    ledger = SpaceLedger([(100, 115)])
    target = _place_one(entry, entry.allocated_clusters, ledger, [(100, 115)], True)
    assert target is not None, "自重叠场景必须返回落点让上层标 relay（不能返回 None）"
    assert target == 100, f"应优先选段首，实际 {target}"
    # 该落点确实与自身 src 相交（由上层 detect_self_overlap 识别）
    assert any(extent.overlaps(target, target + 10) for extent in entry.allocated_extents)


def test_place_one_prefers_clean_spot_when_available() -> None:
    """有干净落点时**不能**退回 relay（否则会把普通搬迁变成两趟搬运）。"""
    entry = _entry("R:\\B.bin", [(100, 4)])
    ledger = SpaceLedger([(100, 200)])
    target = _place_one(entry, 4, ledger, [(100, 200)], True)
    assert target is not None
    assert not any(
        extent.overlaps(target, target + 4) for extent in entry.allocated_extents
    ), f"落点 {target} 不应与自身 src 重叠"
    # 紧贴自身 extent 末尾之后（b 分支）
    assert target == 104


def test_place_one_two_overlaps_still_reachable() -> None:
    """两处自重叠：仍要给出落点（relay 路径不能因为碎片多就失效）。"""
    entry = _entry("R:\\C.bin", [(100, 3), (110, 3), (120, 3)])
    ledger = SpaceLedger([(100, 130)])
    target = _place_one(entry, 9, ledger, [(100, 130)], True)
    assert target is not None
    assert 100 <= target <= 121


def test_fdd_pack_marks_placement_not_failure() -> None:
    """``fdd_pack`` 不能把自重叠场景判成"放不下"（那会让可行性评估误报不可行）。"""
    entry = _entry("R:\\A.bin", [(100, 5), (110, 5)])
    ledger = SpaceLedger([(100, 115)])
    placed, unplaced = fdd_pack([entry], ledger, [(100, 115)])
    assert unplaced == []
    assert placed["R:\\A.bin"] == 100


# --------------------------------------------------------------------------- #
# 2) 端到端：build_range_plan 产出 role="relay" 且计划可执行
# --------------------------------------------------------------------------- #
def test_range_plan_uses_relay_for_self_overlap() -> None:
    entry = _entry("R:\\A.bin", [(100, 5), (110, 5)])
    plan, _policy, _occupied = _plan([entry], (100, 115))

    assert plan.feasibility is not None
    assert plan.feasibility.verdict != VERDICT_IMPOSSIBLE, plan.note
    relay_items = [item for item in plan.items if item.role == "relay"]
    assert relay_items, f"应产出 relay 项，实际 note={plan.note}"
    item = relay_items[0]
    # 两阶段：src→relay（第一段）与 relay→dst（第二段）
    assert len(item.segments) == len(item.entry.allocated_extents) * 2
    assert item.relay_split == len(item.entry.allocated_extents)
    # 任务必须真的被拆成 relay_out + relay_in（分界点不能退化成 0）
    phases = [task.phase for task in plan.tasks]
    assert "relay_out" in phases and "relay_in" in phases, phases
    assert plan.relay_clusters == item.entry.allocated_clusters
    # 依赖序必须成立（relay_out 先于 relay_in，且不覆盖待读源）
    edges = build_dependencies(plan.tasks)
    assert violates_dependency(plan.tasks, edges) is None


def test_range_plan_uses_relay_for_three_fragments() -> None:
    """三碎片且窗口内**无干净落点**（每个空闲缝都 < 文件大小）→ 必须 relay。

    设计 §459/§601：relay 专用于「自重叠」与「依赖环」。三碎片文件若有干净
    连续落点就直接装箱（见 ``test_place_one_prefers_clean_spot``），本用例专门
    覆盖「奇数碎片 + 自重叠」这条需要 relay 的路径，并验证 ``relay_split`` 不会
    退化成 0（旧实现 ``len//2 if 偶数 else 0`` 在奇数段数时把分界点算成 0）。

    构造：窗口 [100,130)，文件占 [100,108)/[110,118)/[120,128)，窗口内空闲缝只有
    [108,110)/[118,120)/[128,130) 各 2 簇，全部 < 文件 24 簇，因此任何连续落点
    都压到文件自身 extent → 自重叠 → 必须中转。
    """
    entry = _entry("R:\\D.bin", [(100, 8), (110, 8), (120, 8)])
    plan, _policy, _occupied = _plan([entry], (100, 130))
    assert plan.feasibility is not None and plan.feasibility.verdict != VERDICT_IMPOSSIBLE
    relay_items = [item for item in plan.items if item.role == "relay"]
    assert relay_items, plan.note
    item = relay_items[0]
    # 三碎片：src→relay 3 段 + relay→dst 3 段
    assert item.relay_split == 3, f"三段文件的分界点应为 3，实际 {item.relay_split}"
    assert len(item.segments) == 6, f"应为 6 段（3+3），实际 {len(item.segments)}"
    assert [task.phase for task in plan.tasks] == ["relay_out", "relay_in"]


def test_swap_ring_is_broken_by_relay() -> None:
    """互为目标的 2 环：必须靠 relay 打断，且最终执行序不违反依赖。"""
    # A 占 [104,108)，B 占 [100,104)；区间正好是 [100,108)，两者要交换位置
    a = _entry("R:\\A.bin", [(104, 4)])
    b = _entry("R:\\B.bin", [(100, 4)])
    plan, _policy, _occupied = _plan([a, b], (100, 108))

    assert plan.feasibility is not None and plan.feasibility.verdict != VERDICT_IMPOSSIBLE, plan.note
    assert plan.tasks, plan.note
    edges = build_dependencies(plan.tasks)
    assert violates_dependency(plan.tasks, edges) is None
    # 2 环无解 → 必然出现中转任务
    phases = [task.phase for task in plan.tasks]
    assert "relay_in" in phases, f"2 环应被打断为中转，实际 {phases}"


# --------------------------------------------------------------------------- #
# 3) FIX-5：路径未知的任务零写调用
# --------------------------------------------------------------------------- #
def test_path_known_false_survives_rewrites_and_blocks_execution() -> None:
    task = MoveTask(
        order=3,
        phase="place",
        path="(record 98765)",
        path_known=False,
        segments=[MoveSegment(vcn=0, src_lcn=10, dst_lcn=200, clusters=4)],
    )
    merged = merge_task_segments_for_execution([task], 65536)
    assert merged[0].path_known is False
    out, inn = split_task_for_relay(merged[0], 300)
    assert out.path_known is False and inn.path_known is False

    guard = SafetyGuard(
        SafetyPolicy(total_clusters=512, bytes_per_cluster=BPC),
        BitmapSnapshot(bits=bytes(64), total_clusters=512, bytes_per_cluster=BPC),
    )
    # 假句柄：`execute_task` 必须在打开文件之前就拒绝，绝不会走到 FSCTL
    for candidate in (task, merged[0], out, inn):
        outcome = mover.execute_task(0, candidate, guard, None, None)
        assert outcome.moved_clusters == 0
        assert outcome.failures, "路径未知必须留下失败记录"
        assert "路径未知" in outcome.failures[0].message


if __name__ == "__main__":
    from tests._harness import exit_with

    exit_with(globals())
