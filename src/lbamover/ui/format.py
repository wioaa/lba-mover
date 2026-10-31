"""展示层格式化。所有"给人看的数字"都从这里出。

统一的好处不只是好看：**同一份数据在不同控件上说法一致**。旧版进度栏写
"1.2 GB/s"、日志里写"1234.5 MB/s"，用户会以为是两个不同的指标。
"""

from __future__ import annotations

__all__ = ["fmt_bytes", "fmt_rate", "fmt_eta", "fmt_count", "fmt_percent", "fmt_blocks"]


def fmt_bytes(n: float, *, precision: int | None = None) -> str:
    """字节 → 人类可读。二进制单位（KiB/MiB），与磁盘工具一致。"""
    sign = "-" if n < 0 else ""
    v = abs(float(n))
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    idx = 0
    while v >= 1024 and idx < len(units) - 1:
        v /= 1024.0
        idx += 1
    if precision is None:
        precision = 0 if idx == 0 else (1 if v < 10 else (2 if v < 100 else 3))
    # 收敛掉 "1023.99 KiB" 这种会让人误读的尾数
    if v >= 999.95 and idx < len(units) - 1:
        v /= 1024.0
        idx += 1
    return f"{sign}{v:.{precision}f} {units[idx]}"


def fmt_rate(bytes_per_sec: float) -> str:
    if bytes_per_sec <= 0:
        return "—"
    return f"{fmt_bytes(bytes_per_sec)}/s"


def fmt_eta(seconds: float | None) -> str:
    """剩余时间。``None`` 表示算不出来（速率过低），显示"—"而不是瞎猜。"""
    if seconds is None or seconds < 0:
        return "—"
    s = int(round(seconds))
    if s < 60:
        return f"{s} 秒"
    if s < 3600:
        return f"{s // 60} 分 {s % 60:02d} 秒"
    if s < 86400:
        return f"{s // 3600} 小时 {s % 3600 // 60:02d} 分"
    return f"{s // 86400} 天 {s % 86400 // 3600} 小时"


def fmt_count(n: int | float) -> str:
    """大整数加千位分隔；超过一百万用 M 缩写。"""
    n = int(n)
    if abs(n) >= 10_000_000:
        return f"{n / 1_000_000:.1f}M"
    return f"{n:,}"


def fmt_percent(ratio: float, decimals: int = 1) -> str:
    return f"{max(0.0, min(1.0, ratio)) * 100:.{decimals}f}%"


def fmt_blocks(blocks: int, block_size: int = 4096) -> str:
    return f"{fmt_count(blocks)} 块 · {fmt_bytes(blocks * block_size)}"
