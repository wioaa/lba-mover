"""1.0.0F 分块流式自动模式的挑选函数回归（``densify.auto_stream_selection``）。

只测「挑谁」，不测落点——落点由文件系统在空闲区分配（流式迁移的语义）。
关键断言：大文件优先、容量上限、放不下就跳过后继续挑、不可移动排除、不限容量时的兜底。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.densify import auto_stream_selection  # noqa: E402
from app.core.models import Extent, FileEntry  # noqa: E402

BPC = 4096
MIB = 1024 * 1024


def _entry(path: str, clusters: int, *, movable: bool = True, reason: str = "") -> FileEntry:
    return FileEntry(
        path=path,
        name=path.rsplit("\\", 1)[-1],
        size=clusters * BPC,
        extents=[Extent(vcn=0, lcn=0, clusters=clusters)],
        movable=movable,
        reason="" if movable else (reason or "不可移动"),
    )


def test_largest_first_order() -> None:
    small = _entry("R:\\a.bin", 10)
    huge = _entry("R:\\b.bin", 900)
    mid = _entry("R:\\c.bin", 100)
    pick = auto_stream_selection([small, huge, mid], bytes_per_cluster=BPC)
    assert [e.path for e in pick.selected] == ["R:\\b.bin", "R:\\c.bin", "R:\\a.bin"]


def test_capacity_is_hard_limit() -> None:
    a = _entry("R:\\a.bin", 100)     # 100 * 4096 = 400 KiB
    b = _entry("R:\\b.bin", 100)
    limit = 400 * 1024 + 100 * 1024  # 只够 a + 四分之一 b（放不下就跳过）
    pick = auto_stream_selection([a, b], capacity_bytes=limit, bytes_per_cluster=BPC)
    assert [e.path for e in pick.selected] == ["R:\\a.bin"]
    assert pick.skipped_oversize == 1
    assert pick.total_bytes <= limit


def test_oversize_skipped_then_smaller_taken() -> None:
    """放不下的大文件被跳过，但更小的文件仍会被挑进来（不是停止挑选）。"""
    huge = _entry("R:\\huge.bin", 5000)      # ~20 MiB
    small = _entry("R:\\small.bin", 100)     # 400 KiB
    limit = 2 * MIB
    pick = auto_stream_selection([huge, small], capacity_bytes=limit, bytes_per_cluster=BPC)
    assert [e.path for e in pick.selected] == ["R:\\small.bin"]
    assert pick.skipped_oversize == 1
    assert "填充率" in pick.note


def test_unmovable_and_zero_cluster_excluded() -> None:
    ok = _entry("R:\\ok.bin", 50)
    locked = _entry("R:\\locked.bin", 9000, movable=False, reason="被独占")
    empty = _entry("R:\\empty.bin", 0)
    pick = auto_stream_selection([ok, locked, empty], bytes_per_cluster=BPC)
    assert [e.path for e in pick.selected] == ["R:\\ok.bin"]
    assert pick.skipped_unmovable == 2


def test_no_capacity_falls_back_to_free_bytes() -> None:
    """未设容量 → 以卷可用空间为兜底上限。"""
    a = _entry("R:\\a.bin", 100)   # 400 KiB
    b = _entry("R:\\b.bin", 100)   # 400 KiB
    free = 500 * 1024              # 只够一个
    pick = auto_stream_selection([a, b], bytes_per_cluster=BPC, free_bytes=free)
    assert pick.capacity_bytes == free
    assert len(pick.selected) == 1


def test_no_capacity_and_no_free_is_unbounded() -> None:
    a = _entry("R:\\a.bin", 10)
    pick = auto_stream_selection([a], bytes_per_cluster=BPC, free_bytes=0)
    assert pick.capacity_bytes == 0        # 0 = 不限
    assert len(pick.selected) == 1
    assert "未设容量上限" in pick.note


def test_min_clusters_filter() -> None:
    big = _entry("R:\\big.bin", 500)
    tiny = _entry("R:\\tiny.bin", 5)
    pick = auto_stream_selection([big, tiny], bytes_per_cluster=BPC, min_clusters=100)
    assert [e.path for e in pick.selected] == ["R:\\big.bin"]
    assert pick.skipped_unmovable == 1


def test_empty_input_is_safe() -> None:
    pick = auto_stream_selection([], bytes_per_cluster=BPC)
    assert pick.selected == []
    assert pick.total_bytes == 0
    assert pick.note


if __name__ == "__main__":
    from tests._harness import exit_with

    exit_with(globals())
