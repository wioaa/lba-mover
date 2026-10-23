"""T02 验收：IO 调度层 ``app/core/io_profile.py``（设计文档 §6 T02 验收 1–3、5）。"""

from __future__ import annotations

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.io_profile import (  # noqa: E402
    MAX_BATCH_CLUSTERS,
    MIN_BATCH_CLUSTERS,
    PREWARM_MIN_BUFFER_MB,
    SWEEP_ASCENDING,
    SWEEP_DESCENDING,
    IoProfile,
    batch_clusters_for,
    effective_batch_bytes,
    media_policy,
    merge_segments,
    merge_task_segments,
    prewarm_budget_bytes,
    prewarm_chunk_bytes,
    sweep_for_window,
)
from app.core.models import MB, MEDIA_HDD, MEDIA_SSD, MEDIA_UNKNOWN, MoveSegment, TargetWindow  # noqa: E402


# --------------------------------------------------------------------------- #
# 1) media_policy 与设计 §4.2 表格逐项一致
# --------------------------------------------------------------------------- #
def test_media_policy_table() -> None:
    assert media_policy(MEDIA_HDD) == (True, True, True)
    assert media_policy(MEDIA_SSD) == (True, True, False)
    assert media_policy(MEDIA_UNKNOWN) == (True, True, False)
    # 未知字符串一律按 unknown 处理（保守回退）
    assert media_policy("nvme??") == (True, True, False)
    assert media_policy("") == (True, True, False)


# --------------------------------------------------------------------------- #
# 2) batch_clusters_for 换算 + 上下限
# --------------------------------------------------------------------------- #
def test_batch_clusters_table() -> None:
    assert batch_clusters_for(256, 4096) == 65_536
    assert batch_clusters_for(64, 4096) == 16_384
    assert batch_clusters_for(1024, 4096) == 65_536        # 被 65,536 硬上限夹住
    assert batch_clusters_for(1, 4096) == 256              # 1 MB / 4 KiB
    assert batch_clusters_for(0, 4096) == MIN_BATCH_CLUSTERS
    assert batch_clusters_for(-8, 4096) == MIN_BATCH_CLUSTERS
    # 簇很大时 1 MB 也凑不满 16 簇 → 被下限抬到 16
    assert batch_clusters_for(1, 1 * MB) == MIN_BATCH_CLUSTERS
    # 非法簇大小不崩
    assert batch_clusters_for(256, 0) == MIN_BATCH_CLUSTERS


def test_batch_clusters_bounded_by_window() -> None:
    """区间很小的时候，批次不应超过区间大小。"""
    assert batch_clusters_for(256, 4096, window_clusters=100) == 100
    assert batch_clusters_for(256, 4096, window_clusters=10_000_000) == MAX_BATCH_CLUSTERS


def test_batch_bytes_consistent() -> None:
    clusters = batch_clusters_for(256, 4096)
    assert effective_batch_bytes(clusters, 4096) == clusters * 4096 == 256 * MB


def test_prewarm_chunk_and_budget() -> None:
    assert prewarm_chunk_bytes(256) == 8 * MB           # 256/4 = 64 → 被上限夹到 8 MiB
    assert prewarm_chunk_bytes(8) == 2 * MB             # 8/4 = 2 MiB
    assert prewarm_chunk_bytes(1) == 1 * MB             # 0.25 → 下限 1 MiB
    assert prewarm_chunk_bytes(0) == 1 * MB
    assert prewarm_budget_bytes(256, 10 * MB) == 10 * MB
    assert prewarm_budget_bytes(64, 1000 * MB) == 64 * MB


# --------------------------------------------------------------------------- #
# 3) merge_segments 的三种连续性与守恒
# --------------------------------------------------------------------------- #
def test_merge_fully_contiguous() -> None:
    segments = [
        MoveSegment(vcn=0, src_lcn=100, dst_lcn=1000, clusters=10),
        MoveSegment(vcn=10, src_lcn=110, dst_lcn=1010, clusters=10),
        MoveSegment(vcn=20, src_lcn=120, dst_lcn=1020, clusters=10),
    ]
    merged = merge_segments(segments, max_clusters=1_000)
    assert len(merged) == 1
    assert merged[0].vcn == 0 and merged[0].src_lcn == 100
    assert merged[0].dst_lcn == 1000 and merged[0].clusters == 30


def test_merge_rejects_src_discontinuity() -> None:
    segments = [
        MoveSegment(vcn=0, src_lcn=100, dst_lcn=1000, clusters=10),
        MoveSegment(vcn=10, src_lcn=200, dst_lcn=1010, clusters=10),   # src 断层
    ]
    assert len(merge_segments(segments, 1_000)) == 2


def test_merge_rejects_dst_discontinuity() -> None:
    segments = [
        MoveSegment(vcn=0, src_lcn=100, dst_lcn=1000, clusters=10),
        MoveSegment(vcn=10, src_lcn=110, dst_lcn=2000, clusters=10),   # dst 断层
    ]
    assert len(merge_segments(segments, 1_000)) == 2


def test_merge_rejects_vcn_discontinuity() -> None:
    segments = [
        MoveSegment(vcn=0, src_lcn=100, dst_lcn=1000, clusters=10),
        MoveSegment(vcn=50, src_lcn=110, dst_lcn=1010, clusters=10),   # vcn 断层
    ]
    assert len(merge_segments(segments, 1_000)) == 2


def test_merge_splits_by_limit_and_conserves() -> None:
    segments = [
        MoveSegment(vcn=0, src_lcn=100, dst_lcn=1000, clusters=40),
        MoveSegment(vcn=40, src_lcn=140, dst_lcn=1040, clusters=40),
    ]
    before = sum(s.clusters for s in segments)
    merged = merge_segments(segments, max_clusters=25)
    after = sum(s.clusters for s in merged)
    assert after == before                                  # 守恒
    assert all(s.clusters <= 25 for s in merged)
    # 切分后仍必须首尾相接，不重不漏
    assert merged[0].vcn == 0 and merged[0].src_lcn == 100 and merged[0].dst_lcn == 1000
    cursor_vcn = 0
    cursor_src = 100
    cursor_dst = 1000
    for seg in merged:
        assert seg.vcn == cursor_vcn
        assert seg.src_lcn == cursor_src
        assert seg.dst_lcn == cursor_dst
        cursor_vcn += seg.clusters
        cursor_src += seg.clusters
        cursor_dst += seg.clusters
    assert cursor_vcn == before


def test_merge_skips_empty_and_handles_empty_input() -> None:
    assert merge_segments([], 100) == []
    segments = [
        MoveSegment(vcn=0, src_lcn=1, dst_lcn=2, clusters=0),
        MoveSegment(vcn=0, src_lcn=1, dst_lcn=2, clusters=5),
    ]
    assert len(merge_segments(segments, 100)) == 1


def test_merge_invalid_limit() -> None:
    try:
        merge_segments([MoveSegment(0, 1, 2, 3)], 0)
    except ValueError:
        return
    raise AssertionError("max_clusters=0 应当抛 ValueError")


def test_merge_task_segments_no_coalesce() -> None:
    grouped = [
        [
            MoveSegment(vcn=0, src_lcn=100, dst_lcn=1000, clusters=10),
            MoveSegment(vcn=10, src_lcn=110, dst_lcn=1010, clusters=10),
        ]
    ]
    assert len(merge_task_segments(grouped, 1000, coalesce=True)) == 1
    assert len(merge_task_segments(grouped, 1000, coalesce=False)) == 2


# --------------------------------------------------------------------------- #
# IoProfile
# --------------------------------------------------------------------------- #
def test_io_profile_hdd_enables_prewarm() -> None:
    profile = IoProfile.build(MEDIA_HDD, 256, 4096)
    assert profile.prewarm is True
    assert profile.batch_clusters == 65_536
    assert profile.badge[1] == "success"
    assert "HDD 加速已启用" in profile.summary


def test_io_profile_ssd_disables_prewarm() -> None:
    profile = IoProfile.build(MEDIA_SSD, 256, 4096)
    assert profile.prewarm is False
    assert profile.badge[1] == "neutral"


def test_io_profile_unknown_is_conservative() -> None:
    profile = IoProfile.build(MEDIA_UNKNOWN, 256, 4096)
    assert profile.prewarm is False
    assert profile.coalesce is True and profile.elevator is True
    assert profile.badge[1] == "warning"
    assert "保守" in profile.summary


def test_io_profile_prewarm_gate_by_buffer() -> None:
    """D1：HDD 且缓冲 ≥ 64 MB 才启用预热。"""
    assert IoProfile.build(MEDIA_HDD, 64, 4096).prewarm is True
    assert IoProfile.build(MEDIA_HDD, 63, 4096).prewarm is False
    assert IoProfile.build(MEDIA_HDD, 256, 4096, prefer_prewarm=False).prewarm is False
    assert PREWARM_MIN_BUFFER_MB == 64


def test_io_profile_cap_note() -> None:
    capped = IoProfile.build(MEDIA_HDD, 1024, 4096)
    assert capped.capped is True
    assert "单次搬运上限" in capped.cap_note
    normal = IoProfile.build(MEDIA_HDD, 64, 4096)
    assert normal.capped is False
    assert "单次搬运上限" not in normal.cap_note


def test_io_profile_honest_note() -> None:
    note = IoProfile.build(MEDIA_HDD, 256, 4096).honest_note
    assert "不保证线性提速" in note
    assert "零收益" in note


def test_sweep_for_window() -> None:
    total = 1000
    assert sweep_for_window(TargetWindow(600, 900), total) == SWEEP_DESCENDING
    assert sweep_for_window(TargetWindow(100, 400), total) == SWEEP_ASCENDING
    assert sweep_for_window(None, total) == SWEEP_ASCENDING
    assert sweep_for_window(TargetWindow(0, 10), 0) == SWEEP_ASCENDING


# --------------------------------------------------------------------------- #
# 5) 零 Qt 依赖断言（干净子进程）
# --------------------------------------------------------------------------- #
def test_module_has_no_qt_or_ctypes() -> None:
    """在干净子进程里导入本模块，断言**新增**加载的模块里没有 Qt / ctypes / app.win32。

    注意：Windows 上的 CPython 在解释器启动阶段就会加载 ``ctypes``（site 初始化），
    所以只能对「导入本模块引起的增量」做断言，这也是真正有意义的边界。
    """
    code = (
        "import sys; sys.path.insert(0, %r);"
        "baseline = set(sys.modules);"
        "import app.core.io_profile as m;"
        "new = set(sys.modules) - baseline;"
        "bad = [n for n in new if n.startswith('PySide6')];"
        "assert not bad, 'io_profile 不应加载 PySide6: %%s' %% bad;"
        "win = [n for n in new if n.startswith('app.win32')];"
        "assert not win, 'io_profile 不应加载 app.win32: %%s' %% win;"
        "assert 'ctypes' not in new, 'io_profile 不应引起 ctypes 加载';"
        "print('clean')"
    ) % os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "clean" in result.stdout


if __name__ == "__main__":
    from tests._harness import exit_with

    exit_with(globals())
