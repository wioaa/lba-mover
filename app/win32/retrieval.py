"""解析 ``FSCTL_GET_RETRIEVAL_POINTERS`` (0x00090073) 得到文件的物理 extents。

``RETRIEVAL_POINTERS_BUFFER`` 是**变长结构**，布局如下（x64）::

    DWORD         ExtentCount;        // 偏移 0（后跟 4 字节填充）
    LARGE_INTEGER StartingVcn;        // 偏移 8
    struct {                          // 偏移 16 起，每项 16 字节
        LARGE_INTEGER NextVcn;
        LARGE_INTEGER Lcn;            // -1 (0xFFFF...) 表示稀疏 / 未分配
    } Extents[];                      // 变长数组

因此第 i 项的偏移 = ``16 + 16 * i``；不能用 ``Structure`` 描述，直接按字节解析。
"""

from __future__ import annotations

import struct
from typing import List, Optional, Sequence, Tuple

from app.core.models import Extent
from app.win32 import api

_HEADER_SIZE = 16
_EXTENT_SIZE = 16

# 缓冲区递增序列：绝大多数文件一次 64 KiB 足够
_BUFFER_STEPS: Sequence[int] = (1 << 16, 1 << 18, 1 << 20, 1 << 22, 1 << 24)


class RetrievalError(RuntimeError):
    """retrieval pointers 解析失败。"""


def _parse_extents(raw: bytes) -> List[Extent]:
    """按变长布局解析 ``RETRIEVAL_POINTERS_BUFFER``。"""
    if len(raw) < _HEADER_SIZE:
        raise RetrievalError(f"RETRIEVAL_POINTERS_BUFFER 头部不足（{len(raw)} 字节）")

    extent_count, starting_vcn = struct.unpack_from("<Ixxxxq", raw, 0)
    max_count = (len(raw) - _HEADER_SIZE) // _EXTENT_SIZE
    if extent_count > max_count:
        # 系统给的缓冲区不够描述全部 extent —— 视为需要更大缓冲区
        raise RetrievalError(
            f"extent 数量 {extent_count} 超出缓冲区容量 {max_count}，请加大缓冲区"
        )

    extents: List[Extent] = []
    current_vcn = int(starting_vcn)
    for index in range(int(extent_count)):
        offset = _HEADER_SIZE + _EXTENT_SIZE * index
        next_vcn, lcn = struct.unpack_from("<qq", raw, offset)
        clusters = int(next_vcn) - current_vcn
        if clusters <= 0:
            current_vcn = int(next_vcn)
            continue
        extents.append(Extent(vcn=current_vcn, lcn=int(lcn), clusters=clusters))
        current_vcn = int(next_vcn)
    return extents


def get_extents(handle: int, buffer_size: Optional[int] = None) -> List[Extent]:
    """从文件句柄读取全部 extents。

    Args:
        handle: 以 ``FILE_READ_ATTRIBUTES`` 打开的文件句柄。
        buffer_size: 指定缓冲区大小；``None`` 时按递增序列自动扩容。

    Returns:
        ``Extent`` 列表（可能为空，例如 0 字节文件或常驻 MFT 小文件）。

    Raises:
        Win32Error: ``DeviceIoControl`` 失败且不是缓冲区不足。
        RetrievalError: 结构解析失败。
    """
    steps = (buffer_size,) if buffer_size else _BUFFER_STEPS
    starting_vcn = 0
    collected: List[Extent] = []
    last_error: Optional[api.Win32Error] = None

    for size in steps:
        collected = []
        starting_vcn = 0
        last_error = None
        for _ in range(64):  # 单文件最多 64 轮续读，防御性上限
            in_bytes = struct.pack("<q", starting_vcn)
            try:
                raw, _ = api.device_io_control(
                    handle,
                    api.FSCTL_GET_RETRIEVAL_POINTERS,
                    in_bytes=in_bytes,
                    out_size=int(size),
                    context="读取文件物理布局",
                )
            except api.Win32Error as exc:
                if exc.winerror == api.ERROR_HANDLE_EOF:
                    return collected  # 已读到末尾
                last_error = exc
                break

            try:
                chunk = _parse_extents(raw)
            except RetrievalError as exc:
                raise RetrievalError(str(exc)) from exc

            collected.extend(chunk)
            if not chunk:
                return collected

            last_vcn = chunk[-1].vcn_end
            if len(chunk) < (len(raw) - _HEADER_SIZE) // _EXTENT_SIZE:
                # 本次返回的 extent 数小于缓冲区容量 => 已经读到末尾
                return collected
            starting_vcn = last_vcn

        if last_error is None:
            return collected
        if last_error.winerror != api.ERROR_MORE_DATA:
            raise last_error

    if last_error is not None:
        raise last_error
    return collected


def get_extents_for_path(path: str) -> Tuple[List[Extent], str]:
    """打开文件并读取 extents（**v1 行为，不加任何前置过滤**）。

    Returns:
        ``(extents, error_reason)``。成功时 ``error_reason`` 为空串；
        失败时 ``extents`` 为空且 ``error_reason`` 是中文说明。
    """
    extents, _attributes, reason = _read_once(path)
    if reason:
        return [], reason
    allocated = [e for e in extents if not e.is_sparse and e.clusters > 0]
    if not allocated:
        return [], "无物理分配（0 字节或全部常驻 / 稀疏）"
    return extents, ""


def get_basic_info(path: str) -> Tuple[List[Extent], int, str]:
    """读取 extents **并前置识别压缩 / EFS**（v2 新增）。

    ``FSCTL_MOVE_FILE`` 对 EFS 加密文件不支持，对 NTFS 压缩文件只能在 16 簇的
    压缩单元边界移动 —— 两者都在这里提前排除，避免"扫得到、搬到一半才失败"。

    Returns:
        ``(extents, attributes, reason)``。命中压缩 / EFS 时返回
        ``([], attributes, 中文原因)``。

    Note:
        ``get_extents_for_path`` 保持 v1 行为不变（不做该过滤），见回归风险 R13。
    """
    extents, attributes, reason = _read_once(path)
    if reason:
        return [], attributes, reason

    if attributes != api.INVALID_FILE_ATTRIBUTES:
        if attributes & api.FILE_ATTRIBUTE_ENCRYPTED:
            return [], attributes, "EFS 加密文件，FSCTL_MOVE_FILE 不支持（扫描阶段已排除）"
        if attributes & api.FILE_ATTRIBUTE_COMPRESSED:
            return [], attributes, "NTFS 压缩文件，只能在压缩单元边界移动（扫描阶段已排除）"

    allocated = [e for e in extents if not e.is_sparse and e.clusters > 0]
    if not allocated:
        return [], attributes, "无物理分配（0 字节或全部常驻 / 稀疏）"
    return extents, attributes, ""


def _read_once(path: str) -> Tuple[List[Extent], int, str]:
    """打开一次文件，同时取属性与 extents。"""
    attributes = api.get_file_attributes(path)
    handle: Optional[int] = None
    try:
        handle = api.open_handle(
            path,
            api.FILE_READ_ATTRIBUTES,
            share=api.FILE_SHARE_READ | api.FILE_SHARE_WRITE | api.FILE_SHARE_DELETE,
            flags=api.FILE_ATTRIBUTE_NORMAL | api.FILE_FLAG_BACKUP_SEMANTICS,
            context="打开文件",
        )
    except api.Win32Error as exc:
        return [], attributes, f"无法打开（{exc.winerror}: {api.format_error(exc.winerror)}）"

    try:
        extents = get_extents(handle)
    except api.Win32Error as exc:
        return [], attributes, f"读取布局失败（{exc.winerror}: {api.format_error(exc.winerror)}）"
    except RetrievalError as exc:
        return [], attributes, f"布局解析失败（{exc}）"
    finally:
        api.close_handle(handle)
    return extents, attributes, ""


def summarize_extents(extents: List[Extent]) -> str:
    """把 extents 渲染成简短描述，便于日志输出。"""
    allocated = [e for e in extents if not e.is_sparse]
    if not allocated:
        return "无分配"
    total = sum(e.clusters for e in allocated)
    return f"{len(allocated)} 段 / {total} 簇"


def extent_pairs(extents: List[Extent]) -> List[Tuple[int, int]]:
    """转换为 ``[(lcn, clusters), ...]`` 便于纯函数处理。"""
    return [(e.lcn, e.clusters) for e in extents if not e.is_sparse and e.clusters > 0]
