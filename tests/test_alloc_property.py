"""分配器的随机对拍。

``IntervalSet.subtract`` 与 ``FreeSpaceMap.allocate`` 是**数据安全**的核心：
前者决定"别人占的块还保不保护"，后者决定"写到哪里"。这两处为了性能从
"整表重算"改成了二分 + 局部重写，逻辑稍错一点就是静默覆盖别人的数据，
所以用朴素实现做对拍比读代码更靠得住。

朴素实现 = 一个布尔数组，逐块标记。只在小卷上跑，慢无所谓。
"""

from __future__ import annotations

import random

import pytest

from lbamover.contract.models import Extent, merge_extents
from lbamover.engine.alloc import FreeSpaceMap, IntervalSet

VOLUME = 240


def naive_occupied(extents) -> list[bool]:
    used = [False] * VOLUME
    for e in extents:
        for i in range(e.start, min(e.end, VOLUME)):
            used[i] = True
    return used


def runs(mask: list[bool]) -> list[Extent]:
    """把布尔掩码压成区间表。"""
    out: list[Extent] = []
    start = None
    for i, on in enumerate(mask):
        if on and start is None:
            start = i
        elif not on and start is not None:
            out.append(Extent(start, i - start))
            start = None
    if start is not None:
        out.append(Extent(start, VOLUME - start))
    return out


def naive_free_extents(used: list[bool]) -> list[Extent]:
    """卷上真正空闲的区间（= 没被任何文件占的块）。"""
    return runs([not u for u in used])


def naive_kept(base, removes) -> list[Extent]:
    """``base - removes``：只保留原本属于 base 且没被挖掉的块。"""
    keep = [False] * VOLUME
    for e in base:
        for i in range(e.start, e.end):
            keep[i] = True
    for e in removes:
        for i in range(e.start, e.end):
            keep[i] = False
    return runs(keep)


def random_extents(rng: random.Random, count: int) -> list[Extent]:
    raw = []
    for _ in range(count):
        start = rng.randrange(0, VOLUME - 1)
        length = rng.randint(1, min(20, VOLUME - start))
        raw.append(Extent(start, length))
    return merge_extents(raw)


class TestSubtractMatchesNaive:
    @pytest.mark.parametrize("seed", range(25))
    def test_subtract(self, seed):
        rng = random.Random(seed)
        base = random_extents(rng, 8)
        removes = random_extents(rng, 5)

        expected = naive_kept(base, removes)

        s = IntervalSet(base)
        s.subtract(removes)
        assert list(s) == expected

    @pytest.mark.parametrize("seed", range(10))
    def test_subtract_in_sequence(self, seed):
        """连续多次挖洞后仍然一致（规划器就是边搬边挖）。"""
        rng = random.Random(1000 + seed)
        base = random_extents(rng, 10)
        s = IntervalSet(base)
        applied: list[Extent] = []
        for _ in range(6):
            removes = random_extents(rng, 3)
            applied.extend(removes)
            s.subtract(removes)
            assert list(s) == naive_kept(base, applied)


class TestAllocateMatchesFirstFit:
    def _naive_allocate(self, free: list[bool], blocks: int, avoid: list[bool]) -> int | None:
        for start in range(0, VOLUME - blocks + 1):
            if all(free[i] and not avoid[i] for i in range(start, start + blocks)):
                return start
        return None

    @pytest.mark.parametrize("seed", range(30))
    def test_first_fit(self, seed):
        rng = random.Random(seed)
        occupied = random_extents(rng, 10)
        avoid_ext = random_extents(rng, 4)
        blocks = rng.randint(1, 6)

        used = naive_occupied(occupied)
        free_bool = [not u for u in used]
        avoid_bool = naive_occupied(avoid_ext)

        fsm = FreeSpaceMap(naive_free_extents(used), VOLUME)
        expected = self._naive_allocate(free_bool, blocks, avoid_bool)
        got = fsm.allocate(blocks, avoid=IntervalSet(avoid_ext))
        assert got == expected

    @pytest.mark.parametrize("seed", range(15))
    def test_repeated_allocation_keeps_state(self, seed):
        """连续分配多次，结果必须与朴素实现逐步一致（含 release）。"""
        rng = random.Random(500 + seed)
        occupied = random_extents(rng, 8)
        used = naive_occupied(occupied)
        fsm = FreeSpaceMap(naive_free_extents(used), VOLUME)
        avoid = IntervalSet()

        for _ in range(12):
            blocks = rng.randint(1, 5)
            free_bool = [not u for u in used]
            expected = None
            for start in range(0, VOLUME - blocks + 1):
                if all(free_bool[i] for i in range(start, start + blocks)):
                    expected = start
                    break
            got = fsm.allocate(blocks, avoid=avoid)
            assert got == expected
            if got is None:
                break
            for i in range(got, got + blocks):
                used[i] = True
            # 偶尔"搬走"一段（归还空间），状态必须继续与朴素实现一致
            if rng.random() < 0.5:
                start = rng.randrange(VOLUME)
                piece = Extent(start, min(rng.randint(1, 6), VOLUME - start))
                fsm.release([piece])
                for i in range(piece.start, piece.end):
                    used[i] = False
