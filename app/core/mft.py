"""N5 · MFT 解析（**纯字节层**，无 ctypes、无 IO、无 Qt）。

本模块只做「给我一段字节，告诉我是哪个文件、占了哪些簇」，**不读盘**。
所有原始读都在 :mod:`app.win32.mft_reader` 里，并经 :class:`MftSpanGuard` 校验。

三道防线（设计 §4.6）：

1. **边界**：:class:`MftSpanGuard` 是唯一校验入口，且**用扇区对齐后的区间**
   做校验（否则"为对齐多读的扇区"会绕过检查）；越界抛
   :class:`MftBoundsError` = 致命 = 整条通道作废 → 回退句柄通道。
2. **降级**：解析失败一律「计数 + 跳过」，绝不猜测、绝不修补；
   只有 :class:`MftBoundsError` 允许穿透解析循环。
3. **自证**：:func:`sample_pick` + :func:`compare_extents` 用
   ``FSCTL_GET_RETRIEVAL_POINTERS`` 按 ``(vcn, lcn, clusters)`` **三元组**复核，
   专门抓「vcn 错位导致文件中间段被搬走」这类静默数据错乱。

为什么 ``vcn`` 必须严格可信：``MoveSegment.vcn`` 会被直接塞进
``MOVE_FILE_DATA.StartingVcn``；若 MFT 给出 ``vcn=8`` 而真实是 ``0``，
移动调用会把文件中间的一段搬到目标位置 —— 数据静默错乱且无任何报错。
"""

from __future__ import annotations

import random
import struct
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

from app.core.models import (
    FILE_ATTRIBUTE_COMPRESSED,
    FILE_ATTRIBUTE_ENCRYPTED,
    FILE_ATTRIBUTE_SPARSE_FILE,
    Extent,
    FileEntry,
)

# --------------------------------------------------------------------------- #
# 常量（设计 §6.3 公开契约）
# --------------------------------------------------------------------------- #
SECTOR_SIZE_FALLBACK: int = 512
MAX_RAW_READ_BYTES: int = 1 << 20
MAX_PATH_DEPTH: int = 64
MAX_RUNS_PER_FILE: int = 4096
HEX_MAGIC_FILE: bytes = b"FILE"
METADATA_RECORD_MAX: int = 15

ATTR_STANDARD_INFORMATION: int = 0x10
ATTR_ATTRIBUTE_LIST: int = 0x20
ATTR_FILE_NAME: int = 0x30
ATTR_OBJECT_ID: int = 0x40
ATTR_DATA: int = 0x80
ATTR_END: int = 0xFFFFFFFF

FLAG_IN_USE: int = 0x0001
FLAG_DIRECTORY: int = 0x0002

ATTR_FLAG_COMPRESSED: int = 0x0001
ATTR_FLAG_ENCRYPTED: int = 0x4000
ATTR_FLAG_SPARSE: int = 0x8000

#: FILE_NAME 命名空间：1=Win32，3=Win32&DOS（都要），2=8.3 DOS 别名（跳过）
NAMESPACE_DOS_ONLY: int = 2
NAMESPACE_WIN32: int = 1
NAMESPACE_WIN32_DOS: int = 3

#: 根目录固定记录号（``$Root``）
ROOT_RECORD_NUMBER: int = 5

#: ``FILE_ATTRIBUTE_REPARSE_POINT``（重解析点 / 符号链接）
FILE_ATTRIBUTE_REPARSE_POINT: int = 0x00040000

#: 已知「被系统独占打开、永远搬不动」的大文件（D9 / R19：要显示，不能藏）
EXCLUSIVE_SYSTEM_FILES = frozenset(
    {"pagefile.sys", "hiberfil.sys", "swapfile.sys", "dumpstack.log.tmp"}
)


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #
class MftBoundsError(RuntimeError):
    """原始读越界 —— 致命，立即中止整个 MFT 通道（绝不截断后继续）。

    设计 §4.6.2：越界后"就地截断继续解析"会得到半条记录 → 记录错位 →
    后面所有记录都是垃圾 → 产出大量**看似合法但完全错误**的 extents。
    """

    def __init__(self, offset: int, length: int, limit: int, what: str) -> None:
        self.offset = int(offset)
        self.length = int(length)
        self.limit = int(limit)
        self.what = what
        super().__init__(
            f"{what}越界：请求 [{self.offset:,} + {self.length:,}) "
            f"超出上限 {self.limit:,}（已中止，不截断）"
        )


class MftParseError(ValueError):
    """记录 / 属性 / runlist 结构非法（调用方计数 + 跳过，不猜测）。"""


# --------------------------------------------------------------------------- #
# 扇区对齐小工具
# --------------------------------------------------------------------------- #
def align_down(value: int, unit: int) -> int:
    """向下对齐到 ``unit`` 的整数倍。"""
    if unit <= 0:
        return int(value)
    return (int(value) // unit) * unit


def align_up(value: int, unit: int) -> int:
    """向上对齐到 ``unit`` 的整数倍。"""
    if unit <= 0:
        return int(value)
    return ((int(value) + unit - 1) // unit) * unit


# --------------------------------------------------------------------------- #
# 边界守卫（设计 §4.6.2）
# --------------------------------------------------------------------------- #
@dataclass(slots=True, frozen=True)
class MftSpanGuard:
    """原始读边界守卫：卷内 + MFT 有效范围双重校验。

    ★ **两条易错点（已落成显式代码 + 单测覆盖）**

    1. 校验必须用**扇区对齐后**的区间。卷句柄的 ``ReadFile`` 要求起止都对齐到
       ``bytes_per_sector``，所以读 ``[off, off+len)`` 实际会读
       ``[align_down(off), align_up(off+len))``。若拿未对齐的原始区间去校验，
       "为对齐多读的那几个扇区"就绕过了越界检查（设计 §4.6.2 明确点出的坑）。
       → 本实现 :meth:`check` 内部**先 normalize 再断言**，不存在第二种顺序。
    2. 只有 :class:`MftBoundsError` 允许穿透解析循环；其余异常逐记录吞掉并计数
       （落在 :meth:`app.win32.mft_reader.scan_volume_mft` 的 ``try/except`` 里）。
    """

    volume_bytes: int = 0          # = total_clusters × bytes_per_cluster
    mft_valid_bytes: int = 0       # = MftValidDataLength
    record_size: int = 1024
    sector_size: int = SECTOR_SIZE_FALLBACK

    @property
    def sector(self) -> int:
        """实际用于对齐的扇区大小（非法值退回 512，绝不抛）。"""
        return self.sector_size if self.sector_size > 0 else SECTOR_SIZE_FALLBACK

    @property
    def valid_limit(self) -> int:
        """MFT 有效范围**上限**（向上取整到扇区，与 normalize 的语义对齐）。"""
        return align_up(self.mft_valid_bytes, self.sector)

    def normalize(self, offset: int, length: int) -> Tuple[int, int]:
        """扇区对齐：向下对齐起点、向上对齐终点，返回真正要读的 ``(offset, length)``。"""
        sector = self.sector
        start = align_down(max(0, int(offset)), sector)
        end = align_up(int(offset) + max(0, int(length)), sector)
        return start, max(0, end - start)

    def check(self, offset: int, length: int) -> None:
        """五条断言，任一失败抛 :class:`MftBoundsError`（附完整上下文）。

        顺序是安全关键：先算对齐区间，再用**对齐区间**做全部范围断言。
        """
        offset = int(offset)
        length = int(length)

        # 1) 长度必须为正且不超过单次原始读上限
        if length <= 0:
            raise MftBoundsError(offset, length, MAX_RAW_READ_BYTES, "读取长度必须为正：")
        if length > MAX_RAW_READ_BYTES:
            raise MftBoundsError(
                offset, length, MAX_RAW_READ_BYTES, f"读取长度超过 {MAX_RAW_READ_BYTES:,} 字节："
            )

        # 2) 起点不得为负
        if offset < 0:
            raise MftBoundsError(offset, length, 0, "读取起点为负：")

        # ★ 3~4 必须用对齐后的区间（易错点 1）
        aligned_offset, aligned_length = self.normalize(offset, length)
        aligned_end = aligned_offset + aligned_length

        # 3) 不越过卷尾
        if aligned_end > self.volume_bytes:
            raise MftBoundsError(
                aligned_offset,
                aligned_length,
                self.volume_bytes,
                "卷内物理读（含扇区对齐补读）：",
            )

        # 4) 不越过 MFT 有效范围
        if aligned_end > self.valid_limit:
            raise MftBoundsError(
                aligned_offset,
                aligned_length,
                self.valid_limit,
                "MFT 有效范围内读取（含扇区对齐补读）：",
            )

        # 5) 对齐后的区间自身必须扇区对齐（ReadFile 的硬要求）
        sector = self.sector
        if aligned_offset % sector or aligned_length % sector:
            raise MftBoundsError(
                aligned_offset,
                aligned_length,
                self.valid_limit,
                f"扇区（{sector} B）对齐失败：",
            )

    def check_physical(self, offset: int, length: int) -> None:
        """**物理**读（卷内字节偏移）校验：只断言卷内，不涉及 MFT 有效范围。

        逻辑偏移合法不代表物理偏移合法 —— 段表里的 ``lcn`` 若指向卷尾之外
        （损坏的 runlist / 被篡改的记录），``ReadFile`` 会读到别的东西或失败。
        """
        aligned_offset, aligned_length = self.normalize(offset, length)
        if aligned_offset < 0 or aligned_offset + aligned_length > self.volume_bytes:
            raise MftBoundsError(
                aligned_offset,
                aligned_length,
                self.volume_bytes,
                "卷内物理读（含扇区对齐补读）：",
            )


#: 设计讨论里出现过两种叫法，统一别名，避免调用方踩空
MftBoundsGuard = MftSpanGuard


# --------------------------------------------------------------------------- #
# $MFT 逻辑字节流（设计 §4.2）
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class MftStream:
    """``$MFT`` 逻辑字节流 → 物理段表。

    ``extents`` 的 ``vcn`` 是**逻辑簇偏移**（不是 LCN）；``lcn`` 是物理起点。
    ``$MFT`` 自身也可能碎片化，绝不能假设物理连续。
    """

    extents: List[Extent] = field(default_factory=list)
    valid_length: int = 0
    record_size: int = 1024
    bytes_per_cluster: int = 4096
    bytes_per_sector: int = SECTOR_SIZE_FALLBACK

    @property
    def record_count(self) -> int:
        """有效范围内的**完整**记录数（不完整尾记录不解析）。"""
        if self.record_size <= 0:
            return 0
        return max(0, self.valid_length // self.record_size)

    @property
    def byte_length(self) -> int:
        """段表覆盖的逻辑字节数。"""
        return sum(e.clusters for e in self.extents) * self.bytes_per_cluster

    @property
    def physical_clusters(self) -> int:
        """``$MFT`` 实际占用的物理簇数。"""
        return sum(e.clusters for e in self.extents)

    def record_offset(self, record_no: int) -> int:
        """记录号 → 逻辑字节偏移。"""
        return int(record_no) * self.record_size

    def locate(self, offset: int, length: int) -> List[Tuple[int, int, int]]:
        """把逻辑区间 ``[offset, offset+length)`` 拆成落在单个 extent 内的物理段。

        Returns:
            ``[(logical_offset, physical_offset, length), ...]``

        Raises:
            MftParseError: 逻辑流存在空洞（段表不连续）—— 说明 runlist 解析不完整。
        """
        if length <= 0:
            return []
        bpc = self.bytes_per_cluster
        if bpc <= 0:
            raise MftParseError("簇大小为 0，无法把逻辑偏移翻译成物理偏移")

        result: List[Tuple[int, int, int]] = []
        cursor = int(offset)
        remaining = int(length)
        for extent in sorted(self.extents, key=lambda e: e.vcn):
            if remaining <= 0:
                break
            extent_start = extent.vcn * bpc
            extent_end = (extent.vcn + extent.clusters) * bpc
            if extent_end <= cursor:
                continue
            if extent_start > cursor:
                raise MftParseError(
                    f"$MFT 逻辑流在 {cursor:,} 处有空洞（下一段从 {extent_start:,} 开始）"
                )
            take = min(cursor + remaining, extent_end) - cursor
            if take <= 0:
                continue
            physical = extent.lcn * bpc + (cursor - extent_start)
            result.append((cursor, physical, take))
            cursor += take
            remaining -= take
        if remaining > 0:
            raise MftParseError(
                f"$MFT 逻辑流不足：仍缺 {remaining:,} 字节（段表覆盖 {self.byte_length:,}）"
            )
        return result

    def describe(self) -> str:
        """一行中文摘要（日志 / UI 用）。"""
        return (
            f"$MFT 逻辑 {self.valid_length:,} B（{self.record_count:,} 条记录 × "
            f"{self.record_size} B），物理 {self.physical_clusters:,} 簇 / "
            f"{len(self.extents)} 段"
        )


# --------------------------------------------------------------------------- #
# runlist 解码（设计 §4.4）
# --------------------------------------------------------------------------- #
def decode_runlist(data: bytes, start_vcn: int = 0) -> Tuple[List[Extent], List[str]]:
    """解析 NTFS runlist。

    头字节：``low nibble = 长度字段字节数``，``high nibble = 偏移字段字节数``；
    偏移是**相对上一个非稀疏 LCN 的有符号增量**；``offset_size == 0`` 表示稀疏段
    （不消耗 LCN，但 VCN 继续推进）。

    Args:
        data: 属性里 ``run_offset`` 之后的字节。
        start_vcn: 该属性自身的起始 VCN（分片 ``$DATA`` 会 > 0）。

    Returns:
        ``(extents, warnings)``。稀疏段的 ``lcn == -1``。
        结构损坏时**返回已解析部分 + 警告**，绝不抛异常、绝不猜测。
    """
    extents: List[Extent] = []
    warnings: List[str] = []
    cursor = 0
    vcn = int(start_vcn)
    current_lcn = 0          # ★ 跨 run 累加的基准（不是每个 run 的绝对 LCN）
    size = len(data)

    while cursor < size:
        if len(extents) >= MAX_RUNS_PER_FILE:
            warnings.append(f"run 数达到上限 {MAX_RUNS_PER_FILE}，已停止解析")
            break
        header = data[cursor]
        cursor += 1
        if header == 0:                      # 结束标记
            break
        length_size = header & 0x0F
        offset_size = header >> 4
        if length_size == 0:
            warnings.append("runlist 头非法（长度字段字节数为 0），已停止")
            break
        if cursor + length_size + offset_size > size:
            warnings.append("runlist 被截断，已返回已解析部分")
            break
        clusters = int.from_bytes(data[cursor : cursor + length_size], "little", signed=False)
        cursor += length_size

        if offset_size == 0:
            # 稀疏段：不消耗 LCN，但推进 VCN
            if clusters > 0:
                extents.append(Extent(vcn=vcn, lcn=-1, clusters=clusters))
                vcn += clusters
            else:
                warnings.append("稀疏段长度为 0，已跳过")
            continue

        delta = int.from_bytes(data[cursor : cursor + offset_size], "little", signed=True)
        cursor += offset_size
        if clusters <= 0:
            warnings.append(f"段长度为 0（header=0x{header:02X}），已停止")
            break
        current_lcn += delta
        if current_lcn < 0:
            warnings.append(f"累加后 LCN 为负（{current_lcn}），已返回已解析部分")
            break
        extents.append(Extent(vcn=vcn, lcn=current_lcn, clusters=clusters))
        vcn += clusters

    return extents, warnings


# --------------------------------------------------------------------------- #
# fixup（更新序列数组，设计 §4.3.1）
# --------------------------------------------------------------------------- #
def apply_fixup(
    record: bytearray, record_size: int, sector_size: int = SECTOR_SIZE_FALLBACK
) -> bool:
    """就地还原 USA 替换的扇区尾字；记录被撕裂时返回 ``False``。

    每条记录尾部的每个扇区最后 2 字节在写入时被替换成 USN，原值存在
    ``usa_offset`` 起的数组里。读回时逐个校验 + 还原；任一扇区 USN 不匹配
    说明**写入中途断电 / 撕裂**，该记录必须整条丢弃。

    ★ 必须在**任何属性解析之前**调用（R16：否则跨扇区属性会解析出垃圾）。

    Returns:
        ``True`` = 已还原（可直接解析）；``False`` = 结构非法或已撕裂（丢弃）。
    """
    if record_size <= 0 or len(record) < record_size or record_size < 8:
        return False
    if sector_size <= 0:
        sector_size = SECTOR_SIZE_FALLBACK

    usa_offset, usa_count = struct.unpack_from("<HH", record, 4)
    # usa_count 必须与实际扇区数一致（含 USA 自身那一条）
    expected_count = record_size // sector_size + 1
    if usa_count != expected_count:
        return False
    if usa_count < 2 or usa_offset + usa_count * 2 > record_size:
        return False

    usn = struct.unpack_from("<H", record, usa_offset)[0]
    for index in range(1, usa_count):
        position = index * sector_size - 2
        if position + 2 > record_size:
            return False
        if struct.unpack_from("<H", record, position)[0] != usn:
            return False
        replacement = struct.unpack_from("<H", record, usa_offset + index * 2)[0]
        struct.pack_into("<H", record, position, replacement)
    return True


# --------------------------------------------------------------------------- #
# 属性头（设计 §6.1 AttrHeader）
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class AttrHeader:
    """属性头解析结果（**不是**完整属性值）。"""

    type: int = 0
    length: int = 0
    non_resident: bool = False
    name_length: int = 0
    name_offset: int = 0
    flags: int = 0
    attr_id: int = 0
    # 常驻
    value_length: int = 0
    value_offset: int = 0
    # 非常驻
    start_vcn: int = 0
    last_vcn: int = 0
    run_offset: int = 0
    compression_unit: int = 0
    alloc_size: int = 0
    data_size: int = 0
    init_size: int = 0
    #: 属性头在记录内的字节偏移（runlist / 值字节的定位基准）
    header_offset: int = 0

    @property
    def end_offset(self) -> int:
        return self.header_offset + self.length

    @property
    def is_anonymous(self) -> bool:
        """匿名属性（无名称）—— 只有匿名 ``$DATA`` 才是文件主数据流。"""
        return self.name_length == 0


def parse_attribute_header(raw: bytes, offset: int, record_size: int) -> Optional[AttrHeader]:
    """解析 ``offset`` 处的属性头；结构非法返回 ``None``（不抛）。"""
    if offset < 0 or offset + 16 > record_size or offset + 16 > len(raw):
        return None
    attr_type, length = struct.unpack_from("<II", raw, offset)
    if attr_type == ATTR_END or attr_type == 0 or length == 0:
        return None
    if length < 16 or offset + length > record_size:
        return None

    non_resident = raw[offset + 8]
    name_length = raw[offset + 9]
    name_offset = struct.unpack_from("<H", raw, offset + 0x0A)[0]
    flags, attr_id = struct.unpack_from("<HH", raw, offset + 0x0C)

    header = AttrHeader(
        type=attr_type,
        length=length,
        non_resident=bool(non_resident),
        name_length=name_length,
        name_offset=name_offset,
        flags=flags,
        attr_id=attr_id,
        header_offset=offset,
    )
    if header.non_resident:
        if length < 0x40:
            return None
        (
            header.start_vcn,
            header.last_vcn,
        ) = struct.unpack_from("<qq", raw, offset + 0x10)
        header.run_offset = struct.unpack_from("<H", raw, offset + 0x20)[0]
        header.compression_unit = struct.unpack_from("<H", raw, offset + 0x22)[0]
        (
            header.alloc_size,
            header.data_size,
            header.init_size,
        ) = struct.unpack_from("<qqq", raw, offset + 0x28)
        if header.start_vcn < 0 or header.last_vcn < header.start_vcn - 1:
            return None
        if header.run_offset >= length:
            return None
    else:
        if length < 0x18:
            return None
        header.value_length = struct.unpack_from("<I", raw, offset + 0x10)[0]
        header.value_offset = struct.unpack_from("<H", raw, offset + 0x14)[0]
        if header.value_offset + header.value_length > length:
            return None
    if name_length:
        if name_offset + name_length * 2 > length:
            return None
    return header


def iter_attribute_headers(
    raw: bytes, start: int, record_size: int
) -> Iterator[AttrHeader]:
    """遍历记录内的属性头（遇到 ``ATTR_END`` / 结构非法即停止）。"""
    cursor = start
    while cursor + 16 <= record_size:
        header = parse_attribute_header(raw, cursor, record_size)
        if header is None:
            return
        yield header
        cursor = header.end_offset


# --------------------------------------------------------------------------- #
# FILE 记录（设计 §6.1 MftRecord）
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class MftRecord:
    """一条 FILE 记录的结构化视图（已过 fixup）。"""

    record_no: int = -1
    in_use: bool = False
    is_directory: bool = False
    base_record_ref: int = 0            # 低 48 位；≠0 表示 extension record
    sequence: int = 0
    attributes: List[AttrHeader] = field(default_factory=list)
    parent_ref: int = -1                # 来自主 $FILE_NAME
    name: str = ""
    namespace: int = 0
    file_attributes: int = 0            # $STANDARD_INFORMATION 的 FILE_ATTRIBUTE_*
    data_flags: int = 0                 # 匿名 $DATA 的压缩/加密/稀疏标志
    extents: List[Extent] = field(default_factory=list)
    resident: bool = False              # 匿名 $DATA 是常驻（数据在记录内）
    has_attribute_list: bool = False
    link_count: int = 1
    #: 匿名 ``$DATA`` 的逻辑大小（常驻 = 值长度；非常驻 = ``data_size``）
    data_size: int = 0
    warnings: List[str] = field(default_factory=list)
    #: 布局不完整（属性列表跨记录 / vcn 空洞）→ 必须走句柄通道复核
    incomplete: bool = False
    #: 是否存在命名数据流（ADS）
    has_ads: bool = False

    # ---------------- 派生属性 ---------------- #
    @property
    def is_compressed(self) -> bool:
        return bool(self.data_flags & ATTR_FLAG_COMPRESSED) or bool(
            self.file_attributes & FILE_ATTRIBUTE_COMPRESSED
        )

    @property
    def is_encrypted(self) -> bool:
        return bool(self.data_flags & ATTR_FLAG_ENCRYPTED) or bool(
            self.file_attributes & FILE_ATTRIBUTE_ENCRYPTED
        )

    @property
    def is_sparse(self) -> bool:
        return bool(self.data_flags & ATTR_FLAG_SPARSE) or bool(
            self.file_attributes & FILE_ATTRIBUTE_SPARSE_FILE
        )

    @property
    def is_reparse(self) -> bool:
        return bool(self.file_attributes & FILE_ATTRIBUTE_REPARSE_POINT)

    @property
    def allocated_clusters(self) -> int:
        return sum(e.clusters for e in self.extents if e.lcn >= 0 and e.clusters > 0)


def _read_name(record: MftRecord, buf: bytearray, header: AttrHeader) -> None:
    """解析 ``$FILE_NAME`` 属性值：父记录号 / 名字 / 命名空间 / 链接计数。"""
    base = header.header_offset + header.value_offset
    if header.value_length < 0x42 or base + 0x42 > len(buf):
        record.warnings.append("$FILE_NAME 过短，已跳过")
        return
    parent_ref = struct.unpack_from("<q", buf, base)[0]
    name_length = buf[base + 0x40]
    namespace = buf[base + 0x41]
    if name_length == 0:
        return
    name_end = base + 0x42 + name_length * 2
    if name_end > len(buf):
        record.warnings.append("$FILE_NAME 名字越界，已跳过")
        return
    raw_name = buf[base + 0x42 : name_end]
    try:
        name = raw_name.decode("utf-16-le")
    except UnicodeDecodeError:
        record.warnings.append("$FILE_NAME 名字解码失败，已跳过")
        return

    record.link_count = len([h for h in record.attributes if h.type == ATTR_FILE_NAME]) or 1
    if namespace == NAMESPACE_DOS_ONLY:
        # 8.3 短名：同一文件会出现两条记录，跳过避免重复
        return
    priority = 2 if namespace in (NAMESPACE_WIN32, NAMESPACE_WIN32_DOS) else 1
    current = 2 if record.namespace in (NAMESPACE_WIN32, NAMESPACE_WIN32_DOS) else (
        1 if record.namespace else 0
    )
    if priority > current:
        record.name = name
        record.namespace = namespace
        record.parent_ref = parent_ref & 0x0000FFFFFFFFFFFF


def parse_record(
    raw: bytes,
    record_no: int = 0,
    record_size: int = 1024,
    stats: Optional["MftScanStats"] = None,
    *,
    sector_size: int = SECTOR_SIZE_FALLBACK,
    bytes_per_cluster: int = 0,
) -> Optional[MftRecord]:
    """解析一条 FILE 记录。

    Args:
        raw: 记录原始字节（长度应为 ``record_size``）。
        record_no: 记录号（由调用方按偏移算出）。
        record_size: 记录大小，**不得硬编码**（来自 ``BytesPerFileRecordSegment``）。
        stats: 可选的计数器；结构失败会就地累加（纯数据，无 IO）。
        sector_size: fixup 的扇区基准（4Kn 盘为 4096）。
        bytes_per_cluster: 用于判断属性列表是否已覆盖全部数据；未知传 0。

    Returns:
        :class:`MftRecord`；结构非法返回 ``None``（已计数，不抛、不猜）。
    """
    def _bump(name: str) -> None:
        if stats is not None:
            setattr(stats, name, int(getattr(stats, name, 0)) + 1)

    if record_size < 48 or len(raw) < record_size:
        _bump("bad_magic")
        return None
    buf = bytearray(raw[:record_size])
    if buf[:4] != HEX_MAGIC_FILE:
        # 全零记录 = MFT 已分配但尚未使用的空间（真实卷上很常见），
        # 它不是损坏，必须计入"未使用"而不是"异常"，否则健康度会被误判
        if not any(buf):
            return MftRecord(record_no=int(record_no), in_use=False)
        _bump("bad_magic")
        return None
    # ★ fixup 必须在任何属性解析之前（R16）
    if not apply_fixup(buf, record_size, sector_size):
        _bump("bad_fixup")
        return None

    flags = struct.unpack_from("<H", buf, 0x16)[0]
    sequence = struct.unpack_from("<H", buf, 0x10)[0]
    attr_offset = struct.unpack_from("<H", buf, 0x14)[0]
    base_ref = struct.unpack_from("<q", buf, 0x20)[0]

    record = MftRecord(
        record_no=int(record_no),
        in_use=bool(flags & FLAG_IN_USE),
        is_directory=bool(flags & FLAG_DIRECTORY),
        base_record_ref=int(base_ref) & 0x0000FFFFFFFFFFFF,
        sequence=sequence,
    )
    if not record.in_use:
        return record
    if attr_offset < 48 or attr_offset >= record_size:
        _bump("bad_attribute")
        return None

    record.attributes = list(iter_attribute_headers(bytes(buf), attr_offset, record_size))
    if not record.attributes:
        _bump("bad_attribute")
        return None

    expected_vcn = 0
    size = 0
    data_found = False
    for header in record.attributes:
        if header.type == ATTR_STANDARD_INFORMATION and not header.non_resident:
            base = header.header_offset + header.value_offset
            if header.value_length >= 0x24 and base + 0x24 <= len(buf):
                record.file_attributes = struct.unpack_from("<I", buf, base + 0x20)[0]
        elif header.type == ATTR_FILE_NAME and not header.non_resident:
            _read_name(record, buf, header)
        elif header.type == ATTR_ATTRIBUTE_LIST:
            record.has_attribute_list = True
        elif header.type == ATTR_DATA:
            if header.name_length:
                # 命名数据流（ADS）：不计入主数据流，也不计入 allocated_clusters
                record.has_ads = True
                _bump("ads_skipped")
                continue
            data_found = True
            if header.non_resident:
                record.data_flags |= header.flags
                if header.start_vcn != expected_vcn:
                    record.incomplete = True
                    record.warnings.append(
                        f"$DATA vcn 空洞（期望 {expected_vcn}，实际 {header.start_vcn}）"
                    )
                blob_start = header.header_offset + header.run_offset
                blob_end = header.end_offset
                if blob_start >= blob_end:
                    _bump("bad_runlist")
                    record.incomplete = True
                    continue
                extents, warnings = decode_runlist(bytes(buf[blob_start:blob_end]), header.start_vcn)
                record.warnings.extend(warnings)
                if warnings and not extents:
                    _bump("bad_runlist")
                    record.incomplete = True
                record.extents.extend(extents)
                expected_vcn = header.start_vcn + sum(e.clusters for e in extents)
                size = max(size, int(header.data_size))
            else:
                record.resident = True
                size = max(size, int(header.value_length))

    # 属性列表跨记录？base 记录里的 runlist 是否已覆盖全部数据？
    if record.has_attribute_list:
        if not data_found:
            # ★ 关键判据（FIX-3）：基础记录里**没有** $DATA —— 数据在扩展记录里。
            # 此时 size == 0、covered == 0，`size > covered` 恒为 False，
            # 用它当"是否完整"的判据会得到 incomplete=False，进而产出
            # `movable=True, size=0, extents=[]` 的条目；规划层看到"0 簇、
            # 无需搬运"就判"已就位" → UI 显示"已整理"而真实碎片一个字节没动。
            # 真盘规模：F: 86 个 / H: 100 个。
            record.incomplete = True
            record.warnings.append(
                "属性列表跨记录：基础记录内没有 $DATA（数据在扩展记录里），extents 未知"
            )
        else:
            covered = expected_vcn * bytes_per_cluster
            if record.resident:
                covered = size
            if bytes_per_cluster <= 0 or size > covered:
                record.incomplete = True
                record.warnings.append(
                    "$ATTRIBUTE_LIST 指向 extension record，extents 可能不完整"
                )

    record.extents.sort(key=lambda e: e.vcn)
    if size <= 0 and not record.resident:
        size = record.allocated_clusters * max(1, bytes_per_cluster)
    record.data_size = int(max(0, size))
    return record


def record_data_size(record: MftRecord) -> int:
    """取 :func:`parse_record` 记录的 ``$DATA`` 大小（0 = 未知/常驻空文件）。"""
    return int(record.data_size)


# --------------------------------------------------------------------------- #
# 路径重建（设计 §4.6.6）
# --------------------------------------------------------------------------- #
def rebuild_path(
    record_no: int,
    names: Dict[int, Tuple[int, str]],
    root_record: int = ROOT_RECORD_NUMBER,
    max_depth: int = MAX_PATH_DEPTH,
) -> Tuple[str, bool]:
    """由父记录号逐级向上拼出**相对**路径（不含盘符与根前缀）。

    Args:
        record_no: 目标记录号。
        names: ``record_no -> (parent_ref_low48, name)``。
        root_record: 根目录记录号（``$Root`` = 5）。
        max_depth: 深度上限（防超长链拖慢）。

    Returns:
        ``(path_or_placeholder, ok)``。``ok=False`` 时 path 形如 ``"(record 12345)"``，
        UI 必须显示为"路径未知"，且该文件在执行期会被拒绝（打不开句柄）。
    """
    if record_no == root_record:
        return "", True
    parts: List[str] = []
    seen: set = set()
    cursor = int(record_no)
    for _ in range(max_depth):
        if cursor in seen:                       # ★ 环检测（伪造/损坏数据也会成环）
            return f"(record {record_no})", False
        seen.add(cursor)
        if cursor == root_record:                # 到达 $Root → 成功
            return "\\".join(reversed(parts)), True
        info = names.get(cursor)
        if info is None:                         # 父链断（父记录缺失 / 解析失败）
            return f"(record {record_no})", False
        parent, name = info
        if not name or name.startswith("$"):      # 元数据名进路径 → 不算有效路径
            return f"(record {record_no})", False
        parts.append(name)
        if parent == cursor:                      # 自引用
            return f"(record {record_no})", False
        cursor = parent
    return f"(record {record_no})", False          # ★ 深度超限


def _exclusion_reason(record: MftRecord, path_known: bool) -> str:
    """按设计 §4.6.5 表格给出不可移动原因（空串 = 可移动）。"""
    if not path_known:
        return "路径无法重建（无法打开句柄，执行期必须拦下）"
    if record.resident:
        return "常驻文件（数据在 MFT 记录内），无独立物理簇"
    if record.is_encrypted:
        return "EFS 加密文件，系统不支持移动（NTFS 拒绝）"
    if record.is_compressed:
        return "NTFS 压缩文件，压缩单元约束，不可搬运"
    if record.is_sparse:
        return "稀疏文件（仅已分配部分可搬，v2 暂不支持）"
    if record.is_reparse:
        return "重解析点/符号链接"
    if record.incomplete:
        if record.has_attribute_list:
            return "属性列表跨记录，布局未知（基础记录内没有完整 $DATA）"
        return "MFT 记录内 $DATA 布局不完整（vcn 空洞 / runlist 异常），布局未知"
    if record.name.lower() in EXCLUSIVE_SYSTEM_FILES:
        return "被独占打开（系统分页/休眠文件），可规划绕行但不可搬运"
    return ""


def record_to_entry(
    record: MftRecord, path: str, path_known: bool
) -> Optional[FileEntry]:
    """把一条记录转成 :class:`FileEntry`（与句柄通道**逐字段语义一致**）。

    Returns:
        ``None`` = 不该产出文件条目（元数据 0–15 / 未使用 / 目录 / extension record）；
        否则返回 ``FileEntry``，不可移动的文件 ``movable=False`` 且 ``reason`` 非空
        —— 它们**仍然会显示**（D9：让用户知道"这里有一大块搬不走"）。
    """
    if record.record_no <= METADATA_RECORD_MAX:
        return None
    if not record.in_use or record.is_directory or record.base_record_ref != 0:
        return None

    reason = _exclusion_reason(record, path_known)
    incomplete = bool(record.incomplete)
    if not reason and not record.resident and record.allocated_clusters <= 0:
        # ★ 全局兜底（FIX-3 要求）：在用的非空文件却一个物理簇都没有 = 布局未知。
        # 绝不允许它以 `movable=True + extents=[]` 的形态进规划层 —— 规划层会判
        # "需要 0 簇、任何位置都放得下"，于是标成"已就位"，UI 报"已整理"，
        # 而真实碎片一个字节都没动（假成功，与数据损坏同级的信任破坏）。
        reason = "MFT 记录内无有效 extents（布局未知），已排除以免被误判为『已就位』"
        incomplete = True
    attributes = record.file_attributes
    if record.is_compressed:
        attributes |= FILE_ATTRIBUTE_COMPRESSED
    if record.is_encrypted:
        attributes |= FILE_ATTRIBUTE_ENCRYPTED
    if record.is_sparse:
        attributes |= FILE_ATTRIBUTE_SPARSE_FILE

    entry = FileEntry(
        path=path,
        name=record.name,
        size=record_data_size(record),
        extents=list(record.extents),
        movable=not reason,
        reason=reason,
        checked=True,
        attributes=attributes,
        record_no=record.record_no,
        link_count=max(1, record.link_count),
        path_known=path_known,
        source="mft",
        incomplete=incomplete,
    )
    if reason:
        entry.status = "不可移动"
    if record.has_ads and "ADS" not in entry.reason:
        entry.reason = (entry.reason + "；" if entry.reason else "") + "含 ADS，未计入"
    return entry


# --------------------------------------------------------------------------- #
# 抽样复核（设计 §4.8 / R15）
# --------------------------------------------------------------------------- #
def _extent_triples(extents: Sequence[Extent]) -> List[Tuple[int, int, int]]:
    """规整成 ``[(vcn, lcn, clusters), ...]``（按 vcn 排序，剔除尾部稀疏）。

    尾部稀疏段被 NTFS 在 retrieval pointers 里常常省略；为了不把"正常差异"
    误判成解析错误，两侧都先去掉**尾部**稀疏段。**内部**稀疏段保留 ——
    一侧过滤就会被判不一致（这正是要抓的）。
    """
    ordered = sorted(
        (e for e in extents if e.clusters > 0), key=lambda e: (e.vcn, e.lcn)
    )
    while ordered and ordered[-1].lcn < 0:
        ordered.pop()
    return [(int(e.vcn), int(e.lcn), int(e.clusters)) for e in ordered]


def compare_extents(mft: Sequence[Extent], handle: Sequence[Extent]) -> bool:
    """按 ``(vcn, lcn, clusters)`` 三元组比对两侧 extents（不比字节数）。

    只比总字节数会漏掉"vcn 错位"这种最危险的错误；三元组能立刻发现。
    """
    return _extent_triples(mft) == _extent_triples(handle)


def sample_pick(
    entries: Sequence[FileEntry], sample_size: int = 32, rng_seed: Optional[int] = None
) -> List[FileEntry]:
    """抽样复核的样本：**加权抽样**，不是纯均匀随机。

    必抽集合（最多 8 个）：extent 数最多的前 3、体量最大的前 3、
    含稀疏段的 1、含 ADS 的 1；其余从"有 extents 且非元数据"的文件里均匀随机。
    """
    if sample_size <= 0:
        return []
    pool = [
        e
        for e in entries
        if e.path_known and e.record_no > METADATA_RECORD_MAX and e.allocated_clusters > 0
    ]
    if not pool:
        return []
    if len(pool) <= sample_size:
        return list(pool)

    forced: List[FileEntry] = []
    by_fragments = sorted(pool, key=lambda e: (-e.fragments, e.record_no))
    forced.extend(by_fragments[:3])
    by_size = sorted(pool, key=lambda e: (-e.allocated_clusters, e.record_no))
    forced.extend(by_size[:3])
    sparse = next((e for e in pool if e.is_sparse), None)
    if sparse is not None:
        forced.append(sparse)
    ads = next((e for e in pool if "ADS" in e.reason), None)
    if ads is not None:
        forced.append(ads)

    chosen: List[FileEntry] = []
    chosen_ids: set = set()
    for entry in forced:
        if entry.record_no in chosen_ids:
            continue
        chosen_ids.add(entry.record_no)
        chosen.append(entry)
        if len(chosen) >= sample_size:
            return chosen[:sample_size]

    remaining = [e for e in pool if e.record_no not in chosen_ids]
    random.Random(rng_seed).shuffle(remaining)
    need = max(0, sample_size - len(chosen))
    return chosen + remaining[:need]


# --------------------------------------------------------------------------- #
# 通道健康度（设计 §4.6.3）
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class MftScanStats:
    """MFT 通道的完整健康度快照。**UI 必须显示**，不允许只写日志。"""

    record_size: int = 0
    total_records: int = 0
    in_use_records: int = 0
    file_records: int = 0
    directory_records: int = 0
    skipped_metadata: int = 0
    skipped_not_in_use: int = 0
    skipped_extension: int = 0
    bad_magic: int = 0
    bad_fixup: int = 0
    bad_attribute: int = 0
    bad_runlist: int = 0
    attr_list_skipped: int = 0
    resident_data: int = 0
    path_unknown: int = 0
    read_error: int = 0
    fatal_error: str = ""
    elapsed_seconds: float = 0.0
    sampled: int = 0
    sample_mismatch: int = 0
    #: ADS 被跳过的属性数（设计 §4.6.5 要求计数）
    ads_skipped: int = 0

    @property
    def skipped_total(self) -> int:
        """被排除的总数（元数据 + 未使用 + extension + 目录）。"""
        return (
            self.skipped_metadata
            + self.skipped_not_in_use
            + self.skipped_extension
            + self.directory_records
        )

    @property
    def anomalies(self) -> int:
        """结构异常总数（magic / fixup / 属性 / runlist / 读错误）。"""
        return (
            self.bad_magic
            + self.bad_fixup
            + self.bad_attribute
            + self.bad_runlist
            + self.read_error
        )

    @property
    def parse_rate(self) -> float:
        """在用记录的解析成功率（1.0 = 全部成功）。"""
        total = self.in_use_records or 0
        if not total:
            return 0.0
        return max(0.0, 1.0 - self.anomalies / total)

    @property
    def mismatch_rate(self) -> float:
        """抽样不一致率（``0.0`` = 全部一致 / 未做抽样）。"""
        if not self.sampled:
            return 0.0
        return self.sample_mismatch / self.sampled

    @property
    def healthy(self) -> bool:
        """通道是否可信：无致命错误且解析率 ≥ 95%。"""
        return not self.fatal_error and self.parse_rate >= 0.95

    def sample_text(self) -> str:
        """抽样复核结论文案（UI caption 直接用）。"""
        if not self.sampled:
            return "未做抽样复核"
        return f"抽样 {self.sampled - self.sample_mismatch}/{self.sampled} 一致"

    def summary_line(self) -> str:
        """一行中文摘要，形如设计 §4.9 的 caption。"""
        elapsed = f"{self.elapsed_seconds:.1f} s" if self.elapsed_seconds else "—"
        return (
            f"MFT 通道 · {elapsed} · 有效 {self.in_use_records:,} / {self.total_records:,} · "
            f"跳过 {self.skipped_total:,} · 异常 {self.anomalies:,} · {self.sample_text()}"
        )

    def describe(self) -> str:
        """多行明细（``ScanStatsDialog`` / 日志用）。"""
        return (
            f"记录大小 {self.record_size} B，记录总数 {self.total_records:,}，"
            f"在用 {self.in_use_records:,}，文件 {self.file_records:,}，"
            f"目录 {self.directory_records:,}\n"
            f"跳过：元数据 {self.skipped_metadata:,} / 未使用 {self.skipped_not_in_use:,} / "
            f"extension {self.skipped_extension:,}\n"
            f"异常：magic {self.bad_magic:,} / fixup {self.bad_fixup:,} / "
            f"属性 {self.bad_attribute:,} / runlist {self.bad_runlist:,} / "
            f"读错误 {self.read_error:,}\n"
            f"降级：属性列表 {self.attr_list_skipped:,}，常驻 {self.resident_data:,}，"
            f"路径未知 {self.path_unknown:,}，ADS {self.ads_skipped:,}\n"
            f"{self.sample_text()}，耗时 {self.elapsed_seconds:.2f} s"
            + (f"\n致命错误：{self.fatal_error}" if self.fatal_error else "")
        )


# --------------------------------------------------------------------------- #
# 通道决策 + 回退原因码（设计 §4.9，纯函数，可单测）
# --------------------------------------------------------------------------- #
#: 回退原因码（UI 文案与日志必须逐一对应，不允许静默回退）
FALLBACK_NOT_NTFS = "not_ntfs"
FALLBACK_NO_PRIVILEGE = "no_privilege"
FALLBACK_RUNLIST_UNAVAILABLE = "mft_runlist_unavailable"
FALLBACK_STRUCTURE_INVALID = "structure_invalid"
FALLBACK_BOUNDS_VIOLATION = "bounds_violation"
FALLBACK_SAMPLE_MISMATCH = "sample_mismatch"
FALLBACK_USER_PREFERRED_HANDLE = "user_preferred_handle"

FALLBACK_MESSAGES: Dict[str, str] = {
    FALLBACK_NOT_NTFS: "当前卷不是 NTFS，已改用句柄扫描",
    FALLBACK_NO_PRIVILEGE: "缺少管理员权限，无法直读 MFT，已改用句柄扫描",
    FALLBACK_RUNLIST_UNAVAILABLE: "无法定位 $MFT 的物理布局，已改用句柄扫描",
    FALLBACK_STRUCTURE_INVALID: "MFT 结构不符合预期，已改用句柄扫描",
    FALLBACK_BOUNDS_VIOLATION: "原始读越界，已中止 MFT 通道并改用句柄扫描",
    FALLBACK_SAMPLE_MISMATCH: "MFT 解析可信度不足，已改用句柄扫描",
    FALLBACK_USER_PREFERRED_HANDLE: "已按用户选择使用句柄扫描",
}


def fallback_message(code: str, detail: str = "") -> str:
    """把回退原因码渲染成用户可读文案（**降级必须留痕**）。"""
    base = FALLBACK_MESSAGES.get(code, f"已回退句柄扫描（原因码 {code or '未知'}）")
    return f"{base}：{detail}" if detail else base
