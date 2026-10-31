"""分块：把「一次搬迁」切成流水线的原子单元。

分块规则只有一条，但必须严格遵守：

    目标偏移 = op 内已累计的块数（**不是**源 LBA 与目标的差）。

因为一个 op 的多个源 extent 在磁盘上并不连续（这正是"碎片"的定义），
搬到目标时必须**紧密拼接**。任何按"源地址 + 位移"计算的写法都会在第二个
extent 上写错位置——这是碎片搬迁最典型的错误，且在小文件上不容易暴露。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

from ..contract.models import Extent, MoveOp, Plan

__all__ = [
    "BlockSlice",
    "iter_extent_chunks",
    "slice_move_op",
    "slice_plan",
    "plan_chunk_count",
]


@dataclass(frozen=True, slots=True)
class BlockSlice:
    """一次「读 src_lba 起 blocks 块，写到 dst_lba 起」的最小作业单元。"""

    op_id: int
    src_lba: int
    dst_lba: int
    blocks: int
    #: 该 slice 在 op 内的块偏移，用于断点记录与日志定位
    offset_blocks: int

    def __post_init__(self) -> None:
        if self.blocks <= 0:
            raise ValueError(f"slice blocks 必须为正: {self.blocks}")
        if self.src_lba < 0 or self.dst_lba < 0:
            raise ValueError("slice 地址不能为负")

    @property
    def src_extent(self) -> Extent:
        return Extent(self.src_lba, self.blocks)

    @property
    def dst_extent(self) -> Extent:
        return Extent(self.dst_lba, self.blocks)

    @property
    def self_overlapping(self) -> bool:
        return self.src_extent.overlaps(self.dst_extent)

    def __str__(self) -> str:  # pragma: no cover
        return f"op{self.op_id}: {self.src_lba}->{self.dst_lba} x{self.blocks}"


def iter_extent_chunks(ext: Extent, chunk_blocks: int) -> Iterator[Extent]:
    """把一段连续区间按 ``chunk_blocks`` 切开。最后一块可能不满。"""
    if chunk_blocks <= 0:
        raise ValueError("chunk_blocks 必须为正")
    pos = ext.start
    left = ext.length
    while left > 0:
        n = min(chunk_blocks, left)
        yield Extent(pos, n)
        pos += n
        left -= n


def slice_move_op(op: MoveOp, chunk_blocks: int) -> tuple[BlockSlice, ...]:
    """把一个 op 切成有序的 :class:`BlockSlice` 序列。

    目标地址在跨源 extent 时**连续推进**，与源地址无关。
    """
    if chunk_blocks <= 0:
        raise ValueError("chunk_blocks 必须为正")
    out: list[BlockSlice] = []
    cursor = 0  # op 内偏移，等于已拼接的块数
    for ext in op.src_extents:
        for piece in iter_extent_chunks(ext, chunk_blocks):
            out.append(
                BlockSlice(
                    op_id=op.op_id,
                    src_lba=piece.start,
                    dst_lba=op.dst_start + cursor,
                    blocks=piece.length,
                    offset_blocks=cursor,
                )
            )
            cursor += piece.length
    return tuple(out)


def slice_plan(plan: Plan, chunk_blocks: int) -> dict[int, tuple[BlockSlice, ...]]:
    """按 op_id 返回各自的 slice 序列。保持 op 顺序。"""
    return {op.op_id: slice_move_op(op, chunk_blocks) for op in plan.ops}


def plan_chunk_count(plan: Plan, chunk_blocks: int) -> int:
    """计划总共需要多少个 chunk（进度分母）。"""
    if chunk_blocks <= 0:
        raise ValueError("chunk_blocks 必须为正")
    total = 0
    for op in plan.ops:
        for ext in op.src_extents:
            total += (ext.length + chunk_blocks - 1) // chunk_blocks
    return total
