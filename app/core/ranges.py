"""区间运算工具（**纯函数**，core 层内部复用）。

之所以独立成模块而不是从 ``app.win32.bitmap`` 复用：

* 设计约定「算法层禁止 import ctypes」，而 ``app.win32.bitmap`` 会经由
  ``app.win32.api`` 间接加载 ctypes；
* 这些函数被 safety / guard / packer / scheduler 共同使用，放在一处避免
  四份重复实现。

所有区间统一为 **半开区间** ``(start, end)``，且都是 ``int`` 簇号。
"""

from __future__ import annotations

from typing import Iterable, List, Sequence, Tuple

Run = Tuple[int, int]


def merge_runs(runs: Iterable[Run]) -> List[Run]:
    """合并重叠 / 相邻区间，返回升序、互不相交的区间列表。"""
    ordered = sorted((int(s), int(e)) for s, e in runs if e > s)
    if not ordered:
        return []
    merged: List[Run] = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            if end > last_end:
                merged[-1] = (last_start, end)
        else:
            merged.append((start, end))
    return merged


def subtract_runs(runs: Sequence[Run], holes: Sequence[Run]) -> List[Run]:
    """从 ``runs`` 中扣除 ``holes``（两者都会先做归一化）。"""
    if not runs:
        return []
    normalized_runs = merge_runs(runs)
    normalized_holes = merge_runs(holes)
    if not normalized_holes:
        return list(normalized_runs)

    result: List[Run] = []
    for start, end in normalized_runs:
        cursor = start
        for hole_start, hole_end in normalized_holes:
            if hole_end <= cursor or hole_start >= end:
                continue
            if hole_start > cursor:
                result.append((cursor, min(hole_start, end)))
            cursor = max(cursor, hole_end)
            if cursor >= end:
                break
        if cursor < end:
            result.append((cursor, end))
    return [(s, e) for s, e in result if e > s]


def run_length(runs: Sequence[Run]) -> int:
    """区间总簇数。"""
    return sum(e - s for s, e in runs)


def overlaps_any(ranges: Sequence[Run], start: int, end: int) -> bool:
    """``[start, end)`` 是否与任一区间相交。"""
    if end <= start:
        return False
    for run_start, run_end in ranges:
        if run_start < end and start < run_end:
            return True
    return False


def find_containing(runs: Sequence[Run], lcn: int) -> int:
    """返回包含 ``lcn`` 的区间下标（-1 表示不落在任何区间内）。

    ``runs`` 必须已归一化（``merge_runs`` 的产物）—— 用二分查找实现。
    """
    low = 0
    high = len(runs) - 1
    while low <= high:
        mid = (low + high) // 2
        start, end = runs[mid]
        if lcn < start:
            high = mid - 1
        elif lcn >= end:
            low = mid + 1
        else:
            return mid
    return -1


def ceil_div(numerator: int, denominator: int) -> int:
    """整数向上取整除法（``denominator`` 必须为正）。"""
    if denominator <= 0:
        raise ValueError("denominator 必须为正数")
    return -(-int(numerator) // int(denominator))


def clamp(value: int, low: int, high: int) -> int:
    """把 ``value`` 夹到 ``[low, high]``；若 ``low > high`` 以 ``low`` 为准。"""
    if high < low:
        high = low
    return max(low, min(value, high))
