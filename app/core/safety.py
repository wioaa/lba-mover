"""安全保留区模型（N1）—— **纯算法层**，无 Qt / 无 ctypes / 无 IO。

核心思想：卷内唯一"看起来像破坏分区表/引导区"的风险是**把数据搬进不该碰的簇**。
因此把「不可作为搬运目标」的簇集合显式建模成 :class:`SafetyPolicy`，并在
规划期（packer）与执行期（``SafetyGuard``）**各校验一次**。

硬下限设计要点：``clamp_reserve_mb`` 是唯一的 MB→簇换算入口，下限校验放在这里
（core 纯函数），而不是 UI 校验 —— 未来即便加 CLI / 配置文件也绕不过去。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence, Tuple

from app.core.models import MB, VolumeInfo, human_bytes
from app.core.ranges import (
    ceil_div,
    clamp,
    merge_runs,
    run_length,
    subtract_runs,
)

Run = Tuple[int, int]

# --------------------------------------------------------------------------- #
# 常量（附录 B）
# --------------------------------------------------------------------------- #
HEAD_RESERVE_MIN_BYTES: int = 1 * MB
TAIL_RESERVE_MIN_BYTES: int = 1 * MB
HEAD_RESERVE_MIN_CLUSTERS: int = 16
TAIL_RESERVE_MIN_CLUSTERS: int = 16
HEAD_RESERVE_DEFAULT_MB: int = 16
TAIL_RESERVE_DEFAULT_MB: int = 16
MFT_GUARD_CLUSTERS: int = 8


class SafetyViolation(RuntimeError):
    """目标簇越过安全保留区 / 卷边界。planner 与 mover 都抛这个。"""

    def __init__(self, lcn: int, clusters: int, reason: str) -> None:
        self.lcn = int(lcn)
        self.clusters = int(clusters)
        self.reason = reason
        super().__init__(f"安全校验失败［LCN {self.lcn:,} + {self.clusters:,} 簇］：{reason}")


@dataclass(slots=True, frozen=True)
class SafetyPolicy:
    """一个卷的安全保留区快照。纯数据，可序列化进计划、可单测。"""

    total_clusters: int = 0
    bytes_per_cluster: int = 0
    head_reserve_clusters: int = 0
    tail_reserve_clusters: int = 0
    mft_zone_start: int = 0
    mft_zone_end: int = 0
    mft_start_lcn: int = 0
    mft2_start_lcn: int = 0
    mft_guard_clusters: int = MFT_GUARD_CLUSTERS
    protect_mft_zone: bool = True
    guard_metadata: bool = True

    # ---------------- 派生量 ---------------- #
    @property
    def allowed_clusters(self) -> int:
        """扣掉禁区后的可用簇数（下限 0）。"""
        return max(0, self.total_clusters - run_length(self.forbidden_runs()))

    def forbidden_runs(self) -> List[Run]:
        """禁区集合（已合并、升序、半开区间）。"""
        total = self.total_clusters
        if total <= 0:
            return []
        runs: List[Run] = []
        if self.head_reserve_clusters > 0:
            runs.append((0, min(total, self.head_reserve_clusters)))
        if self.tail_reserve_clusters > 0:
            runs.append((max(0, total - self.tail_reserve_clusters), total))
        if self.protect_mft_zone and self.mft_zone_end > self.mft_zone_start:
            runs.append((max(0, self.mft_zone_start), min(total, self.mft_zone_end)))
        if self.guard_metadata:
            if self.mft_start_lcn > 0:
                runs.append(
                    (
                        self.mft_start_lcn,
                        min(total, self.mft_start_lcn + self.mft_guard_clusters),
                    )
                )
            if self.mft2_start_lcn > 0:
                runs.append(
                    (
                        self.mft2_start_lcn,
                        min(total, self.mft2_start_lcn + self.mft_guard_clusters),
                    )
                )
        return merge_runs([r for r in runs if r[1] > r[0]])

    def allowed_runs(self) -> List[Run]:
        """允许落位的簇区间（卷内 − 禁区）。"""
        if self.total_clusters <= 0:
            return []
        return subtract_runs([(0, self.total_clusters)], self.forbidden_runs())

    # ---------------- 判定 ---------------- #
    def is_cluster_allowed(self, lcn: int) -> bool:
        if lcn < 0 or lcn >= self.total_clusters:
            return False
        return not any(start <= lcn < end for start, end in self.forbidden_runs())

    def is_range_allowed(self, lcn: int, clusters: int) -> bool:
        """``[lcn, lcn+clusters)`` 是否完整落在允许区内。"""
        if clusters <= 0:
            return False
        if lcn < 0 or lcn + clusters > self.total_clusters:
            return False
        end = lcn + clusters
        for start, forbidden_end in self.forbidden_runs():
            if start < end and lcn < forbidden_end:
                return False
        return True

    def describe(self) -> str:
        """一行中文摘要（日志 / UI）。"""
        if self.total_clusters <= 0:
            return "安全策略未初始化"
        head = human_bytes(self.head_reserve_clusters * self.bytes_per_cluster)
        tail = human_bytes(self.tail_reserve_clusters * self.bytes_per_cluster)
        parts = [
            f"卷首保留 {self.head_reserve_clusters:,} 簇（{head}）",
            f"卷尾保留 {self.tail_reserve_clusters:,} 簇（{tail}）",
        ]
        if self.protect_mft_zone and self.mft_zone_end > self.mft_zone_start:
            parts.append(f"MFT 区 [{self.mft_zone_start:,}, {self.mft_zone_end:,}) 已禁用")
        if self.guard_metadata:
            parts.append(f"MFT/MFTMirr 守卫窗 {self.mft_guard_clusters} 簇")
        return "；".join(parts)

    def as_dict(self) -> dict:
        """便于日志 / 序列化的纯字典。"""
        return {
            "total_clusters": self.total_clusters,
            "bytes_per_cluster": self.bytes_per_cluster,
            "head_reserve_clusters": self.head_reserve_clusters,
            "tail_reserve_clusters": self.tail_reserve_clusters,
            "mft_zone_start": self.mft_zone_start,
            "mft_zone_end": self.mft_zone_end,
            "mft_start_lcn": self.mft_start_lcn,
            "mft2_start_lcn": self.mft2_start_lcn,
            "forbidden_runs": self.forbidden_runs(),
            "allowed_clusters": self.allowed_clusters,
        }


# --------------------------------------------------------------------------- #
# 下限夹紧
# --------------------------------------------------------------------------- #
def min_reserve_clusters(bytes_per_cluster: int, *, head: bool = True) -> int:
    """保留区的簇数硬下限 = max(16 簇, 1 MiB 对应的簇数)。"""
    if bytes_per_cluster <= 0:
        return HEAD_RESERVE_MIN_CLUSTERS if head else TAIL_RESERVE_MIN_CLUSTERS
    min_bytes = HEAD_RESERVE_MIN_BYTES if head else TAIL_RESERVE_MIN_BYTES
    base = HEAD_RESERVE_MIN_CLUSTERS if head else TAIL_RESERVE_MIN_CLUSTERS
    return max(base, ceil_div(min_bytes, bytes_per_cluster))


def clamp_reserve_mb(requested_mb: int, bytes_per_cluster: int, total_clusters: int) -> int:
    """把用户请求的保留区 MB 夹到安全区间内，返回**实际生效的 MB**。

    任何调用方（含未来的 CLI / 配置文件 / 脚本）都无法绕过：
    ``requested_mb <= 0`` 会返回下限而不是 0。

    ★ 换回 MB 时必须用**精确向上取整**（``ceil_div``），不能用 ``// + 1``：
    后者在"请求恰好等于下限"时会把 1 MiB 放大成 2 MiB（1953.125 → 1954），
    数值不直观，且会让 UI 的"实际生效"与用户输入无故差一倍。
    """
    if bytes_per_cluster <= 0:
        return 0
    floor_clusters = min_reserve_clusters(bytes_per_cluster)
    ceil_clusters = max(floor_clusters, total_clusters // 2)
    requested_clusters = max(0, int(requested_mb)) * MB // bytes_per_cluster
    clusters = clamp(requested_clusters, floor_clusters, ceil_clusters)
    # 精确向上取整：保证 `mb * MB // bpc >= clusters`，同时不过冲
    return max(1, ceil_div(clusters * bytes_per_cluster, MB))


def default_head_mb(bytes_per_cluster: int) -> int:
    """卷首保留默认值（MB）；小簇卷自动抬到硬下限以上。"""
    if bytes_per_cluster <= 0:
        return HEAD_RESERVE_DEFAULT_MB
    min_bytes = min_reserve_clusters(bytes_per_cluster, head=True) * bytes_per_cluster
    return max(HEAD_RESERVE_DEFAULT_MB, ceil_div(min_bytes, MB))


def default_tail_mb(bytes_per_cluster: int) -> int:
    """卷尾保留默认值（MB）。"""
    if bytes_per_cluster <= 0:
        return TAIL_RESERVE_DEFAULT_MB
    min_bytes = min_reserve_clusters(bytes_per_cluster, head=False) * bytes_per_cluster
    return max(TAIL_RESERVE_DEFAULT_MB, ceil_div(min_bytes, MB))


# --------------------------------------------------------------------------- #
# 构造
# --------------------------------------------------------------------------- #
def build_policy(info: VolumeInfo, *, head_mb: int, tail_mb: int) -> SafetyPolicy:
    """唯一的 ``SafetyPolicy`` 构造入口，内部强制夹紧。

    Args:
        info: 卷信息（提供总簇数、簇大小、MFT 区与 ``$MFT``/``$MFTMirr`` 起点）。
        head_mb: 用户请求的卷首保留（MB），会被夹到硬下限以上。
        tail_mb: 用户请求的卷尾保留（MB），会被夹到硬下限以上。

    Returns:
        不可变的 :class:`SafetyPolicy`。
    """
    bpc = max(0, int(info.bytes_per_cluster))
    total = max(0, int(info.total_clusters))
    if bpc <= 0 or total <= 0:
        return SafetyPolicy(
            total_clusters=total,
            bytes_per_cluster=bpc,
            mft_zone_start=max(0, int(info.mft_zone_start)),
            mft_zone_end=max(0, int(info.mft_zone_end)),
            mft_start_lcn=max(0, int(info.mft_start_lcn)),
            mft2_start_lcn=max(0, int(info.mft2_start_lcn)),
        )

    head_clusters = clamp_reserve_mb(head_mb, bpc, total) * MB // bpc
    tail_clusters = clamp_reserve_mb(tail_mb, bpc, total) * MB // bpc
    head_clusters = max(head_clusters, min_reserve_clusters(bpc, head=True))
    tail_clusters = max(tail_clusters, min_reserve_clusters(bpc, head=False))

    # 两端保留区不允许吃掉整卷：各让出到总量的一半
    if head_clusters + tail_clusters > total:
        half = total // 2
        head_clusters = min(head_clusters, max(0, half))
        tail_clusters = min(tail_clusters, max(0, total - head_clusters))

    return SafetyPolicy(
        total_clusters=total,
        bytes_per_cluster=bpc,
        head_reserve_clusters=head_clusters,
        tail_reserve_clusters=tail_clusters,
        mft_zone_start=max(0, int(info.mft_zone_start)),
        mft_zone_end=max(0, int(info.mft_zone_end)),
        mft_start_lcn=max(0, int(info.mft_start_lcn)),
        mft2_start_lcn=max(0, int(info.mft2_start_lcn)),
    )


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
def subtract_forbidden(
    runs: Sequence[Run],
    forbidden: Sequence[Run],
) -> List[Run]:
    """从候选区间中扣除禁区。"""
    return subtract_runs(runs, forbidden)


def assert_target_allowed(
    policy: SafetyPolicy,
    lcn: int,
    clusters: int,
    context: str = "",
) -> None:
    """断言目标区间安全，否则抛 :class:`SafetyViolation`。

    Args:
        policy: 安全策略。
        lcn: 目标起始簇号。
        clusters: 目标簇数。
        context: 附加说明（如 "规划" / "执行"），会拼进异常消息。

    Raises:
        SafetyViolation: 越界、跨禁区、簇数非正。
    """
    where = f"{context}｜" if context else ""
    if clusters <= 0:
        raise SafetyViolation(lcn, clusters, f"{where}目标簇数必须大于 0")
    if lcn < 0:
        raise SafetyViolation(lcn, clusters, f"{where}目标起始 LCN 不能为负")
    if policy.total_clusters <= 0:
        raise SafetyViolation(lcn, clusters, f"{where}卷总簇数为 0，安全策略未初始化")
    if lcn + clusters > policy.total_clusters:
        raise SafetyViolation(
            lcn,
            clusters,
            f"{where}目标区间越过卷尾（{lcn + clusters:,} > {policy.total_clusters:,}）",
        )

    head = policy.head_reserve_clusters
    if head > 0 and lcn < head:
        raise SafetyViolation(
            lcn, clusters, f"{where}目标落在卷首保留区 [0, {head:,}) 内"
        )
    tail_start = max(0, policy.total_clusters - policy.tail_reserve_clusters)
    if policy.tail_reserve_clusters > 0 and lcn + clusters > tail_start:
        raise SafetyViolation(
            lcn,
            clusters,
            f"{where}目标越过卷尾保留区起点 {tail_start:,}",
        )
    if (
        policy.protect_mft_zone
        and policy.mft_zone_end > policy.mft_zone_start
        and lcn < policy.mft_zone_end
        and lcn + clusters > policy.mft_zone_start
    ):
        raise SafetyViolation(
            lcn,
            clusters,
            f"{where}目标与 MFT 区 [{policy.mft_zone_start:,}, {policy.mft_zone_end:,}) 相交",
        )
    if policy.guard_metadata:
        for name, basis in (
            ("$MFT", policy.mft_start_lcn),
            ("$MFTMirr", policy.mft2_start_lcn),
        ):
            if basis <= 0:
                continue
            guard_end = min(policy.total_clusters, basis + policy.mft_guard_clusters)
            if lcn < guard_end and lcn + clusters > basis:
                raise SafetyViolation(
                    lcn,
                    clusters,
                    f"{where}目标与 {name} 守卫窗 [{basis:,}, {guard_end:,}) 相交",
                )


# --------------------------------------------------------------------------- #
# 安全论证文案（给 UI 直接渲染）
# --------------------------------------------------------------------------- #
def safety_bullets(info: VolumeInfo, start_offset: int = -1) -> List[str]:
    """生成「为什么不会破坏分区表」的中文要点（逐条，可直接进 UI）。

    Args:
        info: 卷信息。
        start_offset: ``IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS`` 读到的卷起始字节偏移；
            ``-1`` 表示未取到（此时只输出与偏移无关的条目）。
    """
    bullets: List[str] = [
        "本工具只通过 FSCTL_MOVE_FILE 在卷内部搬动数据簇，寻址单位是卷内逻辑簇号（LCN），"
        "不是物理磁盘 LBA。",
        "分区表（MBR / GPT 主头与备份头）位于分区之前与磁盘末尾，属于卷之外；"
        "程序从不打开物理磁盘写句柄，物理上无法触及。",
    ]
    if start_offset is not None and start_offset > 0:
        bullets.append(
            f"实测：本卷起始于磁盘偏移 {human_bytes(start_offset)}（> 0），"
            "分区表位于该偏移之前，不在可写范围内。"
        )
    elif start_offset == 0:
        bullets.append(
            "实测：本卷起始于磁盘偏移 0（整盘单分区），分区表位于卷首保留区内，已被强制保护。"
        )
    bpc = max(1, int(info.bytes_per_cluster))
    bullets.append(
        f"卷首保留区默认 {HEAD_RESERVE_DEFAULT_MB} MiB（硬下限 "
        f"{human_bytes(HEAD_RESERVE_MIN_BYTES)} / {min_reserve_clusters(bpc)} 簇），"
        "用于保护 $Boot 所在的 VBR。"
    )
    bullets.append(
        f"卷尾保留区默认 {TAIL_RESERVE_DEFAULT_MB} MiB（硬下限 "
        f"{human_bytes(TAIL_RESERVE_MIN_BYTES)} / {min_reserve_clusters(bpc, head=False)} 簇），"
        "用于保护 NTFS 备份引导扇区。"
    )
    bullets.append(
        "MFT 区与 $MFT / $MFTMirr 守卫窗强制禁用且不可关闭；$ 开头的系统文件一律不可搬运。"
    )
    return bullets


def policy_from_dict(data: dict) -> SafetyPolicy:
    """从 :meth:`SafetyPolicy.as_dict` 的产物还原（用于计划持久化 / 单测）。"""
    return SafetyPolicy(
        total_clusters=int(data.get("total_clusters", 0)),
        bytes_per_cluster=int(data.get("bytes_per_cluster", 0)),
        head_reserve_clusters=int(data.get("head_reserve_clusters", 0)),
        tail_reserve_clusters=int(data.get("tail_reserve_clusters", 0)),
        mft_zone_start=int(data.get("mft_zone_start", 0)),
        mft_zone_end=int(data.get("mft_zone_end", 0)),
        mft_start_lcn=int(data.get("mft_start_lcn", 0)),
        mft2_start_lcn=int(data.get("mft2_start_lcn", 0)),
    )
