"""合成一个结构合法的 NTFS 镜像，用于测试与演练。

为什么需要它：解析器写完之后，能不能对着**真实 NTFS 结构**跑通，是两件
不同的事。没有管理员权限就读不到真盘的 `$MFT`，而"看代码觉得对"不足以
支撑"这个工具能用"的结论。于是这里按 NTFS 规范手写一份最小但合法的卷：

* 引导扇区（真实 BPB + NTFS 扩展字段）
* $MFT 的 16 条元文件记录 + 若干用户文件记录（带 fixup）
* 非驻留 DATA 属性带真实 runlist（故意做出碎片）
* $Bitmap 标明真实占用，供自由空间计算使用

它**不是**一个能被 Windows 挂载的卷（缺 $LogFile/$Secure/$UpCase 的完整
内容、缺索引树），只保证本项目的解析器能按规范读出正确结果。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from ..contract.models import Extent
from .boot import NtfsBoot
from .record import ATTR, NAMESPACE_WIN32_DOS
from .runlist import encode_runs

__all__ = ["SynthNtfs", "build_ntfs_image", "FILE_PATTERN"]

SECTOR = 512
CLUSTER = 4096
RECORD = 1024

#: 用于填充文件数据的可校验图案（前 8 字节写簇号）
FILE_PATTERN = b"LBAMOVER-NTFS\x00\x00"

_USER_SPEC = [
    ("alpha.bin", (Extent(120, 2), Extent(200, 3))),
    ("beta.bin", (Extent(300, 4),)),
    ("gamma.log", (Extent(360, 1), Extent(420, 1), Extent(480, 1))),
    ("delta.dat", (Extent(520, 6),)),
    ("epsilon.tmp", (Extent(600, 2), Extent(700, 2))),
]


@dataclass(frozen=True, slots=True)
class SynthNtfs:
    boot: NtfsBoot
    #: 文件名 → 簇区间（构造时写入的原始布局）
    layout: tuple[tuple[str, tuple[Extent, ...]], ...]
    mft_lcn: int
    bitmap_lcn: int
    cluster_count: int
    mft_clusters: int = 16
    bitmap_clusters: int = 1

    @property
    def cluster_size(self) -> int:
        return self.boot.cluster_size

    def expected_extents(self, name: str) -> tuple[Extent, ...]:
        for n, extents in self.layout:
            if n == name:
                return extents
        raise KeyError(name)

    @property
    def used_clusters(self) -> int:
        used = {0}
        used.update(range(self.mft_lcn, self.mft_lcn + self.mft_clusters))
        used.update(range(self.bitmap_lcn, self.bitmap_lcn + self.bitmap_clusters))
        for _, extents in self.layout:
            for e in extents:
                used.update(range(e.start, e.end))
        return len(used)


def _align8(n: int) -> int:
    return (n + 7) & ~7


def _resident(attr_type: int, value: bytes, name: str = "", flags: int = 0) -> bytes:
    name_bytes = name.encode("utf-16-le")
    name_off = 0x18 if name_bytes else 0
    value_off = _align8(0x18 + len(name_bytes))
    total = _align8(value_off + len(value))
    head = bytearray(total)
    struct.pack_into("<II", head, 0x00, attr_type, total)
    head[0x08] = 0  # resident
    head[0x09] = len(name)
    struct.pack_into("<H", head, 0x0A, name_off)
    struct.pack_into("<H", head, 0x0C, flags)
    struct.pack_into("<H", head, 0x0E, 0)  # attribute id
    struct.pack_into("<I", head, 0x10, len(value))
    struct.pack_into("<H", head, 0x14, value_off)
    if name_bytes:
        head[name_off : name_off + len(name_bytes)] = name_bytes
    head[value_off : value_off + len(value)] = value
    return bytes(head)


def _non_resident(attr_type: int, extents: list[Extent], real_size: int, flags: int = 0) -> bytes:
    runs = encode_runs(extents)
    head_len = _align8(0x40)
    total = _align8(head_len + len(runs))
    head = bytearray(total)
    struct.pack_into("<II", head, 0x00, attr_type, total)
    head[0x08] = 1  # non-resident
    head[0x09] = 0
    struct.pack_into("<H", head, 0x0A, 0)
    struct.pack_into("<H", head, 0x0C, flags)
    struct.pack_into("<H", head, 0x0E, 0)
    last_vcn = sum(e.length for e in extents) - 1
    struct.pack_into("<qq", head, 0x10, 0, max(0, last_vcn))
    struct.pack_into("<H", head, 0x20, head_len)
    struct.pack_into("<H", head, 0x22, 0)
    allocated = sum(e.length for e in extents) * CLUSTER
    struct.pack_into("<qqq", head, 0x28, allocated, real_size, real_size)
    head[head_len : head_len + len(runs)] = runs
    return bytes(head)


def _file_name_attr(parent: int, name: str, namespace: int = NAMESPACE_WIN32_DOS) -> bytes:
    encoded = name.encode("utf-16-le")
    value = bytearray(0x42 + len(encoded))
    struct.pack_into("<Q", value, 0x00, parent)
    # 4 个时间戳留 0（解析不依赖，且避免测试受时区影响）
    struct.pack_into("<I", value, 0x38, 0)  # flags
    value[0x40] = len(name)
    value[0x41] = namespace
    value[0x42 : 0x42 + len(encoded)] = encoded
    return _resident(ATTR.FILE_NAME, bytes(value))


def _standard_info(flags: int = 0) -> bytes:
    value = bytearray(0x48)
    struct.pack_into("<I", value, 0x20, flags)
    return _resident(ATTR.STANDARD_INFORMATION, bytes(value))


END_MARKER = struct.pack("<II", 0xFFFFFFFF, 0)


def _stamp_fixups(record: bytearray) -> None:
    """把 USA 写进记录：每个扇区末尾 2 字节换成 USN，原值存进 USA 数组。"""
    usa_ofs = 0x30
    usa_count = len(record) // SECTOR + 1
    usn = b"\x01\x00"
    struct.pack_into("<HH", record, 0x04, usa_ofs, usa_count)
    record[usa_ofs : usa_ofs + 2] = usn
    for i in range(1, usa_count):
        pos = i * SECTOR - 2
        record[usa_ofs + i * 2 : usa_ofs + i * 2 + 2] = record[pos : pos + 2]
        record[pos : pos + 2] = usn


def _record(
    number: int,
    *,
    parent: int | None,
    name: str | None,
    is_dir: bool = False,
    data_extents: list[Extent] | None = None,
    real_size: int = 0,
    resident_data: bytes | None = None,
    si_flags: int = 0,
    in_use: bool = True,
) -> bytes:
    body = bytearray()
    body += _standard_info(si_flags)
    if parent is not None and name is not None:
        body += _file_name_attr(parent, name)
    if data_extents is not None:
        body += _non_resident(ATTR.DATA, data_extents, real_size or sum(e.length for e in data_extents) * CLUSTER)
    elif resident_data is not None:
        body += _resident(ATTR.DATA, resident_data)
    body += END_MARKER

    attrs_offset = 0x38
    total = _align8(attrs_offset + len(body))
    if total > RECORD:
        raise ValueError(f"记录 {number} 超过 {RECORD} 字节（{total}），请加 record_slots 或减属性")
    # 必须先补齐到整条记录长度再打 fixup：USA 的条目数由**记录长度**决定，
    # 补齐前后 stamp 出来的 usa_count 不同，解析端会直接判定记录损坏。
    rec = bytearray(RECORD)
    rec[0:4] = b"FILE"
    struct.pack_into("<Q", rec, 0x08, 0)  # lsn
    struct.pack_into("<H", rec, 0x10, 1)  # sequence
    struct.pack_into("<H", rec, 0x12, 1)  # link count
    struct.pack_into("<H", rec, 0x14, attrs_offset)
    flags = 0x01 | (0x02 if is_dir else 0x00)
    struct.pack_into("<H", rec, 0x16, flags if in_use else 0)
    struct.pack_into("<I", rec, 0x18, total)   # used size
    struct.pack_into("<I", rec, 0x1C, RECORD)  # alloc size
    struct.pack_into("<Q", rec, 0x20, 0)       # base record
    struct.pack_into("<H", rec, 0x28, 4)       # next attr id
    rec[attrs_offset : attrs_offset + len(body)] = body
    struct.pack_into("<I", rec, 0x1C, RECORD)  # alloc size
    _stamp_fixups(rec)
    return bytes(rec)


def build_ntfs_image(
    device,
    *,
    total_clusters: int = 1024,
    mft_lcn: int = 4,
    record_slots: int = 64,
) -> SynthNtfs:
    """把一份最小 NTFS 结构写进 ``device``（block_size 必须等于簇大小）。"""
    if device.geometry.block_size != CLUSTER:
        raise ValueError(f"device.block_size 必须是 {CLUSTER}，当前 {device.geometry.block_size}")
    if total_clusters < 256:
        raise ValueError("total_clusters 太小，放不下元文件")

    mft_clusters = (record_slots * RECORD + CLUSTER - 1) // CLUSTER
    bitmap_lcn = mft_lcn + mft_clusters
    bitmap_clusters = (total_clusters + 8 * CLUSTER - 1) // (8 * CLUSTER)

    # ---- 引导扇区 ----
    boot = bytearray(SECTOR)
    boot[0:3] = b"\xEB\x52\x90"
    boot[3:11] = b"NTFS    "
    struct.pack_into("<H", boot, 0x0B, SECTOR)
    boot[0x0D] = CLUSTER // SECTOR
    struct.pack_into("<H", boot, 0x0E, 0)
    boot[0x15] = 0xF8
    struct.pack_into("<H", boot, 0x18, 63)
    struct.pack_into("<H", boot, 0x1A, 255)
    struct.pack_into("<q", boot, 0x28, total_clusters * (CLUSTER // SECTOR))
    struct.pack_into("<q", boot, 0x30, mft_lcn)
    struct.pack_into("<q", boot, 0x38, mft_lcn + mft_clusters)
    struct.pack_into("<b", boot, 0x40, -(RECORD.bit_length() - 1))  # 2^-n 编码
    struct.pack_into("<b", boot, 0x44, 1)
    struct.pack_into("<q", boot, 0x48, 0x1122334455667788)
    boot[510:512] = b"\x55\xAA"

    # ---- 占用位图 ----
    used = bytearray((total_clusters + 7) // 8)
    def mark(c: int) -> None:
        used[c >> 3] |= 1 << (c & 7)

    mark(0)
    for c in range(mft_lcn, mft_lcn + mft_clusters):
        mark(c)
    for c in range(bitmap_lcn, bitmap_lcn + bitmap_clusters):
        mark(c)

    for fname, extents in _USER_SPEC:
        for e in extents:
            if e.end > total_clusters:
                raise ValueError(
                    f"{fname} 的区间 {e} 超出 {total_clusters} 个簇，"
                    "请加大 total_clusters"
                )
    if mft_lcn + mft_clusters > total_clusters or bitmap_lcn + bitmap_clusters > total_clusters:
        raise ValueError("元文件区超出卷容量，请加大 total_clusters")

    layout: list[tuple[str, tuple[Extent, ...]]] = []
    for fname, extents in _USER_SPEC:
        for e in extents:
            for c in range(e.start, e.end):
                mark(c)
        layout.append((fname, tuple(extents)))

    # ---- MFT 记录 ----
    records: dict[int, bytes] = {}
    records[0] = _record(
        0, parent=None, name=None,
        data_extents=[Extent(mft_lcn, mft_clusters)],
        real_size=mft_clusters * CLUSTER,
    )
    records[1] = _record(1, parent=None, name=None, data_extents=[Extent(mft_lcn + 1, 1)])
    records[2] = _record(2, parent=None, name=None, data_extents=[Extent(mft_lcn + 2, 1)])
    records[3] = _record(3, parent=None, name=None, data_extents=[Extent(mft_lcn + 3, 1)])
    records[4] = _record(4, parent=None, name=None, data_extents=[Extent(mft_lcn + 4, 1)])
    records[5] = _record(5, parent=5, name="$Root", is_dir=True, data_extents=[])
    records[6] = _record(
        6, parent=None, name=None,
        data_extents=[Extent(bitmap_lcn, bitmap_clusters)],
        real_size=bitmap_clusters * CLUSTER,
    )
    records[7] = _record(7, parent=None, name=None, data_extents=[Extent(mft_lcn + 5, 1)])
    records[8] = _record(8, parent=None, name=None, data_extents=[Extent(mft_lcn + 6, 1)])
    records[9] = _record(9, parent=None, name=None, data_extents=[Extent(mft_lcn + 7, 1)])
    records[10] = _record(10, parent=None, name=None, data_extents=[Extent(mft_lcn + 8, 1)])

    number = 16
    file_numbers: dict[str, int] = {}
    for name, extents in _USER_SPEC:
        records[number] = _record(
            number,
            parent=5,
            name=name,
            data_extents=list(extents),
            real_size=sum(e.length for e in extents) * CLUSTER,
        )
        file_numbers[name] = number
        number += 1

    # 一个目录 + 子文件，验证路径还原（而不是只有一层）
    dir_number = number
    records[dir_number] = _record(dir_number, parent=5, name="data", is_dir=True, data_extents=[])
    number += 1
    records[number] = _record(
        number, parent=dir_number, name="nested.bin",
        data_extents=[Extent(800, 2)], real_size=2 * CLUSTER,
    )
    for c in (800, 801):
        mark(c)
    layout.append(("data\\nested.bin", (Extent(800, 2),)))
    number += 1

    # 一个常驻小文件 + 一个带系统属性的文件（后者应被"保护策略"拦下而非解析器）
    records[number] = _record(
        number, parent=5, name="tiny.txt", resident_data=b"hello lbamover"
    )
    number += 1
    records[number] = _record(
        number, parent=5, name="locked.sys", si_flags=0x0004,
        data_extents=[Extent(900, 1)], real_size=CLUSTER,
    )
    mark(900)
    layout.append(("locked.sys", (Extent(900, 1),)))
    number += 1

    # ---- 落盘 ----
    bs = device.geometry.block_size
    boot_padded = bytes(boot) + b"\x00" * (bs - len(boot))
    device.write_blocks(0, boot_padded)

    mft_buf = bytearray(mft_clusters * CLUSTER)
    for num, raw in records.items():
        off = num * RECORD
        if off + RECORD <= len(mft_buf):
            mft_buf[off : off + RECORD] = raw.ljust(RECORD, bytes(1))[:RECORD]
    # 簇 == 块（调用方已校验），所以 MFT 可以按块直写
    device.write_blocks(mft_lcn, mft_buf)

    bmp = bytearray(bitmap_clusters * CLUSTER)
    bmp[: len(used)] = used
    device.write_blocks(bitmap_lcn, bmp)

    # 给用户文件填可校验内容（按逻辑块顺序）
    for name, extents in layout:
        pattern = (FILE_PATTERN + name.encode("utf-8"))[:CLUSTER]
        pad = pattern + bytes(bs - len(pattern))
        for e in extents:
            for c in range(e.start, e.end):
                device.write_blocks(c, pad)
    device.flush()

    return SynthNtfs(
        boot=NtfsBoot.parse(bytes(boot)),
        layout=tuple(layout),
        mft_lcn=mft_lcn,
        bitmap_lcn=bitmap_lcn,
        cluster_count=total_clusters,
        mft_clusters=mft_clusters,
        bitmap_clusters=bitmap_clusters,
    )
