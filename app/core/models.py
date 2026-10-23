"""全局数据模型（dataclass）。

这一层不依赖任何 Win32 / Qt 类型，方便单元测试与跨层复用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

GB = 1024 ** 3
MB = 1024 ** 2

# 每字节 1 的个数查表：位图快速校验用（避免依赖 app.win32.bitmap，保持 core 无 ctypes 依赖）
_POPCOUNT: bytes = bytes(bin(i).count("1") for i in range(256))

#: 文件属性位（与 Win32 常量一致，放在 core 里避免 UI/算法层 import win32）
FILE_ATTRIBUTE_READONLY = 0x00000001
FILE_ATTRIBUTE_HIDDEN = 0x00000002
FILE_ATTRIBUTE_SYSTEM = 0x00000004
FILE_ATTRIBUTE_DIRECTORY = 0x00000010
FILE_ATTRIBUTE_SPARSE_FILE = 0x00000200
FILE_ATTRIBUTE_COMPRESSED = 0x00000800
FILE_ATTRIBUTE_ENCRYPTED = 0x00004000

#: 介质类型（IO 策略的输入；``app.win32.storage`` 探测后写入 ``VolumeInfo.media_kind``）
MEDIA_HDD = "hdd"
MEDIA_SSD = "ssd"
MEDIA_UNKNOWN = "unknown"


def human_bytes(value: int) -> str:
    """把字节数格式化为人类可读字符串（保留一位小数）。"""
    if value < 0:
        return "-" + human_bytes(-value)
    if value < 1024:
        return f"{value} B"
    units = ("KB", "MB", "GB", "TB", "PB")
    size = float(value)
    for unit in units:
        size /= 1024.0
        if size < 1024.0:
            return f"{size:.1f} {unit}"
    return f"{size:.1f} EB"


@dataclass(slots=True)
class VolumeInfo:
    """一个本地固定卷的元信息。"""

    letter: str = ""                      # 盘符，如 "C"
    root: str = ""                        # 根路径，如 "C:\\"
    label: str = ""                       # 卷标
    filesystem: str = ""                  # 文件系统名，如 "NTFS"
    total_bytes: int = 0
    free_bytes: int = 0                   # 调用者可用空间
    bytes_per_cluster: int = 0
    total_clusters: int = 0
    free_clusters: int = 0
    mft_zone_start: int = 0
    mft_zone_end: int = 0
    mft_start_lcn: int = 0
    mft_valid_data_length: int = 0
    serial: int = 0
    is_ntfs: bool = False
    volume_error: str = ""                # 读取卷数据失败时的原因（如权限不足）
    # ---- v2 新增（全部追加在末尾且带默认值，避免破坏既有构造点）----
    mft2_start_lcn: int = 0               # $MFTMirr 起始 LCN
    bytes_per_file_record: int = 0        # MFT 记录大小
    media_kind: str = "unknown"           # hdd / ssd / unknown
    disk_start_offset: int = -1           # 卷在物理磁盘的起始字节偏移（-1 = 未取到）
    bytes_per_sector: int = 0             # 扇区大小（MFT 读的扇区对齐基准）
    number_sectors: int = 0               # 卷内扇区总数（NTFS_VOLUME_DATA_BUFFER@8）

    @property
    def display_name(self) -> str:
        """下拉框展示文案，例如 ``C:  952.6 GB 可用 451.5 GB``。"""
        label = f" [{self.label}]" if self.label else ""
        return (
            f"{self.letter}:{label}  {human_bytes(self.total_bytes)}"
            f"  可用 {human_bytes(self.free_bytes)}"
        )

    @property
    def cluster_text(self) -> str:
        """簇大小展示文案。"""
        return human_bytes(self.bytes_per_cluster) if self.bytes_per_cluster else "—"


@dataclass(slots=True)
class Extent:
    """文件的一段连续簇区间。"""

    vcn: int = 0        # 起始虚拟簇号
    lcn: int = 0        # 起始逻辑簇号；-1 表示稀疏 / 未分配
    clusters: int = 0   # 簇数

    @property
    def is_sparse(self) -> bool:
        """稀疏段（LCN 为 -1 / 0xFFFFFFFFFFFFFFFF）不参与物理移动。"""
        return self.lcn < 0

    @property
    def vcn_end(self) -> int:
        return self.vcn + self.clusters

    @property
    def lcn_end(self) -> int:
        return self.lcn + self.clusters

    def overlaps(self, start: int, end: int) -> bool:
        """本 extent 的物理区间是否与 ``[start, end)`` 相交（稀疏段恒为 False）。"""
        if self.is_sparse or self.clusters <= 0:
            return False
        return self.lcn < end and start < self.lcn_end


@dataclass(slots=True)
class FileEntry:
    """一个可参与迁移决策的文件。"""

    path: str = ""
    name: str = ""
    size: int = 0
    extents: List[Extent] = field(default_factory=list)
    movable: bool = True
    reason: str = ""
    checked: bool = False
    status: str = "待迁移"
    # ---- v2 新增 ----
    attributes: int = 0                   # GetFileAttributesW / MFT STANDARD_INFORMATION 原值
    record_no: int = -1                   # N5：MFT 记录号；句柄通道为 -1
    link_count: int = 1                   # N5：$FILE_NAME 属性个数（硬链接数）
    path_known: bool = True               # N5：路径重建是否成功（False 时执行期必须拒绝）
    source: str = "handle"                # N5：mft / handle（UI 标记 + 文件级降级）
    #: N5：布局不完整（属性列表跨记录 / vcn 空洞 / 基础记录内无 ``$DATA``）。
    #: 这类条目**必须**走句柄通道补齐 extents，否则会以 ``movable=True`` +
    #: 空 extents 的形态混进规划层，被当成"0 簇、已就位" → 假成功。
    incomplete: bool = False

    # ---------------- 派生属性 ---------------- #
    @property
    def is_compressed(self) -> bool:
        """NTFS 压缩文件：`FSCTL_MOVE_FILE` 只能在压缩单元边界移动，工具一律排除。"""
        return bool(self.attributes & FILE_ATTRIBUTE_COMPRESSED)

    @property
    def is_encrypted(self) -> bool:
        """EFS 加密文件：`FSCTL_MOVE_FILE` 不支持。"""
        return bool(self.attributes & FILE_ATTRIBUTE_ENCRYPTED)

    @property
    def is_sparse(self) -> bool:
        return bool(self.attributes & FILE_ATTRIBUTE_SPARSE_FILE)

    @property
    def allocated_extents(self) -> List[Extent]:
        """只保留真正占用物理簇的 extents。"""
        return [e for e in self.extents if not e.is_sparse and e.clusters > 0]

    @property
    def allocated_clusters(self) -> int:
        """实际占用的簇数（不含稀疏段）。

        1.0.0D：改成生成器求和（原先经 ``allocated_extents`` 先建中间列表，
        在「10 万 extent 级挡路者识别」的性能剖析里可测）。
        """
        return sum(
            e.clusters for e in self.extents if not e.is_sparse and e.clusters > 0
        )

    @property
    def fragments(self) -> int:
        """碎片数 = 非稀疏 extent 的数量。"""
        return len(self.allocated_extents)

    @property
    def first_lcn(self) -> int:
        """最小 LCN；无 extents 时返回 -1。"""
        extents = self.allocated_extents
        return min(e.lcn for e in extents) if extents else -1

    @property
    def last_lcn(self) -> int:
        """最大 LCN（开区间上界）；无 extents 时返回 -1。"""
        extents = self.allocated_extents
        return max(e.lcn_end for e in extents) if extents else -1

    @property
    def lcn_span(self) -> int:
        """LCN 跨度（末 LCN - 首 LCN）。"""
        if not self.allocated_extents:
            return 0
        return self.last_lcn - self.first_lcn

    @property
    def lcn_text(self) -> str:
        """表格展示用的 LCN 区间文案。"""
        if not self.allocated_extents:
            return "—"
        return f"{self.first_lcn:,} – {self.last_lcn - 1:,}"


@dataclass(slots=True)
class MoveSegment:
    """一次 ``FSCTL_MOVE_FILE`` 调用的参数集合。"""

    vcn: int = 0
    src_lcn: int = 0
    dst_lcn: int = 0
    clusters: int = 0

    @property
    def dst_end(self) -> int:
        return self.dst_lcn + self.clusters


@dataclass(slots=True)
class PlanItem:
    """迁移计划中的单个文件。"""

    entry: FileEntry
    target_lcn: int = 0
    clusters: int = 0
    segments: List[MoveSegment] = field(default_factory=list)
    skipped_reason: str = ""
    #: v2：``place``（装载）/ ``evict``（挡路者迁出）/ ``relay``（中转）。
    #: 放在末尾以保持 v1 的位置参数顺序不变。
    role: str = "place"
    #: v2 辅助：``segments`` 中「第一段（src→relay）／第二段（relay→dst）」的分界下标。
    #: ``0`` 表示不是中转项。仅当 ``role == "relay"`` 时有效。
    relay_split: int = 0

    @property
    def needs_move(self) -> bool:
        """是否真的需要搬运（已经就位则为 False）。"""
        return not self.skipped_reason and bool(self.segments)

    @property
    def move_clusters(self) -> int:
        return sum(s.clusters for s in self.segments)

    @property
    def target_end(self) -> int:
        return self.target_lcn + self.clusters

    @property
    def dst_ranges(self) -> List[Tuple[int, int]]:
        """所有目标切片区间（用于依赖图与安全复查）。"""
        return [(s.dst_lcn, s.dst_end) for s in self.segments]

    @property
    def src_ranges(self) -> List[Tuple[int, int]]:
        """所有源切片区间。"""
        return [(s.src_lcn, s.src_lcn + s.clusters) for s in self.segments]


@dataclass(slots=True)
class PlanFailure:
    """规划阶段无法安排的条目。"""

    entry: FileEntry
    reason: str = ""


@dataclass(slots=True)
class MovePlan:
    """完整的迁移方案。"""

    items: List[PlanItem] = field(default_factory=list)
    failures: List[PlanFailure] = field(default_factory=list)
    total_clusters: int = 0
    reserve_clusters: int = 0
    tail_floor_lcn: int = 0
    note: str = ""
    # ---- v2 新增（全部带默认值，v1 调用点不受影响）----
    window: Optional["TargetWindow"] = None
    evictions: List[PlanItem] = field(default_factory=list)
    tasks: List["MoveTask"] = field(default_factory=list)
    feasibility: Optional["FeasibilityReport"] = None
    safety: Optional[object] = None            # SafetyPolicy（避免 core 内循环依赖，用 object）
    io_profile: Optional[object] = None        # IoProfile
    stats: "PlanStats" = field(default_factory=lambda: PlanStats())
    version: int = 2

    @property
    def movable_items(self) -> List[PlanItem]:
        return [i for i in self.items if i.needs_move]

    @property
    def all_items(self) -> List[PlanItem]:
        """装载项 + 驱逐项。"""
        return list(self.items) + list(self.evictions)

    @property
    def relay_tasks(self) -> List["MoveTask"]:
        return [t for t in self.tasks if t.phase in ("relay_out", "relay_in")]

    @property
    def relay_clusters(self) -> int:
        """中转带来的**额外**搬运簇数（relay_in 那一趟）；v1 无中转恒为 0。"""
        return sum(t.clusters for t in self.tasks if t.phase == "relay_in")

    @property
    def total_move_clusters(self) -> int:
        """需搬运的总簇数（**进度条分母**）。

        v2 起优先用 ``tasks`` 汇总：它是真实的执行量（含驱逐与中转的两趟搬运），
        与 ``MoveWorker`` 的进度分子严格同源。
        v1 计划没有 ``tasks``，退化为「装载 + 驱逐」之和，结果与 v1 完全一致。

        注：设计文档 §5.1 给出的公式是
        ``items + evictions + Σrelay_tasks``，而同一文档又要求中转项的
        ``items[].segments`` 同时包含两趟 —— 两者相加会把中转重复计一次。
        这里以「实际执行量」为准（见交付回报「偏离设计」一节）。
        """
        if self.tasks:
            return sum(t.clusters for t in self.tasks)
        return sum(i.move_clusters for i in self.items) + sum(
            i.move_clusters for i in self.evictions
        )

    @property
    def has_eviction(self) -> bool:
        return bool(self.evictions)

    @property
    def needs_relay(self) -> bool:
        return bool(self.relay_clusters) or any(i.role == "relay" for i in self.items)

    @property
    def relay_rounds(self) -> int:
        return len({t.path for t in self.tasks if t.phase == "relay_in"})

    @property
    def target_ranges(self) -> List[Tuple[int, int]]:
        """所有**装载**目标区间 ``[(start, end), ...]``，用于布局图叠加。

        驱逐项语义不同（不是"新位置"），走独立的 ``evict_target_ranges``。
        """
        return [(i.target_lcn, i.target_end) for i in self.items if i.needs_move]

    @property
    def evict_target_ranges(self) -> List[Tuple[int, int]]:
        ranges: List[Tuple[int, int]] = []
        for item in self.evictions:
            for seg in item.segments:
                ranges.append((seg.dst_lcn, seg.dst_end))
        return ranges

    @property
    def source_ranges(self) -> List[Tuple[int, int]]:
        """所有源区间 ``[(start, end), ...]``。"""
        ranges: List[Tuple[int, int]] = []
        for item in self.all_items:
            for seg in item.segments:
                ranges.append((seg.src_lcn, seg.src_lcn + seg.clusters))
        return ranges

    @property
    def empty(self) -> bool:
        return not self.movable_items and not self.evictions


@dataclass(slots=True)
class MoveFailure:
    """一次真实移动失败的完整记录（绝不吞异常）。"""

    path: str = ""
    src_lcn: int = 0
    dst_lcn: int = 0
    clusters: int = 0
    winerror: int = 0
    message: str = ""


@dataclass(slots=True)
class MoveOutcome:
    """整体移动结果。"""

    moved_clusters: int = 0
    moved_files: int = 0
    cancelled: bool = False
    failures: List[MoveFailure] = field(default_factory=list)


def range_free_fast(
    bits, total_clusters: int, start: int, clusters: int
) -> bool:
    """字节级判定 ``[start, start+clusters)`` 是否**全部空闲**（O(字节数)）。

    ``BitmapSnapshot.range_is_free_fast`` 与
    :class:`app.core.shadow.ShadowBitmap` 共用同一实现 —— 执行期守卫必须在
    "快照"与"影子位图"之间表现完全一致，否则同一段目标会被两种视图判出
    不同结果。

    Args:
        bits: 位图字节（``bytes`` / ``bytearray`` 均可）；bit 0 对应 LCN 0，
            置 1 表示**已占用**。
        total_clusters: 卷总簇数（用于越界判定）。
        start: 起始 LCN。
        clusters: 簇数。
    """
    if clusters <= 0 or start < 0 or start + clusters > total_clusters:
        return False
    end = start + clusters
    first_byte = start >> 3
    last_byte = (end + 7) >> 3
    segment = bits[first_byte:last_byte]
    if not segment:
        return False
    occupied = sum(segment.translate(_POPCOUNT))
    # 位序：byte 内 bit0 对应 LCN 的低位 → 区间之前残留在**低位**，之后残留在**高位**
    start_bit = start & 7
    if start_bit and occupied:
        occupied -= _POPCOUNT[segment[0] & ((1 << start_bit) - 1)]
    end_bit = end & 7
    if end_bit and occupied:
        occupied -= _POPCOUNT[segment[-1] >> end_bit]
    return occupied == 0


@dataclass(slots=True)
class BitmapSnapshot:
    """卷位图快照。"""

    bits: bytes = b""
    total_clusters: int = 0
    bytes_per_cluster: int = 0

    def is_free(self, lcn: int) -> bool:
        """查询单簇是否空闲；越界视为不空闲（安全优先）。"""
        if lcn < 0 or lcn >= self.total_clusters:
            return False
        index = lcn >> 3
        if index >= len(self.bits):
            return False
        return not ((self.bits[index] >> (lcn & 7)) & 1)

    def range_is_free(self, start: int, clusters: int) -> bool:
        """校验 ``[start, start+clusters)`` 是否全部空闲。"""
        if clusters <= 0 or start < 0 or start + clusters > self.total_clusters:
            return False
        for lcn in range(start, start + clusters):
            if not self.is_free(lcn):
                return False
        return True

    def range_is_free_fast(self, start: int, clusters: int) -> bool:
        """``range_is_free`` 的字节级实现（O(字节数)），供 65,536 簇批次校验使用。

        用 ``bytes.translate(POPCOUNT)`` 统计区间内被占用的位数，再用首尾掩码
        扣掉区间之外的位；结果为 0 即整段空闲。
        """
        return range_free_fast(self.bits, self.total_clusters, start, clusters)

    @property
    def byte_length(self) -> int:
        return len(self.bits)


@dataclass(slots=True)
class LogRecord:
    """一条操作日志。"""

    level: str = "INFO"
    message: str = ""
    timestamp: str = ""


# =========================================================================== #
# v2 新增数据结构（区间模式 / 装箱 / 调度 / 可行性）
# =========================================================================== #
VERDICT_DIRECT = "direct"              # 直接可行（不驱逐）
VERDICT_EVICT = "needs_eviction"       # 可行，需驱逐挡路文件
VERDICT_IMPOSSIBLE = "impossible"      # 不可行 + 量化原因 + 建议

#: 驱逐成本里的碎片惩罚系数
EVICT_FRAGMENT_PENALTY: float = 0.35


def blocker_cost(clusters_in_window: int, fragments_in_window: int) -> float:
    """挡路者迁移成本 = 需搬簇数 × (1 + 0.35 × (切片数 - 1))，优先搬「小且连续」。"""
    return clusters_in_window * (1.0 + EVICT_FRAGMENT_PENALTY * max(0, fragments_in_window - 1))


@dataclass(slots=True, frozen=True)
class TargetWindow:
    """目标区间（**开区间** ``[start_lcn, end_lcn)``）。"""

    start_lcn: int = 0
    end_lcn: int = 0

    @property
    def clusters(self) -> int:
        return max(0, self.end_lcn - self.start_lcn)

    @property
    def is_empty(self) -> bool:
        return self.clusters <= 0

    def contains(self, lcn: int, clusters: int) -> bool:
        """``[lcn, lcn+clusters)`` 是否完整落在区间内。"""
        if clusters <= 0:
            return False
        return self.start_lcn <= lcn and lcn + clusters <= self.end_lcn

    def overlaps(self, start: int, end: int) -> bool:
        return self.start_lcn < end and start < self.end_lcn

    def clipped(self, total_clusters: int) -> "TargetWindow":
        """裁剪到 ``[0, total_clusters)`` 并保证 ``start <= end``。"""
        start = max(0, min(self.start_lcn, total_clusters))
        end = max(start, min(self.end_lcn, total_clusters))
        return TargetWindow(start, end)

    def intersect(self, runs: Sequence[Tuple[int, int]]) -> List[Tuple[int, int]]:
        """与给定区间集合求交（返回有序、互不重叠的切片）。"""
        result: List[Tuple[int, int]] = []
        for run_start, run_end in sorted(runs):
            start = max(run_start, self.start_lcn)
            end = min(run_end, self.end_lcn)
            if end > start:
                result.append((start, end))
        return result


@dataclass(slots=True)
class Blocker:
    """目标区间内的挡路文件（其部分簇需要迁出区间）。"""

    path: str = ""
    name: str = ""
    size: int = 0
    total_clusters: int = 0
    clusters_in_window: int = 0
    extents_in_window: List[Extent] = field(default_factory=list)
    fragments_in_window: int = 0
    cost: float = 0.0
    excluded: bool = False
    reason: str = ""

    @property
    def movable(self) -> bool:
        """是否允许自动迁出（未排除、无不可搬原因、确有挡路簇）。"""
        return not self.excluded and not self.reason and self.clusters_in_window > 0


@dataclass(slots=True)
class MoveTask:
    """计划项 → 可执行单元（顺序化之后）。"""

    order: int = 0
    phase: str = "place"        # evict | place | relay_out | relay_in
    item_index: int = 0
    path: str = ""
    segments: List[MoveSegment] = field(default_factory=list)
    #: v2（N5）：路径是否可信。``False`` 表示"路径未知"，**执行期必须拒绝** ——
    #: ``FSCTL_MOVE_FILE`` 需要真实路径打开句柄，``(record N)`` 打不开。
    path_known: bool = True

    @property
    def clusters(self) -> int:
        return sum(s.clusters for s in self.segments)

    @property
    def dst_ranges(self) -> List[Tuple[int, int]]:
        return [(s.dst_lcn, s.dst_end) for s in self.segments]

    @property
    def src_ranges(self) -> List[Tuple[int, int]]:
        return [(s.src_lcn, s.src_lcn + s.clusters) for s in self.segments]

    @property
    def first_dst(self) -> int:
        return min((s.dst_lcn for s in self.segments), default=0)


@dataclass(slots=True)
class PlanStats:
    """计划统计（UI 摘要直接绑定）。"""

    place_files: int = 0
    place_clusters: int = 0
    evict_files: int = 0
    evict_clusters: int = 0
    relay_files: int = 0
    relay_clusters: int = 0
    unknown_owner_clusters: int = 0

    @property
    def total_clusters(self) -> int:
        """合计搬运簇数。

        ``relay_clusters`` 是 ``place_clusters`` 的**子集**（中转文件的两趟都算装载），
        因此这里不能再加一次，否则中转会被重复计数。
        """
        return self.place_clusters + self.evict_clusters


@dataclass(slots=True)
class FeasibilityReport:
    """可行性评估结果（三档 + 量化原因 + 建议）。"""

    verdict: str = VERDICT_IMPOSSIBLE
    window_clusters: int = 0
    usable_clusters: int = 0
    selected_clusters: int = 0
    free_in_window: int = 0
    blocked_clusters: int = 0
    evictable_clusters: int = 0
    unknown_owner_clusters: int = 0
    largest_file_clusters: int = 0
    shortfall_clusters: int = 0
    blockers: List[Blocker] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)

    @property
    def headline(self) -> str:
        """一行徽章文案。"""
        if self.verdict == VERDICT_DIRECT:
            return "直接可行"
        if self.verdict == VERDICT_EVICT:
            return f"需驱逐 {len([b for b in self.blockers if b.movable])} 个文件"
        return "不可行"

    @property
    def ok(self) -> bool:
        return self.verdict in (VERDICT_DIRECT, VERDICT_EVICT)
