"""``FSCTL_MOVE_FILE`` (0x00090074) —— 项目中**唯一**的写操作出口。

``MOVE_FILE_DATA`` 结构（x64）::

    HANDLE        FileHandle;      // 偏移  0，8 字节
    LARGE_INTEGER StartingVcn;     // 偏移  8，8 字节
    LARGE_INTEGER StartingLcn;     // 偏移 16，8 字节
    DWORD         ClusterCount;    // 偏移 24，4 字节
    // 尾部 4 字节对齐填充 —— sizeof == 32

默认对齐（无 ``_pack_``）即可得到正确的 32 字节；这里显式断言，防止未来改动破坏布局。

v2 新增：
* :func:`execute_task` —— 带 ``SafetyGuard`` / ``SafetyPolicy`` / ``MoveJournal`` 的主入口；
* :func:`prewarm_file` —— 只读预热（``FILE_FLAG_SEQUENTIAL_SCAN`` + 大块顺序读）；
* :func:`rollback_segments` —— 逆序反向搬运（**手动触发**，不自动执行）。

**安全不变量**：``FSCTL_MOVE_FILE`` 只有在 ``guard.allow()`` 返回 True 时才会被调用。
``guard`` 为 ``None`` 时退化为 ``validate_target``（v1 行为）；两者都为空则**拒绝执行**，
因为那意味着没有任何目标校验。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Callable, List, Optional

from app.core.guard import SafetyGuard
from app.core.journal import MoveJournal, default_journal_path
from app.core.models import Extent, MoveFailure, MoveOutcome, MoveSegment, MoveTask
from app.core.safety import SafetyPolicy, SafetyViolation, assert_target_allowed
from app.win32 import api


class MOVE_FILE_DATA(ctypes.Structure):
    """``MOVE_FILE_DATA`` 的 ctypes 映射。"""

    _fields_ = [
        ("FileHandle", wintypes.HANDLE),
        ("StartingVcn", ctypes.c_int64),
        ("StartingLcn", ctypes.c_int64),
        ("ClusterCount", wintypes.DWORD),
    ]


EXPECTED_STRUCT_SIZE = 32

if ctypes.sizeof(MOVE_FILE_DATA) != EXPECTED_STRUCT_SIZE:  # pragma: no cover
    raise RuntimeError(
        f"MOVE_FILE_DATA 布局异常：期望 {EXPECTED_STRUCT_SIZE} 字节，"
        f"实际 {ctypes.sizeof(MOVE_FILE_DATA)} 字节"
    )


class MoveError(RuntimeError):
    """安全校验未通过导致的拒绝移动。"""


# --------------------------------------------------------------------------- #
# 单次 FSCTL
# --------------------------------------------------------------------------- #
def move_clusters(
    volume_handle: int,
    file_handle: int,
    starting_vcn: int,
    starting_lcn: int,
    cluster_count: int,
) -> None:
    """执行一次簇搬运（**真实写盘操作**）。

    Args:
        volume_handle: 以 ``GENERIC_READ | GENERIC_WRITE`` 打开的卷句柄。
        file_handle: 以 ``FILE_READ_ATTRIBUTES`` 打开的文件句柄。
        starting_vcn: 源区间起始 VCN。
        starting_lcn: 目标区间起始 LCN。
        cluster_count: 移动的簇数。

    Raises:
        MoveError: 参数不合法。
        Win32Error: ``DeviceIoControl`` 失败（错误码与文本会完整保留）。
    """
    if cluster_count <= 0:
        raise MoveError("ClusterCount 必须大于 0")
    if starting_vcn < 0 or starting_lcn < 0:
        raise MoveError("StartingVcn / StartingLcn 不能为负")

    data = MOVE_FILE_DATA()
    data.FileHandle = file_handle
    data.StartingVcn = int(starting_vcn)
    data.StartingLcn = int(starting_lcn)
    data.ClusterCount = int(cluster_count)

    api.device_io_control(
        volume_handle,
        api.FSCTL_MOVE_FILE,
        in_bytes=bytes(data),
        out_size=0,
        context=f"移动簇 VCN={starting_vcn} LCN={starting_lcn} 数量={cluster_count}",
    )


def open_file_for_move(path: str) -> int:
    """以移动所需的最小权限打开文件。"""
    return api.open_handle(
        path,
        api.FILE_READ_ATTRIBUTES,
        share=api.FILE_SHARE_READ | api.FILE_SHARE_WRITE | api.FILE_SHARE_DELETE,
        flags=api.FILE_ATTRIBUTE_NORMAL,
        context="打开待移动文件",
    )


# --------------------------------------------------------------------------- #
# 段执行（v1 入口，关键字参数向后兼容扩展）
# --------------------------------------------------------------------------- #
def execute_segments(
    volume_handle: int,
    path: str,
    segments: List[MoveSegment],
    validate_target: Optional[Callable[[int, int], bool]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    on_progress: Optional[Callable[[int, int, int], None]] = None,
    *,
    policy: Optional[SafetyPolicy] = None,
    journal: Optional[MoveJournal] = None,
    phase: str = "place",
    guard: Optional[SafetyGuard] = None,
) -> MoveOutcome:
    """按 extent 逐段搬运一个文件。

    Args:
        volume_handle: 卷句柄（写权限）。
        path: 文件完整路径。
        segments: 待搬运段落。
        validate_target: 回调 ``(lcn, clusters) -> bool``。与 ``guard`` 同时给时以
            ``guard`` 为准（v2 语义）。
        should_cancel: 段间检查取消。
        on_progress: ``(clusters_done, clusters_total, segment_index)``。
        policy: 规划期安全策略；给了就在**每次** FSCTL 前再做一次
            ``assert_target_allowed``（双保险）。
        journal: 操作流水（每段一行，逐行 flush）。
        phase: 段所属阶段（``place`` / ``evict`` / ``relay_out`` / ``relay_in`` /
            ``rollback``），写进流水。
        guard: 执行期守卫（第二道校验）。**为 None 且无 validate_target 时拒绝执行**。

    Returns:
        :class:`MoveOutcome`，失败项带完整 Win32 错误信息。
    """
    outcome = MoveOutcome()
    total = sum(seg.clusters for seg in segments)
    done = 0

    if not segments:
        return outcome
    if guard is None and validate_target is None:
        outcome.failures.append(
            MoveFailure(
                path=path,
                winerror=api.ERROR_INVALID_PARAMETER,
                message="拒绝执行：既未提供 SafetyGuard 也未提供目标校验回调",
            )
        )
        return outcome

    file_handle: Optional[int] = None
    try:
        file_handle = open_file_for_move(path)
    except api.Win32Error as exc:
        outcome.failures.append(
            MoveFailure(
                path=path,
                winerror=exc.winerror,
                message=f"打开文件失败：{api.format_error(exc.winerror)}",
            )
        )
        return outcome

    try:
        for index, segment in enumerate(segments):
            if should_cancel is not None and should_cancel():
                outcome.cancelled = True
                break

            allowed, reason = _check_target(segment, guard, validate_target)
            if allowed and policy is not None:
                try:
                    assert_target_allowed(
                        policy, segment.dst_lcn, segment.clusters, "执行"
                    )
                except SafetyViolation as exc:
                    allowed, reason = False, str(exc)

            if not allowed:
                outcome.failures.append(
                    MoveFailure(
                        path=path,
                        src_lcn=segment.src_lcn,
                        dst_lcn=segment.dst_lcn,
                        clusters=segment.clusters,
                        winerror=api.ERROR_INVALID_PARAMETER,
                        message=reason or "目标区间未通过安全校验，已跳过该段",
                    )
                )
                _record(journal, phase, path, segment, False, api.ERROR_INVALID_PARAMETER, reason)
                done += segment.clusters
                if on_progress is not None:
                    on_progress(done, total, index)
                continue

            try:
                move_clusters(
                    volume_handle,
                    file_handle,
                    segment.vcn,
                    segment.dst_lcn,
                    segment.clusters,
                )
            except (api.Win32Error, MoveError) as exc:
                winerror = int(getattr(exc, "winerror", api.ERROR_INVALID_PARAMETER))
                message = f"FSCTL_MOVE_FILE 失败：{api.format_error(winerror)}"
                outcome.failures.append(
                    MoveFailure(
                        path=path,
                        src_lcn=segment.src_lcn,
                        dst_lcn=segment.dst_lcn,
                        clusters=segment.clusters,
                        winerror=winerror,
                        message=message,
                    )
                )
                _record(journal, phase, path, segment, False, winerror, message)
                done += segment.clusters
                if on_progress is not None:
                    on_progress(done, total, index)
                continue

            # 成功：逐段立即登记占用（防止后续段重复落到同一处）
            if guard is not None:
                guard.reserve(segment.dst_lcn, segment.clusters)
            _record(journal, phase, path, segment, True, 0, "")
            done += segment.clusters
            outcome.moved_clusters += segment.clusters
            if on_progress is not None:
                on_progress(done, total, index)
    finally:
        api.close_handle(file_handle)

    if not outcome.failures and not outcome.cancelled:
        outcome.moved_files = 1
    return outcome


def _check_target(
    segment: MoveSegment,
    guard: Optional[SafetyGuard],
    validate_target: Optional[Callable[[int, int], bool]],
) -> tuple:
    """统一的目标校验入口：``guard`` 优先。"""
    if guard is not None:
        return guard.allow(segment.dst_lcn, segment.clusters)
    if validate_target is not None:
        ok = validate_target(segment.dst_lcn, segment.clusters)
        return ok, "" if ok else "目标区间未通过校验"
    return False, "缺少目标校验"


def _record(
    journal: Optional[MoveJournal],
    phase: str,
    path: str,
    segment: MoveSegment,
    ok: bool,
    winerror: int,
    message: str,
) -> None:
    """写一条流水（journal 为空则跳过）。"""
    if journal is None:
        return
    journal.record(
        phase=phase,
        path=path,
        vcn=segment.vcn,
        src_lcn=segment.src_lcn,
        dst_lcn=segment.dst_lcn,
        clusters=segment.clusters,
        ok=ok,
        winerror=winerror,
        message=message,
    )


def execute_task(
    volume_handle: int,
    task: MoveTask,
    guard: SafetyGuard,
    policy: Optional[SafetyPolicy] = None,
    journal: Optional[MoveJournal] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    on_progress: Optional[Callable[[int, int, int], None]] = None,
) -> MoveOutcome:
    """v2 主入口：执行一个 :class:`MoveTask`（内部就是 ``execute_segments``）。

    ★ **路径未知的条目必须在这里拦下**（设计 T04 验收第 8 条）：MFT 通道重建
    路径失败的文件 ``path`` 形如 ``"(record 12345)"``，``CreateFile`` 必然失败；
    即便它混进了计划，也绝不允许进入写流程。
    """
    if not task.path_known or not task.path or task.path.startswith("(record "):
        outcome = MoveOutcome(cancelled=False)
        outcome.failures.append(
            MoveFailure(
                path=task.path or "(空路径)",
                winerror=api.ERROR_INVALID_PARAMETER,
                message="拒绝执行：路径未知（MFT 重建失败），无法打开文件句柄",
            )
        )
        return outcome
    return execute_segments(
        volume_handle,
        task.path,
        task.segments,
        None,
        should_cancel,
        on_progress,
        policy=policy,
        journal=journal,
        phase=task.phase,
        guard=guard,
    )


def rollback_segments(
    volume_handle: int,
    path: str,
    segments: List[MoveSegment],
    guard: Optional[SafetyGuard] = None,
    policy: Optional[SafetyPolicy] = None,
    journal: Optional[MoveJournal] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    on_progress: Optional[Callable[[int, int, int], None]] = None,
) -> MoveOutcome:
    """执行一组**已互换 src↔dst** 的回滚段（**手动触发**，不自动执行）。

    ``segments`` 由 :meth:`app.core.journal.MoveJournal.rollback_segments` 产出：
    其中 ``src_lcn`` 是文件现在的位置（原来的目标），``dst_lcn`` 是原位置。

    注意：``guard`` 必须基于**回滚前重新读取的位图**构造，否则"原 src 现在空闲"
    这一事实不在旧快照里，会被守卫拒绝（这是正确行为，不是 bug）。
    """
    flipped: List[MoveSegment] = [
        MoveSegment(
            vcn=segment.vcn,
            src_lcn=segment.src_lcn,
            dst_lcn=segment.dst_lcn,
            clusters=segment.clusters,
        )
        for segment in segments
    ]
    return execute_segments(
        volume_handle,
        path,
        flipped,
        None,
        should_cancel,
        on_progress,
        policy=policy,
        journal=journal,
        phase="rollback",
        guard=guard,
    )


# --------------------------------------------------------------------------- #
# 预热（只读）
# --------------------------------------------------------------------------- #
def prewarm_file(
    path: str,
    limit_bytes: int,
    chunk_bytes: int,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> int:
    """用 ``FILE_FLAG_SEQUENTIAL_SCAN`` + 大块顺序读把源数据预热进系统缓存。

    * **只读**，绝不写；读完的字节立即丢弃（不驻留 Python 内存）；
    * 每次 read 之间检查 ``should_cancel``；
    * 返回实际预热的字节数；任何打开/读取错误都返回 0（不阻塞搬运）。

    Args:
        path: 文件路径。
        limit_bytes: 预热上限（字节）。
        chunk_bytes: 单次 read 大小。
        should_cancel: 取消检查。
    """
    if limit_bytes <= 0 or chunk_bytes <= 0:
        return 0
    handle: Optional[int] = None
    try:
        handle = api.open_handle(
            path,
            api.GENERIC_READ,
            share=api.FILE_SHARE_READ | api.FILE_SHARE_WRITE | api.FILE_SHARE_DELETE,
            flags=api.FILE_ATTRIBUTE_NORMAL | api.FILE_FLAG_SEQUENTIAL_SCAN,
            context="打开文件（预热）",
        )
    except (api.Win32Error, OSError):
        return 0

    total = 0
    try:
        while total < limit_bytes:
            if should_cancel is not None and should_cancel():
                break
            block = min(int(chunk_bytes), int(limit_bytes) - total)
            if block <= 0:
                break
            try:
                data = api.read_at(handle, total, block, "预热读取")
            except (api.Win32Error, OSError):
                break
            if not data:
                break
            total += len(data)
            del data
    finally:
        api.close_handle(handle)
    return total


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def is_metadata_file(name: str) -> bool:
    """判断是否为 NTFS 元数据文件（``$`` 开头，不可移动）。"""
    return name.startswith("$")


def describe_extents(extents: List[Extent]) -> str:
    """调试用：把 extents 拼成可读字符串。"""
    parts = [f"VCN{e.vcn}+{e.clusters}->LCN{e.lcn}" for e in extents if not e.is_sparse]
    return ", ".join(parts) if parts else "无分配"


def journal_path_for(letter: str) -> str:
    """给 UI 用的默认流水路径（字符串形式）。"""
    return str(default_journal_path(letter))
