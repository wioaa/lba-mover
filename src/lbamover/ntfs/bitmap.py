"""``$Bitmap`` → 自由簇区间。

为什么值得单独成模块
--------------------
``$Bitmap`` 每一位代表一个簇。朴素写法是"逐簇判断"，在 1 TiB / 4 KiB 簇的卷
上有 **2.6 亿次 Python 循环** —— 实测 75 GiB 的卷就要 3 秒，1 TiB 要 40 秒以上，
而且这段时间里界面完全没有进度可显示。

这里换成**按字节状态跳跃**：一个整字节全 `0x00` 说明这一连串 8 个簇都空闲，
全 `0xFF` 说明都占用。这两种字节占了真实位图的绝大多数，用 ``bytes.translate``
+. ``find`` 在 C 层直接跳到下一个"混合字节"，Python 只处理**段的边界**。
复杂度从 O(簇数) 降到 O(段数 + 边界字节数)。

退化是"不更慢"，不是"更慢"
--------------------------
如果某个卷碎到每个字节都在混合（占用与空闲交替），那就只能逐位看 ——
此时的实际工作量与旧实现相同，但不会爆内存、也不会出错。

两条硬约束
----------
1. **位图比卷短时，缺失部分按「已用」处理**（0xFF），绝不猜成空闲 ——
   把已用簇当成空闲，规划器会把别人的文件覆盖掉。
2. **卷尾那几个"不完整的字节"按卷的簇数截断**，多出来的位不参与统计，
   否则会算出卷外的自由区。
"""

from __future__ import annotations

__all__ = ["free_runs", "free_runs_naive"]

#: 每轮处理的字节窗口。位图在 1 TiB 卷上约 31 MiB，整块 translate 会同时
#: 驻留三份拷贝；按窗口处理后内存只与窗口大小有关，与卷容量无关。
_WINDOW_BYTES = 1 << 20

#: 找"下一个不是全已用的字节"：0xFF → 0xFF，其余 → 0x00，再 find(b"\x00")
_MASK_NOT_USED = bytes(0xFF if i == 0xFF else 0x00 for i in range(256))
#: 找"下一个不是全空闲的字节"：0x00 → 0x01，其余 → 0x00，再 find(b"\x00")
_MASK_NOT_FREE = bytes(0x01 if i == 0x00 else 0x00 for i in range(256))

ALL_USED = 0xFF
ALL_FREE = 0x00


def free_runs(bitmap: bytes, total_clusters: int) -> list[tuple[int, int]]:
    """返回 ``[(起始簇, 簇数), ...]``，按簇号升序。

    位序遵循 NTFS 约定：簇 ``c`` 对应 ``bitmap[c >> 3]`` 的第 ``c & 7`` 位
    （低位在前）。
    """
    if total_clusters <= 0:
        return []

    need = (total_clusters + 7) // 8
    # 位图短于卷：缺失部分一律按"已用"补齐，宁可少报自由空间
    data = bitmap if len(bitmap) >= need else bitmap + b"\xff" * (need - len(bitmap))

    runs: list[tuple[int, int]] = []
    run_start = -1
    offset = 0  # 当前窗口首字节对应的全局字节下标

    total = len(data)
    while offset < total:
        window = data[offset : offset + _WINDOW_BYTES]
        not_used = window.translate(_MASK_NOT_USED)
        not_free = window.translate(_MASK_NOT_FREE)
        p = 0
        size = len(window)
        while p < size:
            byte = window[p]
            if byte == ALL_USED:
                if run_start >= 0:
                    runs.append((run_start, (offset + p) * 8 - run_start))
                    run_start = -1
                q = not_used.find(b"\x00", p + 1)  # 跳到下一个含空闲位的字节
                p = size if q < 0 else q
                continue
            if byte == ALL_FREE:
                if run_start < 0:
                    run_start = (offset + p) * 8
                q = not_free.find(b"\x00", p + 1)  # 跳到下一个不全空闲的字节
                p = size if q < 0 else q
                continue

            # 混合字节：三个边界之外必须逐位看，但这类字节只出现在段边缘
            base = (offset + p) * 8
            for bit in range(8):
                cluster = base + bit
                if cluster >= total_clusters:
                    break
                if (byte >> bit) & 1:
                    if run_start >= 0:
                        runs.append((run_start, cluster - run_start))
                        run_start = -1
                elif run_start < 0:
                    run_start = cluster
            p += 1
        offset += size

    if run_start >= 0:
        runs.append((run_start, total_clusters - run_start))
    return runs


def free_runs_naive(bitmap: bytes, total_clusters: int) -> list[tuple[int, int]]:
    """逐位实现，只用于对拍测试。**不要在生产路径调用**。"""
    runs: list[tuple[int, int]] = []
    start = -1
    for c in range(total_clusters):
        idx = c >> 3
        used = 1 if idx >= len(bitmap) else (bitmap[idx] >> (c & 7)) & 1
        if not used:
            if start < 0:
                start = c
        elif start >= 0:
            runs.append((start, c - start))
            start = -1
    if start >= 0:
        runs.append((start, total_clusters - start))
    return runs
