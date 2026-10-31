"""NTFS 引导扇区（BPB + NTFS 扩展字段）。

这是打开一个 NTFS 卷后第一件要读的东西：不知道簇大小就不知道 LCN 的
物理位置，不知道 $MFT 的起始 LCN 就找不到文件表。

所有偏移都按 Windows 的 NTFS 文档写死，不做"猜测式兼容"——猜错的后果是
从错误的扇区读文件表，然后把它当文件解析，最后得到一堆看起来正常的垃圾。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

__all__ = ["NtfsBoot", "NotNtfsError"]

_OEM_OFFSET = 3
_OEM_MAGIC = b"NTFS    "


class NotNtfsError(ValueError):
    """引导扇区不是 NTFS。"""


@dataclass(frozen=True, slots=True)
class NtfsBoot:
    """NTFS 卷的关键几何参数。"""

    bytes_per_sector: int
    sectors_per_cluster: int
    total_sectors: int
    mft_lcn: int
    mftmirr_lcn: int
    clusters_per_record: int
    record_size: int
    clusters_per_index: int
    index_buffer_size: int
    serial: int

    # -- 派生 -------------------------------------------------------------

    @property
    def cluster_size(self) -> int:
        return self.bytes_per_sector * self.sectors_per_cluster

    @property
    def cluster_count(self) -> int:
        return self.total_sectors // self.sectors_per_cluster

    @property
    def total_bytes(self) -> int:
        return self.total_sectors * self.bytes_per_sector

    # -- 解析 -------------------------------------------------------------

    @classmethod
    def parse(cls, sector: bytes) -> "NtfsBoot":
        if len(sector) < 512:
            raise NotNtfsError(f"引导扇区太短：{len(sector)} 字节")

        if sector[_OEM_OFFSET : _OEM_OFFSET + 8] != _OEM_MAGIC:
            raise NotNtfsError("引导扇区没有 NTFS 标记（可能是 FAT/exFAT 或空盘）")

        bytes_per_sector = struct.unpack_from("<H", sector, 0x0B)[0]
        sectors_per_cluster = sector[0x0D]
        total_sectors = struct.unpack_from("<q", sector, 0x28)[0]
        mft_lcn = struct.unpack_from("<q", sector, 0x30)[0]
        mftmirr_lcn = struct.unpack_from("<q", sector, 0x38)[0]
        cpr = struct.unpack_from("<b", sector, 0x40)[0]
        cpi = struct.unpack_from("<b", sector, 0x44)[0]
        serial = struct.unpack_from("<q", sector, 0x48)[0]

        if bytes_per_sector not in (512, 1024, 2048, 4096):
            raise NotNtfsError(f"非法扇区大小：{bytes_per_sector}")
        if sectors_per_cluster == 0 or (sectors_per_cluster & (sectors_per_cluster - 1)):
            raise NotNtfsError(f"非法簇扇区数：{sectors_per_cluster}（必须是 2 的幂）")
        if total_sectors <= 0:
            raise NotNtfsError(f"非法总扇区数：{total_sectors}")
        if mft_lcn <= 0 or mft_lcn >= total_sectors:
            raise NotNtfsError(f"$MFT 起始 LCN 越界：{mft_lcn}")

        # 记录大小：正数表示"几个簇"，负数表示"2^-n 字节"。两种都要认，
        # 因为格式化时 /A: 和 /C: 参数会分别产生这两种编码。
        cluster_size = bytes_per_sector * sectors_per_cluster
        if cpr > 0:
            record_size = cpr * cluster_size
            clusters_per_record = cpr
        elif cpr < 0:
            record_size = 1 << (-cpr)
            clusters_per_record = max(1, record_size // cluster_size)
        else:
            raise NotNtfsError("记录大小编码非法（0）")
        if record_size < 256 or record_size & (record_size - 1):
            raise NotNtfsError(f"记录大小必须是 2 的幂且不小于 256：{record_size}")

        if cpi > 0:
            index_buffer_size = cpi * cluster_size
            clusters_per_index = cpi
        elif cpi < 0:
            index_buffer_size = 1 << (-cpi)
            clusters_per_index = max(1, index_buffer_size // cluster_size)
        else:
            index_buffer_size = cluster_size
            clusters_per_index = 1

        return cls(
            bytes_per_sector=bytes_per_sector,
            sectors_per_cluster=sectors_per_cluster,
            total_sectors=int(total_sectors),
            mft_lcn=int(mft_lcn),
            mftmirr_lcn=int(mftmirr_lcn),
            clusters_per_record=clusters_per_record,
            record_size=record_size,
            clusters_per_index=clusters_per_index,
            index_buffer_size=index_buffer_size,
            serial=int(serial),
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "bytes_per_sector": self.bytes_per_sector,
            "sectors_per_cluster": self.sectors_per_cluster,
            "cluster_size": self.cluster_size,
            "cluster_count": self.cluster_count,
            "record_size": self.record_size,
            "mft_lcn": self.mft_lcn,
        }

    def __str__(self) -> str:  # pragma: no cover - 便于排障
        return (
            f"NTFS(sector={self.bytes_per_sector}, cluster={self.cluster_size}, "
            f"records={self.record_size}, mft_lcn={self.mft_lcn}, "
            f"clusters={self.cluster_count})"
        )
