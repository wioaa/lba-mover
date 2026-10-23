"""执行期**影子位图**（纯算法层，无 Qt / 无 ctypes / 无 IO）。

## 为什么必须有它（v2 阻断缺陷 FIX-1）

规划期用的是**某一瞬间**的位图快照。执行期若继续用它做守卫校验，就会出现：

1. 驱逐任务先把挡路文件搬出目标区间 → 区间内那些簇**现在**已经空闲；
2. 但静态快照里它们仍然"被占用"，于是搬运任务的 `guard.allow()` 被拒绝；
3. 被拒段记一条 failure 后**静默跳过** → 依赖驱逐的计划搬动 **0 簇**。

量化（QA `p0_4b_deep.py` 的 S9-strong：40 簇区间占满 / 10 个 4 簇文件 /
10 个挡路者 / 共 20 段）：静态快照拒绝 **10/20**，每步刷新的影子位图拒绝 **0/20**。
也就是说"目标区间被占用 → 先腾走再搬进来"这个 v2 核心能力在运行期完全失效。

## 三层数据来源（本模块只负责第 2、3 层的数据结构）

1. **基线**：执行开始时**重新读取**一次全卷位图（禁止复用规划期快照）；
2. **本地变更**：每段成功后 `mark_free(src)` + `mark_used(dst)` —— 这是我们
   亲眼确认过的事实，**权威**，后续任何来自磁盘的补丁都不得覆盖它（``_local``
   位图记录"我们改过的簇"）；
3. **外部变更**：每段执行前对该段的 `src`/`dst` 区间做**局部**重读
   （``FSCTL_GET_VOLUME_BITMAP`` 支持 ``StartingLcn``，只取一片），
   专门抓"计划生成之后被外部进程占用的目标簇"，用 :meth:`ShadowBitmap.patch`
   合入；同样只补**非本地**簇。

## 与 ``SafetyGuard`` 的关系

``SafetyGuard._check`` 只需要一个带 ``range_is_free_fast`` 的对象。
:class:`ShadowBitmap` 提供**同名同语义**的方法，因此守卫可以直接把它当成
位图视图（``guard.refresh(shadow)``），不需要第二套校验逻辑。
"""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from app.core.models import BitmapSnapshot, range_free_fast
from app.core.ranges import merge_runs

Run = Tuple[int, int]


class ShadowBitmap:
    """可写的位图影子：位 1 = 已占用，与 :class:`BitmapSnapshot` 完全同构。

    只允许三种变更来源：:meth:`mark_free` / :meth:`mark_used` / :meth:`patch`。
    所有越界操作都被**夹紧**（不抛异常），因为执行期的一次越界不应该让整批任务
    崩掉 —— 守卫会独立地拒绝越界目标。
    """

    __slots__ = ("_bits", "_local", "_total", "_bytes_per_cluster")

    def __init__(
        self,
        bits: bytes = b"",
        total_clusters: int = 0,
        bytes_per_cluster: int = 0,
    ) -> None:
        total = max(0, int(total_clusters))
        needed = (total + 7) // 8
        raw = bytearray(bits[:needed]) if bits else bytearray()
        if len(raw) < needed:
            raw.extend(b"\x00" * (needed - len(raw)))
        self._bits = raw
        self._local = bytearray(len(raw))
        self._total = total
        self._bytes_per_cluster = max(0, int(bytes_per_cluster))

    # ---------------- 构造 / 导出 ---------------- #
    @classmethod
    def from_snapshot(cls, snapshot: Optional[BitmapSnapshot]) -> "ShadowBitmap":
        """从位图快照克隆（``None`` 时得到空影子，随后 :meth:`is_ready` 为 False）。"""
        if snapshot is None:
            return cls()
        return cls(
            bits=snapshot.bits,
            total_clusters=snapshot.total_clusters,
            bytes_per_cluster=snapshot.bytes_per_cluster,
        )

    def clone(self) -> "ShadowBitmap":
        """深拷贝（含本地变更标记），用于"先试算再提交"的场景。"""
        copy = ShadowBitmap(
            bits=bytes(self._bits),
            total_clusters=self._total,
            bytes_per_cluster=self._bytes_per_cluster,
        )
        copy._local = bytearray(self._local)
        return copy

    def to_snapshot(self) -> BitmapSnapshot:
        """导出为只读快照（给不接受影子的旧调用方用）。"""
        return BitmapSnapshot(
            bits=bytes(self._bits),
            total_clusters=self._total,
            bytes_per_cluster=self._bytes_per_cluster,
        )

    # ---------------- 只读属性 ---------------- #
    @property
    def bits(self) -> bytes:
        """当前位图字节（拷贝，外部改不到内部状态）。"""
        return bytes(self._bits)

    @property
    def total_clusters(self) -> int:
        return self._total

    @property
    def bytes_per_cluster(self) -> int:
        return self._bytes_per_cluster

    @property
    def is_ready(self) -> bool:
        """基线是否可用（总簇数 > 0 且字节数足够）。"""
        return self._total > 0 and len(self._bits) * 8 >= self._total

    def is_free(self, lcn: int) -> bool:
        """单簇查询；越界视为不空闲（安全优先，与快照一致）。"""
        if lcn < 0 or lcn >= self._total:
            return False
        index = lcn >> 3
        if index >= len(self._bits):
            return False
        return not ((self._bits[index] >> (lcn & 7)) & 1)

    def range_is_free(self, start: int, clusters: int) -> bool:
        """逐簇校验（慢速版，仅供调试 / 单测）。"""
        if clusters <= 0 or start < 0 or start + clusters > self._total:
            return False
        for lcn in range(start, start + clusters):
            if not self.is_free(lcn):
                return False
        return True

    def range_is_free_fast(self, start: int, clusters: int) -> bool:
        """``SafetyGuard`` 用的入口 —— 与 ``BitmapSnapshot`` **同名同语义**。"""
        return range_free_fast(self._bits, self._total, start, clusters)

    def count_used(self, start: int, end: int) -> int:
        """统计 ``[start, end)`` 内被占用的簇数。"""
        begin = max(0, int(start))
        finish = min(self._total, int(end))
        if finish <= begin:
            return 0
        used = 0
        for lcn in range(begin, finish):
            if not self.is_free(lcn):
                used += 1
        return used

    # ---------------- 变更 ---------------- #
    def mark_free(self, lcn: int, clusters: int = 1) -> int:
        """把 ``[lcn, lcn+clusters)`` 标记为**空闲**，返回实际改变的位数。"""
        return self._apply(lcn, clusters, used=False)

    def mark_used(self, lcn: int, clusters: int = 1) -> int:
        """把 ``[lcn, lcn+clusters)`` 标记为**已占用**，返回实际改变的位数。"""
        return self._apply(lcn, clusters, used=True)

    def patch(
        self,
        lcn: int,
        chunk: bytes,
        *,
        bit_offset: int = 0,
        respect_local: bool = True,
    ) -> int:
        """把一段**从磁盘重读**的原始位图字节合入影子。

        Args:
            lcn: ``chunk`` 第 ``bit_offset`` 位对应的簇号（通常是
                ``read_bitmap_slice`` 返回的对齐起点，此时 ``bit_offset=0``）。
            chunk: 位图字节（bit 1 = 占用），低位在前。
            bit_offset: ``chunk`` 内起始位偏移（0..7）；用于起点未按 8 对齐的情形。
            respect_local: 为 ``True``（默认）时**不覆盖本地已改过的簇** ——
                本地变更是我们亲眼确认的事实（例如"这段 src 刚被搬空"），
                磁盘上的旧字节没有资格推翻它。

        Returns:
            实际写入的位数（被 ``respect_local`` 挡掉的不计）。
        """
        if not chunk:
            return 0
        if bit_offset < 0 or bit_offset > 7:
            raise ValueError("bit_offset 必须在 0..7 之间")

        begin = int(lcn) - bit_offset
        width = len(chunk) * 8
        # chunk 的 bit 0 对应簇 ``begin``，bit ``bit_offset`` 对应簇 ``lcn``；
        # 因此这里**不**对 source 右移（右移会把两个偏移重复应用一次）。
        source = int.from_bytes(chunk, "little")
        if begin < 0:
            shift = -begin
            source >>= shift
            width -= shift
            begin = 0
        if self._total > 0:
            width = min(width, self._total - begin)
        if width <= 0:
            return 0
        source &= (1 << width) - 1

        first_byte = begin >> 3
        bit_in_byte = begin & 7
        span_bytes = (bit_in_byte + width + 7) // 8
        existing = int.from_bytes(self._bits[first_byte : first_byte + span_bytes], "little")
        local = int.from_bytes(self._local[first_byte : first_byte + span_bytes], "little")

        mask = ((1 << width) - 1) << bit_in_byte
        applied = mask & (~local if respect_local else mask)
        if not applied:
            return 0

        value = existing
        # 只覆盖 applied 覆盖到的位
        new_bits = (source << bit_in_byte) & applied
        value = (value & ~applied) | new_bits
        changed = bin((existing ^ value) & applied).count("1")

        self._bits[first_byte : first_byte + span_bytes] = value.to_bytes(
            span_bytes, "little"
        )
        return changed

    def _apply(self, lcn: int, clusters: int, *, used: bool) -> int:
        """位级 set/reset，返回实际改变的位数（越界自动夹紧）。"""
        if clusters <= 0:
            return 0
        start = max(0, int(lcn))
        end = min(self._total, int(lcn) + int(clusters))
        if end <= start:
            return 0

        changed = 0
        first_byte = start >> 3
        last_byte = (end - 1) >> 3
        for index in range(first_byte, last_byte + 1):
            byte_start = index * 8
            low = max(start - byte_start, 0)
            high = min(end - byte_start, 8)
            if high <= low:
                continue
            mask = ((1 << (high - low)) - 1) << low
            old = self._bits[index]
            new = (old | mask) if used else (old & (~mask & 0xFF))
            if new != old:
                changed += bin(old ^ new).count("1")
                self._bits[index] = new
            # 本地变更标记：这些簇从此不接受磁盘补丁的覆盖
            self._local[index] |= mask
        return changed

    # ---------------- 诊断 ---------------- #
    def local_runs(self) -> List[Run]:
        """本地改过的簇区间（已合并），用于日志 / 单测。"""
        runs: List[Run] = []
        for index, byte in enumerate(self._local):
            if not byte:
                continue
            for bit in range(8):
                if byte & (1 << bit):
                    runs.append((index * 8 + bit, index * 8 + bit + 1))
        return merge_runs(runs)

    @property
    def local_clusters(self) -> int:
        """本地改过的簇数。"""
        return sum(int(byte).bit_count() for byte in self._local)

    def describe(self) -> str:
        """一行中文摘要（日志用）。"""
        used = sum(int(byte).bit_count() for byte in self._bits)
        return (
            f"影子位图 {self._total:,} 簇（占用 {used:,}，"
            f"本地变更 {self.local_clusters:,}）"
        )


def apply_move(shadow: ShadowBitmap, src_lcn: int, dst_lcn: int, clusters: int) -> int:
    """登记一次**成功**的搬运：源变空闲、目标被占用。

    顺序不可颠倒（源先释放、目标后占用，两者区间可能相交/相邻）。

    Returns:
        实际改变的位数（便于日志与单测断言）。
    """
    if clusters <= 0:
        return 0
    changed = shadow.mark_free(src_lcn, clusters)
    changed += shadow.mark_used(dst_lcn, clusters)
    return changed


def apply_task_segments(shadow: ShadowBitmap, segments: Sequence[object]) -> int:
    """按段列表登记成功搬运（``segments`` 需有 ``src_lcn``/``dst_lcn``/``clusters``）。"""
    changed = 0
    for segment in segments:
        changed += apply_move(
            shadow,
            int(getattr(segment, "src_lcn", -1)),
            int(getattr(segment, "dst_lcn", -1)),
            int(getattr(segment, "clusters", 0)),
        )
    return changed
