"""跨盘缓存搬运：把数据先顺序落到工作盘，再顺序写回目标。

**这是一个独立引擎，不与 ``executor.NtfsMoveExecutor`` 共享任何执行代码。**

为什么必须分开：两边的失败方式完全不同。MOVE_FILE 那边错的是系统调用
（文件被占用、布局变了、权限不足），这边错的是文件 IO 与容量（缓存盘满了、
路径不可写、读到半截）。混在一个类里，任何一次调试都要同时理解两条路径的
语义 —— 这正是本项目踩过的坑，所以宁可有重复的骨架，也不要共享的控制流。

## 适用边界（必须说清，否则会成为数据损坏的来源）

本引擎只作用于 :class:`BlockDevice`：镜像、内存盘、或**已锁定**的原始卷。

**不能**作用于已挂载的 NTFS 卷。裸写绕过文件系统，直接覆盖 ``$MFT`` 指着的
簇区，文件系统随即损坏 —— 真机整理必须走 ``FSCTL_MOVE_FILE``，让 NTFS 自己
搬簇并更新元数据。这个引擎存在的意义是离线演练与跨盘复制，不是"更快的真机
搬运"。

## 顺序性怎么来的

每个 op 的源区可能是一串不连续的 extent。这里不去管它 —— 读源本来就只能
按文件的实际布局读。真正能优化的是**写**：

* 写缓存：一个 op 一个文件，按 VCN 顺序**追加**，物理上连续；
* 读缓存：从 0 顺序读到尾；
* 写目标：按目标 LBA 递增写出（多段源的段间会跳，但段内连续）。

所以"顺序读写"的收益主要落在缓存盘上，而缓存盘是自己的工作盘，可以选一块
顺序性能好的（比如 SSD 或另一块盘）。目标盘受制于文件的实际布局，跳读无法
避免 —— 这一点不该在指标里含糊过去，:class:`IoStats` 会把两者的顺序率分开记。
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass, field
from typing import Iterator

from ..contract.enums import ErrorClass
from ..contract.models import Extent, MoveOp
from ..devices.base import DeviceError
from .pipeline import OpResult

__all__ = ["CacheConfig", "CachedCopyExecutor", "IoStats"]


@dataclass(frozen=True)
class CacheConfig:
    """缓存引擎的全部外部输入。"""

    #: 缓存文件放哪。可以指向**另一块盘**——那是这个引擎唯一的性能卖点。
    work_dir: str
    #: 每次 IO 的块数。与 ``JobParams.chunk_blocks`` 分开，因为缓存盘和
    #: 目标盘的理想块大小不一定一样。
    chunk_blocks: int = 256
    #: 保留缓存文件（调试用）。打开后每次运行都会在工作目录留下 GB 级文件。
    keep_cache: bool = False
    #: 写入前把文件扩到最终大小。让 NTFS 一次性预留空间，避免写入过程中
    #: 反复扩展导致的物理碎片 —— 缓存文件一碎，顺序性就没了。
    preallocate: bool = True

    def __post_init__(self) -> None:
        if not self.work_dir:
            raise ValueError("缓存工作目录不能为空")
        if self.chunk_blocks <= 0:
            raise ValueError("chunk_blocks 必须为正")


@dataclass
class IoStats:
    """顺序性指标，**按盘分开记**。

    混成一个"顺序率"会掩盖真正的瓶颈：缓存盘的写永远是顺序的（追加），
    真正会跳的是目标盘（受文件实际布局限制）和源盘（同一份布局）。把它们
    分开记，才能回答"这个模式到底有没有帮上忙"。
    """

    #: 源盘：读了多少次、其中多少次接在上一次之后
    src_reads: int = 0
    src_seq_reads: int = 0
    #: 缓存盘：写（追加，天然顺序）与读（从 0 往后）
    cache_writes: int = 0
    cache_reads: int = 0
    #: 目标盘：写与其中连续的次数
    dst_writes: int = 0
    dst_seq_writes: int = 0

    cached_bytes: int = 0
    cache_seconds: float = 0.0

    @staticmethod
    def _ratio(seq: int, total: int) -> float:
        return 0.0 if not total else seq / total

    @property
    def src_read_ratio(self) -> float:
        return self._ratio(self.src_seq_reads, self.src_reads)

    @property
    def dst_write_ratio(self) -> float:
        return self._ratio(self.dst_seq_writes, self.dst_writes)

    def to_dict(self) -> dict[str, float]:
        return {
            "src_reads": self.src_reads,
            "src_seq_ratio": round(self.src_read_ratio, 3),
            "cache_writes": self.cache_writes,
            "cache_reads": self.cache_reads,
            "dst_writes": self.dst_writes,
            "dst_seq_ratio": round(self.dst_write_ratio, 3),
            "cached_mib": round(self.cached_bytes / 1048576, 1),
            "cache_seconds": round(self.cache_seconds, 2),
        }


def _runs(op: MoveOp) -> list[tuple[int, int, int]]:
    """``(源 LBA, 目标 LBA, 块数)``，按 VCN 顺序。

    目标 LBA 是 ``dst_start + 该段之前的累计块数`` —— **不是**"源地址 + 位移"。
    写成后者会让文件内容整体错位，而错位后的数据依然"看起来正常"（长度对、
    校验和自洽），属于最难查的那类静默错误。
    """
    out: list[tuple[int, int, int]] = []
    vcn = 0
    for e in op.src_extents:
        out.append((e.start, op.dst_start + vcn, e.length))
        vcn += e.length
    return out


class CachedCopyExecutor:
    """按 ``run_op`` 契约逐个搬运 op，数据经过工作目录中转。

    与 ``NtfsMoveExecutor`` **接口相同、实现独立**：调度器只认 ``run_op``。
    """

    def __init__(
        self,
        src,
        dst,
        params,
        config: CacheConfig,
        control=None,
    ) -> None:
        self.src = src
        self.dst = dst
        self.params = params
        self.config = config
        self.control = control
        self.stats = IoStats()
        self._seq = 0
        self._closed = False

        os.makedirs(self.config.work_dir, exist_ok=True)
        self._check_space()

    # -- 对外 -----------------------------------------------------------

    def run_op(self, op: MoveOp, start_blocks: int = 0) -> OpResult:
        """搬运一个 op。``start_blocks`` 是断点续做：前这么多块已经搬完了。"""
        block_size = self.params.block_size
        runs = _runs(op)
        total = sum(n for _, _, n in runs)
        if total == 0:
            return OpResult(op.op_id, 0, 0, ok=True)

        path = self._cache_path(op)
        done = max(0, start_blocks)
        written = 0
        try:
            filled = self._fill_cache(op, runs, path, block_size)
            if filled < total:
                # 第一阶段被打断（取消），缓存文件里只有前 filled 块是真数据。
                # **绝不能让第二阶段去读它**：``preallocate`` 把文件扩到了最终
                # 大小，未写部分是零，读出来写进目标就是拿零覆盖别人的数据；
                # 断点续做时更糟 —— 前面 done 块被跳过，正好从零区长开始写。
                return OpResult(
                    op.op_id,
                    0,
                    0,
                    ok=False,
                    cancelled=True,
                    error=f"写缓存被中断（{filled}/{total} 块），未向目标写入任何数据",
                )
            written = self._drain_cache(op, runs, path, block_size, done)
        except DeviceError as exc:
            return OpResult(
                op.op_id,
                written,
                written * block_size,
                ok=False,
                error=f"{exc}",
                stage="缓存搬运",
                error_type=type(exc).__name__,
                error_class=getattr(
                    getattr(exc, "error_class", None), "value", ErrorClass.RETRYABLE.value
                ),
                winerror=getattr(exc, "winerror", 0) or 0,
            )
        except OSError as exc:
            # 缓存盘满、路径不可写、文件被占用 —— 这些都只影响这一个 op，
            # 但都不该重试（同一个错误会立刻再发生一次）。
            hint = ""
            if getattr(exc, "errno", None) == 28:  # ENOSPC / ERROR_DISK_FULL
                need = total * block_size / 1048576
                hint = (
                    f"（这个文件需要 {need:.1f} MiB 中转空间，"
                    f"缓存目录 {self.config.work_dir} 空间不足）"
                )
            return OpResult(
                op.op_id,
                written,
                written * block_size,
                ok=False,
                error=f"缓存文件操作失败{hint}：{exc}",
                stage="写缓存",
                error_type=type(exc).__name__,
                error_class=ErrorClass.FATAL.value,
            )
        finally:
            self._drop_cache(path)

        if self.control is not None and getattr(self.control, "cancelled", False):
            return OpResult(op.op_id, written, written * block_size, ok=False, cancelled=True)

        return OpResult(op.op_id, written, written * block_size, ok=True)

    def close(self) -> None:
        self._closed = True

    # -- 内部 -----------------------------------------------------------

    def _cache_path(self, op: MoveOp) -> str:
        self._seq += 1
        return os.path.join(
            self.config.work_dir, f"lbamover-cache-{op.op_id:06d}-{self._seq}.bin"
        )

    def _drop_cache(self, path: str) -> None:
        if self.config.keep_cache:
            return
        try:
            os.unlink(path)
        except OSError:
            # 删不掉不是搬运失败：缓存目录里留一个孤儿文件，下次运行会覆盖
            # 同名序号（序号带自增，实际是新的文件名）。不该因此判这个 op 失败。
            pass

    def _check_space(self) -> None:
        """缓存盘至少要能装下最大的一次搬运，否则跑到一半才失败。"""
        try:
            free = shutil.disk_usage(self.config.work_dir).free
        except OSError:
            return
        biggest = self.params.chunk_blocks * self.params.block_size
        if free < biggest * 2:
            raise DeviceError(
                f"缓存目录可用空间不足（剩 {free / 1048576:.1f} MiB，"
                f"至少要 {biggest * 2 / 1048576:.1f} MiB）：{self.config.work_dir}"
            )

    def _iter_chunks(self, runs, block_size: int) -> Iterator[tuple[int, int, int, int]]:
        """把 run 列表切成 ``(源 LBA, 目标 LBA, 块数, 整体块偏移)``，整体按序。"""
        offset = 0
        for src_lba, dst_lba, blocks in runs:
            pos = 0
            while pos < blocks:
                n = min(self.config.chunk_blocks, blocks - pos)
                yield src_lba + pos, dst_lba + pos, n, offset + pos
                pos += n
            offset += blocks

    def _fill_cache(self, op: MoveOp, runs, path: str, block_size: int) -> int:
        """阶段一：读源 → 顺序写缓存。返回**真正写进缓存的块数**。

        返回值不是装饰：调用方必须靠它判断缓存文件是不是完整。被打断时缓存
        里只有前半段是真数据，后半段（``preallocate`` 扩出来的）是零 ——
        拿它去写目标就是拿零覆盖别人的数据。
        """
        total = sum(n for _, _, n in runs)
        expected = total * block_size
        started = time.monotonic()
        last_end = -1
        filled = 0
        with open(path, "wb") as fh:
            if self.config.preallocate and expected > 0:
                # 一次性扩到最终大小，让文件系统预留连续空间。写入过程中反复
                # 扩展会把缓存文件切碎，顺序性就没了 —— 那正是这个引擎要的。
                fh.truncate(expected)
                fh.seek(0)
            for src_lba, _dst_lba, n, _off in self._iter_chunks(runs, block_size):
                data = self.src.read_blocks(src_lba, n)
                if len(data) != n * block_size:
                    # 与 BlockDevice 契约一致：短读必须是错误。写进缓存的话，
                    # 它会让整个 op 的数据从这一点起错位（长度对、内容错）。
                    raise DeviceError(
                        f"读取不完整：期望 {n * block_size} 字节，实得 {len(data)} 字节"
                        f"（LBA {src_lba}，{n} 块）"
                    )
                fh.write(data)
                filled += n
                self.stats.src_reads += 1
                # 第一次没有"上一段"可比，它不构成跳读 —— 把起点算成非顺序
                # 会让单段文件永远显示 87.5%，读数的人以为有优化空间。
                if last_end < 0 or src_lba == last_end:
                    self.stats.src_seq_reads += 1
                last_end = src_lba + n
                self.stats.cache_writes += 1  # 追加写，天然顺序
                self.stats.cached_bytes += len(data)
                if self.control is not None and getattr(self.control, "cancelled", False):
                    break
        self.stats.cache_seconds += time.monotonic() - started
        return filled

    def _drain_cache(
        self, op: MoveOp, runs, path: str, block_size: int, done: int
    ) -> int:
        """阶段二：顺序读缓存 → 写目标。返回本次写出的块数。"""
        written = 0
        last_end = -1
        with open(path, "rb") as fh:
            for _src_lba, dst_lba, n, off in self._iter_chunks(runs, block_size):
                if off + n <= done:
                    continue  # 断点之前的部分不再写
                skip = max(0, done - off)
                n -= skip
                dst_lba += skip
                if n <= 0:
                    continue
                fh.seek((off + skip) * block_size)
                data = fh.read(n * block_size)
                if len(data) != n * block_size:
                    raise DeviceError(
                        f"缓存文件读取不完整：期望 {n * block_size} 字节，"
                        f"实得 {len(data)}"
                    )
                self.dst.write_blocks(dst_lba, data)
                if getattr(self.params, "verify", False):
                    # 回读校验。界面上的「校验=开」承诺的就是这件事，而缓存
                    # 往返最典型的错正是"长度对、内容错"——只有回读能发现。
                    back = self.dst.read_blocks(dst_lba, n)
                    if bytes(back) != data:
                        raise DeviceError(
                            f"回读校验不一致（LBA {dst_lba}，{n} 块）："
                            "写下去的数据读回来不一样",
                            error_class=ErrorClass.VERIFY_MISMATCH,
                            lba=dst_lba,
                            count=n,
                        )
                self.stats.cache_reads += 1  # 从 0 往后顺序读
                self.stats.dst_writes += 1
                if last_end < 0 or dst_lba == last_end:
                    self.stats.dst_seq_writes += 1
                last_end = dst_lba + n
                written += n
                if self.control is not None and getattr(self.control, "cancelled", False):
                    break
        return written
