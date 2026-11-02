"""内存缓存搬运：数据经由一个**预分配的固定块池**中转，按块回写。

**这是第三套独立引擎**，与 ``NATIVE``（盘内搬簇 / 逐块直写）和 ``CACHED``
（磁盘文件中转）都不共享执行代码。三条路径的失败方式完全不同，混在一起
调一次就要同时理解三种语义 —— 这个项目在这上面吃过亏，所以宁可有重复骨架。

## 与磁盘缓存的区别（为什么要有它）

``CACHED`` 把数据落到**文件**上，靠文件系统给顺序性；代价是数据要多走两趟
盘（写缓存 + 读缓存）。``MEMCACHED`` 把中间那一段放进内存，于是：

* 缓存侧没有 IO —— 只有内存带宽，不存在"缓存盘成瓶颈"；
* 一次 op 内的**多个小段**可以在内存里攒成**一次大写**。这才是它真正的
  收益：源文件碎成 8 段 × 128 KiB、目标是一段连续的 1 MiB 时，流式搬运要
  写 8 次，块池只在攒满 1 MiB 后写 1 次；
* 内存占用**确定**：块池在 ``prepare`` 一次分配完，运行期不再增长，
  上界就是 ``block_count × block_bytes``。

它**不是**"更快的真机搬运"。和 ``CACHED`` 一样，它只作用于
:class:`BlockDevice`（镜像、内存盘、已锁定的原始卷）；已挂载的 NTFS 卷会被
引擎拒绝 —— 裸写会绕过文件系统覆盖 ``$MFT`` 指着的簇区。

## 块满时的替换策略：先进先出

块池里的每一块都是"写入一次、回写一次"，**没有重复访问**。所以 LRU / LFU
这类"按热度淘汰"的策略在这里毫无意义：它们要预测"哪块还会被用到"，而这里
每块只被用到一次。FIFO 是唯一自洽的选择 —— 最早填满的块最早回写。

这也解释了为什么它不叫"缓存"：它不命中、不复用，是一个**写回缓冲池**。
名字沿用 ``memcache`` 只是为了让"缓存搬运"这个用户概念保持一致。

## 回写粒度

一块攒满就回写；相邻且目标地址连续的多块会**合并成一次写**（上限
``coalesce_limit_bytes``，默认 4 MiB）。合并的代价是临时多出一份字节数组，
所以要有上限 —— 否则 32 MiB 的池每次回写都要再复制 32 MiB。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from ..contract.enums import ErrorClass
from ..contract.models import MoveOp
from ..devices.base import DeviceError
from .pipeline import OpResult

__all__ = ["MemCacheConfig", "MemCacheStats", "MemCacheExecutor"]


@dataclass(frozen=True)
class MemCacheConfig:
    """块池的几何：**块数 × 块大小**。两者的含义不同，不要合并成一个数字。

    * ``block_bytes`` 决定"一次能凑多长"：块越大，目标盘上的小写越少，
      但攒满一块要等更久（延迟更高）；
    * ``block_count`` 决定"能提前读多少"：它同时是池的内存上限，
      也是池满前允许的读-写错位深度。

    默认 32 × 1 MiB = 32 MiB：足够把一个碎成十几段的文件的整段读完再写，
    又小到不会在任何机器上引起内存压力。
    """

    block_count: int = 32
    block_bytes: int = 1 << 20
    #: 相邻脏块合并回写的字节上限。合并要多一次内存拷贝，所以它有上限。
    coalesce_limit_bytes: int = 4 << 20

    def __post_init__(self) -> None:
        if self.block_count <= 0:
            raise ValueError("block_count 必须为正")
        if self.block_bytes <= 0:
            raise ValueError("block_bytes 必须为正")
        if self.coalesce_limit_bytes < self.block_bytes:
            raise ValueError("coalesce_limit_bytes 不能小于单块大小")

    @property
    def total_bytes(self) -> int:
        return self.block_count * self.block_bytes

    @classmethod
    def from_params(cls, params) -> "MemCacheConfig":
        """从 ``JobParams`` 造配置。块大小按**块对齐**，不能留半块尾巴。

        ``mem_cache_block_kib`` 是 KiB，而块大小是块（block_size）的整数倍
        关系 —— 若用户填了一个不是 block_size 整数倍的 KiB 数（例如 100 KiB
        配 4 KiB 块是整数倍，100 KiB 配 3 KiB 块就不是），向下取整到整块，
        宁可小一点也绝不能让块里出现"半个块"，那会让目标地址对不齐。
        """
        block_size = max(1, int(params.block_size))
        want = max(block_size, int(params.mem_cache_block_kib) * 1024)
        aligned = (want // block_size) * block_size
        return cls(
            block_count=max(1, int(params.mem_cache_blocks)),
            block_bytes=aligned,
        )


@dataclass
class MemCacheStats:
    """块池的运行指标。与 ``IoStats`` 分开：这里的重点是**池**的行为
    （淘汰了几次、合并成了多长的写），不是两条盘各自的顺序率。"""

    blocks_allocated: int = 0
    peak_dirty: int = 0
    evictions: int = 0
    flushes: int = 0
    coalesced_blocks: int = 0
    src_reads: int = 0
    src_seq_reads: int = 0
    dst_writes: int = 0
    dst_seq_writes: int = 0
    bytes_read: int = 0
    bytes_written: int = 0

    def to_dict(self) -> dict[str, float]:
        return {
            "blocks_allocated": self.blocks_allocated,
            "peak_dirty": self.peak_dirty,
            "evictions": self.evictions,
            "flushes": self.flushes,
            "coalesced_blocks": self.coalesced_blocks,
            "src_reads": self.src_reads,
            "dst_writes": self.dst_writes,
            "buffered_mib": round(self.bytes_read / 1048576, 1),
        }


class _Slot:
    """池里的一块。``buf`` 在池生命周期内**只分配一次**，之后反复复用。"""

    __slots__ = ("buf", "used", "dst_lba", "dirty")

    def __init__(self, size: int) -> None:
        self.buf = bytearray(size)
        self.used = 0
        self.dst_lba = 0
        self.dirty = False


def _runs(op: MoveOp) -> list[tuple[int, int, int]]:
    """``(源 LBA, 目标 LBA, 块数)``，按 VCN 顺序。

    目标 LBA 是 ``dst_start + 该段之前的累计块数`` —— **不是**"源地址 + 位移"。
    写成后者会让文件内容整体错位，而错位后的数据依然"看起来正常"（长度对、
    校验和自洽），属于最难查的那类静默错误。

    与 ``cache_engine._runs`` 是同一份算术。**刻意不共享**（两个引擎互不
    依赖），代价是可能各自漂移 —— 所以 ``test_memcache_engine.py`` 里有一条
    测试直接对拍两者的输出。
    """
    out: list[tuple[int, int, int]] = []
    vcn = 0
    for e in op.src_extents:
        if e.length <= 0:  # 空段没有数据可读，留着会让切块循环原地打转
            continue
        out.append((e.start, op.dst_start + vcn, e.length))
        vcn += e.length
    return out


class MemCacheExecutor:
    """按 ``run_op`` 契约搬运，中间数据走内存块池。

    接口与 ``NtfsMoveExecutor`` / ``CachedCopyExecutor`` 一致：
    ``prepare(ops)``（可选，调度器会调用）、``run_op(op, start_blocks)``、
    ``close()``。调度器只认这三样，所以三套引擎可以互换。
    """

    def __init__(self, src, dst, params, config: MemCacheConfig, control=None) -> None:
        self.src = src
        self.dst = dst
        self.params = params
        self.config = config
        self.control = control
        self.stats = MemCacheStats()
        self._slots: list[_Slot] = []
        self._free: deque[_Slot] = deque()
        self._dirty: deque[_Slot] = deque()
        self._closed = False
        self._last_src_end = -1
        self._last_dst_end = -1
        #: 当前 op 已回写（真正落到目标上）的块数
        self._written_blocks = 0

        self._block_size = max(1, int(params.block_size))
        #: 一块能装几个卷块。至少 1 —— 配置里块比簇小时向下取整会得到 0，
        #: 那会让下面的循环永远不前进。
        self._blocks_per_slot = max(1, config.block_bytes // self._block_size)

    # -- 生命周期 ---------------------------------------------------------

    def prepare(self, ops=None) -> None:
        """一次性分配整个块池。

        为什么在 ``prepare`` 而不是 ``run_op`` 里懒分配：块池的意义是"内存
        占用固定且可预测"。跑到一半才开始分配，遇到 MemoryError 时已经写过
        一部分块，比一开始就失败糟得多。
        """
        self._ensure_pool()

    def _ensure_pool(self) -> None:
        if self._slots:
            return
        self._slots = [_Slot(self.config.block_bytes) for _ in range(self.config.block_count)]
        self._free.extend(self._slots)
        self.stats.blocks_allocated = len(self._slots)

    def close(self) -> None:
        """释放块池。可重复调用（调度器在 finally 里调，测试也会显式调）。"""
        self._closed = True
        self._slots = []
        self._free.clear()
        self._dirty.clear()

    # -- 对外 -------------------------------------------------------------

    def run_op(self, op: MoveOp, start_blocks: int = 0) -> OpResult:
        """搬运一个 op。``start_blocks`` 是断点续做：前这么多块已经写完了。"""
        runs = _runs(op)
        total = sum(n for _, _, n in runs)
        if total == 0:
            return OpResult(op.op_id, 0, 0, ok=True)

        done = max(0, start_blocks)
        if done >= total:
            # 断点记录说"这块已经搬完"，却又被判为未完成 —— 宁可整份重做，
            # 也不要在这种矛盾状态下跳过数据。
            done = 0

        # **只统计真正落到目标上的块数**。这里最容易出的错是"读了就算搬了"：
        # 断点按这个数续做，多算一块就意味着那一块永远没人写 —— 数据静默缺失。
        self._written_blocks = 0
        try:
            self._ensure_pool()
            for vcn, dst_lba, blocks, parts in self._iter_slots(runs, total, op.dst_start):
                if vcn + blocks <= done:
                    continue
                skip = max(0, done - vcn)
                self._fill_slot(dst_lba + skip, blocks - skip, parts, skip)
                if self._cancelled():
                    # 已经读进池的数据块仍然写出去：它们的目标地址在规划时
                    # 已经验证过是空的，写下去只会让续做少做一点，不会写坏任何
                    # 东西。丢掉它们反而会让上面的计数与磁盘不一致。
                    self._flush_all()
                    n = self._written_blocks
                    return OpResult(
                        op.op_id,
                        n,
                        n * self._block_size,
                        ok=False,
                        cancelled=True,
                    )
            self._flush_all()
        except DeviceError as exc:
            # 读源/写目标的失败：重试可能成功（块级 IO 错误多为随机）
            return OpResult(
                op.op_id,
                self._written_blocks,
                self._written_blocks * self._block_size,
                ok=False,
                error=f"{exc}",
                stage="内存缓存搬运",
                error_type=type(exc).__name__,
                error_class=ErrorClass.RETRYABLE.value,
                winerror=getattr(exc, "winerror", 0) or 0,
            )
        except MemoryError:
            return OpResult(
                op.op_id,
                self._written_blocks,
                self._written_blocks * self._block_size,
                ok=False,
                error=(
                    f"内存块池分配失败（需要 {self.config.total_bytes / 1048576:.0f} MiB，"
                    f"{self.config.block_count} 块 × {self.config.block_bytes / 1024:.0f} KiB）"
                ),
                stage="分配块池",
                error_type="MemoryError",
                error_class=ErrorClass.FATAL.value,
            )

        n = self._written_blocks
        return OpResult(op.op_id, n, n * self._block_size, ok=True)

    # -- 内部：切块 -------------------------------------------------------

    def _iter_slots(self, runs, total: int, dst_start: int):
        """把 op 按块池粒度切块。

        返回值每项是 ``(vcn, 目标 LBA, 块数, [(源 LBA, 块数), ...])``。一个块
        里可以装**多个源段** —— 这正是合并写的前提：源是碎的，目标是连续的，
        内存里攒成一块之后只写一次。
        """
        bps = self._blocks_per_slot
        idx = 0
        off = 0
        vcn = 0
        while vcn < total:
            n = min(bps, total - vcn)
            parts: list[tuple[int, int]] = []
            left = n
            while left > 0:
                src_lba, _dst_lba, blocks = runs[idx]
                take = min(left, blocks - off)
                parts.append((src_lba + off, take))
                left -= take
                off += take
                if off >= blocks:
                    idx += 1
                    off = 0
            yield vcn, dst_start + vcn, n, parts
            vcn += n

    # -- 内部：填块与回写 -------------------------------------------------

    def _fill_slot(
        self, dst_lba: int, blocks: int, parts: list[tuple[int, int]], skip: int
    ) -> None:
        """把这一块的数据读进池里。攒满即成为"脏块"，等回写。"""
        slot = self._acquire_slot()
        buf = slot.buf
        used = 0
        remaining = skip
        for src_lba, take in parts:
            if remaining >= take:
                remaining -= take
                continue
            start = src_lba + remaining
            count = take - remaining
            remaining = 0
            data = self.src.read_blocks(start, count)
            expect = count * self._block_size
            if len(data) != expect:
                # BlockDevice 契约是"要么给满、要么抛错"。短读意味着设备层
                # 有 bug，继续往下写会把残缺数据当正常数据写进目标。
                raise DeviceError(
                    f"读取不完整：期望 {expect} 字节，实得 {len(data)}",
                    error_class=ErrorClass.FATAL,
                    lba=start,
                    count=count,
                )
            buf[used : used + len(data)] = data
            used += len(data)
            self.stats.src_reads += 1
            self.stats.bytes_read += len(data)
            # 顺序率：第一段没有"上一段"可比，算作顺序（否则单段文件永远
            # 显示 87.5%，读数的人会以为有优化空间）
            if self._last_src_end < 0 or start == self._last_src_end:
                self.stats.src_seq_reads += 1
            self._last_src_end = start + count

        slot.used = used
        slot.dst_lba = dst_lba
        slot.dirty = True
        self._dirty.append(slot)
        self.stats.peak_dirty = max(self.stats.peak_dirty, len(self._dirty))
        _ = blocks  # 只用于说明"这一块装了多少块"，不参与记账

    def _acquire_slot(self) -> _Slot:
        """取一块空闲槽位。**池满时按先进先出淘汰最老的一块**（即回写它）。"""
        if not self._free:
            self._flush_oldest()
            self.stats.evictions += 1
        return self._free.popleft()

    def _flush_all(self) -> None:
        while self._dirty:
            self._flush_oldest()

    def _flush_oldest(self) -> None:
        """回写最老的一块；与它目标地址连续的后继块合并成一次写。"""
        first = self._dirty.popleft()
        batch = [first]
        total = first.used
        while self._dirty:
            nxt = self._dirty[0]
            prev = batch[-1]
            # 只有"上一块写完的位置正好是下一块的目标起点"才能合并 ——
            # 否则一次写会跨过别人的数据。
            if nxt.dst_lba * self._block_size != prev.dst_lba * self._block_size + prev.used:
                break
            if total + nxt.used > self.config.coalesce_limit_bytes:
                break
            self._dirty.popleft()
            batch.append(nxt)
            total += nxt.used

        if len(batch) == 1:
            slot = batch[0]
            data = memoryview(slot.buf)[: slot.used]
            self.dst.write_blocks(slot.dst_lba, data)
        else:
            merged = bytearray(total)
            off = 0
            for slot in batch:
                merged[off : off + slot.used] = slot.buf[: slot.used]
                off += slot.used
            self.dst.write_blocks(batch[0].dst_lba, merged)
            self.stats.coalesced_blocks += len(batch)

        # 顺序写统计：目标地址接着上一次写的末尾就算顺序
        head = batch[0]
        self._written_blocks += total // self._block_size
        if self._last_dst_end < 0 or head.dst_lba == self._last_dst_end:
            self.stats.dst_seq_writes += 1
        self._last_dst_end = head.dst_lba + total // self._block_size
        self.stats.dst_writes += 1
        self.stats.flushes += 1
        self.stats.bytes_written += total

        for slot in batch:
            slot.used = 0
            slot.dirty = False
            self._free.append(slot)

    def _cancelled(self) -> bool:
        return self.control is not None and bool(getattr(self.control, "cancelled", False))
