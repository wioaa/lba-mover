"""IO 调度策略（N3）—— **纯算法层**，无 Qt / 无 ctypes / 无 IO。

诚实定位（设计文档 §4.1）：`FSCTL_MOVE_FILE` 的数据链路在内核 NTFS 内部，
用户态**无法**插入自建缓冲。本模块只做四件真实有效的事：

1. 介质检测（HH​D 才启用额外读）；
2. 段合并（减少 FSCTL 往返，仅对**我们自己裁切出来的相邻小段**有收益）；
3. 预读预热（把 MOVE 的读方向变成缓存命中，HDD 才有意义）；
4. 电梯单向排序键（按目标 LCN 单调推进）。

所有策略在 ``media == unknown`` 时按「保守回退」取值：合并/电梯开、预热关。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from app.core.models import (
    MB,
    MEDIA_HDD,
    MEDIA_SSD,
    MEDIA_UNKNOWN,
    MoveSegment,
    TargetWindow,
    human_bytes,
)
from app.core.ranges import clamp

# 批次簇数下限 / 上限（上限 ≈ 256 MB @4K，见设计 §4.4）
MIN_BATCH_CLUSTERS: int = 16
MAX_BATCH_CLUSTERS: int = 65_536

# 预热单次 read 的边界
MIN_PREWARM_CHUNK_BYTES: int = 1 * MB
MAX_PREWARM_CHUNK_BYTES: int = 8 * MB

# 预读预热的缓冲门槛（D1：HDD 且缓冲 ≥ 64 MB 才开启）
PREWARM_MIN_BUFFER_MB: int = 64

SWEEP_ASCENDING = "ascending"
SWEEP_DESCENDING = "descending"


# --------------------------------------------------------------------------- #
# 策略映射
# --------------------------------------------------------------------------- #
def media_policy(media: str) -> Tuple[bool, bool, bool]:
    """介质 → ``(coalesce, elevator, prewarm)``。

    * ``hdd``     → ``(True, True, True)``   —— 预读预热只在 HDD 上有意义
    * ``ssd``     → ``(True, True, False)`` —— 额外顺序读是纯成本
    * ``unknown`` → ``(True, True, False)`` —— 保守回退（设计 §4.2 表格）

    合并与电梯排序对任何介质都无害，因此恒为 ``True``。
    """
    kind = (media or MEDIA_UNKNOWN).lower()
    if kind == MEDIA_HDD:
        return True, True, True
    if kind == MEDIA_SSD:
        return True, True, False
    return True, True, False


def batch_clusters_for(
    buffer_mb: int,
    bytes_per_cluster: int,
    window_clusters: Optional[int] = None,
) -> int:
    """把缓冲 MB 换算成「单次 FSCTL 的最大簇数」。

    ``batch = clamp(floor(buffer_mb × 1MiB / bytes_per_cluster), 16, min(65,536, window))``
    """
    if bytes_per_cluster <= 0:
        return MIN_BATCH_CLUSTERS
    requested = max(0, int(buffer_mb)) * MB // bytes_per_cluster
    upper = MAX_BATCH_CLUSTERS
    if window_clusters is not None and window_clusters > 0:
        upper = min(upper, int(window_clusters))
    return clamp(requested, MIN_BATCH_CLUSTERS, max(MIN_BATCH_CLUSTERS, upper))


def prewarm_chunk_bytes(buffer_mb: int) -> int:
    """单次 ``read()`` 的字节数：``clamp(buffer/4, 1 MiB, 8 MiB)``。"""
    value = max(0, int(buffer_mb)) * MB // 4
    return clamp(value, MIN_PREWARM_CHUNK_BYTES, MAX_PREWARM_CHUNK_BYTES)


def prewarm_budget_bytes(buffer_mb: int, file_bytes: int) -> int:
    """单个文件的预热上限：``min(buffer, 文件字节数)``。"""
    return max(0, min(max(0, int(buffer_mb)) * MB, int(file_bytes)))


def effective_batch_bytes(batch_clusters: int, bytes_per_cluster: int) -> int:
    """批次簇数换算成字节（UI 展示「实际生效」用）。"""
    return max(0, int(batch_clusters)) * max(0, int(bytes_per_cluster))


def sweep_for_window(window: Optional[TargetWindow], total_clusters: int) -> str:
    """依据目标区间位置选择单向方向。

    区间整体落在卷后半段（``start > total/2``）时从卷尾往回扫更近 → ``descending``。
    """
    if window is None or total_clusters <= 0:
        return SWEEP_ASCENDING
    return SWEEP_DESCENDING if window.start_lcn > total_clusters // 2 else SWEEP_ASCENDING


# --------------------------------------------------------------------------- #
# 段合并
# --------------------------------------------------------------------------- #
def merge_segments(
    segments: Sequence[MoveSegment],
    max_clusters: int,
) -> List[MoveSegment]:
    """把**同源文件、VCN 连续、src 连续、dst 连续**的相邻段合并成一次 FSCTL。

    三个连续性缺一不可：``(StartingVcn, StartingLcn, ClusterCount)`` 三元组
    只能表达一段连续映射，src 或 dst 任一不连续都无法合并。

    合并后的总簇数**严格守恒**；超过 ``max_clusters`` 时按上限切分。
    """
    if max_clusters <= 0:
        raise ValueError("max_clusters 必须为正数")
    if not segments:
        return []

    merged: List[MoveSegment] = []
    for segment in segments:
        if segment.clusters <= 0:
            continue
        if merged:
            last = merged[-1]
            contiguous = (
                segment.vcn == last.vcn + last.clusters
                and segment.src_lcn == last.src_lcn + last.clusters
                and segment.dst_lcn == last.dst_lcn + last.clusters
            )
            if contiguous and last.clusters + segment.clusters <= max_clusters:
                merged[-1] = MoveSegment(
                    vcn=last.vcn,
                    src_lcn=last.src_lcn,
                    dst_lcn=last.dst_lcn,
                    clusters=last.clusters + segment.clusters,
                )
                continue
        merged.append(
            MoveSegment(
                vcn=segment.vcn,
                src_lcn=segment.src_lcn,
                dst_lcn=segment.dst_lcn,
                clusters=segment.clusters,
            )
        )

    # 按上限切分（合并可能产生超过上限的段）
    result: List[MoveSegment] = []
    for segment in merged:
        remaining = segment.clusters
        vcn, src, dst = segment.vcn, segment.src_lcn, segment.dst_lcn
        while remaining > max_clusters:
            result.append(MoveSegment(vcn=vcn, src_lcn=src, dst_lcn=dst, clusters=max_clusters))
            vcn += max_clusters
            src += max_clusters
            dst += max_clusters
            remaining -= max_clusters
        if remaining > 0:
            result.append(MoveSegment(vcn=vcn, src_lcn=src, dst_lcn=dst, clusters=remaining))
    return result


def merge_task_segments(
    grouped: Sequence[Sequence[MoveSegment]],
    batch_clusters: int,
    coalesce: bool = True,
) -> List[MoveSegment]:
    """按文件分组做段合并；``coalesce=False`` 时只做上限切分、不跨段合并。"""
    out: List[MoveSegment] = []
    for segments in grouped:
        if coalesce:
            out.extend(merge_segments(segments, batch_clusters))
        else:
            for segment in segments:
                out.extend(merge_segments([segment], batch_clusters))
    return out


# --------------------------------------------------------------------------- #
# IoProfile
# --------------------------------------------------------------------------- #
@dataclass(slots=True, frozen=True)
class IoProfile:
    """一次规划的 IO 策略快照（可序列化进 ``MovePlan``）。"""

    media: str = MEDIA_UNKNOWN
    buffer_mb: int = 256
    bytes_per_cluster: int = 4096
    sweep: str = SWEEP_ASCENDING
    coalesce: bool = True
    elevator: bool = True
    prewarm: bool = False
    batch_clusters: int = 0

    @classmethod
    def build(
        cls,
        media: str,
        buffer_mb: int,
        bytes_per_cluster: int,
        *,
        prefer_prewarm: bool = True,
        window_clusters: Optional[int] = None,
        sweep: Optional[str] = None,
    ) -> "IoProfile":
        """按介质与缓冲构造策略。

        Args:
            media: ``hdd`` / ``ssd`` / ``unknown``。
            buffer_mb: 用户选择的缓冲（MB），会被上限夹紧。
            bytes_per_cluster: 卷簇大小。
            prefer_prewarm: UI 显式开关（D1：默认勾选）。
            window_clusters: 目标区间簇数（用于把批次上限再夹到区间大小）。
            sweep: 覆盖单向方向；``None`` 时按区间位置自动判定。
        """
        kind = (media or MEDIA_UNKNOWN).lower()
        coalesce, elevator, prewarm = media_policy(kind)
        batch = batch_clusters_for(buffer_mb, bytes_per_cluster, window_clusters)
        # D1：HDD 且缓冲 ≥ 64 MB 才真正启用预热
        prewarm_enabled = bool(prewarm and prefer_prewarm and int(buffer_mb) >= PREWARM_MIN_BUFFER_MB)
        return cls(
            media=kind,
            buffer_mb=max(0, int(buffer_mb)),
            bytes_per_cluster=max(0, int(bytes_per_cluster)),
            sweep=sweep or SWEEP_ASCENDING,
            coalesce=coalesce,
            elevator=elevator,
            prewarm=prewarm_enabled,
            batch_clusters=batch,
        )

    # ---------------- 展示 ---------------- #
    @property
    def batch_bytes(self) -> int:
        return effective_batch_bytes(self.batch_clusters, self.bytes_per_cluster)

    @property
    def request_bytes(self) -> int:
        return max(0, self.buffer_mb) * MB

    @property
    def capped(self) -> bool:
        """用户请求的缓冲是否被单次搬运上限夹住（UI 要如实标注）。"""
        return self.batch_bytes > 0 and self.batch_bytes < self.request_bytes

    @property
    def effective_mb(self) -> int:
        """实际生效的缓冲（MB，向下取整）。"""
        if self.bytes_per_cluster <= 0:
            return 0
        return self.batch_bytes // MB

    @property
    def prewarm_chunk(self) -> int:
        return prewarm_chunk_bytes(self.buffer_mb)

    @property
    def summary(self) -> str:
        """一行中文摘要（UI / 日志）。"""
        parts: List[str] = []
        if self.media == MEDIA_HDD:
            parts.append("HDD 加速已启用")
        elif self.media == MEDIA_SSD:
            parts.append("SSD 无需加速")
        else:
            parts.append("未知介质 · 保守模式")
        parts.append(f"批次 {self.batch_clusters:,} 簇 ≈ {human_bytes(self.batch_bytes)}")
        parts.append("单向降序" if self.sweep == SWEEP_DESCENDING else "单向升序")
        if self.prewarm:
            parts.append("预读预热")
        if self.coalesce:
            parts.append("段合并")
        return " · ".join(parts)

    @property
    def badge(self) -> Tuple[str, str]:
        """``(文案, 语义色)``，直接喂给 ``cards.Badge``。"""
        if self.media == MEDIA_HDD:
            return ("HDD 加速已启用", "success")
        if self.media == MEDIA_SSD:
            return ("SSD 无需加速", "neutral")
        return ("未知介质 · 保守模式", "warning")

    @property
    def honest_note(self) -> str:
        """必须如实展示的收益边界说明（设计 §4.1）。"""
        return (
            "加速效果依介质与碎片程度而定，不保证线性提速；HDD 上主要减少磁头换向，"
            "SSD 上这些策略基本无收益（已自动关闭额外读）。段合并只对工具自己裁切出的"
            "相邻小段有效，对原始 extent 零收益（它们本就是最大连续段）。"
        )

    @property
    def cap_note(self) -> str:
        """缓冲被夹紧时的如实标注。"""
        if self.capped:
            return (
                f"实际生效 {self.effective_mb} MB（单次搬运上限 "
                f"{MAX_BATCH_CLUSTERS:,} 簇）"
            )
        return f"实际生效 {self.effective_mb} MB"

    def as_dict(self) -> dict:
        return {
            "media": self.media,
            "buffer_mb": self.buffer_mb,
            "bytes_per_cluster": self.bytes_per_cluster,
            "sweep": self.sweep,
            "coalesce": self.coalesce,
            "elevator": self.elevator,
            "prewarm": self.prewarm,
            "batch_clusters": self.batch_clusters,
        }
