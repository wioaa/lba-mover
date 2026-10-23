"""介质探测与卷-磁盘映射（**严格只读**，不含任何写操作）。

两个能力：

1. :func:`probe_media` —— 通过 ``IOCTL_STORAGE_QUERY_PROPERTY`` 判断 HDD / SSD；
   失败按「SeekPenalty → BusType → 保守 unknown」三级回退，**永不抛异常**。
2. :func:`volume_disk_extents` —— 通过 ``IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS``
   读出卷在物理磁盘上的起始偏移。这是「分区表在卷之外、物理不可达」这一安全
   论证的**实证证据**（``StartingOffset > 0`` 即分区表在卷之前）。

本模块不 import ``mover``，也不含 ``FSCTL_MOVE_FILE`` —— 预览模式可安全调用。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import List, Optional

from app.core.models import MEDIA_HDD, MEDIA_SSD, MEDIA_UNKNOWN
from app.win32 import api

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
StorageDeviceProperty = 0
#: ``STORAGE_PROPERTY_ID.StorageDeviceSeekPenaltyProperty``。
#:
#: v2 第一批曾误写成 ``4``（并因此读到了 ``DEVICE_WRITECACHE_DESCRIPTOR``），
#: 已按实测证据纠正为 **7**。证据见 ``docs/seek-penalty-evidence.md``：
#:
#: * ``prop=4`` 返回 28 字节 ``1c 00 00 00 1c 00 00 00 02 …``（写缓存描述符）；
#: * ``prop=6`` 返回 28 字节，且 ``BytesPerLogicalSector=512`` /
#:   ``BytesPerPhysicalSector=4096`` —— 正是 ``STORAGE_ACCESS_ALIGNMENT_DESCRIPTOR``；
#: * ``prop=7`` 返回 12 字节，第 8 字节（bool）在所有卷上与
#:   ``Get-PhysicalDisk`` 的 ``MediaType`` **6/6 一致**
#:   （C:/G: NVMe SSD → 0，D:/E:/F:/I: HDD → 1）。
StorageDeviceSeekPenaltyProperty = 7
PropertyStandardQuery = 0

BusTypeUsb = 7
BusTypeAta = 3
BusTypeSata = 11
BusTypeSd = 12
BusTypeNvme = 17

#: STORAGE_PROPERTY_QUERY 声明长度（前 8 字节有语义，第 3 个 DWORD 覆盖
#: ``AdditionalParameters[1]`` 的对齐补齐；只发 8 字节会被部分驱动拒绝）
_QUERY_SIZE = 12

#: 查询 seek penalty 时申请的输出缓冲区。**必须给足**：若按"期望长度"申请 8 字节，
#: 驱动只会回 8 字节，``Size`` 字段看起来"吻合"，反而掩盖真实布局。
#: 实测该描述符在本机为 **12 字节**，故此处置 64 以留出余量。
_SEEK_PROBE_SIZE = 64

#: ``DEVICE_SEEK_PENALTY_DESCRIPTOR`` 的两种**真实**布局（只承认这两种）：
#:
#: * 紧凑布局 ``{Version(u32), IncursSeekPenalty(BOOLEAN)}`` + 补齐 = **8** 字节，
#:   布尔在 offset 4（微软头文件声明的形态）；
#: * 带头布局 ``{Version(u32), Size(u32), IncursSeekPenalty(BOOLEAN)}`` + 补齐
#:   = **12** 字节，布尔在 offset 8（本机 Windows 驱动的实际返回）。
#:
#: ★ **长度必须卡死为 8 或 12**（P0-1 复验第 3 轮）。
#: 早期实现写成 ``size >= 12`` 无上界，于是 28 字节的
#: ``STORAGE_ACCESS_ALIGNMENT_DESCRIPTOR``（prop=6）、写缓存描述符（prop=4）、
#: 未知 24 字节结构、驱动没填的全零缓冲**全部**落到 offset 8 读到 0，
#: 被判成"确定的 SSD"。那等于把 HDD 加速反向关掉 —— 与"全判 HDD"同类错误。
#: 判定顺序固定在 :func:`decode_seek_penalty` 里，不存在第二种入口。
_SEEK_DESC_COMPACT = 8
_SEEK_DESC_WITH_HEADER = 12

#: STORAGE_DEVICE_DESCRIPTOR 至少要有到 BusType(@28) 的 4 个字节
_DEVICE_DESC_MIN = 32
_DEVICE_DESC_SIZE = 1024

#: VOLUME_DISK_EXTENTS：8 字节头 + 每项 24 字节
_DISK_EXTENTS_HEADER = 8
_DISK_EXTENT_SIZE = 24


@dataclass(slots=True)
class MediaProbe:
    """介质探测结果。"""

    media: str = MEDIA_UNKNOWN
    source: str = "fallback"          # seek_penalty | bus_type | fallback
    bus_type: int = -1
    incurs_seek_penalty: Optional[bool] = None
    note: str = ""

    @property
    def is_known(self) -> bool:
        return self.media in (MEDIA_HDD, MEDIA_SSD)

    def describe(self) -> str:
        """一行中文说明。"""
        if self.source == "seek_penalty":
            kind = "HDD" if self.media == MEDIA_HDD else "SSD"
            flag = "有" if self.incurs_seek_penalty else "无"
            return f"介质 {kind}（驱动报告{flag}寻道惩罚）"
        if self.source == "bus_type":
            return f"介质 {self.media.upper()}（依据总线类型 {self.bus_type}）"
        return self.note or "无法识别介质：已启用与介质无关的安全策略子集"


@dataclass(slots=True)
class DiskExtent:
    """卷在物理磁盘上的一段映射。"""

    disk_number: int = 0
    starting_offset: int = 0
    extent_length: int = 0


@dataclass(slots=True)
class VolumeDiskMap:
    """卷的物理磁盘映射（可能跨盘，故为列表）。"""

    extents: List[DiskExtent] = field(default_factory=list)
    error: str = ""

    @property
    def first_offset(self) -> int:
        """卷在磁盘上的最小起始偏移；取不到返回 -1。"""
        if not self.extents:
            return -1
        return min(e.starting_offset for e in self.extents)

    @property
    def is_contiguous(self) -> bool:
        return len(self.extents) == 1


# --------------------------------------------------------------------------- #
# 卷设备句柄
# --------------------------------------------------------------------------- #
def open_volume_device(letter: str, context: str = "打开卷设备") -> int:
    """打开卷设备句柄。

    先按 ``GENERIC_READ`` 打开（能读写卷即最通用）；被拒（``ERROR_ACCESS_DENIED``，
    非提权访问固定磁盘的典型表现）时退回 **``access=0``** 再试一次 ——
    ``IOCTL_STORAGE_QUERY_PROPERTY`` / ``IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS``
    都是 ``FILE_ANY_ACCESS``，零访问权限的句柄足以查询。

    这一步很重要：否则非提权状态下 C:/D:/E: 之类固定磁盘全都"介质未知"，
    UI 里的介质徽章会长期停在「未知（保守模式）」，HDD 加速永远不可能生效。

    Raises:
        api.Win32Error: 两种访问权限都打不开。
    """
    cleaned = letter.upper().rstrip(":")
    path = f"\\\\.\\{cleaned}:"
    share = api.FILE_SHARE_READ | api.FILE_SHARE_WRITE
    try:
        return api.open_handle(
            path, api.GENERIC_READ, share=share, flags=0, context=context
        )
    except api.Win32Error as exc:
        if exc.winerror != api.ERROR_ACCESS_DENIED:
            raise
    # 零访问权限只查属性，不读一个字节
    return api.open_handle(path, 0, share=share, flags=0, context=f"{context}（只查询）")


# --------------------------------------------------------------------------- #
# 介质探测
# --------------------------------------------------------------------------- #
def decode_seek_penalty(raw: bytes) -> Optional[bool]:
    """从 ``DEVICE_SEEK_PENALTY_DESCRIPTOR`` 原始字节解出是否 incur seek penalty。

    纯函数，不碰任何句柄，便于单测覆盖两种布局。

    判定规则（**只承认两种真实布局，其余一律返回 ``None``**）：

    * ``len(raw) == 8`` → 紧凑布局，布尔在 **offset 4**；
    * ``len(raw) == 12`` → 带头布局，布尔在 **offset 8**，
      且要求 ``Size``（offset 4）本身等于 12、``Version``（offset 0）非 0 ——
      这两条把「其它属性的描述符」和「驱动没填的全零缓冲」挡在门外；
    * 其它长度（28 / 24 / 64 …）→ ``None``。

    Args:
        raw: ``IOCTL_STORAGE_QUERY_PROPERTY``（``prop=7``）的输出字节。

    Returns:
        ``True`` 表示旋转介质（HDD），``False`` 表示无寻道惩罚（SSD/闪存），
        结构不符合预期时返回 ``None``（调用方继续降级，**绝不猜**）。

    Note:
        返回 ``False`` 是"确定是 SSD"这种强结论，因此必须建立在"确认这是
        seek-penalty 描述符"之上。宁可返回 ``None``（保守模式），也不能把
        一块 HDD 判成 SSD —— 后者会让它静默失去全部加速。
    """
    length = len(raw)
    if length <= 0:
        return None

    if length == _SEEK_DESC_COMPACT:
        offset = 4
    elif length == _SEEK_DESC_WITH_HEADER:
        version = struct.unpack_from("<I", raw, 0)[0]
        size = struct.unpack_from("<I", raw, 4)[0]
        # 带头布局的自证：Size 必须等于 12（本机实测恒成立），Version 不能是 0
        if size != _SEEK_DESC_WITH_HEADER or version <= 0:
            return None
        offset = 8
    else:
        # 28 / 24 / 64 …：不是 seek-penalty 描述符，不许硬套偏移
        return None

    flag = raw[offset]
    if flag not in (0, 1):
        # BOOLEAN 只可能是 0/1，其它值说明布局判断错了
        return None
    return flag == 1


def _query_seek_penalty(handle: int) -> Optional[bool]:
    """返回 ``True/False``（是否 incur seek penalty）；不支持时返回 ``None``。"""
    in_bytes = struct.pack(
        "<III", StorageDeviceSeekPenaltyProperty, PropertyStandardQuery, 0
    )
    ok, raw, _returned, _error = api.device_io_control_raw(
        handle,
        api.IOCTL_STORAGE_QUERY_PROPERTY,
        in_bytes=in_bytes,
        out_size=_SEEK_PROBE_SIZE,
        context="SeekPenalty 查询",
    )
    if not ok or len(raw) < _SEEK_DESC_COMPACT:
        return None
    return decode_seek_penalty(raw)


def _query_bus_type(handle: int) -> int:
    """返回 ``BusType``；取不到返回 -1。"""
    in_bytes = struct.pack("<III", StorageDeviceProperty, PropertyStandardQuery, 0)
    ok, raw, _returned, _error = api.device_io_control_raw(
        handle,
        api.IOCTL_STORAGE_QUERY_PROPERTY,
        in_bytes=in_bytes,
        out_size=_DEVICE_DESC_SIZE,
        context="DeviceProperty 查询",
    )
    if not ok or len(raw) < _DEVICE_DESC_MIN:
        return -1
    return struct.unpack_from("<I", raw, 28)[0]


def probe_media(letter: str) -> MediaProbe:
    """探测某个卷所在介质的类型（HDD / SSD / unknown）。

    按「SeekPenalty → BusType → 保守 unknown」三级回退，**任何失败都不抛异常**。

    Args:
        letter: 盘符（``"C"`` 或 ``"C:"``）。

    Returns:
        :class:`MediaProbe`；``source`` 标明结论来源，便于 UI 如实展示。
    """
    cleaned = letter.upper().rstrip(":")
    handle: Optional[int] = None
    try:
        handle = open_volume_device(cleaned, context="打开卷设备（介质探测）")
    except api.Win32Error as exc:
        return MediaProbe(
            note=f"无法打开卷设备（{exc.winerror}: {api.format_error(exc.winerror)}），介质未知",
        )

    try:
        incurs = _query_seek_penalty(handle)
        if incurs is not None:
            return MediaProbe(
                media=MEDIA_HDD if incurs else MEDIA_SSD,
                source="seek_penalty",
                incurs_seek_penalty=incurs,
            )

        bus_type = _query_bus_type(handle)
        if bus_type >= 0:
            if bus_type in (BusTypeNvme, BusTypeSd):
                return MediaProbe(media=MEDIA_SSD, source="bus_type", bus_type=bus_type)
            if bus_type in (BusTypeAta, BusTypeSata, BusTypeUsb):
                return MediaProbe(
                    media=MEDIA_UNKNOWN,
                    source="bus_type",
                    bus_type=bus_type,
                    note=f"总线类型 {bus_type} 无法区分 SSD/HDD（USB 桥接或 SATA 均可能），按未知处理",
                )
            return MediaProbe(
                media=MEDIA_UNKNOWN,
                source="bus_type",
                bus_type=bus_type,
                note=f"未识别的总线类型 {bus_type}，按未知处理",
            )
        return MediaProbe(note="驱动未实现 SeekPenalty / DeviceProperty 查询，介质未知")
    finally:
        api.close_handle(handle)


# --------------------------------------------------------------------------- #
# 卷 → 物理磁盘映射
# --------------------------------------------------------------------------- #
def volume_disk_extents(letter: str) -> VolumeDiskMap:
    """读取卷在物理磁盘上的映射（用于安全论证 + 跨盘识别）。

    失败时返回空列表并带 ``error``，**不抛异常**。
    """
    cleaned = letter.upper().rstrip(":")
    handle: Optional[int] = None
    try:
        handle = open_volume_device(cleaned, context="打开卷设备（磁盘映射）")
    except api.Win32Error as exc:
        return VolumeDiskMap(
            error=f"无法打开卷设备（{exc.winerror}: {api.format_error(exc.winerror)}）"
        )

    try:
        ok, raw, _returned, error = api.device_io_control_raw(
            handle,
            api.IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS,
            in_bytes=b"",
            out_size=_DISK_EXTENTS_HEADER + _DISK_EXTENT_SIZE * 8,
            context="读取卷磁盘映射",
        )
        if not ok or len(raw) < _DISK_EXTENTS_HEADER:
            return VolumeDiskMap(
                error=f"IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS 失败（{error}）"
            )
        count = struct.unpack_from("<I", raw, 0)[0]
        available = (len(raw) - _DISK_EXTENTS_HEADER) // _DISK_EXTENT_SIZE
        count = min(int(count), available)
        extents: List[DiskExtent] = []
        for index in range(count):
            offset = _DISK_EXTENTS_HEADER + index * _DISK_EXTENT_SIZE
            disk_number = struct.unpack_from("<I", raw, offset)[0]
            starting_offset, extent_length = struct.unpack_from("<qq", raw, offset + 8)
            extents.append(
                DiskExtent(
                    disk_number=int(disk_number),
                    starting_offset=int(starting_offset),
                    extent_length=int(extent_length),
                )
            )
        return VolumeDiskMap(extents=extents)
    finally:
        api.close_handle(handle)


def probe_volume_storage(letter: str) -> tuple:
    """一次性探测介质 + 磁盘映射（UI 打开卷时调用）。

    Returns:
        ``(MediaProbe, VolumeDiskMap)``，两者都不会抛异常。
    """
    return probe_media(letter), volume_disk_extents(letter)
