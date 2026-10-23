"""全卷归属索引（N4 + N5 的协同点）—— **纯算法层**。

Win32 **没有** "由 LCN 反查文件路径" 的 API，因此只靠扫描过的文件清单来回答
「这个簇是谁的」。本索引把「所有已解析文件的 extents」组织成按 LCN 排序的
stabbing 索引，支持 ``区间 → 涉及文件集合`` 的 O(log n + k) 查询。

MFT 通道（N5）一次遍历就能拿到全卷文件表，因此**建索引对 MFT 通道是零额外
IO 成本**；只有回退到句柄通道时才需要单独扫一遍（见 ``IndexWorker``）。
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field
from typing import Dict, List, Sequence, Tuple

from app.core.models import Extent, FileEntry


@dataclass(slots=True)
class OwnershipIndex:
    """LCN 区间 → 文件 的查询索引（纯内存）。

    内部用四个平行数组存 ``(lcn, lcn_end, vcn, path_idx)``，按 ``lcn`` 升序；
    另存 ``_max_end_prefix`` 前缀最大右端点，用于反向剪枝，从而让单次查询
    代价只与"命中数"有关，而不随索引规模线性增长。
    """

    paths: List[str] = field(default_factory=list)
    #: 路径 → FileEntry（构建时保留引用，供挡路者取 size / 属性 / 可搬性判定）
    by_path: Dict[str, FileEntry] = field(default_factory=dict)
    _lcn: List[int] = field(default_factory=list)
    _end: List[int] = field(default_factory=list)
    _vcn: List[int] = field(default_factory=list)
    _pidx: List[int] = field(default_factory=list)
    _max_end_prefix: List[int] = field(default_factory=list)

    # ---------------- 构建 ---------------- #
    @classmethod
    def build(cls, entries: Sequence[FileEntry]) -> "OwnershipIndex":
        """从文件清单构建索引（自动跳过稀疏段与无分配文件）。"""
        index = cls()
        records: List[Tuple[int, int, int, int]] = []
        path_to_slot: Dict[str, int] = {}

        for entry in entries:
            if entry.path:
                index.by_path[entry.path] = entry
            for extent in entry.allocated_extents:
                if extent.clusters <= 0:
                    continue
                slot = path_to_slot.get(entry.path)
                if slot is None:
                    slot = len(index.paths)
                    path_to_slot[entry.path] = slot
                    index.paths.append(entry.path)
                records.append((extent.lcn, extent.lcn_end, extent.vcn, slot))

        records.sort(key=lambda item: (item[0], item[1]))
        index._lcn = [r[0] for r in records]
        index._end = [r[1] for r in records]
        index._vcn = [r[2] for r in records]
        index._pidx = [r[3] for r in records]

        running = 0
        prefix: List[int] = []
        for end in index._end:
            if end > running:
                running = end
            prefix.append(running)
        index._max_end_prefix = prefix
        return index

    # ---------------- 查询 ---------------- #
    def query(self, start: int, end: int) -> List[Tuple[str, Extent]]:
        """返回与 ``[start, end)`` 相交的所有 ``(path, 相交切片)``。

        切片是**裁剪后**的 extent：``vcn`` 已按偏移推进，``lcn`` 为交集的起点，
        因此可以直接用于移动调用（不会搬到文件之外的数据）。
        """
        if end <= start or not self._lcn:
            return []

        # lcn 升序：所有可能相交的记录都满足 lcn < end
        hi = bisect_left(self._lcn, end)
        hits: List[Tuple[str, Extent]] = []
        index = hi - 1
        while index >= 0:
            # 前缀最大值 <= start ⇒ 之前所有记录都结束于 start 之前，可以整体停
            if self._max_end_prefix[index] <= start:
                break
            rec_end = self._end[index]
            if rec_end > start:
                rec_start = self._lcn[index]
                left = max(rec_start, start)
                right = min(rec_end, end)
                if right > left:
                    hits.append(
                        (
                            self.paths[self._pidx[index]],
                            Extent(
                                vcn=self._vcn[index] + (left - rec_start),
                                lcn=left,
                                clusters=right - left,
                            ),
                        )
                    )
            index -= 1
        hits.reverse()
        return hits

    def covered(self, start: int, end: int) -> int:
        """``[start, end)`` 内已被索引覆盖的簇数。"""
        return sum(extent.clusters for _path, extent in self.query(start, end))

    def paths_in(self, start: int, end: int) -> List[str]:
        """``[start, end)`` 内出现过的文件路径（去重，保持首次出现顺序）。"""
        seen: Dict[str, None] = {}
        for path, _extent in self.query(start, end):
            seen.setdefault(path, None)
        return list(seen.keys())

    # ---------------- 状态 ---------------- #
    @property
    def extent_count(self) -> int:
        return len(self._lcn)

    @property
    def file_count(self) -> int:
        return len(self.paths)

    @property
    def total_clusters(self) -> int:
        return sum(e - s for s, e in zip(self._lcn, self._end))

    def is_empty(self) -> bool:
        return not self._lcn
