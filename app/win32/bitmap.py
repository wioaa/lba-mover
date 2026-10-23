"""卷位图读取：``FSCTL_GET_VOLUME_BITMAP`` (0x0009006F)。

``DeviceIoControl`` 的输出缓冲区有实际上限，大卷（位图数十 MB）必须**分块请求**
再拼接。这里把请求起点固定为 8 的倍数（字节对齐），从而保证返回位图的 bit 0
恰好对应请求的 ``StartingLcn``，拼接时无需做位偏移运算。
"""

from __future__ import annotations

import struct
from typing import Callable, List, Optional, Tuple

from app.core.models import BitmapSnapshot
from app.win32 import api

# VOLUME_BITMAP_BUFFER 头部长度：StartingLcn(8) + BitmapSize(8)
_HEADER_SIZE = 16

# 单次请求的输出缓冲区大小（1 MiB -> 约 838 万簇 / 次）
_CHUNK_BYTES = 1 << 20

# 位图总量上限，防止误读超大卷耗尽内存（512 MiB 位图 ≈ 4.09e9 簇）
_MAX_BITMAP_BYTES = 512 << 20

# 每字节 1 的个数查表（用于快速统计占用簇数）
POPCOUNT = bytes(bin(i).count("1") for i in range(256))

# 提取空闲区间时的分块大小（64 KiB -> 512K 簇）
_RUN_CHUNK_BYTES = 1 << 16


def read_volume_bitmap(
    handle: int,
    total_clusters: int,
    progress: Optional[Callable[[int, int], None]] = None,
) -> BitmapSnapshot:
    """分块读取整卷位图。

    Args:
        handle: 已打开的卷句柄。
        total_clusters: 卷总簇数。
        progress: 可选回调 ``(已完成簇数, 总簇数)``。

    Returns:
        :class:`BitmapSnapshot`，其中 ``bits`` 的第 0 位代表 LCN 0。

    Raises:
        Win32Error: 底层 ``DeviceIoControl`` 失败。
        RuntimeError: 位图规模超出内部上限。
    """
    if total_clusters <= 0:
        raise ValueError("total_clusters 必须为正数")

    needed = (total_clusters + 7) // 8
    if needed > _MAX_BITMAP_BYTES:
        raise RuntimeError(
            f"卷位图规模 {needed / (1 << 20):.0f} MiB 超出工具上限 "
            f"{_MAX_BITMAP_BYTES / (1 << 20):.0f} MiB，已中止以避免内存耗尽"
        )

    bits = bytearray()
    expected_lcn = 0
    clusters_per_chunk = (_CHUNK_BYTES - _HEADER_SIZE) * 8

    while expected_lcn < total_clusters:
        want_clusters = min(clusters_per_chunk, total_clusters - expected_lcn)
        # 保证请求起点是 8 的倍数（字节对齐）
        request_lcn = expected_lcn - (expected_lcn % 8)
        out_size = _HEADER_SIZE + (want_clusters + 7) // 8
        in_bytes = struct.pack("<qq", request_lcn, out_size)

        # 输出缓冲区不足以容纳整段位图时，系统返回 ERROR_MORE_DATA
        # 并同时写入"能放下的那部分"，这里必须接受这种部分成功。
        ok, raw, _returned, error = api.device_io_control_raw(
            handle,
            api.FSCTL_GET_VOLUME_BITMAP,
            in_bytes=in_bytes,
            out_size=out_size,
            context="读取卷位图",
        )
        if not ok and error != api.ERROR_MORE_DATA:
            raise api.Win32Error(error, "读取卷位图")
        if len(raw) < _HEADER_SIZE:
            raise RuntimeError(f"卷位图返回数据过短（{len(raw)} 字节）")

        returned_lcn, bitmap_size = struct.unpack_from("<qq", raw, 0)
        bitmap_size = max(0, min(int(bitmap_size), len(raw) - _HEADER_SIZE))
        chunk = raw[_HEADER_SIZE : _HEADER_SIZE + bitmap_size]
        if not chunk:
            raise RuntimeError("卷位图返回 0 字节，无法继续")

        # 若系统把起点向下取整到字节边界，跳过前缀字节保持严格对齐
        if returned_lcn < expected_lcn:
            skip = (expected_lcn - returned_lcn) // 8
            chunk = chunk[skip:]
        elif returned_lcn > expected_lcn:
            raise RuntimeError(
                f"卷位图返回起点 {returned_lcn} 晚于请求起点 {expected_lcn}，无法拼接"
            )

        bits.extend(chunk)
        expected_lcn += len(chunk) * 8

        if progress is not None:
            progress(min(expected_lcn, total_clusters), total_clusters)

    # 截断到精确长度
    del bits[needed:]

    return BitmapSnapshot(
        bits=bytes(bits),
        total_clusters=total_clusters,
        bytes_per_cluster=0,
    )


def read_bitmap_slice(
    handle: int,
    start_lcn: int,
    clusters: int,
) -> Tuple[int, bytes]:
    """只读**一小段**位图（执行期局部核验用）。

    全卷位图在大卷上是几十 MB，执行期每段都整卷重读不可接受。
    ``FSCTL_GET_VOLUME_BITMAP`` 的 ``STARTING_LCN_INPUT_BUFFER`` 允许从任意
    （8 对齐的）簇号开始取，因此这里只请求 ``[start_lcn, start_lcn+clusters)``
    这一片，单次开销与搬运批次同量级（几 KB）。

    Args:
        handle: 已打开的卷句柄（读权限即可）。
        start_lcn: 期望的起始簇号（内部会向下对齐到 8 的倍数）。
        clusters: 需要的簇数。

    Returns:
        ``(对齐后的起始簇号, 位图字节)``；位图字节的 bit 0 对应返回的起始簇号。
        失败返回 ``(-1, b"")``，**不抛异常** —— 调用方会退回"只信影子位图"，
        由守卫/FSCTL 自身兜底。

    Note:
        返回的起点是 8 的倍数，因此调用方可以直接
        ``shadow.patch(returned_lcn, chunk)``（``bit_offset=0``）。
    """
    wanted = max(0, int(clusters))
    if wanted <= 0:
        return -1, b""
    request_lcn = max(0, int(start_lcn)) - (max(0, int(start_lcn)) % 8)
    # 起点已向下对齐，最多多取 7 簇
    want_clusters = wanted + (max(0, int(start_lcn)) - request_lcn)
    out_size = _HEADER_SIZE + (want_clusters + 7) // 8
    in_bytes = struct.pack("<qq", request_lcn, out_size)

    ok, raw, _returned, error = api.device_io_control_raw(
        handle,
        api.FSCTL_GET_VOLUME_BITMAP,
        in_bytes=in_bytes,
        out_size=out_size,
        context="读取局部位图",
    )
    if not ok and error != api.ERROR_MORE_DATA:
        return -1, b""
    if len(raw) < _HEADER_SIZE:
        return -1, b""

    returned_lcn, bitmap_size = struct.unpack_from("<qq", raw, 0)
    bitmap_size = max(0, min(int(bitmap_size), len(raw) - _HEADER_SIZE))
    chunk = raw[_HEADER_SIZE : _HEADER_SIZE + bitmap_size]
    if not chunk:
        return -1, b""
    returned_lcn = max(0, int(returned_lcn))
    if returned_lcn > request_lcn:
        # 系统把起点往后挪了：按字节差裁掉前缀，保持位与簇的对应关系
        skip = (returned_lcn - request_lcn + 7) // 8
        chunk = chunk[skip:]
        if not chunk:
            return -1, b""
    elif returned_lcn < request_lcn:
        # 系统往前挪：无法在不重读的前提下对齐，放弃（不猜）
        return -1, b""
    return returned_lcn, chunk


def count_occupied(bits: bytes, start_cluster: int = 0, end_cluster: Optional[int] = None) -> int:
    """统计区间内被占用的簇数（基于 popcount 查表，速度较快）。"""
    if not bits:
        return 0
    total_bits = len(bits) * 8
    if end_cluster is None or end_cluster > total_bits:
        end_cluster = total_bits
    start_cluster = max(0, start_cluster)
    if start_cluster >= end_cluster:
        return 0

    first_byte = start_cluster >> 3
    last_byte = (end_cluster + 7) >> 3
    segment = bits[first_byte:last_byte].translate(POPCOUNT)
    occupied = sum(segment)

    # 扣掉头尾不属于区间的位
    head_extra = start_cluster - first_byte * 8
    if head_extra:
        occupied -= bin(bits[first_byte] >> head_extra << head_extra).count("1")
    tail_extra = last_byte * 8 - end_cluster
    if tail_extra:
        occupied -= bin(bits[last_byte - 1] >> (8 - tail_extra)).count("1")
    return max(0, occupied)


def free_runs_in_window(
    bits: bytes,
    start_cluster: int,
    end_cluster: int,
) -> List[Tuple[int, int]]:
    """提取 ``[start_cluster, end_cluster)`` 内的空闲连续区间。

    按 64 KiB 分块，用大整数位运算一次性定位块内的 0-run，再跨块合并。
    分块的关键作用是让每次位运算的规模有上界，避免在数 MB 位图上出现
    O(位图大小 × 区间数) 的耗时。

    返回值按起点升序，形如 ``[(start, end), ...]``（``end`` 为开区间上界）。
    """
    if end_cluster <= start_cluster:
        return []
    total_bits = len(bits) * 8
    start_cluster = max(0, int(start_cluster))
    end_cluster = min(int(end_cluster), total_bits)
    if end_cluster <= start_cluster:
        return []

    first_byte = start_cluster >> 3
    last_byte = (end_cluster + 7) >> 3

    runs: List[Tuple[int, int]] = []
    current_start = -1
    current_end = -1

    for base in range(first_byte, last_byte, _RUN_CHUNK_BYTES):
        piece = bits[base : base + _RUN_CHUNK_BYTES]
        if not piece:
            break
        bit_len = len(piece) * 8
        value = int.from_bytes(piece, "little")
        free_mask = value ^ ((1 << bit_len) - 1)  # 1 = 空闲
        position = 0
        while free_mask:
            lowest = free_mask & -free_mask
            offset = lowest.bit_length() - 1
            free_mask >>= offset
            position += offset

            inverted = ~free_mask
            length = (inverted & -inverted).bit_length() - 1

            run_start = base * 8 + position
            run_end = run_start + length
            if current_start >= 0 and run_start <= current_end:
                current_end = max(current_end, run_end)
            else:
                if current_start >= 0:
                    runs.append((current_start, current_end))
                current_start, current_end = run_start, run_end

            free_mask >>= length
            position += length

    if current_start >= 0:
        runs.append((current_start, current_end))

    clamped: List[Tuple[int, int]] = []
    for run_start, run_end in runs:
        begin = max(run_start, start_cluster)
        finish = min(run_end, end_cluster)
        if finish <= begin:
            continue
        if clamped and begin <= clamped[-1][1]:
            clamped[-1] = (clamped[-1][0], max(clamped[-1][1], finish))
        else:
            clamped.append((begin, finish))
    return clamped


def merge_runs(runs: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """合并相邻 / 重叠的区间（输入无需有序）。"""
    if not runs:
        return []
    ordered = sorted(runs)
    merged: List[Tuple[int, int]] = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            if end > last_end:
                merged[-1] = (last_start, end)
        else:
            merged.append((start, end))
    return merged


def subtract_runs(
    runs: List[Tuple[int, int]],
    holes: List[Tuple[int, int]],
) -> List[Tuple[int, int]]:
    """从 ``runs`` 中扣除 ``holes``（两者都需是已合并有序区间）。"""
    if not holes:
        return list(runs)
    result: List[Tuple[int, int]] = []
    for start, end in runs:
        cursor = start
        for hole_start, hole_end in holes:
            if hole_end <= cursor or hole_start >= end:
                continue
            if hole_start > cursor:
                result.append((cursor, hole_start))
            cursor = max(cursor, hole_end)
            if cursor >= end:
                break
        if cursor < end:
            result.append((cursor, end))
    return result
