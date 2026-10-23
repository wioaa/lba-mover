"""后台线程：位图加载、大文件扫描、全卷归属索引、迁移规划、真实移动。

分层约定：本模块只做「调用顺序 + 信号」，不含算法（算法在 ``packer`` /
``planner`` / ``scheduler``）；UI 只连信号。

⚠ 写操作模块 ``app.win32.mover`` 只在 :meth:`MoveWorker.run` 内部按需导入，
保证「预览模式」这条代码路径上根本不存在 ``FSCTL_MOVE_FILE`` 的调用点。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple

from PySide6.QtCore import QThread, Signal

from app.core import densify, planner
from app.core.guard import SafetyGuard
from app.core.io_profile import IoProfile
from app.core.journal import MoveJournal
from app.core.models import (
    MEDIA_UNKNOWN,
    BitmapSnapshot,
    FileEntry,
    MoveFailure,
    MoveOutcome,
    MovePlan,
    PlanFailure,
    VolumeInfo,
)
from app.core.ownership import OwnershipIndex
from app.core.safety import SafetyPolicy, subtract_forbidden
from app.core.shadow import ShadowBitmap
from app.win32 import api, bitmap as bitmap_mod, retrieval, volume

# 扫描时永久跳过的系统目录（无用户数据 + 极易触发权限问题）
SKIP_DIR_NAMES = frozenset(
    {
        "$recycle.bin",
        "system volume information",
        "$extend",
        "$winreagent",
        "config.msi",
        "$sysreset",
        "recovery",
        "windows.old",
    }
)

FILE_ATTRIBUTE_REPARSE_POINT = 0x400

#: 全卷索引的 extent 上限（400 万条 ≈ 60 MB，超出则拒绝并提示）
MAX_INDEX_EXTENTS = 4_000_000

#: 连续失败多少段视为致命错误（设计 §3.7）
MAX_CONSECUTIVE_FAILURES = 8

#: 执行期局部核验的余量（簇）。每段执行前只重读
#: ``[min(区间) - margin, max(区间) + margin)`` 这一片位图，
#: 4096 簇 ≈ 16 MB（4K 簇），足够覆盖对齐误差与相邻簇的边界效应。
SEGMENT_VERIFY_MARGIN = 4096

#: 抽样复核默认规模（D7：默认 32，每次扫描都做；候选不足则全量复核）
DEFAULT_SAMPLE_SIZE = 32

#: 通道耗时记录（用于"本卷 MFT 收益有限"的诚实建议，不为了测速而重复扫全卷）
_CHANNEL_TIMINGS: dict = {}


def build_scan_channel(
    info: VolumeInfo, prefer_mft: bool = True, runtime_reason: str = ""
) -> Tuple[str, str]:
    """扫描通道决策（**纯函数**，可单测）。

    Args:
        info: 卷信息。
        prefer_mft: 用户是否允许 MFT 快速通道（UI 的「强制句柄通道」开关关掉即 ``False``）。
        runtime_reason: 运行期已知的回退原因码（越界 / 抽样不一致 / 结构异常），
            由调用方在重扫时透传，便于统一文案。

    Returns:
        ``(channel, reason_code)``。``channel == "mft"`` 时 ``reason_code == ""``。
        6 种不可用情形都会返回 ``"handle"`` + 对应原因码（设计 §4.9），
        **不做静默退化**。
    """
    from app.core import mft as mft_mod

    if runtime_reason:
        return "handle", runtime_reason
    if not prefer_mft:
        return "handle", mft_mod.FALLBACK_USER_PREFERRED_HANDLE
    if info is None or not info.is_ntfs:
        return "handle", mft_mod.FALLBACK_NOT_NTFS
    # 用「卷信息是否真的读到了」判断权限，比 is_admin() 更准确：
    # 某些卷（VHD / 可移动介质）在非提升进程里也能以只读方式打开卷设备。
    if info.total_clusters <= 0 or info.bytes_per_cluster <= 0 or info.volume_error:
        return "handle", mft_mod.FALLBACK_NO_PRIVILEGE
    if info.bytes_per_file_record <= 0 or info.mft_valid_data_length <= 0:
        return "handle", mft_mod.FALLBACK_RUNLIST_UNAVAILABLE
    return "mft", ""


def note_channel_timing(letter: str, channel: str, seconds: float) -> Optional[str]:
    """记录一次通道耗时；两种通道都有数据且比值 < 2 时给出**诚实建议**。

    设计 §4.10 要求"若 < 2×，必须在日志给出'本卷 MFT 收益有限'的建议，不许沉默"。
    我们不为了测速而强制跑两遍全卷 —— 只有用户两种通道都用过之后才有数据可比。
    """
    if seconds <= 0:
        return None
    timings = _CHANNEL_TIMINGS.setdefault(letter.upper().rstrip(":"), {})
    timings[channel] = float(seconds)
    mft_seconds = timings.get("mft")
    handle_seconds = timings.get("handle")
    if not mft_seconds or not handle_seconds:
        return None
    ratio = handle_seconds / mft_seconds
    if ratio < 2.0:
        return (
            f"本卷 MFT 通道收益有限（句柄 {handle_seconds:.1f} s / MFT {mft_seconds:.1f} s"
            f"，仅 {ratio:.1f}×），可考虑切回句柄扫描"
        )
    return f"MFT 通道为句柄通道的 {ratio:.1f}×（{mft_seconds:.1f} s vs {handle_seconds:.1f} s）"


@dataclass(slots=True)
class ScanConfig:
    """扫描参数。"""

    root: str = "C:\\"
    min_size: int = 256 * 1024 * 1024
    max_matches: int = 4000
    skip_dirs: frozenset = field(default_factory=lambda: SKIP_DIR_NAMES)


# --------------------------------------------------------------------------- #
# 位图加载
# --------------------------------------------------------------------------- #
class BitmapWorker(QThread):
    """读取卷位图（只读，安全）。"""

    progress = Signal(int, int)          # 已完成簇, 总簇
    ready = Signal(object)               # BitmapSnapshot
    failed = Signal(str)

    def __init__(self, info: VolumeInfo, parent=None) -> None:
        super().__init__(parent)
        self._info = info
        self._cancel = False

    def request_cancel(self) -> None:
        """请求取消（在读块之间生效）。"""
        self._cancel = True

    def run(self) -> None:  # noqa: D102 - QThread 钩子
        handle = volume.VolumeHandle(self._info.letter)
        try:
            handle.open()
            snapshot = bitmap_mod.read_volume_bitmap(
                handle.handle,
                self._info.total_clusters,
                progress=lambda done, total: self.progress.emit(done, total),
            )
            snapshot.bytes_per_cluster = self._info.bytes_per_cluster
            if self._cancel:
                self.failed.emit("位图读取已取消")
                return
            self.ready.emit(snapshot)
        except api.Win32Error as exc:
            self.failed.emit(f"读取卷位图失败：{exc}")
        except (RuntimeError, ValueError) as exc:
            self.failed.emit(f"读取卷位图失败：{exc}")
        finally:
            handle.close()


# --------------------------------------------------------------------------- #
# 可行性评估（1.0.0D：移出 UI 线程，**只读**）
# --------------------------------------------------------------------------- #
class FeasibilityWorker(QThread):
    """在后台线程跑 :func:`packer.assess`（纯函数，只读）。

    * 输入里的 ``bits`` 是**不可变**的 ``bytes`` 位图快照 —— 跨线程共享无
      可变状态；``selected`` / ``ownership`` 在评估期间只被读取；
    * ``request_cancel`` 在重活开始前生效（评估本身是单次纯计算，
      中途不可打断 —— 所以取消语义是「丢弃结果」，由调用方按代次比对）；
    * 结果带 ``seq`` 代次：UI 侧参数若已变化则直接丢弃并按最新参数重投。
    """

    completed = Signal(int, object)      # (seq, FeasibilityReport)
    failed = Signal(int, str)            # (seq, message)

    def __init__(
        self,
        seq: int,
        bits: bytes,
        window: Tuple[int, int],
        allowed: List[Tuple[int, int]],
        selected: List[FileEntry],
        ownership,
        excluded: Tuple[str, ...],
        bytes_per_cluster: int,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._seq = int(seq)
        self._bits = bits
        self._window = (int(window[0]), int(window[1]))
        self._allowed = list(allowed)
        self._selected = list(selected)
        self._ownership = ownership
        self._excluded = tuple(excluded)
        self._bytes_per_cluster = int(bytes_per_cluster)
        self._cancel = False

    def request_cancel(self) -> None:
        """请求取消（在重活开始前生效；之后的结果由 UI 按代次丢弃）。"""
        self._cancel = True

    def run(self) -> None:  # noqa: D102 - QThread 钩子
        if self._cancel:
            return
        try:
            # 按需导入：位图切片与评估都是只读路径，与写出口无任何关系
            from app.core import packer
            from app.core.models import TargetWindow

            start, end = self._window
            free_runs = bitmap_mod.free_runs_in_window(self._bits, start, end)
            if self._cancel:
                return
            # 区间内被占用的簇 = 区间 − 空闲
            occupied_runs = _subtract_in_window(free_runs, start, end)
            report = packer.assess(
                self._selected,
                TargetWindow(start, end),
                self._allowed,
                free_runs,
                self._ownership,
                occupied_runs,
                self._excluded,
                self._bytes_per_cluster,
            )
            if self._cancel:
                return
            self.completed.emit(self._seq, report)
        except Exception as exc:  # noqa: BLE001 - 评估失败不能让 UI 崩
            self.failed.emit(self._seq, f"可行性评估失败：{type(exc).__name__}: {exc}")


def _subtract_in_window(
    free_runs: List[Tuple[int, int]], start: int, end: int
) -> List[Tuple[int, int]]:
    """区间 − 空闲 = 占用（``free_runs`` 已限制在区间内）。"""
    occupied: List[Tuple[int, int]] = []
    cursor = start
    for run_start, run_end in free_runs:
        if run_start > cursor:
            occupied.append((cursor, min(run_start, end)))
        cursor = max(cursor, run_end)
        if cursor >= end:
            break
    if cursor < end:
        occupied.append((cursor, end))
    return [(s, e) for s, e in occupied if e > s]


# --------------------------------------------------------------------------- #
# 大文件扫描（阈值模式，v1 行为）
# --------------------------------------------------------------------------- #
class ScanWorker(QThread):
    """递归扫描卷上的大文件并解析其物理 extents。

    ``mode="handle"``（默认）与 v1 **逐行等价**；``mode="mft"`` 走 N5 直读通道
    （需要 ``info``）。MFT 通道不可用时自动回退句柄通道，并在进度文案里明示原因。
    """

    progress = Signal(int, int, str)     # 已扫描文件数, 已命中数, 当前路径
    found = Signal(object)               # FileEntry（增量推送）
    completed = Signal(object)           # list[FileEntry]
    failed = Signal(str)

    def __init__(
        self,
        config: ScanConfig,
        parent=None,
        *,
        info: Optional[VolumeInfo] = None,
        mode: str = "handle",
        sample_size: int = DEFAULT_SAMPLE_SIZE,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._info = info
        self._mode = mode
        self._sample_size = sample_size
        self._cancel = False

    def request_cancel(self) -> None:
        """请求取消扫描。"""
        self._cancel = True

    def run(self) -> None:  # noqa: D102 - QThread 钩子
        if self._mode == "mft" and self._info is not None:
            if self._run_mft_scan():
                return
            # 回退是自动的，但必须明示（设计 R14 / §4.9）
            self.progress.emit(0, 0, "MFT 通道不可用，已自动改用句柄扫描")
        self._run_handle_scan()

    # ------------------------------------------------------------------ #
    def _run_mft_scan(self) -> bool:
        """MFT 直读通道；成功返回 ``True``，需要回退返回 ``False``。"""
        from app.core import mft as mft_mod
        from app.win32 import mft_reader

        channel, reason = build_scan_channel(self._info, True)  # type: ignore[arg-type]
        if channel != "mft":
            self.progress.emit(0, 0, mft_mod.fallback_message(reason))
            return False
        try:
            outcome = mft_reader.scan_volume_mft(
                self._info,  # type: ignore[arg-type]
                sample_size=self._sample_size,
                should_cancel=lambda: self._cancel,
                on_progress=lambda done, total: self.progress.emit(
                    done, total, "解析 MFT 记录"
                ),
            )
        except Exception as exc:  # noqa: BLE001 - 兜底：绝不静默
            self.progress.emit(0, 0, f"MFT 通道异常（{type(exc).__name__}: {exc}），已回退")
            return False
        if not outcome.ok:
            self.progress.emit(
                0, 0, mft_mod.fallback_message(outcome.fallback_code, outcome.stats.fatal_error)
            )
            return False

        cfg = self._config
        results = [e for e in outcome.entries if e.movable and e.size >= cfg.min_size]
        results = results[: cfg.max_matches]
        results.sort(key=lambda e: e.size, reverse=True)
        for entry in results:
            self.found.emit(entry)
        self.progress.emit(len(outcome.entries), len(results), "MFT 扫描完成")
        self.completed.emit(results)
        return True

    def _run_handle_scan(self) -> None:
        """v1 句柄通道（**逐行保留**，任何优化都不得改变其行为）。"""
        cfg = self._config
        results: List[FileEntry] = []
        scanned = 0
        last_emit = 0.0
        stack: List[str] = [cfg.root]

        try:
            while stack:
                if self._cancel:
                    self.completed.emit(results)
                    return
                current_dir = stack.pop()
                try:
                    with os.scandir(current_dir) as iterator:
                        for item in iterator:
                            if self._cancel:
                                self.completed.emit(results)
                                return
                            try:
                                if item.is_dir(follow_symlinks=False):
                                    if self._should_skip_dir(item):
                                        continue
                                    stack.append(item.path)
                                    continue
                                if not item.is_file(follow_symlinks=False):
                                    continue
                                stat_result = item.stat(follow_symlinks=False)
                            except (OSError, PermissionError):
                                continue

                            if self._is_reparse(stat_result):
                                continue
                            scanned += 1
                            size = int(stat_result.st_size)
                            if size < cfg.min_size:
                                continue
                            if len(results) >= cfg.max_matches:
                                continue

                            entry = self._build_entry(item.path, size)
                            results.append(entry)
                            self.found.emit(entry)

                            now = time.monotonic()
                            if now - last_emit > 0.12:
                                last_emit = now
                                self.progress.emit(scanned, len(results), item.path)
                except (OSError, PermissionError):
                    continue

                self.progress.emit(scanned, len(results), current_dir)

            self.progress.emit(scanned, len(results), "扫描完成")
            results.sort(key=lambda e: e.size, reverse=True)
            self.completed.emit(results)
        except Exception as exc:  # noqa: BLE001 - 兜底，绝不静默失败
            self.failed.emit(f"扫描异常终止：{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------ #
    def _should_skip_dir(self, item: os.DirEntry) -> bool:
        lowered = item.name.lower()
        if lowered in self._config.skip_dirs:
            return True
        try:
            stat_result = item.stat(follow_symlinks=False)
        except OSError:
            return True
        return self._is_reparse(stat_result)

    @staticmethod
    def _is_reparse(stat_result: os.stat_result) -> bool:
        attributes = getattr(stat_result, "st_file_attributes", 0)
        return bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT)

    @staticmethod
    def _build_entry(path: str, size: int) -> FileEntry:
        """构造条目：前置识别压缩 / EFS（扫描阶段即标"不可移动"）。"""
        name = os.path.basename(path) or path
        entry = FileEntry(path=path, name=name, size=size)
        extents, attributes, reason = retrieval.get_basic_info(path)
        entry.extents = extents
        entry.attributes = attributes
        if reason:
            entry.movable = False
            entry.reason = reason
            entry.status = "不可移动"
        elif name.startswith("$"):
            entry.movable = False
            entry.reason = "NTFS 元数据文件，禁止移动"
            entry.status = "不可移动"
        return entry


# --------------------------------------------------------------------------- #
# 全卷归属索引（D2：MFT 通道一次遍历 = 文件表 + 归属索引）
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class IndexPayload:
    """``IndexWorker`` 的输出（D2 的两样东西 + 诊断信息）。"""

    entries: List[FileEntry] = field(default_factory=list)
    index: Optional[OwnershipIndex] = None
    channel: str = "handle"          # mft | handle
    total_files: int = 0
    total_extents: int = 0
    elapsed: float = 0.0
    notes: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.index is not None


class IndexWorker(QThread):
    """建立「全卷文件表 + LCN→文件 归属索引」。

    **通道策略（D2 合并结论）**

    * 优先走 **MFT 直读**（``app.win32.mft_reader``）：一次遍历同时产出文件表与
      归属索引，挡路者归属**零额外成本**；实测 75 GB 卷 ≈ 0.25 s；
    * MFT 通道不可用（非管理员 / 非 NTFS / 解析质量过低）时回退到**句柄通道**
      （逐文件 ``CreateFile`` + retrieval pointers），此时才有 20 s~2 min 的代价，
      UI 应在触发前征求用户同意。
    """

    progress = Signal(int, int, str)     # 已完成, 总数, 当前项
    ready = Signal(object)               # IndexPayload
    failed = Signal(str)

    def __init__(
        self,
        info: VolumeInfo,
        config: Optional[ScanConfig] = None,
        parent=None,
        *,
        channel: str = "auto",
        sample_size: int = DEFAULT_SAMPLE_SIZE,
    ) -> None:
        super().__init__(parent)
        self._info = info
        self._sample_size = sample_size
        self._config = config or ScanConfig(
            root=info.root or f"{info.letter}:\\", min_size=1, max_matches=2_000_000
        )
        self._channel = channel
        self._cancel = False

    def request_cancel(self) -> None:
        self._cancel = True

    # ------------------------------------------------------------------ #
    def run(self) -> None:  # noqa: D102 - QThread 钩子
        started = time.perf_counter()
        payload = IndexPayload()

        if self._channel in ("auto", "mft"):
            payload = self._run_mft()
            if payload.ok:
                payload.elapsed = time.perf_counter() - started
                self.ready.emit(payload)
                return

        if self._channel == "mft":
            self.failed.emit("MFT 通道不可用：" + "；".join(payload.notes or ["未知原因"]))
            return

        fallback = self._run_handle()
        fallback.notes = list(payload.notes) + list(fallback.notes)
        fallback.elapsed = time.perf_counter() - started
        if fallback.ok:
            self.ready.emit(fallback)
        else:
            self.failed.emit("；".join(fallback.notes or ["索引建立失败"]))

    # ------------------------------------------------------------------ #
    def _run_mft(self) -> IndexPayload:
        """MFT 直读通道：文件表 + 归属索引**一次遍历**产出（D2）。

        这就是 D2 的合并实现：MFT 通道成功时，归属索引是**零额外成本的副产品**，
        不存在"再花 20 s~2 min 单独扫一遍"的问题。
        """
        from app.core import mft as mft_mod
        from app.win32 import mft_reader

        payload = IndexPayload(channel="mft")
        channel, reason = build_scan_channel(self._info, True)
        if channel != "mft":
            # ★ 回退必须留痕：原因码 + 人话都进 notes（UI 写日志/徽章都靠它）
            payload.notes.append(
                f"[回退] {reason}：{mft_mod.fallback_message(reason)}"
            )
            payload.channel = "handle"
            return payload

        outcome = mft_reader.scan_volume_mft(
            self._info,
            sample_size=self._sample_size,
            should_cancel=lambda: self._cancel,
            on_progress=lambda done, total: self.progress.emit(
                done, total, "解析 MFT 记录"
            ),
        )
        payload.elapsed = outcome.stats.elapsed_seconds
        payload.notes.extend(outcome.notes)

        if not outcome.ok:
            payload.notes.insert(
                0,
                f"[回退] {outcome.fallback_code}："
                + mft_mod.fallback_message(
                    outcome.fallback_code, outcome.stats.fatal_error
                ),
            )
            payload.channel = "handle"
            return payload

        entries = outcome.entries
        if not entries:
            payload.notes.append("MFT 通道未解析出任何文件（异常卷或空卷）")
            return payload
        if outcome.total_extents > MAX_INDEX_EXTENTS:
            payload.notes.append(
                f"extent 数量 {outcome.total_extents:,} 超出上限 "
                f"{MAX_INDEX_EXTENTS:,}，已中止并使用句柄通道"
            )
            payload.channel = "handle"
            return payload

        payload.entries = entries
        payload.index = OwnershipIndex.build(entries)
        payload.total_files = len(entries)
        payload.total_extents = payload.index.extent_count
        payload.notes.append(
            f"MFT 通道一次遍历即产出 文件表 {len(entries):,} + 归属索引 "
            f"{payload.index.extent_count:,} 段（挡路者归属零额外成本）"
        )
        advice = note_channel_timing(self._info.letter, "mft", payload.elapsed)
        if advice:
            payload.notes.append(advice)
        return payload

    def _run_handle(self) -> IndexPayload:
        """句柄通道：逐文件 CreateFile + retrieval pointers（慢，但无需 MFT 权限）。"""
        payload = IndexPayload(channel="handle")
        cfg = self._config
        entries: List[FileEntry] = []
        scanned = 0
        last_emit = 0.0
        stack: List[str] = [cfg.root]

        while stack:
            if self._cancel:
                payload.notes.append("已取消")
                break
            current_dir = stack.pop()
            try:
                with os.scandir(current_dir) as iterator:
                    for item in iterator:
                        if self._cancel:
                            break
                        try:
                            if item.is_dir(follow_symlinks=False):
                                if item.name.lower() in SKIP_DIR_NAMES:
                                    continue
                                stat_result = item.stat(follow_symlinks=False)
                                if getattr(stat_result, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT:
                                    continue
                                stack.append(item.path)
                                continue
                            if not item.is_file(follow_symlinks=False):
                                continue
                            stat_result = item.stat(follow_symlinks=False)
                        except (OSError, PermissionError):
                            continue
                        if getattr(stat_result, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT:
                            continue
                        scanned += 1
                        size = int(stat_result.st_size)
                        if size < cfg.min_size:
                            continue
                        name = item.name
                        if name.startswith("$"):
                            continue
                        extents, attributes, reason = retrieval.get_basic_info(item.path)
                        if reason or not extents:
                            continue
                        entry = FileEntry(
                            path=item.path,
                            name=name,
                            size=size,
                            extents=extents,
                            attributes=attributes,
                        )
                        entries.append(entry)
                        now = time.monotonic()
                        if now - last_emit > 0.15:
                            last_emit = now
                            self.progress.emit(scanned, len(entries), item.path)
            except (OSError, PermissionError):
                continue
            self.progress.emit(scanned, len(entries), current_dir)

        payload.entries = entries
        payload.index = OwnershipIndex.build(entries)
        payload.total_files = len(entries)
        payload.total_extents = payload.index.extent_count
        payload.notes.append(
            f"句柄通道：扫描 {scanned:,} 个文件，索引 {len(entries):,} 个文件 / "
            f"{payload.index.extent_count:,} 段"
        )
        return payload


# --------------------------------------------------------------------------- #
# N5 · MFT 快速扫描（独立 worker，信号语义固定，见 R18）
# --------------------------------------------------------------------------- #
class MftScanWorker(QThread):
    """直读 ``$MFT`` 扫描整个卷（**只读**），产出文件表 + 健康度统计。

    **信号语义固定（R18）**：

    * ``fallback(str)`` = **可恢复**，携带回退原因码，主窗口据此自动重启句柄通道；
    * ``failed(str)`` = **真失败**（不可恢复），不要反复重试。

    两者绝不能混用 —— 否则用户看到"扫描失败"而不是"已自动回退"。
    """

    progress = Signal(int, int, str)     # 已解析记录数, 总记录数, 当前阶段
    found = Signal(object)               # FileEntry（增量推送）
    stats = Signal(object)               # MftScanStats（健康度快照）
    completed = Signal(object)           # list[FileEntry]
    fallback = Signal(str)               # 回退原因码 —— 可恢复
    failed = Signal(str)                 # 真失败

    def __init__(
        self,
        info: VolumeInfo,
        config: Optional[ScanConfig] = None,
        parent=None,
        *,
        sample_size: int = DEFAULT_SAMPLE_SIZE,
        should_fallback: Optional[Callable[[str], bool]] = None,
        prefer_mft: bool = True,
    ) -> None:
        super().__init__(parent)
        self._info = info
        self._config = config or ScanConfig(
            root=info.root or f"{info.letter}:\\", min_size=1, max_matches=2_000_000
        )
        self._sample_size = sample_size
        self._should_fallback = should_fallback
        self._prefer_mft = prefer_mft
        self._cancel = False

    def request_cancel(self) -> None:
        """请求取消（MFT 扫描每 512 条记录检查一次；取消也发 completed）。"""
        self._cancel = True

    def run(self) -> None:  # noqa: D102 - QThread 钩子
        from app.core import mft as mft_mod
        from app.win32 import mft_reader

        channel, reason = build_scan_channel(self._info, self._prefer_mft)
        if channel != "mft":
            self._emit_fallback(reason)
            return

        try:
            outcome = mft_reader.scan_volume_mft(
                self._info,
                sample_size=self._sample_size,
                should_cancel=lambda: self._cancel,
                on_progress=lambda done, total: self.progress.emit(
                    done, total, "解析 MFT 记录"
                ),
            )
        except Exception as exc:  # noqa: BLE001 - 兜底：绝不静默失败
            self.failed.emit(f"MFT 扫描异常终止：{type(exc).__name__}: {exc}")
            return

        self.stats.emit(outcome.stats)
        if not outcome.ok:
            self._emit_fallback(outcome.fallback_code, outcome.stats.fatal_error)
            return

        cfg = self._config
        entries = [e for e in outcome.entries if e.size >= cfg.min_size][: cfg.max_matches]
        for entry in entries:
            self.found.emit(entry)
        advice = note_channel_timing(
            self._info.letter, "mft", outcome.stats.elapsed_seconds
        )
        if advice:
            self.progress.emit(len(outcome.entries), len(entries), advice)
        self.progress.emit(len(outcome.entries), len(entries), outcome.stats.summary_line())
        self.completed.emit(entries)

    # ------------------------------------------------------------------ #
    def _emit_fallback(self, code: str, detail: str = "") -> None:
        """发 ``fallback`` 还是 ``failed``：由 ``should_fallback`` 回调决定。"""
        from app.core import mft as mft_mod

        message = mft_mod.fallback_message(code, detail)
        if self._should_fallback is not None and not self._should_fallback(code):
            self.failed.emit(f"{message}（自动回退已被禁止，请手动选择句柄扫描）")
            return
        self.fallback.emit(code or mft_mod.FALLBACK_NO_PRIVILEGE)


# --------------------------------------------------------------------------- #
# 规划
# --------------------------------------------------------------------------- #
class PlanWorker(QThread):
    """在后台计算迁移方案（``mode="tail"`` 走 v1，``mode="range"`` 走 v2）。"""

    completed = Signal(object)           # MovePlan
    failed = Signal(str)

    def __init__(
        self,
        info: VolumeInfo,
        snapshot: BitmapSnapshot,
        entries: Sequence[FileEntry],
        reserve_clusters: int = 0,
        parent=None,
        *,
        window=None,
        safety: Optional[SafetyPolicy] = None,
        io_profile: Optional[IoProfile] = None,
        ownership: Optional[OwnershipIndex] = None,
        excluded_paths: Sequence[str] = (),
        mode: str = "tail",
    ) -> None:
        super().__init__(parent)
        self._info = info
        self._snapshot = snapshot
        self._entries = list(entries)
        self._reserve = int(reserve_clusters)
        self._window = window
        self._safety = safety
        self._io_profile = io_profile
        self._ownership = ownership
        self._excluded = tuple(excluded_paths)
        self._mode = mode
        self._cancel = False

    def request_cancel(self) -> None:
        self._cancel = True

    # ------------------------------------------------------------------ #
    def run(self) -> None:  # noqa: D102 - QThread 钩子
        try:
            if self._mode == "range":
                plan = self._plan_range()
            elif self._mode == "auto_fill":
                plan = self._plan_auto_fill()
            elif self._mode == "compact":
                plan = self._plan_compact()
            elif self._mode == "zone":
                plan = self._plan_zone()
            else:
                plan = self._plan_tail()
            if self._cancel:
                return
            self.completed.emit(plan)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"生成迁移计划失败：{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------ #
    def _policy(self) -> SafetyPolicy:
        if self._safety is not None:
            return self._safety
        return SafetyPolicy(
            total_clusters=self._info.total_clusters,
            bytes_per_cluster=self._info.bytes_per_cluster,
            mft_zone_start=self._info.mft_zone_start,
            mft_zone_end=self._info.mft_zone_end,
            mft_start_lcn=self._info.mft_start_lcn,
            mft2_start_lcn=self._info.mft2_start_lcn,
        )

    def _plan_tail(self) -> MovePlan:
        """v1 末端模式（输出与 v1 逐字段一致）。"""
        total = self._info.total_clusters
        floor = planner.tail_floor_lcn(total, self._reserve)
        runs = bitmap_mod.free_runs_in_window(self._snapshot.bits, floor, total)
        params = planner.PlanParams(
            total_clusters=total,
            bytes_per_cluster=self._info.bytes_per_cluster,
            reserve_clusters=self._reserve,
            free_runs=runs,
            mft_zone_start=self._info.mft_zone_start,
            mft_zone_end=self._info.mft_zone_end,
        )
        return planner.build_plan(self._entries, params)

    def _plan_range(self) -> MovePlan:
        """v2 目标区间模式。"""
        total = self._info.total_clusters
        policy = self._policy()
        window = self._window or planner.tail_window_for(
            total, self._reserve, policy.tail_reserve_clusters
        )
        free_all = bitmap_mod.free_runs_in_window(self._snapshot.bits, 0, total)
        free_all = subtract_forbidden(free_all, policy.forbidden_runs())
        occupied = self._complement(free_all, total)
        io_profile = self._io_profile or IoProfile.build(
            MEDIA_UNKNOWN, 256, self._info.bytes_per_cluster or 4096,
            window_clusters=window.clusters or None,
        )
        params = planner.RangePlanParams(
            total_clusters=total,
            bytes_per_cluster=self._info.bytes_per_cluster,
            window=window,
            policy=policy,
            free_runs=free_all,
            occupied_runs=occupied,
            ownership=self._ownership,
            excluded_paths=self._excluded,
            io_profile=io_profile,
        )
        return planner.build_range_plan(self._entries, params)

    # ---- 1.0.0C 密实化策略（densify）----
    def _plan_auto_fill(self) -> MovePlan:
        """策略①：全池自动挑选文件填入末端预留区空闲（大文件优先、放不下跳过）。"""
        total = self._info.total_clusters
        runs = bitmap_mod.free_runs_in_window(self._snapshot.bits, 0, total)
        params = planner.PlanParams(
            total_clusters=total,
            bytes_per_cluster=self._info.bytes_per_cluster,
            reserve_clusters=self._reserve,
            free_runs=runs,
            mft_zone_start=self._info.mft_zone_start,
            mft_zone_end=self._info.mft_zone_end,
        )
        return densify.auto_fill_tail(self._entries, params)

    def _plan_compact(self) -> MovePlan:
        """策略②：选定区间预密实化（区间内可移动文件密集重排、coalesce 空闲）。"""
        if self._window is None:
            raise ValueError("区间预密实化需要先设定目标区间")
        total = self._info.total_clusters
        free_all = bitmap_mod.free_runs_in_window(self._snapshot.bits, 0, total)
        return densify.compact_range(
            self._entries,
            (self._window.start_lcn, self._window.end_lcn),
            free_all,
            total,
            self._info.mft_zone_start,
            self._info.mft_zone_end,
        )

    def _plan_zone(self) -> MovePlan:
        """策略③：按文件大小把目标区间切成尺寸区段装箱。"""
        if self._window is None:
            raise ValueError("按大小分区需要先设定目标区间")
        return densify.zone_plan(
            self._entries,
            (self._window.start_lcn, self._window.end_lcn),
            total_clusters=self._info.total_clusters,
        )

    @staticmethod
    def _complement(runs: Sequence[Tuple[int, int]], total: int) -> List[Tuple[int, int]]:
        """空闲区间的补集 = 占用区间。"""
        from app.core.ranges import subtract_runs

        return subtract_runs([(0, total)], runs)


# --------------------------------------------------------------------------- #
# 真实移动
# --------------------------------------------------------------------------- #
class MoveWorker(QThread):
    """真实执行 ``FSCTL_MOVE_FILE``（危险操作）。

    v2 变更（对照 v1）：

    * 遍历对象从 ``plan.movable_items`` 变为 ``plan.tasks``（R3：漏改会导致驱逐
      任务不执行且**静默成功**）；
    * 启动前做**卷身份校验**（序列号 + 总簇数），不一致直接拒绝且零写调用；
    * 每段前用 ``SafetyGuard`` 二次校验，``allow() == False`` 时**不调用** FSCTL；
    * 逐段写 JSONL 流水；可选预读预热；支持 ``on_error`` 继续/停止。

    四个信号契约保持不变：``progress(int,int,str)`` / ``file_result(str,bool,str)``
    / ``completed(object)`` / ``failed(str)``。
    """

    progress = Signal(int, int, str)      # 已完成簇, 总簇, 当前文件
    file_result = Signal(str, bool, str)  # 路径, 是否成功, 说明
    completed = Signal(object)            # MoveOutcome
    failed = Signal(str)

    def __init__(
        self,
        info: VolumeInfo,
        snapshot: BitmapSnapshot,
        plan: MovePlan,
        parent=None,
        *,
        safety: Optional[SafetyPolicy] = None,
        io_profile: Optional[IoProfile] = None,
        journal: Optional[MoveJournal] = None,
        on_error: str = "continue",
        mode: str = "execute",
    ) -> None:
        super().__init__(parent)
        self._info = info
        self._snapshot = snapshot
        self._plan = plan
        self._safety = safety
        self._io_profile = io_profile
        self._journal = journal
        self._on_error = on_error if on_error in ("continue", "halt") else "continue"
        self._mode = mode
        self._cancel = False

    def request_cancel(self) -> None:
        self._cancel = True

    # ------------------------------------------------------------------ #
    def _policy(self) -> SafetyPolicy:
        if self._safety is not None:
            return self._safety
        from app.core.models import MovePlan as _MovePlan  # noqa: F401 - 只为类型清晰

        plan_policy = getattr(self._plan, "safety", None)
        if isinstance(plan_policy, SafetyPolicy):
            return plan_policy
        # 最保守策略：total_clusters = 0 ⇒ is_range_allowed 恒 False ⇒ 拒绝一切目标
        return SafetyPolicy()

    def _tasks(self) -> List[object]:
        """执行队列表：优先 ``plan.tasks``，否则用 ``items`` 合成（R3 降级路径）。"""
        if self._plan.tasks:
            return list(self._plan.tasks)
        from app.core.scheduler import build_tasks

        return build_tasks(self._plan.items, self._plan.evictions)

    def _verify_volume_identity(self) -> str:
        """重读卷身份，返回错误信息（空串表示一致）。

        ★ 序列号必须与规划期**同源比对**：``self._info.serial`` 来自
        ``GetVolumeInformation``（32 位 DWORD，见 ``volume.read_volume_info``）。
        早期实现改用 ``FSCTL_GET_NTFS_VOLUME_DATA`` 的 64 位
        ``VolumeSerialNumber`` 直接比全值，两种 API 的表示 / 宽度不同，会把
        **同一卷**误报成「序列号已变化」而阻断执行。故执行期也用
        ``GetVolumeInformation`` 取序列号；总簇数继续用 FSCTL（规划期
        ``total_clusters`` 即来自 FSCTL，两边同源）。
        """
        handle = volume.VolumeHandle(self._info.letter)
        try:
            handle.open()
            data = handle.ntfs_volume_data()
        except (api.Win32Error, ValueError) as exc:
            return f"无法重读卷结构以校验身份：{exc}"
        finally:
            handle.close()

        current_serial = volume.read_volume_serial(self._info.letter)
        expected_serial = int(self._info.serial)
        expected_clusters = int(self._info.total_clusters)
        if (
            expected_serial
            and current_serial is not None
            and (current_serial & 0xFFFFFFFF) != (expected_serial & 0xFFFFFFFF)
        ):
            return (
                f"卷序列号已变化（计划 {expected_serial:#x} → 现在 "
                f"{current_serial:#x}），拒绝执行"
            )
        if expected_clusters and int(data.total_clusters) != expected_clusters:
            return (
                f"卷总簇数已变化（计划 {expected_clusters:,} → 现在 "
                f"{int(data.total_clusters):,}），拒绝执行"
            )
        return ""

    # ------------------------------------------------------------------ #
    def _load_shadow(self) -> Tuple[Optional[ShadowBitmap], str]:
        """**执行前重读**全卷位图，构造影子位图的基线。

        ★ 绝不能复用 ``self._snapshot``（规划期快照）：那份位图在执行开始时
        就已经过期，用它做守卫会让"驱逐腾出来的空间"永远显示为被占用，
        依赖驱逐的计划整批被拒 → v2 核心能力静默失效（QA FIX-1 阻断项）。

        Returns:
            ``(影子位图, 错误信息)``；``shadow is None`` 表示必须放弃执行。
        """
        handle = volume.VolumeHandle(self._info.letter)
        try:
            handle.open()
            snapshot = bitmap_mod.read_volume_bitmap(
                handle.handle,
                self._info.total_clusters,
                progress=lambda done, total: self.progress.emit(
                    0, max(1, total), "重读卷位图（执行前校验）"
                ),
            )
            snapshot.bytes_per_cluster = self._info.bytes_per_cluster
        except api.Win32Error as exc:
            return None, f"执行前重读卷位图失败，已拒绝执行（{exc}）"
        except (RuntimeError, ValueError) as exc:
            return None, f"执行前重读卷位图失败，已拒绝执行（{exc}）"
        finally:
            handle.close()

        shadow = ShadowBitmap.from_snapshot(snapshot)
        if not shadow.is_ready:
            return None, "执行前重读的卷位图不可用（总簇数为 0 或字节数不足），已拒绝执行"
        return shadow, ""

    def _refresh_task_regions(
        self, volume_handle: int, shadow: ShadowBitmap, task
    ) -> int:
        """对一条任务涉及的簇做**局部**位图重读，并合入影子位图。

        目的：抓"计划生成之后，目标簇被外部进程占用"这种规划期无法预见的
        变化。只读一小片（``SEGMENT_VERIFY_MARGIN`` 余量），不做全卷重读。

        Returns:
            实际从磁盘补进来的位数（0 表示读不到或全部被本地变更挡住）。
        """
        patched = 0
        total = shadow.total_clusters
        for segment in task.segments:
            windows = (
                (segment.dst_lcn, segment.clusters),
                (segment.src_lcn, segment.clusters),
            )
            for start, clusters in windows:
                if clusters <= 0:
                    continue
                low = max(0, int(start) - SEGMENT_VERIFY_MARGIN)
                high = min(total, int(start) + int(clusters) + SEGMENT_VERIFY_MARGIN)
                if high <= low:
                    continue
                returned, chunk = bitmap_mod.read_bitmap_slice(
                    volume_handle, low, high - low
                )
                if returned < 0 or not chunk:
                    continue
                patched += shadow.patch(returned, chunk)
        return patched

    @staticmethod
    def _apply_success(shadow: ShadowBitmap, segments: Sequence[object]) -> int:
        """把**成功**的段登记进影子位图：源释放、目标占用。

        ``segments`` 必须已经剔除失败段 —— 失败段既没有释放源，也没有占用目标。
        """
        changed = 0
        for segment in segments:
            clusters = int(getattr(segment, "clusters", 0))
            if clusters <= 0:
                continue
            changed += shadow.mark_free(int(segment.src_lcn), clusters)
            changed += shadow.mark_used(int(segment.dst_lcn), clusters)
        return changed

    @staticmethod
    def _succeeded_segments(task, result) -> Optional[List[object]]:
        """判定一条任务里**确实搬成功**的段。

        Returns:
            成功段列表；``None`` 表示"无法判定"（取消 / 失败项无法归属到具体段），
            此时调用方必须退化为"从磁盘重读该任务区域"，**不许猜**。

        Note:
            猜错的后果是双向的：把失败的段当成功 → 影子位图认为目标已占用、
            源已空闲，后续任务的守卫可能放行到真正被占用的簇；把成功的段当失败
            → 幽灵占用，后续任务被误拒。两种都必须避免，所以宁可多读一次位图。
        """
        failures = list(getattr(result, "failures", ()) or ())
        if getattr(result, "cancelled", False):
            return None
        if not failures:
            return list(task.segments)
        if any(int(getattr(f, "clusters", 0)) <= 0 for f in failures):
            # 有"无法归属到具体段"的失败（打开文件失败 / 路径未知 / 参数错误）
            return None
        failed = {
            (int(f.src_lcn), int(f.dst_lcn), int(f.clusters)) for f in failures
        }
        succeeded = [
            seg
            for seg in task.segments
            if (int(seg.src_lcn), int(seg.dst_lcn), int(seg.clusters)) not in failed
        ]
        if len(succeeded) == len(task.segments):
            # 失败项一条都没对上 → 归属不明，走磁盘重读
            return None
        return succeeded

    @staticmethod
    def _failed_keys(result) -> set:
        """失败段的 ``(src_lcn, dst_lcn, clusters)`` 集合（诊断 / 单测用）。"""
        keys = set()
        for failure in getattr(result, "failures", ()):  # noqa: B902 - 兼容空对象
            keys.add((int(failure.src_lcn), int(failure.dst_lcn), int(failure.clusters)))
        return keys

    # ------------------------------------------------------------------ #
    def run(self) -> None:  # noqa: D102 - QThread 钩子
        # 唯一的写操作出口：仅在此处按需导入
        from app.win32 import mover

        outcome = MoveOutcome(failures=[])
        tasks = self._tasks()
        if not tasks:
            self.failed.emit("计划中没有可执行的任务")
            return

        identity_error = self._verify_volume_identity()
        if identity_error:
            self.failed.emit(identity_error)
            return

        policy = self._policy()
        io_profile = self._io_profile or getattr(self._plan, "io_profile", None)
        if not isinstance(io_profile, IoProfile):
            io_profile = IoProfile.build(
                MEDIA_UNKNOWN, 256, self._info.bytes_per_cluster or 4096
            )

        # ---- 影子位图基线：必须重读，禁止复用规划期快照 ----
        shadow, shadow_error = self._load_shadow()
        if shadow is None:
            self.failed.emit(shadow_error)
            return
        guard = SafetyGuard(policy, shadow)

        total = max(1, self._plan.total_move_clusters)
        executed = 0
        done = 0
        consecutive_failures = 0
        rejected_segments = 0
        prewarmed: set = set()

        volume_handle = volume.VolumeHandle(self._info.letter)
        try:
            volume_handle.open(write_access=True)
        except api.Win32Error as exc:
            self.failed.emit(f"打开卷写句柄失败：{exc}")
            return

        try:
            for task in tasks:
                if self._cancel:
                    outcome.cancelled = True
                    break

                path = task.path
                self.progress.emit(done, total, path)

                if io_profile.prewarm and path not in prewarmed:
                    prewarmed.add(path)
                    try:
                        mover.prewarm_file(
                            path,
                            io_profile.request_bytes,
                            io_profile.prewarm_chunk,
                            should_cancel=lambda: self._cancel,
                        )
                    except Exception:  # noqa: BLE001 - 预热失败绝不阻塞搬运
                        pass

                # ★ 每段执行前：局部重读位图 → 合入影子 → 守卫据此判定
                self._refresh_task_regions(volume_handle.handle, shadow, task)
                guard.refresh(shadow)

                result = mover.execute_task(
                    volume_handle.handle,
                    task,
                    guard,
                    policy,
                    self._journal,
                    should_cancel=lambda: self._cancel,
                )
                outcome.failures.extend(result.failures)
                outcome.moved_clusters += result.moved_clusters
                executed += sum(seg.clusters for seg in task.segments)
                if result.cancelled:
                    outcome.cancelled = True

                # ★ 每段成功后：源释放、目标占用（之后的校验都读影子位图）
                succeeded = self._succeeded_segments(task, result)
                if succeeded is None:
                    # 无法判定哪些段真的执行了 → 从磁盘重读该任务区域（权威数据）
                    self._refresh_task_regions(volume_handle.handle, shadow, task)
                    rejected_segments += len(result.failures)
                else:
                    self._apply_success(shadow, succeeded)
                    rejected_segments += len(task.segments) - len(succeeded)
                guard.refresh(shadow)

                if result.failures:
                    consecutive_failures += len(result.failures)
                    first = result.failures[0]
                    detail = (
                        f"{first.message}（源 LCN {first.src_lcn} → 目标 LCN "
                        f"{first.dst_lcn}，{first.clusters} 簇，错误码 {first.winerror}）"
                    )
                    self.file_result.emit(path, False, detail)
                    if not succeeded and task.segments:
                        # 整个任务的所有段都没通过：明确报"任务整体失败"，
                        # 不许以"部分成功"的形式静默掩盖（FIX-1 要求 6）
                        outcome.failures.append(
                            MoveFailure(
                                path=path,
                                winerror=int(first.winerror),
                                message=(
                                    f"任务整体失败：{len(task.segments)} 段全部未执行"
                                    f"（阶段 {task.phase}）—— {first.message}"
                                ),
                            )
                        )
                        self.file_result.emit(
                            path,
                            False,
                            f"任务整体失败（{len(task.segments)} 段全部未执行）",
                        )
                    if self._on_error == "halt":
                        outcome.cancelled = True
                        break
                else:
                    consecutive_failures = 0
                    outcome.moved_files += 1
                    self.file_result.emit(path, True, "移动完成")

                done = min(total, done + task.clusters)
                self.progress.emit(done, total, path)

                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    outcome.failures.append(
                        MoveFailure(
                            path=path,
                            winerror=0,
                            message=(
                                f"连续 {consecutive_failures} 段失败，判定为致命错误并停止"
                                "（卷状态可能已变化）"
                            ),
                        )
                    )
                    outcome.cancelled = True
                    break

                if self._cancel:
                    outcome.cancelled = True
                    break
        except Exception as exc:  # noqa: BLE001 - 绝不吞异常
            outcome.failures.append(
                MoveFailure(
                    path="",
                    winerror=0,
                    message=f"执行期异常：{type(exc).__name__}: {exc}",
                )
            )
        finally:
            volume_handle.close()

        # 被拒段必须留痕（不许静默跳过）
        if rejected_segments:
            outcome.failures.append(
                MoveFailure(
                    path="",
                    winerror=0,
                    message=(
                        f"本次共有 {rejected_segments:,} 段的源/目标未通过执行期校验"
                        "（目标在位图里不是空闲，或源已变化），已在结果里逐条列出"
                    ),
                )
            )

        # R3 守护：执行量与计划总量不一致时明确报错（而不是"静默成功"）
        expected = self._plan.total_move_clusters
        if not outcome.cancelled and executed != expected:
            outcome.failures.append(
                MoveFailure(
                    path="",
                    winerror=0,
                    message=(
                        f"执行量与计划不一致：计划 {expected:,} 簇，实际提交 "
                        f"{executed:,} 簇（请检查 plan.tasks 是否完整）"
                    ),
                )
            )

        self.completed.emit(outcome)


# --------------------------------------------------------------------------- #
# 手动回滚（T05 要求 7）
# --------------------------------------------------------------------------- #
class RollbackWorker(QThread):
    """执行**手动回滚**：把本次会话搬过去的段按逆向搬回来。

    与 :class:`MoveWorker` 的差异：

    * 数据来源是 :class:`app.core.journal.MoveJournal` 的
      :meth:`rollback_segments`（逆序 + ``src↔dst`` 互换），不是 ``MovePlan``；
    * 守卫必须基于**回滚前重新读取的位图** —— 旧快照里"原 src 现在空闲"
      这一事实并不存在，用旧快照会被守卫正确地拒绝；
    * 仍然只有这里按需导入 :mod:`app.win32.mover`，导入隔离不变。

    信号与 ``MoveWorker`` 保持一致，主窗口可以复用同一套进度条与日志。
    """

    progress = Signal(int, int, str)      # 已完成簇, 总簇, 当前文件
    file_result = Signal(str, bool, str)  # 路径, 是否成功, 说明
    completed = Signal(object)            # MoveOutcome
    failed = Signal(str)

    def __init__(
        self,
        info: VolumeInfo,
        segments: Sequence[object],
        parent=None,
        *,
        policy: Optional[SafetyPolicy] = None,
        journal: Optional[object] = None,
    ) -> None:
        super().__init__(parent)
        self._info = info
        self._segments = list(segments)
        self._policy = policy
        self._journal = journal
        self._cancel = False

    def request_cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:  # noqa: D102 - QThread 钩子
        # 唯一的写操作出口：仅在此处按需导入
        from app.win32 import mover

        outcome = MoveOutcome(failures=[])
        if not self._segments:
            self.failed.emit("没有可回滚的段（本次会话没有成功写入的流水）")
            return

        # ---- 1) 回滚前重读位图，构造新鲜守卫 ----
        snapshot = BitmapSnapshot()
        handle = volume.VolumeHandle(self._info.letter)
        try:
            handle.open()
            snapshot = bitmap_mod.read_volume_bitmap(
                handle.handle,
                self._info.total_clusters,
                progress=lambda done, total: self.progress.emit(
                    done, max(1, total), "重读位图（回滚前校验）"
                ),
            )
            snapshot.bytes_per_cluster = self._info.bytes_per_cluster
        except api.Win32Error as exc:
            self.failed.emit(f"回滚前重读位图失败：{exc}")
            return
        except (RuntimeError, ValueError) as exc:
            self.failed.emit(f"回滚前重读位图失败：{exc}")
            return
        finally:
            handle.close()

        policy = self._policy
        if not isinstance(policy, SafetyPolicy):
            policy = SafetyPolicy(
                total_clusters=self._info.total_clusters,
                bytes_per_cluster=self._info.bytes_per_cluster or 4096,
            )
        guard = SafetyGuard(policy, snapshot)

        # ---- 2) 按文件聚合后逐个回滚 ----
        grouped: dict = {}
        order: list = []
        for path, segment in self._segments:
            if path not in grouped:
                grouped[path] = []
                order.append(path)
            grouped[path].append(segment)

        total = max(1, sum(seg.clusters for _p, seg in self._segments))
        done = 0

        volume_handle = volume.VolumeHandle(self._info.letter)
        try:
            volume_handle.open(write_access=True)
        except api.Win32Error as exc:
            self.failed.emit(f"打开卷写句柄失败：{exc}")
            return

        try:
            for path in order:
                if self._cancel:
                    outcome.cancelled = True
                    break
                self.progress.emit(done, total, f"回滚 {path}")
                try:
                    result = mover.rollback_segments(
                        volume_handle.handle,
                        path,
                        grouped[path],
                        guard=guard,
                        policy=policy,
                        journal=self._journal,
                        should_cancel=lambda: self._cancel,
                    )
                except Exception as exc:  # noqa: BLE001 - 绝不吞异常
                    outcome.failures.append(
                        MoveFailure(
                            path=path,
                            winerror=0,
                            message=f"回滚异常：{type(exc).__name__}: {exc}",
                        )
                    )
                    self.file_result.emit(path, False, f"回滚异常：{exc}")
                    continue

                outcome.failures.extend(result.failures)
                outcome.moved_clusters += result.moved_clusters
                if result.cancelled:
                    outcome.cancelled = True
                if result.failures:
                    first = result.failures[0]
                    self.file_result.emit(
                        path,
                        False,
                        f"{first.message}（错误码 {first.winerror}）",
                    )
                else:
                    outcome.moved_files += 1
                    self.file_result.emit(path, True, "已回滚")
                done = min(total, done + sum(s.clusters for s in grouped[path]))
                self.progress.emit(done, total, f"回滚 {path}")
        finally:
            volume_handle.close()

        self.completed.emit(outcome)


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def scan_config_for(info: VolumeInfo, min_size: int) -> ScanConfig:
    """根据卷信息构造扫描配置。"""
    return ScanConfig(root=info.root or f"{info.letter}:\\", min_size=min_size)


def describe_failures(failures: Sequence[PlanFailure]) -> str:
    """把规划失败项拼成日志文案。"""
    if not failures:
        return ""
    return "；".join(f"{f.entry.name}: {f.reason}" for f in failures)


def human_seconds(seconds: float) -> str:
    """秒数转中文可读文案。"""
    if seconds < 60:
        return f"{seconds:.1f} 秒"
    minutes = int(seconds // 60)
    return f"{minutes} 分 {seconds - minutes * 60:.0f} 秒"


def describe_index_payload(payload: IndexPayload) -> str:
    """把索引结果拼成一行日志文案。"""
    if not payload.ok:
        return "；".join(payload.notes) or "索引建立失败"
    return (
        f"{'MFT' if payload.channel == 'mft' else '句柄'}通道："
        f"{payload.total_files:,} 个文件 / {payload.total_extents:,} 段，"
        f"耗时 {human_seconds(payload.elapsed)}"
    )


# --------------------------------------------------------------------------- #
# 1.0.0C 用户态分块流式搬迁（特殊工作模式）
# --------------------------------------------------------------------------- #
class BlockStreamWorker(QThread):
    """用户态分块流式搬迁：把选定文件拷到目的地、校验后移到回收站。

    与 :class:`MoveWorker`（``FSCTL_MOVE_FILE`` 簇级、需管理卷特权）不同：本 worker
    在用户态用普通 ``ReadFile`` / ``WriteFile`` 按 65536 簇 block 分块流式拷贝（经
    ``app.win32.file_io.copy_stream``，内存 / 硬盘两种可调缓存），**拷贝并校验完整后**
    才把源文件移到回收站（可恢复），从而腾出源卷空间。绝不永久删除。
    """

    progress = Signal(int, int, str)
    file_result = Signal(str, bool, str)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        entries: Sequence[FileEntry],
        *,
        mode: str = "memory",
        cache_dir: Optional[str] = None,
        buffer_blocks: int = 2,
        bytes_per_cluster: int = 4096,
        block_clusters: int = 65536,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._entries = list(entries)
        self._mode = mode
        self._cache_dir = cache_dir
        self._buffer_blocks = int(buffer_blocks)
        self._bpc = int(bytes_per_cluster)
        self._block_clusters = int(block_clusters)
        self._cancel = False

    def request_cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:  # noqa: D102 - QThread 钩子
        import os

        from app.win32 import file_io

        total = len(self._entries)
        moved = failed = 0
        moved_clusters = 0
        done = 0
        for entry in self._entries:
            if self._cancel:
                break
            done += 1
            self.progress.emit(done, total, entry.name)
            if not entry.movable or entry.allocated_clusters <= 0:
                failed += 1
                self.file_result.emit(entry.path, False, "不可移动或无物理分配")
                continue
            src = entry.path
            # 同卷临时文件：读出 src（腾空前部簇）→ 写 tmp（FS 在空闲区分配落点）→
            # 校验 → 源移回收站 → 改回原路径。**无外部目的地**，全程同卷内。
            tmp = src + ".lbastream.tmp"
            try:
                src_size = os.path.getsize(src)
                written, nblocks = file_io.copy_stream(
                    src, tmp,
                    mode=self._mode,
                    cache_dir=self._cache_dir,
                    buffer_blocks=self._buffer_blocks,
                    bytes_per_cluster=self._bpc,
                    block_clusters=self._block_clusters,
                    preallocate_dst=True,  # 先预填充一段连续簇，再流式覆盖（前端→后端直搬）
                )
                if written != src_size:
                    raise OSError(f"拷贝不完整（写入 {written} / 源 {src_size}）")
                if not file_io.recycle(src):
                    raise OSError("无法把源移入回收站（源未删，可重试）")
                os.replace(tmp, src)  # 簇已由文件系统重分配到空闲区，改回原路径
                moved += 1
                moved_clusters += entry.allocated_clusters
                self.file_result.emit(entry.path, True, f"{nblocks} 块 / {entry.allocated_clusters:,} 簇")
            except Exception as exc:  # noqa: BLE001
                try:
                    if os.path.exists(tmp):
                        file_io.recycle(tmp)  # 清理残留临时文件
                except Exception:  # noqa: BLE001
                    pass
                failed += 1
                self.file_result.emit(entry.path, False, f"{type(exc).__name__}: {exc}")
        self.completed.emit(
            {"moved": moved, "failed": failed, "clusters": moved_clusters}
        )

