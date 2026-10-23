"""FIX-1 回归：执行期影子位图（``app/core/shadow.py``）+ 守卫动态刷新。

覆盖三件事：

1. :class:`ShadowBitmap` 的位级语义（跨字节、越界、重复标记、patch 对齐、
   本地变更优先、与 ``BitmapSnapshot`` 的快/慢判定一致）；
2. **S9-strong 量化对照**（QA `p0_4b_deep.py` 的场景）：40 簇区间被占满、
   10 个 4 簇文件要装进去、10 个挡路者要迁出，共 20 段 ——
   静态快照被守卫拒绝 10/20，读影子位图后 **0/20**；
3. "计划生成之后目标被外部占用"必须被局部重读 + 影子位图拦住，且只影响那一段。

全部为纯内存运算，不碰任何设备句柄，也不执行一次 ``FSCTL_MOVE_FILE``。
"""

from __future__ import annotations

import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.guard import SafetyGuard  # noqa: E402
from app.core.models import (  # noqa: E402
    MEDIA_UNKNOWN,
    BitmapSnapshot,
    Extent,
    FileEntry,
    MoveFailure,
    MoveTask,
    TargetWindow,
)
from app.core.io_profile import IoProfile  # noqa: E402
from app.core.ownership import OwnershipIndex  # noqa: E402
from app.core.planner import RangePlanParams, build_range_plan  # noqa: E402
from app.core.safety import SafetyPolicy  # noqa: E402
from app.core.scheduler import merge_task_segments_for_execution, split_task_for_relay  # noqa: E402
from app.core.scanner import MoveWorker  # noqa: E402
from app.core.shadow import ShadowBitmap, apply_move  # noqa: E402
from app.core.models import MoveSegment  # noqa: E402

BPC = 4096


# --------------------------------------------------------------------------- #
# 构造工具
# --------------------------------------------------------------------------- #
def _bits(total: int, occupied: set) -> bytes:
    """由"占用簇集合"生成位图字节（bit 1 = 占用）。"""
    bits = bytearray((total + 7) // 8)
    for lcn in occupied:
        if 0 <= lcn < total:
            bits[lcn >> 3] |= 1 << (lcn & 7)
    return bytes(bits)


def _runs(total: int, occupied: set, *, want_free: bool) -> list:
    """把簇集合折成区间列表（``want_free=True`` 返回空闲区间）。"""
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


def _entry(path: str, lcn: int, clusters: int) -> FileEntry:
    return FileEntry(
        path=path,
        name=path.rsplit("\\", 1)[-1],
        size=clusters * BPC,
        extents=[Extent(vcn=0, lcn=lcn, clusters=clusters)],
    )


def _policy(total: int) -> SafetyPolicy:
    return SafetyPolicy(total_clusters=total, bytes_per_cluster=BPC)


# --------------------------------------------------------------------------- #
# ShadowBitmap 位级语义
# --------------------------------------------------------------------------- #
def test_mark_used_and_free_across_byte_boundary() -> None:
    shadow = ShadowBitmap(bytes(8), total_clusters=64)
    assert shadow.is_ready is True
    assert shadow.mark_used(5, 4) == 4
    for lcn in range(5, 9):
        assert shadow.is_free(lcn) is False, f"LCN {lcn} 应被占用"
    assert shadow.is_free(4) is True
    assert shadow.is_free(9) is True
    # 幂等：重复标记不产生新的变化
    assert shadow.mark_used(5, 4) == 0
    assert shadow.mark_free(5, 4) == 4
    assert shadow.range_is_free_fast(0, 64) is True


def test_mark_clamps_at_volume_edges() -> None:
    shadow = ShadowBitmap(bytes(8), total_clusters=64)
    # 越卷尾：只标记卷内部分，不抛异常
    assert shadow.mark_used(60, 100) == 4
    assert shadow.is_free(63) is False
    # 负起点：夹到 0（[-5, 3) 只有 0–2 落在卷内，故只改 3 位）
    assert shadow.mark_used(-5, 8) == 3
    for lcn in range(0, 3):
        assert shadow.is_free(lcn) is False
    assert shadow.is_free(3) is True
    # 完全越界：什么都不做
    assert shadow.mark_used(1000, 4) == 0
    assert shadow.mark_used(10, 0) == 0
    assert shadow.mark_used(10, -3) == 0


def test_range_check_matches_snapshot_on_random_data() -> None:
    """快判定必须与 ``BitmapSnapshot`` 逐簇判定完全一致（两个视图不能打架）。"""
    rng = random.Random(2024)
    total = 512
    for _ in range(40):
        occupied = {lcn for lcn in range(total) if rng.random() < 0.35}
        bits = _bits(total, occupied)
        shadow = ShadowBitmap(bits, total_clusters=total)
        snapshot = BitmapSnapshot(bits=bits, total_clusters=total, bytes_per_cluster=BPC)
        for _ in range(20):
            start = rng.randrange(0, total)
            clusters = rng.randint(1, 32)
            assert shadow.range_is_free_fast(start, clusters) == snapshot.range_is_free(
                start, clusters
            )
        # 越界两侧都必须判"不空闲"
        assert shadow.range_is_free_fast(total - 1, 2) is False
        assert shadow.range_is_free_fast(-1, 2) is False


def test_patch_respects_local_changes() -> None:
    """本地变更（我们亲眼确认的搬运结果）不得被磁盘补丁推翻。"""
    total = 64
    shadow = ShadowBitmap(bytes(8), total_clusters=total)
    shadow.mark_free(16, 8)                       # 本地：16–23 已释放
    assert shadow.local_clusters == 8
    # 磁盘补丁说 16–23 被占用（陈旧字节）→ 本地变更优先，位不变
    changed = shadow.patch(16, bytes([0xFF]))
    assert changed == 0
    assert shadow.range_is_free_fast(16, 8) is True
    # 同一字节里 24–31 没有本地标记 → 补丁生效
    shadow.patch(24, bytes([0x0F]))
    assert shadow.is_free(24) is False
    assert shadow.is_free(27) is False
    assert shadow.is_free(28) is True
    # 显式关掉 respect_local 时补丁可以覆盖本地标记
    assert shadow.patch(16, bytes([0xFF]), respect_local=False) == 8
    assert shadow.is_free(16) is False


def test_patch_with_bit_offset_and_unaligned_start() -> None:
    total = 64
    shadow = ShadowBitmap(bytes(8), total_clusters=total)
    # chunk 的 bit 3 对应簇 10 → chunk 的 bit3..7 落到簇 10..14（共 5 位）
    shadow.patch(10, bytes([0b11111000]), bit_offset=3)
    for lcn in range(10, 15):
        assert shadow.is_free(lcn) is False, lcn
    assert shadow.is_free(9) is True and shadow.is_free(15) is True
    # 负起点：起点 = 2 - 4 = -2，前 2 位被丢掉，绝不越界写
    shadow2 = ShadowBitmap(bytes(8), total_clusters=total)
    shadow2.patch(2, bytes([0xFF]), bit_offset=4)   # 6 位有效 → 簇 0..5
    for lcn in range(0, 6):
        assert shadow2.is_free(lcn) is False, lcn
    assert shadow2.is_free(6) is True


def test_clone_and_snapshot_are_independent() -> None:
    shadow = ShadowBitmap(bytes(8), total_clusters=64)
    shadow.mark_used(0, 8)
    copy = shadow.clone()
    copy.mark_free(0, 8)
    assert shadow.is_free(0) is False, "clone 后改副本不能影响原对象"
    assert copy.is_free(0) is True
    snapshot = shadow.to_snapshot()
    assert isinstance(snapshot, BitmapSnapshot)
    assert snapshot.total_clusters == 64
    assert snapshot.is_free(0) is False


def test_apply_move_frees_source_and_occupies_target() -> None:
    shadow = ShadowBitmap(_bits(64, set(range(8, 16))), total_clusters=64)
    assert shadow.range_is_free_fast(40, 8) is True
    apply_move(shadow, 8, 40, 8)
    assert shadow.range_is_free_fast(8, 8) is True, "源必须变空闲"
    assert shadow.range_is_free_fast(40, 8) is False, "目标必须变占用"


# --------------------------------------------------------------------------- #
# S9-strong：静态快照 vs 影子位图（FIX-1 的核心量化）
# --------------------------------------------------------------------------- #
def _s9_strong_plan():
    """区间 40 簇被 10 个挡路者占满，10 个 4 簇文件要装进去。"""
    total = 1024
    window = TargetWindow(300, 340)
    selected = [_entry(f"R:\\sel{i}.bin", i * 8, 4) for i in range(10)]
    blockers = [_entry(f"R:\\blk{i}.bin", 300 + i * 4, 4) for i in range(10)]

    occupied: set = set()
    for entry in selected + blockers:
        for extent in entry.extents:
            occupied.update(range(extent.lcn, extent.lcn + extent.clusters))

    ownership = OwnershipIndex.build(selected + blockers)
    params = RangePlanParams(
        total_clusters=total,
        bytes_per_cluster=BPC,
        window=window,
        policy=_policy(total),
        free_runs=_runs(total, occupied, want_free=True),
        occupied_runs=_runs(total, occupied, want_free=False),
        ownership=ownership,
        io_profile=IoProfile.build(MEDIA_UNKNOWN, 64, BPC, window_clusters=window.clusters),
    )
    plan = build_range_plan(selected, params)
    return plan, _policy(total), occupied


def test_s9_strong_static_snapshot_rejects_half() -> None:
    """对照组：如果守卫只看规划期静态快照，驱逐腾出的空间永远显示被占用。"""
    plan, policy, occupied = _s9_strong_plan()
    assert plan.feasibility is not None
    assert plan.feasibility.verdict == "needs_eviction"
    segments = [seg for task in plan.tasks for seg in task.segments]
    assert len(segments) == 20, f"场景应有 20 段，实际 {len(segments)}"

    guard = SafetyGuard(policy, BitmapSnapshot(bits=_bits(1024, occupied), total_clusters=1024))
    rejected = 0
    for task in plan.tasks:
        for segment in task.segments:
            ok, _reason = guard.allow(segment.dst_lcn, segment.clusters)
            if not ok:
                rejected += 1
                continue
            guard.reserve(segment.dst_lcn, segment.clusters)
    assert rejected == 10, f"静态快照应拒绝 10/20 段（当前 {rejected}）"


def test_s9_strong_shadow_bitmap_passes_all() -> None:
    """修复后：每段前刷新守卫、每段后登记影子位图 → 20/20 全部通过。"""
    plan, policy, occupied = _s9_strong_plan()
    shadow = ShadowBitmap(_bits(1024, occupied), total_clusters=1024)
    guard = SafetyGuard(policy, shadow)

    rejected = 0
    moved = 0
    for task in plan.tasks:
        guard.refresh(shadow)
        for segment in task.segments:
            ok, reason = guard.allow(segment.dst_lcn, segment.clusters)
            if not ok:
                rejected += 1
                continue
            guard.reserve(segment.dst_lcn, segment.clusters)
            apply_move(shadow, segment.src_lcn, segment.dst_lcn, segment.clusters)
            moved += segment.clusters
    assert rejected == 0, f"影子位图下不应有段被拒（实际 {rejected}）"
    assert moved == plan.total_move_clusters

    # 收尾不变量：所有目标块现在都"被占用"（我们刚写进去）——
    # 装载目标、驱逐目标都不例外（驱逐目标在区间之外）。
    for task in plan.tasks:
        for segment in task.segments:
            assert shadow.range_is_free_fast(segment.dst_lcn, segment.clusters) is False, (
                task.phase,
                segment,
            )


# --------------------------------------------------------------------------- #
# 外部占用：局部重读必须拦住，且只影响那一段
# --------------------------------------------------------------------------- #
def test_external_occupancy_is_caught_and_scoped() -> None:
    total = 64
    shadow = ShadowBitmap(bytes(8), total_clusters=total)
    guard = SafetyGuard(_policy(total), shadow)
    assert guard.allow(20, 4)[0] is True

    # 模拟"局部重读位图"拿到的字节：簇 20–23 被外部进程占用（bit4..7）
    shadow.patch(16, bytes([0xF0]))
    ok, reason = guard.allow(20, 4)
    assert ok is False
    assert "空闲" in reason

    # 其它区域不受影响（只影响被占用的那一段，不废掉整个计划）
    assert guard.allow(40, 4)[0] is True
    assert guard.allow(0, 4)[0] is True


# --------------------------------------------------------------------------- #
# MoveWorker 的段归属判定（不实例化 QThread）
# --------------------------------------------------------------------------- #
def _task(count: int = 3) -> MoveTask:
    return MoveTask(
        path="R:\\x.bin",
        segments=[MoveSegment(vcn=i, src_lcn=100 + i * 10, dst_lcn=500 + i * 10, clusters=4)
                  for i in range(count)],
    )


def test_succeeded_segments_all_when_no_failure() -> None:
    task = _task()
    class _Result:
        failures: list = []
        cancelled = False
    assert MoveWorker._succeeded_segments(task, _Result()) == task.segments


def test_succeeded_segments_none_when_cancelled() -> None:
    task = _task()
    class _Result:
        failures: list = []
        cancelled = True
    assert MoveWorker._succeeded_segments(task, _Result()) is None


def test_succeeded_segments_none_when_failure_not_attributable() -> None:
    """失败项 ``clusters == 0``（打开文件失败 / 路径未知）→ 无法归属 → None。"""
    task = _task()
    class _Result:
        cancelled = False
        failures = [MoveFailure(path="R:\\x.bin", message="打开文件失败")]
    assert MoveWorker._succeeded_segments(task, _Result()) is None


def test_succeeded_segments_partial() -> None:
    task = _task()
    failed_segment = task.segments[1]
    class _Result:
        cancelled = False
        failures = [
            MoveFailure(
                path="R:\\x.bin",
                src_lcn=failed_segment.src_lcn,
                dst_lcn=failed_segment.dst_lcn,
                clusters=failed_segment.clusters,
                winerror=5,
            )
        ]
    succeeded = MoveWorker._succeeded_segments(task, _Result())
    assert succeeded == [task.segments[0], task.segments[2]]


# --------------------------------------------------------------------------- #
# FIX-5：path_known 在任务重组后必须保留
# --------------------------------------------------------------------------- #
def test_path_known_survives_task_rewrites() -> None:
    task = MoveTask(
        order=7,
        phase="place",
        path="(record 4211)",
        path_known=False,
        segments=[MoveSegment(vcn=0, src_lcn=10, dst_lcn=100, clusters=4)],
    )
    merged = merge_task_segments_for_execution([task], 65536)
    assert merged[0].path_known is False, "段合并后 path_known 被丢成 True"
    relay_out, relay_in = split_task_for_relay(task, 900)
    assert relay_out.path_known is False
    assert relay_in.path_known is False
    assert relay_out.order == task.order
    assert relay_in.order == -1


if __name__ == "__main__":
    from tests._harness import exit_with

    exit_with(globals())
