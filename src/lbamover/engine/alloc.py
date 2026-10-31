"""自由空间管理与目标区分配。

这里的核心不变量比"能不能塞进去"更重要：

    **任何 op 的目标区，绝不允许覆盖"尚未搬走的文件"的源区。**

违反它的后果不是"效率低"，而是读到一半的数据被自己的写入覆盖 —— 换句话
说，源数据永久丢失。因此分配器接受一个 ``avoid`` 集合，规划器把"还没搬
的文件占的块"全部塞进去。
"""

from __future__ import annotations

from typing import Iterable, Sequence

from ..contract.models import Extent, merge_extents

__all__ = ["IntervalSet", "FreeSpaceMap"]


class IntervalSet:
    """不可重叠的区间集合。用于 ``avoid`` 与中间结果聚合。"""

    __slots__ = ("_items",)

    def __init__(self, extents: Iterable[Extent] = ()) -> None:
        self._items: list[Extent] = merge_extents(extents)

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self):
        return iter(self._items)

    @property
    def items(self) -> tuple[Extent, ...]:
        return tuple(self._items)

    def add(self, ext: Extent) -> None:
        if ext.length > 0:
            self._items = merge_extents([*self._items, ext])

    def add_many(self, extents: Iterable[Extent]) -> None:
        self._items = merge_extents([*self._items, *extents])

    def subtract(self, extents: Iterable[Extent]) -> None:
        """从集合中挖掉若干区间。

        必须按区间做差，而不是「丢掉被完全覆盖的整段」——合并后的区间
        常常横跨多个文件，整段丢弃会把仍需要保护的区域一起放开。
        """
        removes = [e for e in extents if e.length > 0]
        if not removes:
            return
        pieces: list[Extent] = []
        for e in self._items:
            pieces.extend(_subtract(e, removes))
        self._items = merge_extents(pieces)

    def copy(self) -> "IntervalSet":
        return IntervalSet(self._items)

    def intersects(self, ext: Extent) -> bool:
        return bool(self.overlapping(ext))

    def overlapping(self, ext: Extent) -> list[Extent]:
        if ext.length <= 0:
            return []
        return [e for e in self._items if e.overlaps(ext)]

    def contains_range(self, ext: Extent) -> bool:
        """``ext`` 是否被完全覆盖（用于断言目标区确实是空的）。"""
        cursor = ext.start
        for e in self._items:
            if e.start > cursor:
                return False
            if e.start <= cursor < e.end:
                cursor = e.end
                if cursor >= ext.end:
                    return True
        return cursor >= ext.end


def _subtract(ext: Extent, avoid: Sequence[Extent]) -> list[Extent]:
    """``ext`` 减去 ``avoid`` 后的极大片段，升序。"""
    pieces = [ext]
    for a in avoid:
        nxt: list[Extent] = []
        for p in pieces:
            if not p.overlaps(a):
                nxt.append(p)
                continue
            if a.start > p.start:
                nxt.append(Extent(p.start, a.start - p.start))
            if a.end < p.end:
                nxt.append(Extent(a.end, p.end - a.end))
        pieces = nxt
        if not pieces:
            break
    return [p for p in pieces if p.length > 0]


class FreeSpaceMap:
    """有序自由区间表，first-fit 分配。

    选 first-fit 而不是 best-fit：整理的目标本身就是"把文件往低地址聚拢"，
    best-fit 会把文件撒到各处，反而增加碎片。这个取舍写在
    ``docs/ARCHITECTURE.md`` 的"策略与取舍"一节。
    """

    __slots__ = ("_free", "_capacity")

    def __init__(self, extents: Iterable[Extent], capacity: int = 0) -> None:
        self._free: list[Extent] = merge_extents(e for e in extents if e.length > 0)
        self._capacity = capacity

    # -- 观测 -------------------------------------------------------------

    @property
    def free_blocks(self) -> int:
        return sum(e.length for e in self._free)

    @property
    def largest_run(self) -> int:
        return max((e.length for e in self._free), default=0)

    @property
    def run_count(self) -> int:
        return len(self._free)

    @property
    def extents(self) -> tuple[Extent, ...]:
        return tuple(self._free)

    def __len__(self) -> int:
        return len(self._free)

    # -- 分配 -------------------------------------------------------------

    def allocate(
        self,
        blocks: int,
        *,
        avoid: IntervalSet | Sequence[Extent] | None = None,
        from_tail: bool = False,
        min_start: int = 0,
        max_end: int | None = None,
    ) -> int | None:
        """找一段不小于 ``blocks`` 的空闲区间并**立即占用**。

        返回起始 LBA；无解返回 ``None``（调用方必须处理"放不下"这一分支，
        而不是假设磁盘总是够用）。
        """
        if blocks <= 0:
            raise ValueError("blocks 必须为正")
        avoid_seq = list(avoid) if avoid is not None else []
        order = list(reversed(self._free)) if from_tail else list(self._free)
        for ext in order:
            for piece in _subtract(ext, avoid_seq):
                lo = max(piece.start, min_start)
                hi = piece.end if max_end is None else min(piece.end, max_end)
                if hi - lo < blocks:
                    continue
                start = hi - blocks if from_tail else lo
                self._consume(Extent(start, blocks))
                return start
        return None

    def reserve(self, ext: Extent) -> bool:
        """占用指定区间。返回是否成功（区间必须完全空闲）。"""
        if not self.is_free(ext):
            return False
        self._consume(ext)
        return True

    def release(self, extents: Iterable[Extent]) -> None:
        self._free = merge_extents([*self._free, *extents])

    def is_free(self, ext: Extent) -> bool:
        if ext.length <= 0:
            return True
        return IntervalSet(self._free).contains_range(ext)

    # -- 内部 -------------------------------------------------------------

    def _consume(self, ext: Extent) -> None:
        kept: list[Extent] = []
        for e in self._free:
            if not e.overlaps(ext):
                kept.append(e)
                continue
            if ext.start > e.start:
                kept.append(Extent(e.start, ext.start - e.start))
            if ext.end < e.end:
                kept.append(Extent(ext.end, e.end - ext.end))
        self._free = merge_extents(kept)

    def __repr__(self) -> str:  # pragma: no cover
        return f"FreeSpaceMap(runs={self.run_count}, free={self.free_blocks})"
