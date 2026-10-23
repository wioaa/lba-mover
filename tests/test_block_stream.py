"""1.0.0C 用户态分块流式搬迁内核回归（``app/core/block_stream.py``，纯逻辑）。

覆盖：65536 簇为 block 的分块计划、按 block 倍数积攒的可调缓存、双阶段流水线调度
（store 必在 load 后、暂存驻留 ≤ 缓冲倍数、缓冲 ≥2 时有读写重叠、=1 时串行）。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.block_stream import (  # noqa: E402
    BLOCK_CLUSTERS,
    Block,
    BlockBuffer,
    has_pipeline_overlap,
    plan_blocks,
    simulate_peak_resident,
    stream_schedule,
)

BPC = 4096
BLOCK_BYTES = BLOCK_CLUSTERS * BPC  # 65536 簇 = 256 MiB


# ---------------------------------------------------------------- 分块计划
def test_plan_blocks_empty_on_nonpositive() -> None:
    assert plan_blocks(0, BPC) == []
    assert plan_blocks(-100, BPC) == []
    assert plan_blocks(1000, 0) == []  # 每簇字节非法


def test_plan_blocks_exact_and_partial() -> None:
    assert plan_blocks(BLOCK_BYTES, BPC) == [Block(0, 0, BLOCK_BYTES)]
    total = BLOCK_BYTES * 2 + 100
    blocks = plan_blocks(total, BPC)
    assert blocks == [
        Block(0, 0, BLOCK_BYTES),
        Block(1, BLOCK_BYTES, BLOCK_BYTES),
        Block(2, BLOCK_BYTES * 2, 100),
    ]


def test_plan_blocks_uses_65536_cluster_block() -> None:
    """默认 block 就是 65536 簇。"""
    one = plan_blocks(BLOCK_BYTES, BPC)
    assert one[0].length == 65536 * BPC


def test_plan_blocks_custom_block_for_unit() -> None:
    blocks = plan_blocks(10, bytes_per_cluster=1, block_clusters=4)
    assert blocks == [Block(0, 0, 4), Block(1, 4, 4), Block(2, 8, 2)]


# ---------------------------------------------------------------- 可调缓存
def test_block_buffer_capacity_is_block_multiple() -> None:
    buf = BlockBuffer(buffer_blocks=3)
    assert buf.capacity_clusters == 3 * BLOCK_CLUSTERS == 196608
    assert buf.capacity_bytes == 196608 * BPC
    # 恒为 block 的整数倍
    assert buf.capacity_clusters % BLOCK_CLUSTERS == 0


def test_block_buffer_single_block() -> None:
    buf = BlockBuffer(buffer_blocks=1)
    assert buf.capacity_clusters == BLOCK_CLUSTERS


def test_block_buffer_rejects_zero() -> None:
    try:
        BlockBuffer(buffer_blocks=0)
    except ValueError:
        return
    raise AssertionError("buffer_blocks=0 应抛 ValueError")


# ---------------------------------------------------------------- 流水线调度
def _check_covers_and_orders(ops, num_blocks: int) -> None:
    loads = [i for k, i in ops if k == "load"]
    stores = [i for k, i in ops if k == "store"]
    assert sorted(loads) == list(range(num_blocks)), "每块都应被 load 一次"
    assert sorted(stores) == list(range(num_blocks)), "每块都应被 store 一次"
    for i in range(num_blocks):
        assert ops.index(("load", i)) < ops.index(("store", i)), f"块 {i} 的 store 必须先 load"


def test_stream_schedule_covers_and_orders() -> None:
    ops = stream_schedule(6, buffer_blocks=3)
    _check_covers_and_orders(ops, 6)


def test_stream_schedule_respects_buffer_bound() -> None:
    for buf in (1, 2, 3, 5):
        ops = stream_schedule(9, buffer_blocks=buf)
        assert simulate_peak_resident(ops) <= buf, f"驻留不得超过缓冲倍数 {buf}"


def test_stream_schedule_overlaps_when_buffer_ge2() -> None:
    ops = stream_schedule(6, buffer_blocks=2)
    assert has_pipeline_overlap(ops), "缓冲 ≥2 应有 load/store 重叠（流水加速）"
    assert simulate_peak_resident(ops) == 2


def test_stream_schedule_serial_when_buffer_1() -> None:
    ops = stream_schedule(4, buffer_blocks=1)
    assert simulate_peak_resident(ops) == 1
    assert not has_pipeline_overlap(ops), "缓冲=1 应严格串行"
    assert ops == [
        ("load", 0), ("store", 0),
        ("load", 1), ("store", 1),
        ("load", 2), ("store", 2),
        ("load", 3), ("store", 3),
    ]


def test_stream_schedule_rejects_zero_buffer() -> None:
    try:
        stream_schedule(4, buffer_blocks=0)
    except ValueError:
        return
    raise AssertionError("buffer_blocks=0 应抛 ValueError")


if __name__ == "__main__":
    from tests._harness import exit_with

    exit_with(globals())
