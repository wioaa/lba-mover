"""碎片检测：从「文件 → LBA 区间」计算出可决策的指标。

这里刻意把**指标定义**和**可视化分桶**分开：

* 指标（``FragReport.file_frag_score`` / ``free_frag_score``）用于决策，
  口径必须稳定，改一次要能解释清楚；
* 分桶（``Bucket``）只用于画图，桶数变化不影响任何决策。

混在一起是常见错误：为了让图好看去调指标，最后整理策略跟着图一起漂移。
"""

from __future__ import annotations

from typing import Iterable, Sequence

from ..contract.models import (
    Bucket,
    Extent,
    FileFrag,
    FragReport,
    Geometry,
    merge_extents,
)

__all__ = [
    "analyze",
    "bucketize",
    "free_extents_from_files",
    "used_extents",
    "DEFAULT_BUCKETS",
]

#: 分布图默认桶数。256 在 4K 簇的 1TB 卷上约等于每桶 1GB，
#: 屏幕上每个桶 2~4 像素，既有细节又不至于画 100 万个矩形。
DEFAULT_BUCKETS = 256


def used_extents(files: Iterable[FileFrag]) -> tuple[Extent, ...]:
    """所有文件占用的区间（已合并）。"""
    flat: list[Extent] = []
    for f in files:
        flat.extend(f.extents)
    return tuple(merge_extents(flat))


def free_extents_from_files(
    geometry: Geometry, files: Iterable[FileFrag]
) -> tuple[Extent, ...]:
    """由「已用区间」反推自由区间（卷容量的补集）。

    真实实现里自由空间应该由文件系统的位图给出；这里用补集是因为
    **两条路径必须能互相校验**——真机扫描出来的自由区间接上文件表后，
    两者合并后应当正好等于卷容量，不等就说明扫描漏了东西。
    """
    used = sorted(
        (e for e in used_extents(files) if e.length > 0),
        key=lambda e: e.start,
    )
    out: list[Extent] = []
    cursor = 0
    for ext in used:
        lo = min(max(ext.start, 0), geometry.block_count)
        hi = min(max(ext.end, 0), geometry.block_count)
        if lo > cursor:
            out.append(Extent(cursor, lo - cursor))
        cursor = max(cursor, hi)
        if cursor >= geometry.block_count:
            break
    if cursor < geometry.block_count:
        out.append(Extent(cursor, geometry.block_count - cursor))
    return tuple(out)


def bucketize(
    geometry: Geometry,
    files: Sequence[FileFrag],
    free_extents: Sequence[Extent],
    bucket_count: int = DEFAULT_BUCKETS,
) -> tuple[Bucket, ...]:
    """把卷等分成 ``bucket_count`` 个桶，统计每桶的占用与 extent 密度。

    一次遍历完成，不构造 ``block_count`` 长度的数组——1TB 卷 4K 簇有
    2.6 亿块，任何「每块一个元素」的实现都会直接 OOM。
    """
    total = geometry.block_count
    n = max(1, min(bucket_count, total))
    used = [0] * n
    ecount = [0] * n

    def spread(ext: Extent) -> None:
        if ext.length <= 0:
            return
        lo = max(0, ext.start)
        hi = min(total, ext.end)
        if lo >= hi:
            return
        b0 = (lo * n) // total
        b1 = ((hi - 1) * n) // total
        for b in range(b0, b1 + 1):
            bs = (b * total) // n
            be = ((b + 1) * total) // n
            used[b] += max(0, min(hi, be) - max(lo, bs))
            ecount[b] += 1

    for f in files:
        for ext in f.extents:
            spread(ext)
    # 注意：自由区间**不**参与 extent 密度统计。整卷自由区通常就是一个大
    # extent，让它计入会给每个桶都加 1，整条热力带变成均匀底色 —— 看起来
    # "到处都是碎片"，实际信息量为零。自由空间的碎片程度另有
    # ``FragReport.free_frag_score`` 专门表达。

    out: list[Bucket] = []
    for b in range(n):
        bs = (b * total) // n
        be = ((b + 1) * total) // n
        length = be - bs
        out.append(
            Bucket(
                index=b,
                start=bs,
                end=be,
                used_blocks=min(used[b], length),
                free_blocks=max(0, length - used[b]),
                extent_count=ecount[b],
            )
        )
    return tuple(out)


def analyze(
    files: Sequence[FileFrag],
    free_extents: Sequence[Extent],
    geometry: Geometry,
    *,
    bucket_count: int = DEFAULT_BUCKETS,
    elapsed_s: float = 0.0,
    truncated: bool = False,
    note: str = "",
) -> FragReport:
    """汇总一份碎片报告。顺序：规范化 → 分桶 → 出指标。"""
    normalized_files = tuple(
        FileFrag(
            file_id=f.file_id,
            path=f.path,
            extents=tuple(merge_extents(f.extents)),
            attributes=f.attributes,
        )
        for f in files
    )
    free = tuple(merge_extents(free_extents))
    buckets = bucketize(geometry, normalized_files, free, bucket_count)
    return FragReport(
        geometry=geometry,
        files=normalized_files,
        free_extents=free,
        buckets=buckets,
        elapsed_s=elapsed_s,
        truncated=truncated,
        note=note,
    )
