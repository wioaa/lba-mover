"""卷枚举与 NTFS 卷信息读取（**只读**）。

主要调用：
* ``GetLogicalDrives`` / ``GetDriveTypeW`` —— 枚举本地固定卷
* ``GetVolumeInformationW`` —— 卷标 / 文件系统 / 序列号
* ``GetDiskFreeSpaceExW`` —— 总容量 / 可用空间
* ``FSCTL_GET_NTFS_VOLUME_DATA`` (0x00090064) —— 簇大小 / 总簇数 / MFT 区
"""

from __future__ import annotations

import struct
from typing import List, Optional

from app.core.models import VolumeInfo
from app.win32 import api


class NtfsVolumeData:
    """``NTFS_VOLUME_DATA_BUFFER`` 的解析结果。

    结构布局（x64，共 96 字节）::

        LARGE_INTEGER VolumeSerialNumber;        0
        LARGE_INTEGER NumberSectors;             8
        LARGE_INTEGER TotalClusters;            16
        LARGE_INTEGER FreeClusters;             24
        LARGE_INTEGER TotalReserved;            32
        DWORD         BytesPerSector;           40
        DWORD         BytesPerCluster;          44
        DWORD         BytesPerFileRecordSegment;48
        DWORD         ClustersPerFileRecordSegment;52
        LARGE_INTEGER MftValidDataLength;       56
        LARGE_INTEGER MftStartLcn;              64
        LARGE_INTEGER Mft2StartLcn;             72
        LARGE_INTEGER MftZoneStart;             80
        LARGE_INTEGER MftZoneEnd;               88
    """

    STRUCT_SIZE = 96

    def __init__(self, raw: bytes) -> None:
        if len(raw) < self.STRUCT_SIZE:
            raise ValueError(
                f"NTFS_VOLUME_DATA_BUFFER 长度不足：期望 {self.STRUCT_SIZE}，实际 {len(raw)}"
            )
        (
            self.volume_serial,
            self.number_sectors,
            self.total_clusters,
            self.free_clusters,
            self.total_reserved,
            self.bytes_per_sector,
            self.bytes_per_cluster,
            self.bytes_per_file_record,
            self.clusters_per_file_record,
            self.mft_valid_data_length,
            self.mft_start_lcn,
            self.mft2_start_lcn,
            self.mft_zone_start,
            self.mft_zone_end,
        ) = struct.unpack_from("<qqqqqIIIIqqqqq", raw, 0)


class VolumeHandle:
    """卷设备句柄（``\\\\.\\C:``）的 RAII 包装。"""

    def __init__(self, letter: str) -> None:
        self.letter = letter.upper().rstrip(":")
        self.path = f"\\\\.\\{self.letter}:"
        self._handle: Optional[int] = None

    @property
    def handle(self) -> int:
        """返回已打开的句柄；未打开则抛 ``RuntimeError``。"""
        if self._handle is None:
            raise RuntimeError(f"卷 {self.letter}: 句柄尚未打开")
        return self._handle

    def open(self, write_access: bool = False) -> int:
        """打开卷设备。

        Args:
            write_access: 传 ``True`` 时请求 ``GENERIC_READ | GENERIC_WRITE``，
                真实移动文件需要写权限。

        Returns:
            句柄值。
        """
        access = api.GENERIC_READ | (api.GENERIC_WRITE if write_access else 0)
        self._handle = api.open_handle(
            self.path,
            access,
            share=api.FILE_SHARE_READ | api.FILE_SHARE_WRITE,
            flags=0,
            context="打开卷设备",
        )
        return self._handle

    def close(self) -> None:
        """关闭句柄（幂等）。"""
        if self._handle is not None:
            api.close_handle(self._handle)
            self._handle = None

    def __enter__(self) -> "VolumeHandle":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    def ntfs_volume_data(self) -> NtfsVolumeData:
        """读取 ``NTFS_VOLUME_DATA_BUFFER``。"""
        raw, _ = api.device_io_control(
            self.handle,
            api.FSCTL_GET_NTFS_VOLUME_DATA,
            out_size=NtfsVolumeData.STRUCT_SIZE,
            context=f"读取 NTFS 卷数据 {self.letter}:",
        )
        return NtfsVolumeData(raw)


# --------------------------------------------------------------------------- #
# 卷枚举
# --------------------------------------------------------------------------- #
def list_fixed_drives() -> List[str]:
    """返回所有本地固定磁盘的盘符（如 ``["C", "D"]``）。"""
    mask = api.kernel32.GetLogicalDrives()
    letters: List[str] = []
    for index in range(26):
        if not (mask >> index) & 1:
            continue
        letter = chr(ord("A") + index)
        if api.kernel32.GetDriveTypeW(f"{letter}:\\") == api.DRIVE_FIXED:
            letters.append(letter)
    return letters


def read_volume_info(letter: str) -> VolumeInfo:
    """读取单个卷的完整信息。

    即使 ``FSCTL_GET_NTFS_VOLUME_DATA`` 失败（非 NTFS 或权限不足），
    也会返回带有基础容量信息的对象，``is_ntfs`` 为 ``False``。
    """
    root = f"{letter}:\\"
    info = VolumeInfo(letter=letter.upper(), root=root)

    name_buf = api.ctypes.create_unicode_buffer(261)
    fs_buf = api.ctypes.create_unicode_buffer(261)
    serial = api.wintypes.DWORD(0)
    max_component = api.wintypes.DWORD(0)
    fs_flags = api.wintypes.DWORD(0)

    if api.kernel32.GetVolumeInformationW(
        root,
        name_buf,
        261,
        api.ctypes.byref(serial),
        api.ctypes.byref(max_component),
        api.ctypes.byref(fs_flags),
        fs_buf,
        261,
    ):
        info.label = name_buf.value
        info.filesystem = fs_buf.value
        info.serial = serial.value
    else:
        # 卷标读取失败不影响主流程，仅记录空值
        info.label = ""
        info.filesystem = ""

    free_to_caller = api.ctypes.c_ulonglong(0)
    total = api.ctypes.c_ulonglong(0)
    total_free = api.ctypes.c_ulonglong(0)
    if api.kernel32.GetDiskFreeSpaceExW(
        root,
        api.ctypes.byref(free_to_caller),
        api.ctypes.byref(total),
        api.ctypes.byref(total_free),
    ):
        info.total_bytes = int(total.value)
        info.free_bytes = int(free_to_caller.value)
        if not info.total_bytes:
            info.total_bytes = int(total_free.value)

    info.is_ntfs = info.filesystem.upper() == "NTFS"

    handle = VolumeHandle(letter)
    try:
        handle.open()
        ntfs = handle.ntfs_volume_data()
        info.bytes_per_cluster = int(ntfs.bytes_per_cluster)
        info.total_clusters = int(ntfs.total_clusters)
        info.free_clusters = int(ntfs.free_clusters)
        info.mft_zone_start = int(ntfs.mft_zone_start)
        info.mft_zone_end = int(ntfs.mft_zone_end)
        info.mft_start_lcn = int(ntfs.mft_start_lcn)
        info.mft_valid_data_length = int(ntfs.mft_valid_data_length)
        # v2：$MFTMirr 起点与 MFT 记录大小（守卫窗 / MFT 解析都要用）
        info.mft2_start_lcn = int(ntfs.mft2_start_lcn)
        info.bytes_per_file_record = int(ntfs.bytes_per_file_record)
        # v2（N5）：扇区大小是「原始读必须扇区对齐」的基准；扇区总数用于交叉校验
        info.bytes_per_sector = int(ntfs.bytes_per_sector)
        info.number_sectors = int(ntfs.number_sectors)
        if not info.total_bytes:
            info.total_bytes = info.total_clusters * info.bytes_per_cluster
        info.is_ntfs = True
    except api.Win32Error as exc:
        # 无管理员权限时打开 \\.\X: 会被拒绝（错误码 5）
        info.volume_error = str(exc)
        if not info.bytes_per_cluster:
            info.bytes_per_cluster = 4096
        info.total_clusters = 0
    except ValueError as exc:
        info.volume_error = f"NTFS_VOLUME_DATA_BUFFER 解析失败：{exc}"
        if not info.bytes_per_cluster:
            info.bytes_per_cluster = 4096
    finally:
        handle.close()

    return info


def read_volume_serial(letter: str) -> Optional[int]:
    """只读卷序列号（``GetVolumeInformation``，32 位 DWORD）；失败返回 ``None``。

    与 :func:`read_volume_info` 里 ``info.serial`` **同源**。执行前的卷身份校验
    必须两边用同一个 API：规划期取的是这个 32 位序列号，执行期若改用
    ``FSCTL_GET_NTFS_VOLUME_DATA`` 的 64 位 ``VolumeSerialNumber`` 直接比全值，
    两种 API 的表示 / 宽度不同，会把**同一卷**误报成「序列号已变化」而阻断执行。
    """
    root = f"{letter.upper()}:\\"
    serial = api.wintypes.DWORD(0)
    ok = api.kernel32.GetVolumeInformationW(
        root, None, 0, api.ctypes.byref(serial), None, None, None, 0
    )
    return int(serial.value) if ok else None


def list_volumes() -> List[VolumeInfo]:
    """枚举全部本地固定卷并读取信息。"""
    volumes: List[VolumeInfo] = []
    for letter in list_fixed_drives():
        try:
            volumes.append(read_volume_info(letter))
        except api.Win32Error:
            # 单个卷不可读时跳过，不影响其它卷
            continue
    return volumes


def volume_serial_text(serial: int) -> str:
    """把卷序列号格式化为 ``XXXX-XXXX``。"""
    return f"{(serial >> 16) & 0xFFFF:04X}-{serial & 0xFFFF:04X}"
