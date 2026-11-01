"""把 NTFS 元数据组装成引擎能吃的「文件 → 块区间」。

流程：读引导扇区 → 从 $MFT 起始簇读出记录 0 → 拿到 $MFT 自己的 runlist →
顺序扫完整个文件表 → 用 FILE_NAME 的父引用还原路径 → 用 $Bitmap 读出
真实自由空间 → 转成引擎的 block Extent。

**跳过策略是这一层最重要的部分。** 下面这些文件一律不参与整理，原因不是
"实现不了"，而是搬它们会出事：

============================  ==================================================
跳过对象                        原因
============================  ==================================================
记录号 < 16 的元文件            $MFT/$LogFile/$Bitmap 等，它们的位置被系统写死
目录                            没有簇数据，搬了没意义
常驻（resident）小文件          数据在 MFT 记录里，没有独立簇
含 ATTRIBUTE_LIST 的文件        runlist 分散在额外记录里，本版不解，漏解会搬错
稀疏 / 压缩 / 加密              逻辑簇≠物理簇，搬簇语义改变
重解析点（symlink/junction）    搬过去指向关系可能失效
============================  ==================================================

漏掉任何一条都不是"少整理一个文件"，而是可能搬错或搬坏。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from ..contract.models import Extent, FileFrag, Geometry, VolumeInfo, normalize_extents
from ..devices.base import DeviceError
from .bitmap import free_runs
from .boot import NtfsBoot, NotNtfsError
from .record import FileRecord, parse_record

__all__ = ["CatalogStats", "MftCatalog", "NtfsScanError"]

#: 元文件记录号上界（0..15 都是系统元文件）
FIRST_USER_RECORD = 16
ROOT_RECORD = 5
BITMAP_RECORD = 6

#: 顺序读 MFT 的分块大小。太小 → 系统调用次数爆炸；太大 → 扫小卷时浪费内存。
_CHUNK_BYTES = 4 * 1024 * 1024


class NtfsScanError(RuntimeError):
    """扫描失败（不是 NTFS、$MFT 读不出来等）。"""


@dataclass(frozen=True, slots=True)
class CatalogStats:
    #: 数据来源，只用于展示：``MFT``（$MFT 解析）/ ``遍历``（目录 + 簇映射）
    kind: str = "MFT"
    records_read: int = 0
    records_bad: int = 0
    records_skipped: int = 0
    files: int = 0
    free_clusters: int = 0
    elapsed_s: float = 0.0
    #: ``bitmap`` = 自由空间来自 $Bitmap；``complement`` = 由已用区间反推
    free_source: str = "bitmap"
    notes: tuple[str, ...] = ()

    def summary(self, cluster_size: int) -> str:
        mb = self.free_clusters * cluster_size / 1048576
        return (
            f"{self.kind} {self.records_read} 条（异常 {self.records_bad}），"
            f"可用文件 {self.files}，自由 {mb:.1f} MiB（来源 {self.free_source}）"
        )


class MftCatalog:
    """实现 :class:`lbamover.contract.ports.FileCatalog` 的真机版本。"""

    def __init__(
        self,
        device,
        boot: NtfsBoot | None = None,
        *,
        min_clusters: int = 1,
        max_records: int | None = None,
        root: str = "",
        on_progress: Callable[[int, str], None] | None = None,
    ) -> None:
        self.device = device
        self._boot = boot
        #: ``(已看条目数, 当前路径)``。百万条记录的卷上解析要十几秒，
        #: 没有回调界面会静止十几秒（用户以为卡死）。
        self._on_progress = on_progress
        #: 路径前缀（``R:\``）。MFT 里存的是相对卷根的路径，界面上直接显示
        #: "下载中转.zip" 会让人分不清是哪个盘。
        self._root = root
        self._min_clusters = max(1, min_clusters)
        self._max_records = max_records
        #: 进度上报的基数（上一阶段已完成的条目数），保证 N 只增不减
        self._report_base = 0
        self._files: tuple[FileFrag, ...] | None = None
        self._free: tuple[Extent, ...] | None = None
        self._stats = CatalogStats()

    # -- 公开接口 ---------------------------------------------------------

    @property
    def boot(self) -> NtfsBoot:
        if self._boot is None:
            self._boot = self._read_boot()
        return self._boot

    @property
    def stats(self) -> CatalogStats:
        return self._stats

    @classmethod
    def open(cls, device, **kw) -> "MftCatalog":
        """打开并立刻校验引导扇区，失败尽早暴露。"""
        cat = cls(device, **kw)
        _ = cat.boot
        return cat

    def scan(self) -> CatalogStats:
        """完整扫描一次并缓存结果。重复调用直接返回缓存。"""
        if self._files is not None:
            return self._stats
        t0 = time.monotonic()
        boot = self.boot
        notes: list[str] = []

        self._check_block_size(boot)
        mft_runs = self._mft_runs()
        records, bad, entries = self._read_records(mft_runs)

        self._report_base = len(records)
        files, skipped, skipped_notes = self._build_files(records, entries, boot)
        notes.extend(skipped_notes)

        free, source = self._free_extents(records, files, boot)
        self._files = files
        self._free = free
        self._stats = CatalogStats(
            records_read=len(records),
            records_bad=bad,
            records_skipped=skipped,
            files=len(files),
            free_clusters=sum(e.length for e in free) // max(1, self._blocks_per_cluster),
            elapsed_s=time.monotonic() - t0,
            free_source=source,
            notes=tuple(notes),
        )
        return self._stats

    def close(self) -> None:
        """释放底层设备句柄。扫描完就该关：真机卷句柄长期持有会挡住别人。"""
        dev = getattr(self, "device", None)
        if dev is not None:
            try:
                dev.close()
            except OSError:
                pass

    def __enter__(self) -> "MftCatalog":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def list_files(self, volume: VolumeInfo) -> tuple[FileFrag, ...]:
        self.scan()
        assert self._files is not None
        return self._files

    def free_extents(self, volume: VolumeInfo) -> tuple[Extent, ...]:
        self.scan()
        assert self._free is not None
        return self._free

    # -- 几何 -------------------------------------------------------------

    @property
    def _blocks_per_cluster(self) -> int:
        bs = self.device.geometry.block_size
        cs = self.boot.cluster_size
        return max(1, cs // bs)

    def _check_block_size(self, boot: NtfsBoot) -> None:
        bs = self.device.geometry.block_size
        if boot.cluster_size % bs != 0:
            raise NtfsScanError(
                f"簇大小 {boot.cluster_size} 不能被块大小 {bs} 整除，无法换算 LCN"
            )

    def _read_boot(self) -> NtfsBoot:
        bs = self.device.geometry.block_size
        raw = self.device.read_blocks(0, 1)
        try:
            return NtfsBoot.parse(raw[: max(512, bs)])
        except NotNtfsError as exc:
            raise NtfsScanError(str(exc)) from exc

    # -- $MFT -------------------------------------------------------------

    def _mft_runs(self):
        """读 $MFT 的记录 0，拿到它自己的 runlist。"""
        boot = self.boot
        rec_bytes = boot.record_size
        need_blocks = max(1, (rec_bytes + self.device.geometry.block_size - 1) //
                          self.device.geometry.block_size)
        raw = self.device.read_blocks(boot.mft_lcn * self._blocks_per_cluster, need_blocks)
        rec = parse_record(raw[:rec_bytes], 0, boot.bytes_per_sector)
        if rec is None:
            raise NtfsScanError("$MFT 记录 0 解析失败（fixup 校验不通过或结构损坏）")
        if rec.data is None or rec.data.runlist is None:
            raise NtfsScanError("$MFT 记录 0 没有可用的 runlist")
        if rec.data.runlist.has_sparse:
            raise NtfsScanError("$MFT 自身含稀疏区，本版不支持")
        return rec.data.runlist.extents

    def _iter_record_bytes(self, runs):
        """按 runlist 顺序读出整张 MFT 的字节流，按记录大小切分。"""
        boot = self.boot
        rec_size = boot.record_size
        cluster_bytes = boot.cluster_size
        blocks_per_cluster = self._blocks_per_cluster
        target = max(_CHUNK_BYTES, rec_size * 4)
        step_clusters = max(1, target // cluster_bytes)

        buf = bytearray()
        offset = 0  # 已消费字节数 = 下一条记录的虚拟偏移

        def drain():
            nonlocal buf, offset
            out = []
            while len(buf) >= rec_size:
                out.append((offset, bytes(buf[:rec_size])))
                del buf[:rec_size]
                offset += rec_size
            return out

        for run in runs:
            lcn = run.start
            left = run.length
            while left > 0:
                step = min(left, step_clusters)
                try:
                    data = self.device.read_blocks(
                        lcn * blocks_per_cluster, step * blocks_per_cluster
                    )
                except DeviceError:
                    # 单段读失败不终止整卷扫描：丢弃半条记录、前移虚拟偏移，
                    # 保证后续记录号仍然正确（宁可坏一块，不能全错位）。
                    buf.clear()
                    offset += step * cluster_bytes
                    lcn += step
                    left -= step
                    continue
                buf += data
                for item in drain():
                    yield item
                lcn += step
                left -= step

        for item in drain():
            yield item

    def _report(self, count: int, current: str) -> None:
        """上报进度。``count`` 是**本阶段内**的计数，这里加上前面阶段的基数。

        不加基数的话，界面上的"N 项"会从 46000 突然掉回 1000（记录解析结束、
        开始组装路径），看起来像任务重启了。
        """
        if self._on_progress is not None:
            self._on_progress(self._report_base + count, current)

    def _read_records(self, mft_runs):
        """扫全表，返回（记录字典, 坏记录数, FRN→(父,名) 表）。"""
        boot = self.boot
        records: dict[int, FileRecord] = {}
        entries: dict[int, tuple[int, str]] = {}
        bad = 0
        index = 0
        for index, raw in self._iter_record_bytes(mft_runs):
            number = index // boot.record_size
            if not number & 0xFFF:  # 每 4096 条报一次，够密且几乎不花钱
                self._report(number, f"$MFT 记录 {number}")
            if self._max_records is not None and number >= self._max_records:
                break
            # 未使用的 MFT 槽位是全零：这是正常状态，不是"坏记录"。
            # 真实卷里这种槽位能占很大比例，算成坏记录会让人以为盘要坏了。
            if not any(raw[:16]):
                continue
            rec = parse_record(raw, number, boot.bytes_per_sector)
            if rec is None:
                bad += 1
                continue
            if not rec.in_use:
                continue
            records[number] = rec
            name = rec.best_name
            if name is not None and name.name:
                entries[number] = (name.parent_ref, name.name)
        return records, bad, entries

    # -- 组装 -------------------------------------------------------------

    def _build_files(self, records, entries, boot: NtfsBoot) -> tuple[tuple[FileFrag, ...], int, list[str]]:
        scale = self._blocks_per_cluster
        files: list[FileFrag] = []
        skipped = 0
        reasons: dict[str, int] = {}

        def skip(reason: str) -> None:
            nonlocal skipped
            skipped += 1
            reasons[reason] = reasons.get(reason, 0) + 1

        for count, (number, rec) in enumerate(sorted(records.items()), 1):
            if not count & 0x3FFF:  # 每 16384 个文件报一次
                self._report(count, f"组装路径 {count}/{len(records)}")
            if number < FIRST_USER_RECORD:
                continue  # 元文件不算"跳过"，它们本来就不是整理对象
            if rec.is_dir:
                skip("目录")
                continue
            if rec.base_ref != 0:
                skip("扩展记录（extents 在别处）")
                continue
            if rec.has_attribute_list:
                skip("含 ATTRIBUTE_LIST")
                continue
            if rec.is_reparse:
                skip("重解析点")
                continue
            if rec.data is None:
                skip("无数据属性")
                continue
            if not rec.data.non_resident:
                skip("常驻小文件（无独立簇）")
                continue
            if rec.data.runlist is None:
                skip("runlist 解析失败")
                continue
            if rec.data.is_compressed or rec.data.is_encrypted or rec.data.is_sparse:
                skip("压缩/加密/稀疏")
                continue
            if rec.data.runlist.has_sparse:
                skip("含稀疏洞")
                continue

            extents = rec.extents
            total_clusters = sum(e.length for e in extents)
            if total_clusters < self._min_clusters:
                skip("不足最小簇数")
                continue

            rel = self._resolve_path(number, entries)
            if not rel:
                skip("路径无法还原")
                continue

            block_extents = normalize_extents(
                Extent(e.start * scale, e.length * scale) for e in extents
            )
            try:
                frag = FileFrag(
                    file_id=str(number),
                    path=self._root + rel,
                    extents=block_extents,
                    attributes=rec.si_flags,
                )
            except ValueError:
                # 单个文件的 runlist 异常（重叠/乱序，多见于事务日志这类特殊
                # 文件）不能把整卷扫描带崩：跳过它并计数，报告里会有体现。
                skip("簇区间异常")
                continue
            files.append(frag)

        notes = []
        for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
            notes.append(f"跳过 {count} 个：{reason}")
        return tuple(files), skipped, notes

    def _resolve_path(self, number: int, entries: dict[int, tuple[int, str]]) -> str:
        """沿父引用上溯拼出相对路径。有环或断链返回空串。"""
        parts: list[str] = []
        seen: set[int] = set()
        cur = number
        for _ in range(64):
            if cur in seen:
                return ""
            seen.add(cur)
            item = entries.get(cur)
            if item is None:
                return ""
            parent, name = item
            if cur == ROOT_RECORD or parent == cur:
                break
            parts.append(name)
            cur = parent
        else:
            return ""
        parts.reverse()
        return "\\".join(parts)

    # -- 自由空间 ---------------------------------------------------------

    def _free_extents(self, records, files, boot: NtfsBoot):
        """优先用 $Bitmap；失败则退化为"已用区间的补集"并如实标注。"""
        bitmap = self._read_bitmap(records, boot)
        scale = self._blocks_per_cluster
        if bitmap is not None:
            total = boot.cluster_count
            # 位图遍历是整个扫描里最容易变成瓶颈的一步：1 TiB / 4 KiB 簇的卷
            # 有 2.6 亿位，逐位循环要几十秒。free_runs 按字节跳跃，只碰段边界。
            out = [
                Extent(start * scale, length * scale)
                for start, length in free_runs(bitmap, total)
            ]
            return tuple(out), "bitmap"

        from ..engine.frag import free_extents_from_files

        return free_extents_from_files(self.device.geometry, files), "complement"

    def _read_bitmap(self, records, boot: NtfsBoot) -> bytes | None:
        rec = records.get(BITMAP_RECORD)
        if rec is None or rec.data is None or rec.data.runlist is None:
            return None
        if rec.data.runlist.has_sparse:
            return None
        need = (boot.cluster_count + 7) // 8
        out = bytearray()
        scale = self._blocks_per_cluster
        for run in rec.data.runlist.extents:
            if len(out) >= need:
                break
            remaining = need - len(out)
            clusters = min(run.length, (remaining + boot.cluster_size - 1) // boot.cluster_size)
            if clusters <= 0:
                break
            try:
                out += self.device.read_blocks(run.start * scale, clusters * scale)
            except DeviceError:
                return None
        return bytes(out[:need]) if out else None
