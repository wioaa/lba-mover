r"""未提权时的扫描路径：目录遍历 + 逐文件 ``FSCTL_GET_RETRIEVAL_POINTERS``。

为什么需要这条路径
------------------
管理员能打开 ``\\.\C:`` 读 ``$MFT``，那是**快**路径（一次解析拿到全卷文件）。
但普通用户的进程连那个句柄都拿不到——``CreateFileW`` 直接 ``ERROR_ACCESS_DENIED``。
于是"扫描"在没提权时必然失败，而扫描**恰恰是用户第一次点的那颗按钮**。

关键事实：**查询一个文件占哪些簇不需要特权**，只要拿到这个文件的句柄
（``FSCTL_GET_RETRIEVAL_POINTERS``）。所以未提权也能扫，只是要一个文件一个
文件地问，慢得多，而且打不开的文件看不到。

这条路径的**诚实边界**（都写进 ``CatalogStats.notes`` 和报告里）：
- 自由空间只能是"已用簇的补集"，不是 ``$Bitmap`` 的权威答案；
- 打不开的文件（独占/权限不足）不计入，所以补集里可能混进别人的簇；
- 因此结果标记 ``restricted``，**只允许预览，不许执行**。执行要卷句柄，
  那本身就需要提权，正好对齐。
"""

from __future__ import annotations

import os
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import Callable, Iterable, Iterator

from ..contract.models import Extent, FileFrag, Geometry, VolumeInfo, normalize_extents
from ..platform.errors import DeviceUnavailable
from ..ntfs.catalog import CatalogStats

__all__ = ["DirectoryCatalog", "FILE_ATTRIBUTE_REPARSE_POINT"]

#: 重解析点（符号链接 / 挂载点 / OneDrive 占位符）。跟进去会绕圈或跨卷。
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_ATTRIBUTE_DIRECTORY = 0x10

#: 这些目录里的文件由保护层统一排除，但**遍历仍要进去**：
#: 少了它们的占用，占用带会把 Windows 目录画成"空闲"。
_WALK_MAX_DEPTH = 64

#: 并发查询的默认线程数上限。
#:
#: 每个文件要跑一次 ``CreateFileW`` + ``DeviceIoControl``，都是**同步系统调用**，
#: ctypes 在调用期间释放 GIL，所以多线程能重叠等待。实测（88 万文件的 C: 卷）
#: 单文件约 0.28 ms，串行 4 分钟；8 路能到 1.6~1.8 倍。
#:
#: **加速比不高不是线程池的问题，是内核的问题**：同一卷上的这些调用最终都在
#: NTFS 的目录/记录锁上排队，进程级并发（多进程实测）也只到 1.3~1.6 倍。
#: 真正的量级差异来自"提权后用 $MFT 一次读完"，而不是这里的并发度。
_MAX_WORKERS = 8
#: 在飞任务上限 = 线程数 × 该系数。太小喂不饱线程，太大则内存与"取消滞后"变差。
_INFLIGHT_FACTOR = 8


def _default_workers() -> int:
    return min(_MAX_WORKERS, max(2, (os.cpu_count() or 4) // 2))


class DirectoryCatalog:
    """用文件句柄枚举「文件 → 簇区间」。不需要管理员权限。

    ``scanner`` / ``query`` 可注入，便于在非 Windows 或无权限环境下测试：
    遍历逻辑与 Win32 调用分开，测试跑的是真逻辑，不是假的。
    """

    def __init__(
        self,
        volume: VolumeInfo,
        *,
        scanner: Callable[[str], Iterable[tuple[str, int, int]]] | None = None,
        query: Callable[[str], tuple[tuple[int, int, int], ...]] | None = None,
        on_progress: Callable[[int, str], None] | None = None,
        max_files: int | None = None,
        progress_every: int = 2000,
        workers: int | None = None,
    ) -> None:
        self.volume = volume
        self._scanner = scanner or _walk
        self._query = query or _query_extents
        self._on_progress = on_progress
        self._max_files = max_files
        self._progress_every = max(1, progress_every)
        #: 并发查询线程数。``1`` 表示完全串行——测试与排障时用它拿到确定顺序。
        self._workers = _default_workers() if workers is None else max(1, workers)
        self._files: tuple[FileFrag, ...] = ()
        self._free: tuple[Extent, ...] = ()
        self._stats = CatalogStats()
        self._scanned = False

    # -- 契约 -------------------------------------------------------------

    @property
    def stats(self) -> CatalogStats:
        return self._stats

    @property
    def restricted(self) -> bool:
        """受限模式：没有 ``$Bitmap``，自由空间是反推的，且可能漏掉打不开的文件。"""
        return True

    @property
    def free_source(self) -> str:
        return self._stats.free_source

    def list_files(self, volume: VolumeInfo | None = None) -> tuple[FileFrag, ...]:
        self._ensure_scanned()
        return self._files

    def free_extents(self, volume: VolumeInfo | None = None) -> tuple[Extent, ...]:
        self._ensure_scanned()
        return self._free

    def close(self) -> None:  # 保持与 MftCatalog 一致的接口
        pass

    def __enter__(self) -> "DirectoryCatalog":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- 实现 -------------------------------------------------------------

    def _ensure_scanned(self) -> None:
        if self._scanned:
            return
        self._scanned = True
        t0 = time.monotonic()
        files: list[FileFrag] = []
        seen = 0
        skipped_dir = 0
        skipped_link = 0
        failed = 0
        truncated = False
        last_report = 0
        geometry = self.volume.geometry

        def accept(runs, path: str, attributes: int) -> None:
            """收下一次查询结果。异常只计数，绝不打断整卷。"""
            nonlocal failed
            if self._max_files is not None and len(files) >= self._max_files:
                # 并发时会有多个结果同时回来，上限必须在这里再挡一道，
                # 否则一次收割就能把结果数顶过 max_files。
                return
            extents = _runs_to_extents(runs, geometry)
            if not extents:
                return
            try:
                files.append(
                    FileFrag(
                        # **file_id 用路径，不用遍历序号。**
                        # 序号在两次扫描之间会漂移（遍历顺序、并发完成顺序都
                        # 会变），而 file_id 要跨扫描使用：手动选择文件后点
                        # 「开始整理」会重新扫描一次，计划指纹、断点续做也都
                        # 按 file_id 对齐。用序号意味着"用户选的 12 个文件"
                        # 在重新扫描后指向了别的文件 —— 这是最不能接受的一类错。
                        file_id=path,
                        path=path,
                        extents=extents,
                        attributes=attributes,
                    )
                )
            except ValueError:
                # 单文件数据异常（极端稀疏/损坏）不能打断整卷扫描
                failed += 1

        def reached_limit() -> bool:
            return self._max_files is not None and len(files) >= self._max_files

        def report(path: str) -> None:
            nonlocal last_report
            if self._on_progress is None or seen - last_report < self._progress_every:
                return
            last_report = seen
            self._on_progress(seen, path)

        source = self._scanner(self.volume.mount_point)
        try:
            if self._workers <= 1:
                for path, attributes, size in source:
                    seen += 1
                    if attributes & FILE_ATTRIBUTE_DIRECTORY:
                        skipped_dir += 1
                        continue
                    if attributes & FILE_ATTRIBUTE_REPARSE_POINT:
                        skipped_link += 1
                        continue
                    if size <= 0:
                        continue  # 空文件 / 无独立簇，问了也是空
                    try:
                        runs = self._query(path)
                    except (OSError, DeviceUnavailable):
                        # 独占打开（页面文件、注册表 hive、正在写的日志）很常见，
                        # 不能因为一个文件失败就放弃整卷。
                        failed += 1
                        continue
                    accept(runs, path, attributes)
                    if reached_limit():
                        truncated = True
                        break
                    report(path)
            else:
                window = max(1, self._workers * _INFLIGHT_FACTOR)
                pending: dict[Future, tuple[str, int]] = {}

                def collect(fut: Future, meta: tuple[str, int]) -> None:
                    nonlocal failed
                    path, attributes = meta
                    try:
                        runs = fut.result()
                    except (OSError, DeviceUnavailable):
                        failed += 1
                        return
                    accept(runs, path, attributes)

                with ThreadPoolExecutor(
                    max_workers=self._workers, thread_name_prefix="lbamover-scan"
                ) as pool:
                    for path, attributes, size in source:
                        seen += 1
                        if attributes & FILE_ATTRIBUTE_DIRECTORY:
                            skipped_dir += 1
                            report(path)
                            continue
                        if attributes & FILE_ATTRIBUTE_REPARSE_POINT:
                            skipped_link += 1
                            report(path)
                            continue
                        if size <= 0:
                            report(path)
                            continue
                        pending[pool.submit(self._query, path)] = (path, attributes)
                        if len(pending) >= window:
                            # 在飞任务满了才回收：这样遍历与查询真正重叠，
                            # 内存占用恒为 window 条，与卷的文件数无关。
                            done, _ = wait(pending, return_when=FIRST_COMPLETED)
                            for fut in done:
                                collect(fut, pending.pop(fut))
                            if reached_limit():
                                truncated = True
                                break
                        report(path)
                    if pending and not truncated:
                        done, _ = wait(pending)
                        for fut in done:
                            collect(fut, pending.pop(fut))
                        if reached_limit():
                            truncated = True
        except OSError as exc:
            raise DeviceUnavailable(f"遍历 {self.volume.mount_point} 失败：{exc}") from exc

        files.sort(key=lambda f: f.extents[0].start)
        self._files = tuple(files)
        self._free = _free_from_used(geometry, self._files)

        notes = [
            f"遍历 {seen} 项（目录 {skipped_dir}、重解析点 {skipped_link}、打不开 {failed}）",
        ]
        if failed:
            notes.append(
                f"{failed} 个文件打不开，它们的占用不在补集里 —— 因此本模式只允许预览"
            )
        if truncated:
            notes.append(f"达到上限 {self._max_files} 个文件，结果不完整")

        self._stats = CatalogStats(
            kind="遍历",
            records_read=seen,
            records_bad=failed,
            records_skipped=skipped_dir + skipped_link,
            files=len(self._files),
            free_clusters=sum(e.length for e in self._free),
            elapsed_s=time.monotonic() - t0,
            free_source="complement",
            notes=tuple(notes),
        )


# --------------------------------------------------------------------------
# 默认实现：Win32
# --------------------------------------------------------------------------


def _walk(mount: str) -> Iterator[tuple[str, int, int]]:
    """深度优先遍历，产出 ``(path, attributes, size)``。

    用 ``os.scandir`` 而不是递归 ``listdir``：scandir 的 stat 走目录项缓存，
    在百万级文件的卷上差好几倍时间。
    """
    stack: list[tuple[str, int]] = [(mount, 0)]
    while stack:
        directory, depth = stack.pop()
        if depth > _WALK_MAX_DEPTH:
            continue
        try:
            with os.scandir(directory) as it:
                for entry in it:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    attrs = getattr(st, "st_file_attributes", 0)
                    yield entry.path, attrs, st.st_size
                    if attrs & FILE_ATTRIBUTE_DIRECTORY and not attrs & FILE_ATTRIBUTE_REPARSE_POINT:
                        stack.append((entry.path, depth + 1))
        except OSError:
            # 单个目录没权限（每个卷都有几个）不该中断整卷
            continue


def _query_extents(path: str) -> tuple[tuple[int, int, int], ...]:
    """问文件系统「这个文件占了哪些簇」。只要文件句柄，不需要卷句柄。"""
    from ..devices.win32 import Win32Native

    handle = Win32Native.open_file(path, writable=False, attributes_only=True)
    try:
        return Win32Native.query_retrieval_pointers(handle)
    finally:
        Win32Native.close(handle)


def _runs_to_extents(
    runs: tuple[tuple[int, int, int], ...], geometry: Geometry
) -> tuple[Extent, ...]:
    """把 retrieval pointers 转成按 LCN 升序的 ``Extent``。

    三处必须处理，否则 ``FileFrag`` 会直接抛错、或者算出错的碎片度：

    * ``lcn == -1`` 是**稀疏洞**（VCN 有、物理簇没有），必须跳过；
    * VCN 递增但 **LCN 可以往回走**（文件后半段落在前面的空闲区），
      所以要按起始簇重新排序；
    * 极少数文件（压缩/稀疏/系统卷的日志）给出的 run 会**首尾重叠**，
      直接交给 ``FileFrag`` 会抛 ``extents 必须升序且不重叠``，
      进而把整卷扫描打断。合并成并集即可——它表达的是"这个文件占了这些
      物理簇"，重叠部分的第二次出现没有额外信息。
    """
    # retrieval pointers 的单位是簇，而 ``Geometry.block_size`` 在卷信息里
    # 就是簇大小（见 platform/volumes.py），所以 1 簇 == 1 块，不需要换算。
    out: list[Extent] = []
    for _vcn, lcn, count in runs:
        if lcn < 0 or count <= 0:
            continue
        out.append(Extent(lcn, count))
    return normalize_extents(out)


def _free_from_used(geometry: Geometry, files: tuple[FileFrag, ...]) -> tuple[Extent, ...]:
    from ..engine.frag import free_extents_from_files

    return free_extents_from_files(geometry, files)
