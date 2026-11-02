"""执行器：把「计划里的一个 op」真正落到卷上。

两条路径，语义完全不同，不能混用：

================================  ==========================================
执行器                             适用场景
================================  ==========================================
:class:`BlockCopyExecutor`        内存盘 / 镜像文件：逐块 读→写，可回读校验
:class:`NtfsMoveExecutor`         真机 NTFS：``FSCTL_MOVE_FILE`` 让文件系统
                                  自己搬簇，同时更新元数据
================================  ==========================================

**为什么真机不能用裸块拷贝。** 裸拷贝只动了数据区，NTFS 的 `$MFT` 里
runlist 仍指向旧位置 —— 文件系统看到的是一个"数据莫名其妙变了"的卷，
结果是文件内容错乱、chunk 链断掉。这不是理论风险，是必然。所以真机路径
必须走 MOVE_FILE，而 MOVE_FILE 要**文件句柄**，不是卷句柄。

还有一个容易被忽略的前置条件：MOVE_FILE 要求卷**没有被独占锁定**。
一旦 `FSCTL_LOCK_VOLUME`，卷上的文件就打不开了。这跟"裸写必须先锁卷"
正好相反，两条路径的打开方式因此必然不同。
"""

from __future__ import annotations

from typing import Callable, Protocol, runtime_checkable

from ..contract.enums import ErrorClass
from ..contract.models import Extent, JobParams, MoveOp
from ..devices.base import DeviceError
from .chunk import BlockSlice
from .control import RunControl
from .errors import CancelledError
from .pipeline import ChunkPipeline, OpResult
from .retry import RetryPolicy, classify_exception, run_with_retry

__all__ = [
    "Executor",
    "BlockCopyExecutor",
    "NtfsMoveExecutor",
    "ClusterMover",
    "layout_mismatch",
]


@runtime_checkable
class ClusterMover(Protocol):
    """簇级搬迁后端。真机实现见 ``devices.win32.Win32ClusterMover``。

    把这三个动作抽象出来，是为了让 :class:`NtfsMoveExecutor` 的全部调度
    逻辑（布局复核、分块、校验、取消）能在没有 Windows 管理员权限的机器上
    被真实测试——测试里注入一个会真正改变布局的假后端即可。
    """

    def open_file(self, path: str, writable: bool = True) -> object: ...

    def query(self, handle: object) -> tuple[tuple[int, int, int], ...]: ...

    def move(self, handle: object, start_vcn: int, start_lcn: int, count: int) -> None: ...

    def close_file(self, handle: object) -> None: ...

    # -- 可选能力：半锁定 ---------------------------------------------------
    # 下面两个**不在 Protocol 里强制**：测试用的假后端只要实现上面四个动作
    # 就能跑完整调度，锁是可选的增强。执行器用 getattr 探测，没有就跳过。
    #
    #   def lock_range(self, handle: object, offset: int, length: int) -> None: ...
    #   def unlock_range(self, handle: object, offset: int, length: int) -> None: ...
    #
    # 语义（真机实现见 Win32ClusterMover）：offset/length 是**文件内字节**
    # 偏移，加的是共享锁 —— 别的进程仍能读，但写不进来。


@runtime_checkable
class Executor(Protocol):
    """JobRunner 眼里只有这一个接口。"""

    def run_op(self, op: MoveOp, start_blocks: int = 0) -> OpResult: ...

    def close(self) -> None: ...


class BlockCopyExecutor:
    """逐块拷贝（内存盘 / 镜像文件 / 离线镜像）。"""

    def __init__(
        self,
        src,
        dst,
        params: JobParams,
        control: RunControl,
        *,
        limiter=None,
        on_chunk: Callable[[BlockSlice], None] | None = None,
        on_retry: Callable[[int, BaseException, float], None] | None = None,
    ) -> None:
        self._pipe = ChunkPipeline(
            src,
            dst,
            params,
            control,
            limiter=limiter,
            on_chunk=on_chunk,
            on_retry=on_retry,
        )

    def run_op(self, op: MoveOp, start_blocks: int = 0) -> OpResult:
        return self._pipe.run_op(op, start_blocks)

    def close(self) -> None:
        return None


def _runs_from_extents(extents: tuple[Extent, ...] | list[Extent]) -> list[tuple[int, int, int]]:
    """把 op 的源区间换算成 ``(start_vcn, start_lcn, count)``。

    引擎的 Extent 已经是「文件内偏移」的顺序排列，所以 VCN 直接累加即可。
    """
    runs: list[tuple[int, int, int]] = []
    vcn = 0
    for e in extents:
        runs.append((vcn, e.start, e.length))
        vcn += e.length
    return runs


def lock_ranges(op: MoveOp, cluster_size: int) -> list[tuple[int, int]]:
    """op 的源区间 → 文件内字节范围 ``(offset, length)``，供半锁定使用。

    VCN 从 0 累加（``Extent`` 按**文件内顺序**排列），乘簇大小得到字节偏移。
    这里绝不能改成"按 LCN 排序再累加"：那样算出的偏移与文件实际内容错位，
    锁到的是别的数据，而"已经锁住了"这个假象会让后面的布局复核也一起失去
    意义 —— 复核比的是 VCN→LCN 映射，锁错地方它照样通过。

    **逐段锁，段与段之间的空洞不锁**：空洞里的簇属于别的文件，替它上锁既
    没必要，也可能挡住与我们无关的写入。这正是「部分簇范围」的字面意思 ——
    只占住本 op 真正要动的那几段，不是把整个文件圈起来。

    相邻两段若首尾相接，上层已先合并为一个 ``Extent``，所以这里不必再合并，
    锁的段数就等于 ``src_extents`` 的长度。
    """
    ranges: list[tuple[int, int]] = []
    vcn = 0
    for extent in op.src_extents:
        if extent.length > 0:
            ranges.append((vcn * cluster_size, extent.length * cluster_size))
        vcn += extent.length
    return ranges


def layout_mismatch(
    live: tuple[tuple[int, int, int], ...], expected: list[tuple[int, int, int]]
) -> str | None:
    """比对「现在的布局」与「扫描时的布局」。返回不一致的原因。

    只校验"扫描时记录的每个 VCN 区间，现在是否仍映射到同一个 LCN"，而**不**
    要求分段完全一致：文件系统可能把相邻的两段合并成一段，那是等价的，不该
    因此拒绝执行。反之，只要有一段对不上，就说明文件在扫描之后被动过，搬它
    会搬到错误的位置。
    """
    if not live:
        return "文件当前没有任何簇映射（可能已被删除或截断）"
    if any(lcn < 0 for _, lcn, _ in live):
        return "文件含稀疏区，本工具不搬这类文件"

    for vcn, lcn, count in expected:
        vcn_left = vcn
        need = count
        for run_vcn, run_lcn, run_count in live:
            if run_lcn < 0:
                continue
            if run_vcn + run_count <= vcn_left:
                continue
            if run_vcn > vcn_left:
                return f"VCN {vcn_left} 附近出现空洞（扫描后被改动）"
            offset = vcn_left - run_vcn
            if offset + need <= run_count:
                if run_lcn + offset != lcn:
                    return (
                        f"VCN {vcn_left} 现在映射到 LCN {run_lcn + offset}，"
                        f"扫描时是 LCN {lcn}（文件已被移动）"
                    )
                need = 0
                break
            # 跨 run 的连续比对：要求下一段的 LCN 紧接
            if run_lcn + offset != lcn:
                return f"VCN {vcn_left} 起始 LCN 不符（扫描时为 {lcn}）"
            consumed = run_count - offset
            need -= consumed
            vcn_left += consumed
            lcn += consumed
        if need > 0:
            return f"VCN {vcn} 起的 {count} 个簇在磁盘上已不连续（扫描后被改动）"
    return None


class NtfsMoveExecutor:
    """真机 NTFS 执行器：靠 ``FSCTL_MOVE_FILE`` 让文件系统自己搬簇。"""

    def __init__(
        self,
        mover: ClusterMover,
        params: JobParams,
        control: RunControl,
        *,
        cluster_size: int,
        limiter=None,
        on_chunk: Callable[[BlockSlice], None] | None = None,
        on_retry: Callable[[int, BaseException, float], None] | None = None,
        verify: bool = True,
        preoccupy=None,
    ) -> None:
        self.mover = mover
        self.params = params
        self.control = control
        self.cluster_size = max(512, cluster_size)
        self.limiter = limiter
        self.on_chunk = on_chunk
        self.on_retry = on_retry
        self.verify = verify
        #: 半锁定没拿到的次数与第一个错误。锁是增强项，失败不该让文件失败，
        #: 但必须留痕 —— 否则用户会以为"开了锁就万事大吉"。
        self.lock_failures = 0
        self.last_lock_error = ""
        #: 目标区预占（可选）。占位文件按 op 建，执行该 op 前释放那一段。
        #: 它是增强项：占不住就按原样跑，只在日志里说明，不阻止搬运。
        self.preoccupy = preoccupy
        self.policy = RetryPolicy(
            max_retries=params.max_retries,
            base_delay=params.retry_base_ms / 1000.0,
            max_delay=params.retry_max_ms / 1000.0,
        )

    # -- 生命周期 ---------------------------------------------------------

    def close(self) -> None:
        """收尾：把还没释放的占位文件全部删掉。

        正常路径下每个 op 执行前都释放过，走到这里时字典应该是空的。
        但取消、异常、崩在中间时不会 —— 那些占位文件留在卷根上，
        用户的资源管理器里会多出一堆 `.lbamover-placeholder-*.tmp`。
        """
        if self.preoccupy is not None:
            self.preoccupy.close()

    def prepare(self, ops) -> None:
        """在任何 op 开始前占住目标区。占不住不阻止执行。

        为什么不能"占不住就拒绝跑"：预占是**降低风险**的手段，不是正确性前提。
        布局复核与 execute 前的审计才是把关的。把它变成硬前置，会让这个功能
        在权限不足的机器上直接把整理功能废掉。
        """
        if self.preoccupy is None:
            return
        report = self.preoccupy.occupy(ops)
        if report.requested:
            level = "占位未完全生效" if not report.all_ok else "占位完成"
            self.lock_failures += 0 if report.all_ok else 1
            self.last_lock_error = report.summary() if not report.all_ok else ""
            if hasattr(self, "on_log") and callable(self.on_log):
                self.on_log(f"{level}：{report.summary()}")
            else:
                self._preoccupy_note = f"{level}：{report.summary()}"

    # -- 单个 op ----------------------------------------------------------

    def run_op(self, op: MoveOp, start_blocks: int = 0) -> OpResult:
        """搬一个文件。

        ``start_blocks`` 在这里被**刻意忽略**：``FSCTL_MOVE_FILE`` 一次搬一段
        连续簇，中途失败会让文件处于"一部分在新位置"的混合状态，此时按旧断点
        跳块没有任何意义。op 级断点（整个文件已完成）仍然有效，见 JobRunner。
        """
        total = op.blocks
        if total <= 0:
            return OpResult(op.op_id, 0, 0, ok=True)

        if self.preoccupy is not None:
            # 释放这个 op 的目标段，紧接着就搬进去 —— 中间只隔一次 open_file。
            # 预占的意义全在这个"紧接着"上：拖久了窗口又变回原来的大小。
            self.preoccupy.release_for(op.op_id)

        try:
            handle = self.mover.open_file(op.path, writable=True)
        except DeviceError as exc:
            # 打不开文件是**最常见**的失败（被独占、权限不足、路径太长），
            # 而这三者的处理方式完全不同，所以阶段与系统错误码必须带出去。
            return self._fail(op, str(exc), cancelled=False, stage="打开文件", exc=exc)
        except OSError as exc:
            return self._fail(
                op, str(exc), cancelled=False, stage="打开文件",
                exc=exc, error_type=type(exc).__name__,
            )

        done = 0
        locked: list[tuple[int, int]] = []
        try:
            if self.params.partial_lock:
                # 加锁必须在 query **之前**：先锁住要动的那几段，再读当前布局，
                # 这样"读到的布局"和"接下来搬的布局"之间不存在可被外部改动的
                # 时间窗。反过来先 query 再加锁，窗口依然存在，等于白锁。
                locked = self._lock_op_ranges(handle, op)
            live = self.mover.query(handle)
            expected = _runs_from_extents(op.src_extents)
            mismatch = layout_mismatch(live, expected)
            if mismatch is not None:
                # 扫描到执行之间文件变了：宁可不搬，也不能搬到错位置
                return self._fail(
                    op, f"布局复核不通过：{mismatch}", cancelled=False,
                    stage="复核布局", error_type="LayoutMismatch",
                )

            chunk = max(1, self.params.chunk_blocks)
            while done < total:
                self.control.checkpoint()
                count = min(chunk, total - done)
                self._move_with_retry(handle, done, op.dst_start + done, count)
                done += count
                if self.limiter is not None:
                    self.limiter.acquire(count * self.cluster_size, self.control)
                if self.on_chunk is not None:
                    # 这里 src_lba/dst_lba 是**簇号**（VCN/LCN），不是块号；
                    # 对调度器而言只有 blocks 与 offset 有意义。
                    self.on_chunk(
                        BlockSlice(
                            op_id=op.op_id,
                            src_lba=done - count,
                            dst_lba=op.dst_start + done - count,
                            blocks=count,
                            offset_blocks=done - count,
                        )
                    )
        except CancelledError:
            return OpResult(op.op_id, done, done * self.cluster_size, ok=False,
                            cancelled=True, error="已取消", stage="搬簇",
                            error_type="CancelledError")
        except DeviceError as exc:
            return self._fail(op, str(exc), cancelled=False, blocks_done=done,
                              stage="搬簇", exc=exc)
        except OSError as exc:
            return self._fail(op, str(exc), cancelled=False, blocks_done=done,
                              stage="搬簇", exc=exc, error_type=type(exc).__name__)
        finally:
            # 先解锁、再关句柄。关句柄本身也会释放锁，但那依赖"没人提前
            # return 忘掉它"；显式解锁让锁的持有区间在代码里可见。
            self._release_op_ranges(handle, locked)
            try:
                self.mover.close_file(handle)
            except Exception:  # noqa: BLE001 - 关句柄失败不该影响结果判定
                pass

        verified = True
        if self.verify:
            verified = self._verify_layout(op)

        return OpResult(
            op.op_id,
            done,
            done * self.cluster_size,
            ok=True,
            verified=verified,
        )

    # -- 内部 -------------------------------------------------------------

    def _move_with_retry(self, handle, vcn: int, lcn: int, count: int) -> None:
        def once() -> None:
            self.mover.move(handle, vcn, lcn, count)

        run_with_retry(
            once,
            self.policy,
            on_retry=self.on_retry,
            control=self.control,
        )

    def _verify_layout(self, op: MoveOp) -> bool:
        """回读校验：搬完之后这个文件应该是「从 dst 起的一段连续簇」。

        这是文件级校验，不是块级校验 —— 走的路径不同，能验的东西也不同。
        """
        try:
            handle = self.mover.open_file(op.path, writable=False)
        except DeviceError:
            return False
        try:
            live = self.mover.query(handle)
        except DeviceError:
            return False
        finally:
            try:
                self.mover.close_file(handle)
            except Exception:  # noqa: BLE001
                pass
        if not live:
            return False
        runs = [r for r in live if r[1] >= 0]
        if len(runs) != 1:
            return False
        vcn, lcn, count = runs[0]
        return vcn == 0 and lcn == op.dst_start and count == op.blocks

    # -- 半锁定 -----------------------------------------------------------

    def _lock_op_ranges(self, handle: object, op: MoveOp) -> list[tuple[int, int]]:
        """给 op 要动的簇区间加共享锁，返回**实际锁上的**范围列表。

        锁不上**不阻止搬运**：锁是"降低被改概率"的增强，不是正确性前提 ——
        布局复核那一步仍然会把关，扫描结果过期照样会被拦下。它的价值是把
        "可能失败"变成"大概率不会失败"，而不是把校验换成锁。

        但锁也不是免费的：给别的进程正在用的文件加锁，会让对方的写失败。
        所以失败次数记在 ``lock_failures`` 上，由上层决定怎么告诉用户。
        """
        locker = getattr(self.mover, "lock_range", None)
        if not callable(locker):
            # 假后端（测试里的内存实现）可能不提供锁能力。**静默跳过**，
            # 不记失败：这不是"锁没拿到"，是这套后端根本没有锁。
            return []
        got: list[tuple[int, int]] = []
        for offset, length in lock_ranges(op, self.cluster_size):
            try:
                locker(handle, offset, length)
                got.append((offset, length))
            except (DeviceError, OSError) as exc:
                self.lock_failures += 1
                if not self.last_lock_error:
                    self.last_lock_error = str(exc)
        return got

    def _release_op_ranges(self, handle: object, locked: list[tuple[int, int]]) -> None:
        """释放本 op 拿到的锁。必须在 ``close_file`` 之前调用。"""
        if not locked:
            return
        unlock = getattr(self.mover, "unlock_range", None)
        if not callable(unlock):
            return
        for offset, length in locked:
            try:
                unlock(handle, offset, length)
            except (DeviceError, OSError):
                # 解锁失败不改变这个文件的结论：句柄紧接着就会关掉，
                # Windows 在句柄关闭时自动释放它持有的全部范围锁。
                pass

    def _fail(
        self,
        op: MoveOp,
        message: str,
        *,
        cancelled: bool,
        blocks_done: int = 0,
        stage: str = "",
        exc: BaseException | None = None,
        error_type: str = "",
    ) -> OpResult:
        return OpResult(
            op.op_id,
            blocks_done,
            blocks_done * self.cluster_size,
            ok=False,
            cancelled=cancelled,
            error=message,
            stage=stage,
            winerror=int(getattr(exc, "winerror", 0) or 0),
            error_type=error_type or (type(exc).__name__ if exc is not None else ""),
            error_class=(
                classify_exception(exc).value if exc is not None else ErrorClass.FATAL.value
            ),
        )

    def close(self) -> None:
        close = getattr(self.mover, "close", None)
        if callable(close):
            close()
