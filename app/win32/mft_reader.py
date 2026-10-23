"""N5 · ``$MFT`` 原始读取（**严格只读**，全项目仅有的两处原始读点之一）。

职责边界：本模块只做「按段表把 ``$MFT`` 的逻辑字节流读出来」，字节怎么解释
全部交给 :mod:`app.core.mft`。

只读保证（设计 §4.6.1）：

* 只有读操作（ReadFile / SetFilePointerEx），**没有任何写操作码**
  （验收里用 grep 断言三个写符号零命中）；
* 与写出口模块**零依赖**：本模块的 import 链上没有它
  （``import app.core.mft`` 后 ``sys.modules`` 里不含那个模块）；
* 卷句柄只请求读权限（``GENERIC_READ``），绝不请求写权限。

越界处理（设计 §4.6.2）：所有原始读都过 :class:`~app.core.mft.MftSpanGuard`，
``(offset, length)`` **先扇区对齐再校验**；越界抛 ``MftBoundsError`` = 致命
= 整条通道作废 → 由调用方明示回退句柄通道（绝不静默）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from app.core.mft import (
    FALLBACK_BOUNDS_VIOLATION,
    FALLBACK_NOT_NTFS,
    FALLBACK_NO_PRIVILEGE,
    FALLBACK_RUNLIST_UNAVAILABLE,
    FALLBACK_SAMPLE_MISMATCH,
    FALLBACK_STRUCTURE_INVALID,
    MAX_RAW_READ_BYTES,
    METADATA_RECORD_MAX,
    MftBoundsError,
    MftParseError,
    MftRecord,
    MftScanStats,
    MftSpanGuard,
    MftStream,
    align_up,
    compare_extents,
    parse_record,
    rebuild_path,
    record_to_entry,
    sample_pick,
)
from app.core.models import Extent, FileEntry, VolumeInfo
from app.win32 import api, retrieval

#: MFT 通道判为"结构异常"的阈值（设计 §4.9：magic/fixup 累计超阈值）
STRUCTURE_INVALID_MIN: int = 8
STRUCTURE_INVALID_RATE: float = 0.05


class MftReadError(RuntimeError):
    """MFT 原始读取失败（无法定位 / 无法读取 / 逻辑流有空洞）。"""


@dataclass(slots=True)
class MftScanOutcome:
    """一次 MFT 通道扫描的全部产出（**纯数据**，便于单测与信号传递）。"""

    entries: List[FileEntry] = field(default_factory=list)
    stats: MftScanStats = field(default_factory=MftScanStats)
    stream: Optional[MftStream] = None
    channel: str = "mft"
    #: 非空 = 本通道不可用，调用方应改用句柄通道（原因码见 ``mft.FALLBACK_*``）
    fallback_code: str = ""
    notes: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.fallback_code

    @property
    def total_extents(self) -> int:
        return sum(len(e.allocated_extents) for e in self.entries)


# --------------------------------------------------------------------------- #
# 第一步：取 $MFT 自身 runlist（D6 选定方案）
# --------------------------------------------------------------------------- #
def _sector_of(info: VolumeInfo) -> int:
    """扇区大小（信息缺失时退回 512，绝不抛）。"""
    return int(info.bytes_per_sector) if info.bytes_per_sector else 512


def open_mft_stream(letter: str, info: VolumeInfo) -> Tuple[MftStream, List[str]]:
    """取 ``$MFT`` 自身 runlist 并组装逻辑流；失败返回 ``(空流, 原因列表)``。

    **取法（D6 拍板）**：``CreateFile("X:\\$MFT")`` + ``FSCTL_GET_RETRIEVAL_POINTERS``。
    该路径在个别系统 / 安全软件策略下会被拒绝（``ERROR_ACCESS_DENIED``）——
    此时**整体回退句柄通道**并如实说明，不去实现更底层的引导扇区直解析。
    """
    problems: List[str] = []
    cleaned = letter.upper().rstrip(":")
    if not cleaned:
        return MftStream(), ["未指定盘符"]
    if not info.is_ntfs:
        return MftStream(), ["卷不是 NTFS，无法直读 $MFT"]

    bytes_per_cluster = int(info.bytes_per_cluster)
    record_size = int(info.bytes_per_file_record)
    if bytes_per_cluster <= 0 or record_size <= 0:
        return MftStream(), ["卷信息不完整（簇大小 / MFT 记录大小未知），需管理员权限"]

    extents, reason = retrieval.get_extents_for_path(f"{cleaned}:\\$MFT")
    if reason or not extents:
        # 方案 B：``X:\$MFT`` 在非提升进程 / 安全软件策略下会被拒绝（错误码 5）。
        # 此时改用**卷设备句柄 + 已知的 MftStartLcn** 读记录 0 拿它自己的 runlist
        # —— 只读、不解析引导扇区、不做任何猜测。两条都不行才整体回退句柄通道。
        problems.append(f"无法通过 $MFT 文件句柄读取布局（{reason or '无物理分配'}）")
        fallback_extents, fallback_problems = _read_self_runlist_from_volume(cleaned, info)
        problems.extend(fallback_problems)
        if not fallback_extents:
            return MftStream(), problems
        extents = fallback_extents
        problems.append("已改用卷设备句柄读取 $MFT 记录 0 的 runlist（只读）")

    stream = MftStream(
        extents=sorted(extents, key=lambda e: e.vcn),
        valid_length=0,
        record_size=record_size,
        bytes_per_cluster=bytes_per_cluster,
        bytes_per_sector=_sector_of(info),
    )
    # 逻辑流覆盖范围：$MFT 的分配字节数 vs MftValidDataLength，取小者（不越过真实分配）
    covered = stream.byte_length
    requested = int(info.mft_valid_data_length)
    if requested <= 0:
        requested = int(info.total_clusters) * bytes_per_cluster
        problems.append("MftValidDataLength 缺失，已退化为按 $MFT 分配范围扫描")
    stream.valid_length = min(requested, covered)
    if stream.valid_length <= 0:
        problems.append("$MFT 逻辑流长度为 0，无法解析")
        return MftStream(), problems
    if stream.valid_length % record_size:
        problems.append(
            f"MFT 有效长度 {stream.valid_length:,} B 不是记录大小 {record_size} B 的整数倍，"
            "已向下取整（尾部不完整记录不解析）"
        )
    return stream, problems


def _read_self_runlist_from_volume(
    letter: str, info: VolumeInfo
) -> Tuple[List[Extent], List[str]]:
    """方案 B：从卷设备句柄读 ``$MFT`` 的**记录 0**，解析它自己的 ``$DATA`` runlist。

    只用 ``NtfsVolumeData`` 里的 ``MftStartLcn`` 定位，**不解析引导扇区**；
    读的字节数就是一条记录，绝不越界。
    """
    problems: List[str] = []
    bytes_per_cluster = int(info.bytes_per_cluster)
    record_size = int(info.bytes_per_file_record)
    start_lcn = int(info.mft_start_lcn)
    if bytes_per_cluster <= 0 or record_size <= 0 or start_lcn <= 0:
        return [], ["卷信息不足，无法定位 $MFT 记录 0"]

    offset = start_lcn * bytes_per_cluster
    volume_bytes = int(info.total_clusters) * bytes_per_cluster
    if offset < 0 or offset + record_size > volume_bytes:
        return [], ["$MFT 记录 0 的偏移超出卷范围"]

    handle = 0
    try:
        handle = api.open_handle(
            f"\\\\.\\{letter}:",
            api.GENERIC_READ,
            share=api.FILE_SHARE_READ | api.FILE_SHARE_WRITE,
            flags=0,
            context="打开卷设备（读取 $MFT 记录 0）",
        )
        raw = api.read_at(handle, offset, record_size, "$MFT 记录 0")
    except api.Win32Error as exc:
        return [], [f"读取 $MFT 记录 0 失败：{exc}"]
    finally:
        api.close_handle(handle)

    record = parse_record(
        raw,
        0,
        record_size,
        None,
        sector_size=_sector_of(info),
        bytes_per_cluster=bytes_per_cluster,
    )
    if record is None:
        return [], ["$MFT 记录 0 解析失败（fixup / 结构非法）"]
    if record.incomplete or not record.extents:
        return [], ["$MFT 记录 0 的 runlist 不完整"]
    extents = [
        Extent(vcn=e.vcn, lcn=e.lcn, clusters=e.clusters)
        for e in record.extents
        if e.lcn >= 0 and e.clusters > 0
    ]
    if not extents:
        return [], ["$MFT 记录 0 没有可用的物理段"]
    return extents, problems


# --------------------------------------------------------------------------- #
# 第二步：唯一原始读入口
# --------------------------------------------------------------------------- #
class MftReader:
    """在卷句柄上按 :class:`MftStream` 做**扇区对齐**的顺序读（只读，永不写）。

    所有读都经 :meth:`read`，其中：

    1. ``guard.check(offset, length)`` —— 内部先做扇区对齐再用对齐区间校验
       （易错点：用原始区间校验会被"为对齐多读的扇区"绕过）；
    2. 按段表拆成"落在单个 extent 内"的物理段逐段 ``ReadFile``；
    3. 读回后裁掉对齐补读的前后缀。
    """

    def __init__(
        self,
        handle: int,
        stream: MftStream,
        guard: MftSpanGuard,
        buffer_bytes: int = 1 << 20,
    ) -> None:
        self._handle = int(handle)
        self._stream = stream
        self._guard = guard
        self._buffer_bytes = max(4096, min(int(buffer_bytes), MAX_RAW_READ_BYTES))
        self.bytes_read = 0

    # ------------------------------------------------------------------ #
    @property
    def stream(self) -> MftStream:
        return self._stream

    @property
    def guard(self) -> MftSpanGuard:
        return self._guard

    def read(self, offset: int, length: int) -> bytes:
        """读逻辑字节流 ``[offset, offset+length)``。

        Raises:
            MftBoundsError: 越界（致命，调用方必须中止通道）。
            MftReadError: 逻辑流有空洞 / 物理读失败 / 读不满。
        """
        if length <= 0:
            return b""
        # ★ 先校验（内部用对齐区间），再按对齐区间读
        self._guard.check(offset, length)
        aligned_offset, aligned_length = self._guard.normalize(offset, length)
        try:
            segments = self._stream.locate(aligned_offset, aligned_length)
        except MftParseError as exc:
            raise MftReadError(str(exc)) from exc
        if not segments:
            raise MftReadError(f"$MFT 逻辑流未覆盖 [{aligned_offset:,} + {aligned_length:,})")

        chunks: List[bytes] = []
        for _logical, physical, seg_length in segments:
            # ★ 逻辑偏移合法 ≠ 物理偏移合法：段表 lcn 若指向卷尾之外必须拦下
            self._guard.check_physical(physical, seg_length)
            try:
                data = api.read_at(self._handle, physical, seg_length, "$MFT 读取")
            except api.Win32Error as exc:
                raise MftReadError(f"$MFT 读取失败：{exc}") from exc
            if len(data) != seg_length:
                raise MftReadError(
                    f"$MFT 读取不完整（期望 {seg_length:,} 实际 {len(data):,}）"
                )
            self.bytes_read += len(data)
            chunks.append(data)

        blob = b"".join(chunks)
        if len(blob) != aligned_length:
            raise MftReadError(
                f"$MFT 逻辑流读取长度不符（期望 {aligned_length:,} 实际 {len(blob):,}）"
            )
        head = offset - aligned_offset
        return blob[head : head + length]

    def iter_records(
        self, start: int = 0, end: int = -1, batch: int = 512
    ) -> Iterator[Tuple[int, bytes]]:
        """按批迭代 ``[start, end)`` 记录，产出 ``(记录号, 记录原始字节)``。

        ★ ``end`` 超出生效范围时**抛** :class:`MftBoundsError`（不返回空、不截断）
        —— 这是设计 §4.6.2 与 R17 的硬要求：截断会导致后续记录全部错位。
        """
        record_size = self._stream.record_size
        if record_size <= 0:
            raise MftReadError("MFT 记录大小非法（0）")
        first = max(0, int(start))
        last = int(end) if int(end) >= 0 else self._stream.record_count
        if last <= first:
            return
        max_per_read = max(1, MAX_RAW_READ_BYTES // record_size)
        step = max(1, min(int(batch), max_per_read))
        # 有效范围内的记录先按批读满；越界部分再单独尝试一次 —— 这样"第 N 条越界"
        # 会在**已经产出 N-1 条**之后才抛错（不是整批作废，也不是静默截断）
        safe_end = min(last, self._stream.record_count)
        number = first
        while number < last:
            if number < safe_end:
                count = min(step, safe_end - number)
            else:
                count = min(step, last - number)
            blob = self.read(number * record_size, count * record_size)
            for index in range(count):
                begin = index * record_size
                yield number + index, blob[begin : begin + record_size]
            number += count

    def close(self) -> None:
        """兼容接口：句柄由调用方负责关闭（RAII 在 :func:`scan_volume_mft`）。"""
        return

    def __enter__(self) -> "MftReader":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


# --------------------------------------------------------------------------- #
# 第三步：扫描 + 解析 + 抽样复核
# --------------------------------------------------------------------------- #
def _build_guard(info: VolumeInfo, stream: MftStream) -> MftSpanGuard:
    """构造边界守卫（``mft_valid_bytes`` 向上对齐到扇区）。"""
    sector = _sector_of(info)
    volume_bytes = int(info.total_clusters) * int(info.bytes_per_cluster)
    if volume_bytes <= 0:
        volume_bytes = int(stream.byte_length)
    return MftSpanGuard(
        volume_bytes=volume_bytes,
        mft_valid_bytes=align_up(stream.valid_length, sector),
        record_size=stream.record_size,
        sector_size=sector,
    )


def _structure_invalid(stats: MftScanStats) -> bool:
    """结构异常是否已超阈值（magic / fixup / 属性头全炸）。"""
    broken = stats.bad_magic + stats.bad_fixup + stats.bad_attribute
    if not broken:
        return False
    threshold = max(STRUCTURE_INVALID_MIN, int(stats.in_use_records * STRUCTURE_INVALID_RATE))
    return broken > threshold


def _verify_sample(
    entries: List[FileEntry],
    stats: MftScanStats,
    sample_size: int,
    retrieve: Callable[[str], Tuple[List[Extent], str]],
    notes: List[str],
) -> bool:
    """抽样复核（D7：默认 32 个，每次扫描都做）。

    Returns:
        ``True`` = 可以继续使用 MFT 结果；``False`` = 整体降级（不一致率 > 5%）。

    不一致率 ``0 < r ≤ 5%`` 时做**文件级降级**：把不一致文件的 extents 换成
    句柄通道结果并标 ``source="handle"``，其余仍用 MFT 结果（设计 §4.8）。
    """
    if sample_size <= 0:
        notes.append("抽样复核：未做（用户关闭），结果可信度未验证")
        return True

    picks = sample_pick(entries, sample_size)
    if not picks:
        notes.append("抽样复核：没有可核对的文件（跳过）")
        return True

    mismatched: List[FileEntry] = []
    unverifiable = 0
    for entry in picks:
        extents, reason = retrieve(entry.path)
        if reason or not extents:
            unverifiable += 1
            continue
        stats.sampled += 1
        if not compare_extents(entry.extents, extents):
            stats.sample_mismatch += 1
            mismatched.append(entry)

    if stats.sampled == 0:
        notes.append(
            f"抽样复核：{len(picks)} 个样本均无法用句柄核对"
            f"（{unverifiable} 个打开失败），结果可信度未验证"
        )
        return True

    if stats.mismatch_rate > 0.05:
        notes.append(
            f"抽样复核失败：{stats.sample_mismatch}/{stats.sampled} 不一致"
            f"（{stats.mismatch_rate:.0%} > 5%），MFT 解析不可信"
        )
        return False

    if mismatched:
        downgraded = 0
        for entry in mismatched:
            extents, reason = retrieve(entry.path)
            if reason or not extents:
                entry.movable = False
                entry.status = "不可移动"
                entry.reason = f"MFT 与句柄结果不一致且无法复核（{reason}）"
                continue
            entry.extents = extents
            entry.source = "handle"
            downgraded += 1
        notes.append(
            f"抽样复核：{stats.sampled - stats.sample_mismatch}/{stats.sampled} 一致，"
            f"{downgraded} 个不一致文件已改用句柄通道复核"
        )
    else:
        notes.append(
            f"抽样复核：{stats.sampled}/{stats.sampled} 一致（按 (vcn, lcn, clusters) 三元组比对）"
        )
    if unverifiable:
        notes.append(f"抽样复核：{unverifiable} 个样本无法核对（路径不可达 / 打开失败）")
    return True


def scan_volume_mft(
    info: VolumeInfo,
    *,
    sample_size: int = 32,
    should_cancel: Optional[Callable[[], bool]] = None,
    on_progress: Optional[Callable[[int, int], None]] = None,
    retrieve: Optional[Callable[[str], Tuple[List[Extent], str]]] = None,
) -> MftScanOutcome:
    """直读 ``$MFT`` 扫描整个卷（**只读**），产出文件表 + 健康度快照。

    Args:
        info: 卷信息（必须含簇大小 / 记录大小 / MftValidDataLength）。
        sample_size: 抽样复核数量（D7 默认 32；0 = 关闭，但必须显式标注）。
        should_cancel: 取消回调（每批记录检查一次；取消时**保留已解析结果**）。
        on_progress: ``(已解析记录数, 总记录数)``。
        retrieve: 抽样用的 extents 读取函数（默认 ``retrieval.get_extents_for_path``，
            便于单测注入）。

    Returns:
        :class:`MftScanOutcome`。``fallback_code`` 非空表示必须改用句柄通道，
        **绝不静默**：原因同时写进 ``notes`` 与 ``stats.fatal_error``。
    """
    resolve = retrieve or retrieval.get_extents_for_path
    outcome = MftScanOutcome()
    stats = outcome.stats
    started = time.perf_counter()

    # ---- 0. 卷类型前置判断（FIX-8：原因码与 ``build_scan_channel`` 统一）----
    # 同一个 exFAT 卷，早期这里报 ``mft_runlist_unavailable``（文案"卷不是 NTFS"），
    # 而 ``scanner.build_scan_channel`` 报 ``not_ntfs`` —— UI 按码分支会漏掉一半。
    if not info.is_ntfs:
        outcome.fallback_code = FALLBACK_NOT_NTFS
        stats.fatal_error = "当前卷不是 NTFS，无法直读 $MFT"
        outcome.channel = "handle"
        outcome.notes.append(stats.fatal_error)
        stats.elapsed_seconds = time.perf_counter() - started
        return outcome

    # ---- 1. 定位 $MFT ----
    stream, problems = open_mft_stream(info.letter, info)
    outcome.notes.extend(problems)
    stats.record_size = stream.record_size
    if not stream.extents or stream.valid_length <= 0:
        outcome.fallback_code = FALLBACK_RUNLIST_UNAVAILABLE
        stats.fatal_error = "；".join(problems) or "无法定位 $MFT"
        outcome.channel = "handle"
        stats.elapsed_seconds = time.perf_counter() - started
        return outcome
    outcome.stream = stream
    outcome.notes.insert(0, stream.describe())

    handle = 0
    try:
        # ---- 2. 打开卷（★ 只读：GENERIC_READ）----
        try:
            handle = api.open_handle(
                f"\\\\.\\{info.letter}:",
                api.GENERIC_READ,
                share=api.FILE_SHARE_READ | api.FILE_SHARE_WRITE,
                flags=0,
                context="打开卷设备（直读 MFT）",
            )
        except api.Win32Error as exc:
            outcome.fallback_code = FALLBACK_NO_PRIVILEGE
            stats.fatal_error = f"无法打开卷设备：{exc}"
            outcome.channel = "handle"
            return outcome

        guard = _build_guard(info, stream)
        reader = MftReader(handle, stream, guard, buffer_bytes=MAX_RAW_READ_BYTES)
        total_records = stream.record_count

        names: Dict[int, Tuple[int, str]] = {}
        pending: List[MftRecord] = []

        # ---- 3. 逐记录解析（只有 MftBoundsError 允许向上抛）----
        try:
            for number, raw in reader.iter_records(0, total_records):
                if should_cancel is not None and (number % 512 == 0) and should_cancel():
                    outcome.notes.append("扫描已取消（结果不完整）")
                    break
                stats.total_records += 1
                try:
                    record = parse_record(
                        raw,
                        number,
                        stream.record_size,
                        stats,
                        sector_size=guard.sector,
                        bytes_per_cluster=stream.bytes_per_cluster,
                    )
                except MftBoundsError:
                    raise                      # ★ 唯一向上抛的异常
                except Exception as exc:       # noqa: BLE001 —— 逐记录隔离，绝不崩溃
                    stats.read_error += 1
                    outcome.notes.append(
                        f"记录 {number} 解析异常：{type(exc).__name__}: {exc}"
                    )
                    continue
                if record is None:
                    continue
                if not record.in_use:
                    stats.skipped_not_in_use += 1
                    continue
                stats.in_use_records += 1
                if record.record_no <= METADATA_RECORD_MAX:
                    stats.skipped_metadata += 1
                    # $Root(5) 的名字也进路径表（严格来说不需要，但便于诊断）
                    if record.name:
                        names[record.record_no] = (record.parent_ref, record.name)
                    continue
                if record.base_record_ref != 0:
                    stats.skipped_extension += 1
                    continue
                if record.name:
                    names[record.record_no] = (record.parent_ref, record.name)
                if record.is_directory:
                    stats.directory_records += 1
                    continue
                pending.append(record)
                if on_progress is not None and stats.total_records % 2048 == 0:
                    on_progress(stats.total_records, total_records)
        except MftBoundsError as exc:
            outcome.fallback_code = FALLBACK_BOUNDS_VIOLATION
            stats.fatal_error = str(exc)
            outcome.channel = "handle"
            outcome.notes.append(f"原始读越界，已中止 MFT 通道：{exc}")
            return outcome

        # ---- 4. 重建路径 → FileEntry ----
        root_prefix = (info.root or f"{info.letter}:\\").rstrip("\\")
        entries: List[FileEntry] = []
        for record in pending:
            relative, path_known = rebuild_path(record.record_no, names)
            if path_known:
                path = f"{root_prefix}\\{relative}" if relative else root_prefix + "\\"
            else:
                path = relative                       # "(record N)"
            if not path_known:
                stats.path_unknown += 1
            if record.resident:
                stats.resident_data += 1
            if record.incomplete:
                stats.attr_list_skipped += 1
            entry = record_to_entry(record, path, path_known)
            if entry is None:
                continue
            entries.append(entry)

        stats.file_records = len(entries)
        outcome.entries = entries

        # ---- 4.5 属性列表降级的**文件级**句柄回退（设计 §4.6.5 / 验收 9②）----
        # ★ 触发条件是**结构化**的 `entry.incomplete`，不是 `reason` 的字符串匹配。
        # 早期实现写的是 `if "属性列表" not in entry.reason: continue`，而真盘上
        # 一批条目的 reason 是空串（`movable=True, extents=[]`），于是永远进不来
        # 兜底分支。改判 `incomplete` 后，这些条目要么被补齐 extents，要么被明确
        # 标成不可移动 —— 都能被看见（D9），不再有"静默假成功"。
        recovered = 0
        gave_up = 0
        for entry in entries:
            if entry.movable or not entry.path_known:
                continue
            if not entry.incomplete:
                continue
            extents, reason = resolve(entry.path)
            if reason or not extents:
                gave_up += 1
                entry.incomplete = False
                entry.reason = (
                    (entry.reason + "；") if entry.reason else ""
                ) + f"属性列表跨记录且句柄复核失败（{reason or '无物理分配'}），未猜测"
                entry.status = "不可移动"
                continue
            entry.extents = extents
            if entry.size <= 0:
                # 基础记录里没有 $DATA 时 size 也是 0；用句柄通道拿到的分配量补齐，
                # 否则 UI 会显示"0 B"，而规划层看到的却是 N 簇（两个口径打架）。
                bpc = stream.bytes_per_cluster or int(info.bytes_per_cluster) or 4096
                entry.size = sum(int(e.clusters) for e in extents if e.lcn >= 0) * bpc
            # 保留唯一的"信息性"提示：含 ADS 的集群未计入逐段比对
            note = "含 ADS，未计入" if "ADS" in entry.reason else ""
            entry.reason = note
            entry.movable = True
            entry.status = "待迁移"
            entry.source = "handle"
            entry.incomplete = False
            recovered += 1
        if recovered or gave_up:
            outcome.notes.append(
                f"属性列表降级：{recovered} 个文件用句柄通道补齐了 extents，"
                f"{gave_up} 个补齐失败（已标为不可移动，未猜测）"
            )

        # ---- 5. 结构异常阈值 ----
        if _structure_invalid(stats):
            outcome.fallback_code = FALLBACK_STRUCTURE_INVALID
            stats.fatal_error = (
                f"MFT 结构不符合预期（magic {stats.bad_magic}，fixup {stats.bad_fixup}，"
                f"属性头 {stats.bad_attribute}）"
            )
            outcome.channel = "handle"
            outcome.notes.append(stats.fatal_error)
            return outcome

        # ---- 6. 抽样复核（D7：每次扫描都做）----
        if not _verify_sample(entries, stats, sample_size, resolve, outcome.notes):
            outcome.fallback_code = FALLBACK_SAMPLE_MISMATCH
            stats.fatal_error = (
                f"MFT 解析可信度不足（{stats.sample_mismatch}/{stats.sampled} 不一致）"
            )
            outcome.channel = "handle"
            return outcome

        outcome.notes.append(stats.summary_line())
        return outcome
    finally:
        api.close_handle(handle)
        stats.elapsed_seconds = time.perf_counter() - started
