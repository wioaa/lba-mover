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
    ) -> None:
        self.mover = mover
        self.params = params
        self.control = control
        self.cluster_size = max(512, cluster_size)
        self.limiter = limiter
        self.on_chunk = on_chunk
        self.on_retry = on_retry
        self.verify = verify
        self.policy = RetryPolicy(
            max_retries=params.max_retries,
            base_delay=params.retry_base_ms / 1000.0,
            max_delay=params.retry_max_ms / 1000.0,
        )

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
        try:
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
