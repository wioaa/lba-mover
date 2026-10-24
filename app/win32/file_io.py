"""用户态文件读写（ctypes）——分块流式搬迁模式的“手”（1.0.0C 特殊工作模式）。

只做普通 ``CreateFileW`` / ``ReadFile`` / ``WriteFile``，**不碰 ``FSCTL_MOVE_FILE``、
不需要管理卷特权**。与 ``app.core.block_stream``（分块计划 / 可调缓存 / 流水线调度）
配合，实现「源 → 暂存盘 → 目的」的双阶段分块流式搬迁：

* block = 65536 簇；缓冲按 block 倍数可调（``buffer_blocks``）；
* ``load``（读源块 → 写暂存盘）与 ``store``（读暂存块 → 写目的）按
  :func:`block_stream.stream_schedule` 重叠推进；
* 数据经文件系统落地，源簇读出后逻辑上即刻被重新占用。

这些原语**可对临时文件直接单测**（验证 ctypes 绑定与 staged_copy 正确性）；
真正的卷级 LBA 重排效果需在真实磁盘上验证。
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import tempfile
from typing import List, Optional, Tuple

from app.core.block_stream import (
    BLOCK_CLUSTERS,
    Block,
    plan_blocks,
    stream_schedule,
)
from app.win32 import api
from app.win32.api import kernel32, wintypes

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
CREATE_ALWAYS = 2
FILE_BEGIN = 0
_INVALID = ctypes.c_void_p(-1).value

# WriteFile 之前未绑定（读路径只用 ReadFile）；这里按 ReadFile 对称补上
kernel32.WriteFile.argtypes = [
    wintypes.HANDLE,
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    wintypes.LPVOID,
]
kernel32.WriteFile.restype = wintypes.BOOL

kernel32.SetEndOfFile.argtypes = [wintypes.HANDLE]
kernel32.SetEndOfFile.restype = wintypes.BOOL


def _ok_handle(handle: int, context: str) -> int:
    if handle is None or handle == _INVALID:
        raise api.Win32Error(kernel32.GetLastError(), context)
    return handle


def open_read(path: str) -> int:
    """以只读 + 共享方式打开已存在文件。"""
    handle = kernel32.CreateFileW(
        str(path), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
        None, OPEN_EXISTING, 0, None,
    )
    return _ok_handle(handle, f"open_read({path})")


def open_write(path: str, truncate: bool = True) -> int:
    """创建/覆盖一个只写文件。

    ``truncate=False`` 时用 ``OPEN_EXISTING``（不截断）——用于覆盖一个已预分配
    （preallocate）好的文件，保留其已占用的连续簇。
    """
    disposition = CREATE_ALWAYS if truncate else OPEN_EXISTING
    handle = kernel32.CreateFileW(
        str(path), GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE,
        None, disposition, 0, None,
    )
    return _ok_handle(handle, f"open_write({path})")


def preallocate_size(handle: int, size: int) -> None:
    """把文件扩展到 ``size`` 字节（SetEndOfFile），**预先填充一段（尽量连续）的簇**。

    之后用 ``open_write(truncate=False)`` 打开同一路径做流式覆盖，数据即落入这段
    预占的连续簇——实现「前端 → 后端连续区」的整体直搬（落点 LBA 由文件系统在
    空闲区分配，预分配保证连续性）。
    """
    if not kernel32.SetFilePointerEx(handle, int(size), None, FILE_BEGIN):
        raise api.Win32Error(kernel32.GetLastError(), f"SetFilePointerEx@{size}")
    if not kernel32.SetEndOfFile(handle):
        raise api.Win32Error(kernel32.GetLastError(), "SetEndOfFile")


def open_readwrite(path: str) -> int:
    """创建/覆盖一个可读写文件（用作暂存盘）。"""
    handle = kernel32.CreateFileW(
        str(path), GENERIC_READ | GENERIC_WRITE, 0,
        None, CREATE_ALWAYS, 0, None,
    )
    return _ok_handle(handle, f"open_readwrite({path})")


def close(handle: int) -> None:
    if handle and handle != _INVALID:
        kernel32.CloseHandle(handle)


def read_at(handle: int, offset: int, size: int) -> bytes:
    """从 ``offset`` 读 **恰好** ``size`` 字节（定位 + 循环 ReadFile）。

    D-003：``ReadFile`` 允许返回**少于请求**的字节数（短读：压缩 / 稀疏文件、
    某些过滤驱动、跨缓存边界等）。早期实现直接 ``return buf.raw[:done.value]``，
    一次短读就让该块被**静默截断**——紧随其后的 :func:`write_at` 只写入短数据，
    分块流式迁移（本模式的正确性前提）因此产生**数据不一致**。

    这里改为循环读满 ``size``：只有遇到真 EOF（``ReadFile`` 返回 0 字节）才停止，
    且此时**必须显式抛错**，绝不静默返回短数据。

    Raises:
        api.Win32Error: 定位 / 读取失败；或未读满 ``size`` 即遇到 EOF
            （错误码 :data:`app.win32.api.ERROR_HANDLE_EOF`）。
    """
    size = int(size)
    if size <= 0:
        return b""
    if not kernel32.SetFilePointerEx(handle, int(offset), None, FILE_BEGIN):
        raise api.Win32Error(kernel32.GetLastError(), f"SetFilePointerEx@{offset}")

    buf = ctypes.create_string_buffer(size)
    done = wintypes.DWORD(0)
    got = 0
    while got < size:
        # 在**同一块缓冲**里按已读进度切一段视图续读，避免每次短读都重新分配
        view = (ctypes.c_char * (size - got)).from_buffer(buf, got)
        if not kernel32.ReadFile(handle, view, size - got, ctypes.byref(done), None):
            raise api.Win32Error(kernel32.GetLastError(), f"ReadFile@{offset}")
        if done.value <= 0:
            # 真 EOF：还没读满 size 就没数据了 → 报错，而不是返回截断的数据
            raise api.Win32Error(
                api.ERROR_HANDLE_EOF,
                f"ReadFile@{offset} 未读满即 EOF（请求 {size} 字节，实得 {got} 字节）",
            )
        got += int(done.value)
    return buf.raw[:size]


def write_at(handle: int, offset: int, data: bytes) -> int:
    """把 ``data`` **完整**写到 ``offset``（定位 + 循环 WriteFile），返回写入字节数。

    D-011（D-003 的写侧孪生）：``WriteFile`` 同样允许返回**少于请求**的字节数
    （短写：磁盘满 / 配额 / 驱动行为 / IO 排队）。原实现既不重试、也不校验
    ``done.value``，短写会让落盘数据**静默少写**，上层完全无感知——读侧（D-003）
    已经改成显式抛错，写侧留着同样的洞，流式迁移的数据一致性就没闭环。

    这里改为循环写满 ``len(data)``：续写时用缓冲区**视图切片**推进（``from_buffer``，
    不重复分配整块缓冲）；首次调用直接写原始字节串（**不额外复制**），只有真的
    发生短写时才建一份可写副本供切片。若在没写满时 ``WriteFile`` 返回 0 字节
    （真写不进去了），**显式抛错**，绝不把已写字节数当成功返回。

    Raises:
        api.Win32Error: 定位 / 写入失败；或写入停滞（0 字节）导致无法写满
            （错误码 :data:`app.win32.api.ERROR_DISK_FULL`）。
    """
    n = len(data)
    if n <= 0:
        return 0
    if not kernel32.SetFilePointerEx(handle, int(offset), None, FILE_BEGIN):
        raise api.Win32Error(kernel32.GetLastError(), f"SetFilePointerEx@{offset}")

    done = wintypes.DWORD(0)
    written = 0
    buf = None               # 仅在真发生短写时才建的可写副本（供视图切片）
    while written < n:
        if written == 0:
            # 快路径：直接写原始字节串，零额外内存
            chunk: object = data
            size = n
        else:
            if buf is None:
                buf = ctypes.create_string_buffer(data)
            chunk = (ctypes.c_char * (n - written)).from_buffer(buf, written)
            size = n - written
        if not kernel32.WriteFile(handle, chunk, size, ctypes.byref(done), None):
            raise api.Win32Error(kernel32.GetLastError(), f"WriteFile@{offset}")
        if done.value <= 0:
            # 写入停滞：一个字节都写不进去了 —— 报错，而不是把已写字节当成功
            raise api.Win32Error(
                api.ERROR_DISK_FULL,
                f"WriteFile@{offset} 写入停滞（请求 {n} 字节，已写 {written} 字节）",
            )
        written += int(done.value)
    return written


def copy_block(src: int, dst: int, offset: int, length: int) -> int:
    """单跳：把 ``[offset, offset+length)`` 从 ``src`` 读到 ``dst``。"""
    data = read_at(src, offset, length)
    return write_at(dst, offset, data)


def _contiguous_runs(indices) -> "List[List[int]]":
    """把升序的块下标序列切成连续段（``[2,3,4,7]`` → ``[[2,3,4],[7]]``）。"""
    runs: List[List[int]] = []
    for index in indices:
        if runs and index == runs[-1][-1] + 1:
            runs[-1].append(index)
        else:
            runs.append([index])
    return runs


#: 攒批落盘时**单次合并写入**的字节上限（D-008）。
#: 整段 ``b"".join`` 会额外复制出一份与段等长的连续 buffer，内存峰值约为
#: 1 个 block 的 2 倍；超过本上限即切成多段顺序写（**落盘字节完全一致**），
#: 峰值削到 ``min(段长, MAX_MERGE_BYTES)``。默认 8 MiB：远大于常规 block，
#: 保留原有的「连续块拼成单次大写」收益，同时封住超大 block 的翻倍峰值。
MAX_MERGE_BYTES = 8 << 20


def _write_run(
    scratch: int,
    blocks: List[Block],
    pending: dict,
    run: List[int],
    max_merge: Optional[int] = None,
) -> None:
    """把一段**连续块**写进暂存盘：连续块仍合并，但单次合并长度受上限约束。

    D-008：``b"".join(...)`` 会把整段再复制一份（峰值 ≈ 2 个 block）；这里按
    ``max_merge`` 切成若干段顺序写——**落盘字节与整段 join 完全一致**，峰值
    削到 ``min(段长, max_merge)``。单个块本身就超过上限时不再复制，直接整块写。

    Args:
        scratch: 暂存盘句柄。
        blocks: :func:`plan_blocks` 的块计划（取块内偏移）。
        pending: 块下标 -> bytes 的攒批字典。
        run: 一段连续（下标递增 1）的块下标。
        max_merge: 单次合并字节上限；``None`` 用 :data:`MAX_MERGE_BYTES`。
    """
    limit = int(MAX_MERGE_BYTES if max_merge is None else max_merge)
    if limit <= 0:
        limit = 1
    offset = blocks[run[0]].offset
    segment: List[bytes] = []
    segment_len = 0

    def _flush_segment() -> None:
        """把当前攒下的小段落盘（单块时不复制，直接写）。"""
        nonlocal segment, segment_len, offset
        if not segment:
            return
        write_at(scratch, offset, segment[0] if len(segment) == 1 else b"".join(segment))
        offset += segment_len
        segment = []
        segment_len = 0

    for index in run:
        chunk = pending[index]
        if len(chunk) >= limit:
            # 单个块已达上限：先落掉攒下的段，再整块直写（省掉一次 join 复制）
            _flush_segment()
            write_at(scratch, offset, chunk)
            offset += len(chunk)
            continue
        if segment and segment_len + len(chunk) > limit:
            _flush_segment()
        segment.append(chunk)
        segment_len += len(chunk)
    _flush_segment()


def staged_copy(
    src_path: str,
    scratch_path: str,
    dst_path: str,
    *,
    buffer_blocks: int = 2,
    bytes_per_cluster: int = 4096,
    block_clusters: int = BLOCK_CLUSTERS,
    preallocate_dst: bool = False,
) -> Tuple[int, int]:
    """双阶段分块流式搬迁：源 →(load) 暂存盘 →(store) 目的。

    按 :func:`block_stream.stream_schedule` 推进；暂存盘同时驻留块数 ≤
    ``buffer_blocks``。``preallocate_dst=True`` 时先按总大小预填充一段（尽量连续）
    的簇再流式覆盖。返回 ``(写入目的字节数, 块数)``。

    **暂存写合并（1.0.0D 方案 D）**：``load`` 不再逐 block 写暂存盘，而是攒满
    ``buffer_blocks`` 个 block（或流末尾、或 store 需要）后，把**连续的块区间
    拼成单次顺序写**落盘 —— 功能输出不变（dst 内容逐字节一致），显著降低
    WriteFile syscall 次数与暂存盘碎片。

    **写入完整性（D-011）**：``write_at`` 内部已循环写满并校验；这里再逐块
    比对「实写字节数 vs 块长度」，不一致即抛 :class:`api.Win32Error`——
    不让短写被 ``written`` 的静默累加吞掉。
    """
    total = os.path.getsize(src_path)
    blocks = plan_blocks(total, bytes_per_cluster, block_clusters)
    if not blocks:
        handle = open_write(dst_path)
        close(handle)
        return 0, 0

    if preallocate_dst and total > 0:
        ph = open_write(dst_path)
        preallocate_size(ph, total)
        close(ph)
    src = open_read(src_path)
    scratch = open_readwrite(scratch_path)
    dst = open_write(dst_path, truncate=not preallocate_dst)

    pending: dict = {}          # 块下标 -> bytes（攒批，待合并落盘）

    def _flush_pending() -> None:
        """把攒住的块按**连续段**合并写进暂存盘（每段一次或多次 write_at）。"""
        nonlocal pending
        if not pending:
            return
        for run in _contiguous_runs(sorted(pending)):
            _write_run(scratch, blocks, pending, run)
        pending = {}

    result: Tuple[int, int] = (0, 0)
    try:
        written = 0
        for kind, index in stream_schedule(len(blocks), buffer_blocks):
            block = blocks[index]
            if kind == "load":
                pending[index] = read_at(src, block.offset, block.length)
                if len(pending) >= max(1, buffer_blocks):
                    _flush_pending()
            else:  # store
                if index in pending:
                    # 保险：目标块还没落盘（时序异常时兜底），先强制落盘
                    _flush_pending()
                chunk = read_at(scratch, block.offset, block.length)
                # D-011 位置二：write_at 的返回值**必须与块长度对齐**才累加——
                # 不比对就把返回值静默累加，等于把「少写了多少」这个信息吞掉。
                n = write_at(dst, block.offset, chunk)
                if n != block.length:
                    raise api.Win32Error(
                        api.ERROR_DISK_FULL,
                        f"目的块 {index} 短写（请求 {block.length} 字节，实写 {n} 字节）",
                    )
                written += n
        _flush_pending()
        result = (written, len(blocks))
    finally:
        close(src)
        close(scratch)
        close(dst)
        # ★ C-1：清理暂存盘必须放在 **finally**（异常路径也要清现场）——
        # 早期版本只在正常路径可达，进程/写入中途失败时 `.stage` 文件残留。
        # ★ 必须在句柄关闭**之后**删 —— Windows 上删除一个以 share=0 打开的
        # 文件会失败。删除失败不掩盖原异常（包 except 静默）。
        try:
            os.remove(scratch_path)
        except OSError:
            pass
    return result


def memory_copy(
    src_path: str,
    dst_path: str,
    *,
    buffer_blocks: int = 2,
    bytes_per_cluster: int = 4096,
    block_clusters: int = BLOCK_CLUSTERS,
    preallocate_dst: bool = False,
) -> Tuple[int, int]:
    """内存分配缓存模式：块在 RAM 缓冲里中转（无暂存盘）。

    ``load`` 读源块进 RAM、``store`` 从 RAM 写目的；RAM 同时驻留 ≤ ``buffer_blocks``
    个 block（可调）。最快，但占用内存 = ``buffer_blocks × block``。
    ``preallocate_dst=True`` 时先按总大小预分配一段（尽量连续）的簇，再流式覆盖
    （「预先填充 → 流式覆盖」，前端→后端整体直搬）。返回 ``(写入目的字节数, 块数)``。
    """
    total = os.path.getsize(src_path)
    blocks = plan_blocks(total, bytes_per_cluster, block_clusters)
    if not blocks:
        handle = open_write(dst_path)
        close(handle)
        return 0, 0

    if preallocate_dst and total > 0:
        ph = open_write(dst_path)      # 建文件
        preallocate_size(ph, total)    # 预先填充一段（尽量连续）的簇
        close(ph)
    src = open_read(src_path)
    dst = open_write(dst_path, truncate=not preallocate_dst)  # 预分配则覆盖其连续簇、否则新建
    cache: dict = {}  # index -> bytes，RAM 驻留 ≤ buffer_blocks
    try:
        written = 0
        for kind, index in stream_schedule(len(blocks), buffer_blocks):
            block = blocks[index]
            if kind == "load":
                cache[index] = read_at(src, block.offset, block.length)
            else:  # store
                written += write_at(dst, block.offset, cache.pop(index))
        return written, len(blocks)
    finally:
        close(src)
        close(dst)


#: 内存分配缓存模式
CACHE_MODE_MEMORY = "memory"
#: 设定缓存目录的硬盘缓存模式
CACHE_MODE_DISK = "disk"

#: disk 缓存未指定目录时的默认暂存目录名（位于**源文件所在卷根**下）
DEFAULT_SCRATCH_DIRNAME = ".lba-tmp-scratch"


def _volume_root_of(path: str) -> str:
    """源路径所在卷根（如 ``R:\\``）；取不到返回空串（测试可通过替换本函数模拟）。"""
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    return drive + os.sep if drive else ""


def default_scratch_dir(src_path: str) -> str:
    """disk 缓存未指定目录时的默认暂存目录（1.0.0D 方案 D）。

    取**源文件所在卷根**下的 ``.lba-tmp-scratch\\``（同卷暂存，避免暂存盘在
    另一块盘上造成来回搬运）；卷根不可写时回落到 ``%TEMP%\\lba-scratch``。

    Returns:
        已创建、且探测过可写的目录路径（不抛异常）。
    """
    root = _volume_root_of(src_path)
    if root:
        candidate = os.path.join(root, DEFAULT_SCRATCH_DIRNAME)
        try:
            os.makedirs(candidate, exist_ok=True)
            probe = os.path.join(candidate, ".write-probe")
            with open(probe, "w", encoding="utf-8") as handle:
                handle.write("1")
            os.remove(probe)
            return candidate
        except OSError:
            pass  # 卷根不可写（只读介质 / 权限）→ 回落 TEMP
    fallback = os.path.join(tempfile.gettempdir(), "lba-scratch")
    try:
        os.makedirs(fallback, exist_ok=True)
    except OSError:
        pass
    return fallback


def _scratch_name(dst_path: str) -> str:
    """disk 模式的暂存文件名：``<sha1(目标绝对路径)>.stage``（D-004）。

    早期实现取 ``basename(dst_path) + ".stage"`` —— 只按文件名、**不区分目录**，
    不同目录下的同名目标会复用同一个暂存盘（崩溃残留复用 / 外部共享
    ``cache_dir`` / 将来改并发时互相覆盖、数据错乱）。

    改用目标**完整路径**的 sha1 摘要：跨目录唯一、同一目标可复现（大小写与
    相对路径差异归一化），且不含用户文件名（避免超长名 / 非法字符）。仍以
    ``.stage`` 结尾——worker 收尾按该后缀清理残留。
    """
    key = os.path.normcase(os.path.abspath(str(dst_path)))
    return hashlib.sha1(key.encode("utf-8", "surrogatepass")).hexdigest() + ".stage"


def copy_stream(
    src_path: str,
    dst_path: str,
    *,
    mode: str = CACHE_MODE_MEMORY,
    cache_dir: Optional[str] = None,
    buffer_blocks: int = 2,
    bytes_per_cluster: int = 4096,
    block_clusters: int = BLOCK_CLUSTERS,
    preallocate_dst: bool = False,
) -> Tuple[int, int]:
    """统一入口：按缓存模式做分块流式搬运。

    * ``mode="memory"``：内存分配缓存——块在 RAM 中转，最快、占内存；
    * ``mode="disk"``：硬盘缓存——块落 ``cache_dir`` 暂存盘再读出，占磁盘、
      可缓冲更大（``buffer_blocks`` 仍按 65536 簇 block 的倍数可调）。
      ``cache_dir`` 为空时**默认暂存在源文件所在卷根**的
      ``.lba-tmp-scratch\\``（同卷；卷根不可写回落 ``%TEMP%``）——
      注意暂存盘会多一倍写入，内存充足时建议内存缓存。
      暂存文件名由 :func:`_scratch_name` 按目标**完整路径**哈希生成，跨目录唯一。
    * ``preallocate_dst=True``：先按总大小**预填充一段（尽量连续）的簇**，再流式覆盖
      （「预先填充 → 流式覆盖」，实现前端→后端整体直搬；落点 LBA 由文件系统在空闲区
      分配，预分配保证连续性）。

    Returns:
        ``(写入目的字节数, 块数)``。
    """
    if mode == CACHE_MODE_MEMORY:
        return memory_copy(
            src_path, dst_path,
            buffer_blocks=buffer_blocks,
            bytes_per_cluster=bytes_per_cluster,
            block_clusters=block_clusters,
            preallocate_dst=preallocate_dst,
        )
    if mode == CACHE_MODE_DISK:
        if not cache_dir:
            cache_dir = default_scratch_dir(src_path)   # 1.0.0D 方案 D：同卷默认暂存
        os.makedirs(cache_dir, exist_ok=True)
        # D-004：暂存名按**目标完整路径**哈希，跨目录唯一（不再撞同名目标）
        scratch = os.path.join(cache_dir, _scratch_name(dst_path))
        return staged_copy(
            src_path, scratch, dst_path,
            buffer_blocks=buffer_blocks,
            bytes_per_cluster=bytes_per_cluster,
            block_clusters=block_clusters,
            preallocate_dst=preallocate_dst,
        )
    raise ValueError(f"未知缓存模式：{mode!r}（可选 {CACHE_MODE_MEMORY!r} / {CACHE_MODE_DISK!r}）")


# ---------------------------------------------------------------- 回站删除
_FO_DELETE = 3
_FOF_ALLOWUNDO = 0x40
_FOF_NOCONFIRMATION = 0x10
_FOF_SILENT = 0x4
_FOF_NOERRORUI = 0x400


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", ctypes.c_uint),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", wintypes.LPVOID),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


_shell32 = ctypes.windll.shell32
_shell32.SHFileOperationW.argtypes = [ctypes.POINTER(_SHFILEOPSTRUCTW)]
_shell32.SHFileOperationW.restype = ctypes.c_int


def recycle(path: str) -> bool:
    """把文件移到回收站（可恢复），而非永久删除。返回是否成功。"""
    op = _SHFILEOPSTRUCTW()
    op.wFunc = _FO_DELETE
    op.pFrom = f"{path}\0\0"   # pFrom 需以双空字符结尾
    op.fFlags = _FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_SILENT | _FOF_NOERRORUI
    return _shell32.SHFileOperationW(ctypes.byref(op)) == 0 and not op.fAnyOperationsAborted
