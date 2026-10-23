"""MFT 测试夹具：构造合法的 FILE 记录 / 假卷字节流 / 打桩 Win32 读写。

为什么自己造字节而不是读真盘：

* 真盘上的记录不可能是"故意撕裂的 fixup"或"vcn 空洞"；
* 造字节可以精确覆盖设计 §4.6.5 里每一种降级分支；
* 单测必须可脱离 QApplication / 脱离管理员权限运行。

只用标准库 + ``app`` 内部模块，不引入新依赖。
"""

from __future__ import annotations

import struct
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterable, Iterator, List, Optional, Sequence, Tuple

from app.core.mft import (
    ATTR_ATTRIBUTE_LIST,
    ATTR_DATA,
    ATTR_END,
    ATTR_FILE_NAME,
    ATTR_STANDARD_INFORMATION,
    FLAG_DIRECTORY,
    FLAG_IN_USE,
    MftStream,
    ROOT_RECORD_NUMBER,
)
from app.core.models import Extent, VolumeInfo
from app.win32 import api

SECTOR = 512
BYTES_PER_CLUSTER = 4096
RECORD_BYTES = 1024


# --------------------------------------------------------------------------- #
# 字节编码工具
# --------------------------------------------------------------------------- #
def align(length: int, unit: int = 8) -> int:
    """向上对齐到 ``unit``。"""
    return (length + unit - 1) // unit * unit


def encode_runlist(runs: Sequence[Tuple[int, int]], length_bytes: int = 2, offset_bytes: int = 4) -> bytes:
    """把 ``[(lcn 或 -1, clusters)]`` 编成 runlist（偏移用有符号增量）。

    Args:
        runs: ``lcn == -1`` 表示稀疏段。
        length_bytes: 长度字段字节数（2 足够测试；0 用于构造"非法头"）。
        offset_bytes: 偏移字段字节数（4 便于放负增量；0 表示稀疏段）。
    """
    out = bytearray()
    previous = 0
    for lcn, clusters in runs:
        if lcn < 0:
            if offset_bytes == 0:
                header = 0x00 | (length_bytes & 0x0F)
                out.append(header)
                out += int(clusters).to_bytes(max(1, length_bytes), "little")
                continue
            header = (0 << 4) | (length_bytes & 0x0F)
            out.append(header)
            out += int(clusters).to_bytes(max(1, length_bytes), "little")
            continue
        delta = int(lcn) - previous
        previous = int(lcn)
        header = ((offset_bytes & 0x0F) << 4) | (length_bytes & 0x0F)
        out.append(header)
        out += int(clusters).to_bytes(max(1, length_bytes), "little")
        out += delta.to_bytes(max(1, offset_bytes), "little", signed=True)
    out.append(0)
    return bytes(out)


def resident_attr(attr_type: int, value: bytes, flags: int = 0, name: bytes = b"") -> bytes:
    """构造驻留属性（``name`` 为 UTF-16 字节，非空则是命名流）。"""
    header = 0x18
    name_offset = header if name else 0
    value_offset = header + len(name)
    total = align(value_offset + len(value))
    body = bytearray(total)
    struct.pack_into("<II", body, 0x00, attr_type, total)
    body[0x08] = 0
    body[0x09] = len(name) // 2
    struct.pack_into("<H", body, 0x0A, name_offset)
    struct.pack_into("<H", body, 0x0C, flags)
    struct.pack_into("<H", body, 0x0E, 1)
    struct.pack_into("<I", body, 0x10, len(value))
    struct.pack_into("<H", body, 0x14, value_offset)
    if name:
        body[header : header + len(name)] = name
    body[value_offset : value_offset + len(value)] = value
    return bytes(body)


def non_resident_attr(
    attr_type: int,
    runlist: bytes,
    data_size: int,
    flags: int = 0,
    start_vcn: int = 0,
    clusters: int = 0,
    name: bytes = b"",
) -> bytes:
    """构造非常驻属性（``runlist`` 已编码，``clusters`` 用于算 ``last_vcn``）。"""
    header = 0x40
    name_offset = header if name else 0
    run_offset = header + len(name)
    total = align(run_offset + len(runlist))
    body = bytearray(total)
    struct.pack_into("<II", body, 0x00, attr_type, total)
    body[0x08] = 1
    body[0x09] = len(name) // 2
    struct.pack_into("<H", body, 0x0A, name_offset)
    struct.pack_into("<H", body, 0x0C, flags)
    struct.pack_into("<H", body, 0x0E, 1)
    struct.pack_into("<qq", body, 0x10, start_vcn, start_vcn + max(0, clusters - 1))
    struct.pack_into("<H", body, 0x20, run_offset)
    struct.pack_into("<H", body, 0x22, 0)
    struct.pack_into("<qqq", body, 0x28, data_size, data_size, data_size)
    if name:
        body[header : header + len(name)] = name
    body[run_offset : run_offset + len(runlist)] = runlist
    return bytes(body)


def file_name_value(parent: int, name: str, namespace: int = 1) -> bytes:
    """构造 ``$FILE_NAME`` 属性值（``parent`` 为记录号，命名空间 1=Win32）。"""
    value = bytearray(0x42)
    struct.pack_into("<q", value, 0x00, parent)
    struct.pack_into("<q", value, 0x28, 0)          # alloc size
    struct.pack_into("<q", value, 0x30, 0)          # real size
    value[0x40] = len(name)
    value[0x41] = namespace
    return bytes(value) + name.encode("utf-16-le")


def std_info_value(attributes: int = 0) -> bytes:
    """构造 ``$STANDARD_INFORMATION`` 值（``FILE_ATTRIBUTE_*`` 在 0x20）。"""
    value = bytearray(0x48)
    struct.pack_into("<I", value, 0x20, attributes)
    return bytes(value)


# --------------------------------------------------------------------------- #
# 记录构造
# --------------------------------------------------------------------------- #
def build_record(
    number: int,
    name: str = "test.bin",
    parent: int = ROOT_RECORD_NUMBER,
    runs: Sequence[Tuple[int, int]] = ((100, 10),),
    attributes: int = 0,
    flags: int = FLAG_IN_USE,
    data_flags: int = 0,
    record_bytes: int = RECORD_BYTES,
    sector: int = SECTOR,
    resident_data: Optional[bytes] = None,
    base_ref: int = 0,
    attribute_list: bool = False,
    ads_name: str = "",
    extra_data_attrs: Sequence[bytes] = (),
    name_namespace: int = 1,
    data_size: int = 0,
    garbage_signature: bool = False,
) -> bytes:
    """构造一条合法（或按需求"非法"）的 FILE 记录，含真实 USA fixup。

    Args:
        number: 记录号（不写进记录，仅用于调用方对齐）。
        runs: 匿名 ``$DATA`` 的 runlist（``lcn=-1`` = 稀疏）。
        resident_data: 传了就构造**常驻** ``$DATA``。
        extra_data_attrs: 手工构造的额外属性（用于 vcn 空洞 / 分片 $DATA）。
        garbage_signature: 故意写坏签名（测试 ``bad_magic`` 计数）。
    """
    clusters_total = sum(c for _l, c in runs)
    size = data_size or clusters_total * BYTES_PER_CLUSTER

    blobs: List[bytes] = [
        resident_attr(ATTR_STANDARD_INFORMATION, std_info_value(attributes)),
        resident_attr(ATTR_FILE_NAME, file_name_value(parent, name, name_namespace)),
    ]
    if attribute_list:
        blobs.append(resident_attr(ATTR_ATTRIBUTE_LIST, b"\x10\x00" * 8))
    if resident_data is not None:
        blobs.append(resident_attr(ATTR_DATA, resident_data))
    elif extra_data_attrs:
        blobs.extend(extra_data_attrs)
    else:
        blobs.append(
            non_resident_attr(
                ATTR_DATA,
                encode_runlist(runs),
                data_size=size,
                flags=data_flags,
                clusters=clusters_total,
            )
        )
    if ads_name:
        blobs.append(
            non_resident_attr(
                ATTR_DATA,
                encode_runlist(((900, 4),)),
                data_size=4 * BYTES_PER_CLUSTER,
                clusters=4,
                name=ads_name.encode("utf-16-le"),
            )
        )
    blobs.append(struct.pack("<II", ATTR_END, 0))
    attr_blob = b"".join(blobs)

    record = bytearray(record_bytes)
    record[0:4] = b"XXXX" if garbage_signature else b"FILE"
    usa_offset = 0x30
    usa_count = record_bytes // sector + 1
    struct.pack_into("<HH", record, 0x04, usa_offset, usa_count)
    struct.pack_into("<H", record, 0x10, 1)                 # sequence
    struct.pack_into("<H", record, 0x12, 1)                 # link count
    attr_offset = align(usa_offset + usa_count * 2, 8)
    struct.pack_into("<H", record, 0x14, attr_offset)
    struct.pack_into("<H", record, 0x16, flags)
    struct.pack_into("<I", record, 0x18, attr_offset + len(attr_blob))
    struct.pack_into("<I", record, 0x1C, record_bytes)
    struct.pack_into("<q", record, 0x20, base_ref)
    record[attr_offset : attr_offset + len(attr_blob)] = attr_blob

    # 应用 fixup：每扇区尾 2 字节替换为 USN，原值存进 USA
    usn = 0x0001
    struct.pack_into("<H", record, usa_offset, usn)
    for index in range(1, usa_count):
        position = index * sector - 2
        original = struct.unpack_from("<H", record, position)[0]
        struct.pack_into("<H", record, usa_offset + index * 2, original)
        struct.pack_into("<H", record, position, usn)
    return bytes(record)


def directory_record(
    number: int, name: str, parent: int = ROOT_RECORD_NUMBER, **kwargs: object
) -> bytes:
    """构造目录记录（``flags = IN_USE | DIRECTORY``，无 ``$DATA``）。"""
    return build_record(
        number,
        name=name,
        parent=parent,
        runs=(),
        flags=FLAG_IN_USE | FLAG_DIRECTORY,
        resident_data=b"",
        **kwargs,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# 假卷
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class FakeVolume:
    """一块内存"磁盘" + 与之匹配的 ``VolumeInfo`` / ``MftStream``。"""

    info: VolumeInfo
    disk: bytearray
    stream: MftStream
    #: 供抽样复核用的"句柄通道结果"：path -> extents
    handle_extents: dict = field(default_factory=dict)
    #: 记录 ``api.open_handle`` 收到的 (path, access)
    open_calls: List[Tuple[str, int]] = field(default_factory=list)

    def write_records(self, records: Iterable[bytes], start_lcn: int = 10) -> None:
        """把记录按顺序写进 ``start_lcn`` 起的物理位置。"""
        offset = start_lcn * BYTES_PER_CLUSTER
        for record in records:
            self.disk[offset : offset + len(record)] = record
            offset += len(record)


def make_volume(
    records: Sequence[bytes],
    *,
    start_lcn: int = 10,
    total_clusters: int = 4096,
    record_bytes: int = RECORD_BYTES,
    bytes_per_cluster: int = BYTES_PER_CLUSTER,
    bytes_per_sector: int = SECTOR,
    letter: str = "R",
    mft_valid_bytes: Optional[int] = None,
) -> FakeVolume:
    """按记录序列造一个假卷（``$MFT`` 物理连续，放在 ``start_lcn``）。"""
    volume_bytes = total_clusters * bytes_per_cluster
    disk = bytearray(volume_bytes)
    needed = len(records) * record_bytes
    clusters = (needed + bytes_per_cluster - 1) // bytes_per_cluster
    stream = MftStream(
        extents=[Extent(vcn=0, lcn=start_lcn, clusters=clusters)],
        valid_length=mft_valid_bytes if mft_valid_bytes is not None else needed,
        record_size=record_bytes,
        bytes_per_cluster=bytes_per_cluster,
        bytes_per_sector=bytes_per_sector,
    )
    info = VolumeInfo(
        letter=letter,
        root=f"{letter}:\\",
        label="TEST",
        filesystem="NTFS",
        total_bytes=volume_bytes,
        free_bytes=volume_bytes // 2,
        bytes_per_cluster=bytes_per_cluster,
        total_clusters=total_clusters,
        free_clusters=total_clusters // 2,
        mft_zone_start=start_lcn,
        mft_zone_end=start_lcn + clusters,
        mft_start_lcn=start_lcn,
        mft_valid_data_length=stream.valid_length,
        serial=0x1234ABCD,
        is_ntfs=True,
        bytes_per_file_record=record_bytes,
        bytes_per_sector=bytes_per_sector,
        number_sectors=total_clusters * (bytes_per_cluster // bytes_per_sector),
    )
    volume = FakeVolume(info=info, disk=disk, stream=stream)
    volume.write_records(records, start_lcn=start_lcn)
    return volume


@contextmanager
def patched_io(volume: FakeVolume) -> Iterator[FakeVolume]:
    """把 ``api.read_at`` / ``api.open_handle`` / ``api.close_handle`` 换成内存实现。

    用途：让 :class:`app.win32.mft_reader.MftReader` 与 ``scan_volume_mft`` 能在
    完全没有真实盘的情况下被测（**不会真的读盘，更不会写**）。
    """
    original = (api.read_at, api.open_handle, api.close_handle)

    def fake_open_handle(
        path: str,
        access: int,
        share: int = 0,
        flags: int = 0,
        context: str = "",
    ) -> int:
        volume.open_calls.append((path, int(access)))
        return 0x5EED

    def fake_read_at(handle: int, offset: int, length: int, context: str = "") -> bytes:
        if offset < 0 or offset + length > len(volume.disk):
            raise api.Win32Error(api.ERROR_INVALID_PARAMETER, "读越界（假卷）")
        return bytes(volume.disk[offset : offset + length])

    api.read_at = fake_read_at            # type: ignore[assignment]
    api.open_handle = fake_open_handle    # type: ignore[assignment]
    api.close_handle = lambda handle: None  # type: ignore[assignment]
    try:
        yield volume
    finally:
        api.read_at, api.open_handle, api.close_handle = original  # type: ignore[assignment]
