"""自由空间管理与目标区分配。

这里的核心不变量比"能不能塞进去"更重要：

    **任何 op 的目标区，绝不允许覆盖"尚未搬走的文件"的源区。**

违反它的后果不是"效率低"，而是读到一半的数据被自己的写入覆盖 —— 换句话
说，源数据永久丢失。因此分配器接受一个 ``avoid`` 集合，规划器把"还没搬
的文件占的块"全部塞进去。
"""

from __future__ import annotations

import bisect
from typing import Iterable, Sequence

from ..contract.models import Extent, merge_extents

__all__ = ["IntervalSet", "FreeSpaceMap"]


class IntervalSet:
    """不可重叠的区间集合。用于 ``avoid`` 与中间结果聚合。"""

    __slots__ = ("_items", "_starts")

    def __init__(self, extents: Iterable[Extent] = ()) -> None:
        self._items: list[Extent] = merge_extents(extents)
        self._starts: list[int] = [e.start for e in self._items]

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self):
        return iter(self._items)

    def _reindex(self) -> None:
        self._starts = [e.start for e in self._items]

    @property
    def items(self) -> tuple[Extent, ...]:
        return tuple(self._items)

    def add(self, ext: Extent) -> None:
        if ext.length > 0:
            self._items = merge_extents([*self._items, ext])
            self._reindex()

    def add_many(self, extents: Iterable[Extent]) -> None:
        self._items = merge_extents([*self._items, *extents])
        self._reindex()

    def subtract(self, extents: Iterable[Extent]) -> None:
        """从集合中挖掉若干区间。

        必须按区间做差，而不是「丢掉被完全覆盖的整段」——合并后的区间
        常常横跨多个文件，整段丢弃会把仍需要保护的区域一起放开。

        用双指针扫一遍（两边都有序），而不是对每个元素调一次 ``_subtract``：
        后者是 O(元素数 × 被挖区间数)，真实卷上规划会跑到几分钟。
        """
        removes = merge_extents(e for e in extents if e.length > 0)
        if not removes:
            return
        out: list[Extent] = []
        i = 0
        m = len(removes)
        for e in self._items:
            cur: Extent | None = e
            while i < m and removes[i].end <= e.start:
                i += 1
            j = i
            while cur is not None and j < m and removes[j].start < cur.end:
                a = removes[j]
                if a.start > cur.start:
                    out.append(Extent(cur.start, a.start - cur.start))
                if a.end >= cur.end:
                    cur = None
                else:
                    cur = Extent(a.end, cur.end - a.end)
                    j += 1
            if cur is not None:
                out.append(cur)
        self._items = out
        self._reindex()

    def copy(self) -> "IntervalSet":
        return IntervalSet(self._items)

    def intersects(self, ext: Extent) -> bool:
        return bool(self.overlapping(ext))

    def overlapping(self, ext: Extent) -> list[Extent]:
        if ext.length <= 0:
            return []
        return self.overlap_range(ext.start, ext.end)

    def overlap_range(self, start: int, end: int) -> list[Extent]:
        """返回与 ``[start, end)`` 相交的区间，按起点升序。只碰真正相关的那几个。"""
        if end <= start:
            return []
        i = bisect.bisect_left(self._starts, start)
        if i > 0 and self._items[i - 1].end > start:
            i -= 1
        out: list[Extent] = []
        items = self._items
        n = len(items)
        while i < n and items[i].start < end:
            if items[i].end > start:
                out.append(items[i])
            i += 1
        return out

    def contains_range(self, ext: Extent) -> bool:
        """``ext`` 是否被完全覆盖（用于断言目标区确实是空的）。"""
        cursor = ext.start
        for e in self.overlap_range(ext.start, ext.end):
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

    __slots__ = ("_free", "_starts", "_capacity")

    def __init__(self, extents: Iterable[Extent], capacity: int = 0) -> None:
        self._free: list[Extent] = merge_extents(e for e in extents if e.length > 0)
        self._starts: list[int] = [e.start for e in self._free]
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

        实现要点：``avoid`` 是**有序**区间集合，所以"这段空闲区里哪部分被
        挡住"可以二分定位，只看真正重叠的那几个。早先的写法对每段空闲区都
        扫一遍整个 ``avoid``（几千个区间 × 上千个文件），真实卷上规划要跑
        好几分钟——看起来就像界面卡死。
        """
        if blocks <= 0:
            raise ValueError("blocks 必须为正")
        avoid_set = avoid if isinstance(avoid, IntervalSet) else IntervalSet(avoid or ())
        indices = (
            range(len(self._free) - 1, -1, -1) if from_tail else range(len(self._free))
        )
        for idx in indices:
            ext = self._free[idx]
            lo = max(ext.start, min_start)
            hi = ext.end if max_end is None else min(ext.end, max_end)
            if hi - lo < blocks:
                continue
            start = self._fit_in(avoid_set, lo, hi, blocks, from_tail)
            if start is not None:
                self._consume(Extent(start, blocks))
                return start
        return None

    def _fit_in(
        self, avoid: IntervalSet, lo: int, hi: int, blocks: int, from_tail: bool
    ) -> int | None:
        """在 ``[lo, hi)`` 里找一段避开 ``avoid`` 的连续空间。"""
        blockers = avoid.overlap_range(lo, hi)
        if not blockers:
            return hi - blocks if from_tail else lo
        gaps: list[tuple[int, int]] = []
        cursor = lo
        for a in blockers:
            s = max(a.start, lo)
            e = min(a.end, hi)
            if s > cursor:
                gaps.append((cursor, s))
            cursor = max(cursor, e)
        if cursor < hi:
            gaps.append((cursor, hi))
        for s, e in (reversed(gaps) if from_tail else gaps):
            if e - s >= blocks:
                return e - blocks if from_tail else s
        return None

    def reserve(self, ext: Extent) -> bool:
        """占用指定区间。返回是否成功（区间必须完全空闲）。"""
        if not self.is_free(ext):
            return False
        self._consume(ext)
        return True

    def release(self, extents: Iterable[Extent]) -> None:
        """归还未用区间，并与左右邻接合并。

        用有序表的二分插入，而不是"整表再 merge 一次"：后者每次 O(n log n)，
        在两千多个 op 的计划里是几十秒的开销，而这里只需要碰相邻两项。
        """
        for ext in sorted((e for e in extents if e.length > 0), key=lambda e: e.start):
            i = bisect.bisect_left(self._starts, ext.start)
            if i > 0 and self._free[i - 1].end >= ext.start:
                prev = self._free[i - 1]
                ext = Extent(prev.start, max(prev.end, ext.end) - prev.start)
                del self._free[i - 1]
                del self._starts[i - 1]
                i -= 1
            while i < len(self._free) and self._free[i].start <= ext.end:
                ext = Extent(ext.start, max(ext.end, self._free[i].end) - ext.start)
                del self._free[i]
                del self._starts[i]
            self._free.insert(i, ext)
            self._starts.insert(i, ext.start)

    def is_free(self, ext: Extent) -> bool:
        if ext.length <= 0:
            return True
        return IntervalSet(self._free).contains_range(ext)

    def covers(self, start: int, length: int) -> bool:
        """``[start, start+length)`` 是否**完全**落在某一段空闲区里。O(log n)。

        与 :meth:`is_free` 的区别是它不重建 ``IntervalSet``：后者是
        O(空闲段数)，在"每个文件查一次"的循环里会退化成 O(文件数 × 段数)，
        真实卷上就是几十秒的规划时间。尾部压实每一轮都要查，必须走这条。
        """
        if length <= 0:
            return True
        i = bisect.bisect_right(self._starts, start) - 1
        if i < 0:
            return False
        e = self._free[i]
        return e.start <= start and e.end >= start + length

    # -- 内部 -------------------------------------------------------------

    def _consume(self, ext: Extent) -> None:
        """占用一段区间：只重写与它重叠的那几个元素。"""
        i0 = bisect.bisect_left(self._starts, ext.start)
        if i0 > 0 and self._free[i0 - 1].end > ext.start:
            i0 -= 1
        i1 = i0
        n = len(self._free)
        while i1 < n and self._free[i1].start < ext.end:
            i1 += 1
        new: list[Extent] = []
        for e in self._free[i0:i1]:
            if not e.overlaps(ext):
                new.append(e)
                continue
            if ext.start > e.start:
                new.append(Extent(e.start, ext.start - e.start))
            if ext.end < e.end:
                new.append(Extent(ext.end, e.end - ext.end))
        self._free[i0:i1] = new
        self._starts[i0:i1] = [e.start for e in new]

    def __repr__(self) -> str:  # pragma: no cover
        return f"FreeSpaceMap(runs={self.run_count}, free={self.free_blocks})"
