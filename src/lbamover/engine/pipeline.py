"""分块流式流水线：单次搬迁的读/写重叠执行。

结构（一个 op 一条流水线）::

    生产者线程                       消费者（调用方线程）
    ┌──────────────┐  有界队列(深度=D)  ┌──────────────┐
    │ 读 chunk N   │ ────────────────> │ 写 chunk N   │
    │ 读 chunk N+1 │ ────────────────> │ 写 chunk N+1 │
    └──────────────┘                   └──────────────┘

三个必须守住的点：

1. **队列有界，深度 = ``JobParams.queue_depth``。** 这是「内存可控」的全部
   依据：在途数据最多 ``深度 × chunk 大小``，与卷大小无关。无界队列会让
   一个 2TB 的卷把内存吃满——这是"流式"实现最常见的假流式。
2. **顺序必须保持。** FIFO 队列天然保证同 op 内的写入顺序与读取顺序一致；
   一旦为了让并发更高而乱序，断点续做的"已写入前 K 块"就不再成立。
3. **取消后必须继续排空队列。** 直接退出会让生产者永久阻塞在 ``put()`` 上，
   线程泄漏，进程关不掉。这是"取消没反应"的典型成因。
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable

from ..contract.models import JobParams, MoveOp
from ..devices.base import DeviceError
from .chunk import BlockSlice, slice_move_op
from .control import RunControl
from .errors import CancelledError, VerifyError
from .retry import RetryPolicy, classify_exception, run_with_retry

__all__ = ["ChunkPipeline", "OpResult"]

_DONE = object()


@dataclass(frozen=True, slots=True)
class OpResult:
    op_id: int
    blocks_done: int
    bytes_done: int
    ok: bool
    cancelled: bool = False
    error: str = ""
    retries: int = 0
    verified: bool = True
    skipped_blocks: int = 0
    #: 出错阶段（"打开文件" / "读取数据" / "搬簇" …），见 FailureDetail。
    #: 没有它的话日志只能说"这个文件失败了"，用户和 AI 都不知道卡在哪。
    stage: str = ""
    #: Windows 错误码，0 表示不适用
    winerror: int = 0
    #: 异常类名，便于在代码里定位抛出点
    error_type: str = ""
    #: 错误分类（retryable / fatal / verify_mismatch / cancelled）。
    #: 用户最想知道的一条是"还要不要再点一次开始"，它就写在这里。
    error_class: str = ""


class ChunkPipeline:
    """把一对象（:class:`MoveOp`）按 chunk 流式从源区搬到目标区。"""

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
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.src = src
        self.dst = dst
        self.params = params
        self.control = control
        self.limiter = limiter
        self.on_chunk = on_chunk
        self.on_retry = on_retry
        self._sleep = sleep
        self.policy = RetryPolicy(
            max_retries=params.max_retries,
            base_delay=params.retry_base_ms / 1000.0,
            max_delay=params.retry_max_ms / 1000.0,
        )
        self.retries = 0
        #: 读取侧半锁定：开关打开且后端**声称**支持时才走加锁路径。
        #: 内存盘这类没有外部写入者的设备在 ``lock_range`` 里返回 False，
        #: 白调一次而已，不必在这里再判断能力。
        self._lock_reads = bool(getattr(params, "partial_lock", False)) and callable(
            getattr(src, "lock_range", None)
        )

    # -- 单次操作 ---------------------------------------------------------

    def run_op(self, op: MoveOp, start_blocks: int = 0) -> OpResult:
        """执行一个 op。``start_blocks`` 用于断点续做：跳过已写入的块数。"""
        slices = slice_move_op(op, self.params.chunk_blocks)
        if not slices:
            return OpResult(op.op_id, 0, 0, ok=True, verified=self.params.verify)

        q: queue.Queue = queue.Queue(maxsize=max(1, self.params.queue_depth))
        #: 消费者不干了（写失败 / 已取消 / 已决定放弃）的信号。
        #:
        #: **不能只靠 ``control.cancelled``**：单个文件写失败时整个任务并没有
        #: 取消，生产者不知道消费者已经走了，就会一直卡在满队列的 ``put()``
        #: 上。``join(timeout=5)`` 等不到它，于是每失败一个文件就泄漏一条
        #: 读线程 —— 失败几十个之后进程里全是睡死的线程。
        stop = threading.Event()
        done_blocks = 0
        skipped = 0
        cancelled = False
        failure: BaseException | None = None
        #: 主循环是否**自己读到过** ``_DONE``。收尾时用它决定还要不要排空：
        #: 已经读到就没有第二个 ``_DONE`` 了，再等只会白白等到超时。
        saw_done = False
        #: 失败发生在哪一端。"读不出来"和"写不进去"的处理方式完全不同：
        #: 前者多是坏块/介质问题，后者多是目标越界或卷写保护。
        failure_stage = ""

        def producer() -> None:
            try:
                for sl in slices:
                    if stop.is_set() or self.control.cancelled:
                        raise CancelledError("读取阶段已停止")
                    if sl.offset_blocks + sl.blocks <= start_blocks:
                        continue  # 断点之前的块已经写好了
                    self.control.checkpoint()
                    payload = self._read_slice(sl)
                    # put 可能阻塞：消费者慢时这是我们想要的背压。
                    # 每一轮都先看 stop，否则消费者退出后就再也出不来了。
                    while True:
                        if stop.is_set():
                            raise CancelledError("读取阶段已停止")
                        try:
                            q.put((sl, payload), timeout=0.1)
                            break
                        except queue.Full:
                            if self.control.cancelled:
                                raise CancelledError("读取阶段被取消") from None
            except BaseException as exc:  # noqa: BLE001 - 交给消费者统一处理
                _put_blocking(q, ("__error__", exc), stop)
            finally:
                _put_blocking(q, _DONE, stop)

        th = threading.Thread(target=producer, name=f"lba-read-op{op.op_id}", daemon=True)
        th.start()

        try:
            while True:
                item = q.get()
                if item is _DONE:
                    saw_done = True
                    break
                marker, payload = item
                if marker == "__error__":
                    failure = payload
                    failure_stage = "读取数据"
                    cancelled = isinstance(payload, CancelledError)
                    break
                sl, data = marker, payload
                if cancelled or self.control.cancelled:
                    cancelled = True
                    continue  # 继续排空，避免生产者卡死
                try:
                    self._write_slice(sl, data)
                except CancelledError:
                    cancelled = True
                    continue
                except BaseException as exc:  # noqa: BLE001
                    failure = exc
                    failure_stage = "写入数据"
                    cancelled = False
                    break
                done_blocks += sl.blocks
                if self.on_chunk is not None:
                    self.on_chunk(sl)
        finally:
            # 顺序不能反：先立牌子告诉生产者"别读了"，再排空队列，
            # 最后才 join。反过来的话生产者会继续往队列里塞，排空永远追不上。
            stop.set()
            if not saw_done:
                self._drain(q)
            th.join(timeout=1.0)

        skipped = start_blocks
        bytes_done = done_blocks * self.params.block_size
        if failure is not None:
            return OpResult(
                op_id=op.op_id,
                blocks_done=done_blocks,
                bytes_done=bytes_done,
                ok=False,
                cancelled=isinstance(failure, CancelledError),
                error=f"{type(failure).__name__}: {failure}",
                retries=self.retries,
                skipped_blocks=skipped,
                stage=failure_stage,
                winerror=int(getattr(failure, "winerror", 0) or 0),
                error_type=type(failure).__name__,
                error_class=classify_exception(failure).value,
            )
        if cancelled:
            return OpResult(
                op_id=op.op_id,
                blocks_done=done_blocks,
                bytes_done=bytes_done,
                ok=False,
                cancelled=True,
                error="已取消",
                retries=self.retries,
                skipped_blocks=skipped,
            )
        return OpResult(
            op_id=op.op_id,
            blocks_done=done_blocks,
            bytes_done=bytes_done,
            ok=True,
            retries=self.retries,
            verified=self.params.verify,
            skipped_blocks=skipped,
        )

    # -- 读写 -------------------------------------------------------------

    def _read_slice(self, sl: BlockSlice) -> bytes:
        if not self._lock_reads:
            return run_with_retry(
                lambda: self.src.read_blocks(sl.src_lba, sl.blocks),
                self.policy,
                on_retry=self._on_retry,
                control=self.control,
                sleep=self._sleep,
            )

        block_size = self.params.block_size
        offset = sl.src_lba * block_size
        length = sl.blocks * block_size

        def guarded() -> bytes:
            # 加锁与读取必须成对包在**同一次尝试**里：重试时重新加锁，
            # 而不是沿用上一次可能已经失效的锁。读取耗时是外部进程改它的
            # 唯一窗口，这个窗口越窄，"读到的数据是同一份"越可信。
            held = self.src.lock_range(offset, length)
            try:
                return self.src.read_blocks(sl.src_lba, sl.blocks)
            finally:
                if held:
                    self.src.unlock_range(offset, length)

        return run_with_retry(
            guarded,
            self.policy,
            on_retry=self._on_retry,
            control=self.control,
            sleep=self._sleep,
        )

    def _write_slice(self, sl: BlockSlice, data: bytes) -> None:
        def once() -> None:
            self.dst.write_blocks(sl.dst_lba, data)
            if self.params.verify:
                got = self.dst.read_blocks(sl.dst_lba, sl.blocks)
                if got != data:
                    raise VerifyError(
                        f"回读校验不一致：目标 LBA {sl.dst_lba} 起 {sl.blocks} 块",
                        lba=sl.dst_lba,
                        blocks=sl.blocks,
                    )
            if self.limiter is not None:
                self.limiter.acquire(len(data), self.control)

        run_with_retry(
            once,
            self.policy,
            on_retry=self._on_retry,
            control=self.control,
            sleep=self._sleep,
        )

    def _on_retry(self, attempt: int, exc: BaseException, delay: float) -> None:
        self.retries += 1
        if self.on_retry is not None:
            self.on_retry(attempt, exc, delay)

    @staticmethod
    def _drain(q: queue.Queue, timeout: float = 5.0) -> None:
        """消费队列直到 ``_DONE``。

        **必须是阻塞式的。** 原来是 ``get_nowait``，队列一空就返回 —— 而
        队列空恰恰是生产者正在读取、马上就要 ``put`` 的时刻。返回之后没人
        消费，生产者就永久卡在 ``put`` 上，``join`` 超时，线程留了下来。
        症状是"失败的文件越多，程序越慢"以及进程里堆着一堆睡死的读线程。
        """
        deadline = time.monotonic() + timeout
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                return
            try:
                item = q.get(timeout=min(0.05, remain))
            except queue.Empty:
                continue
            if item is _DONE:
                return


def _put_blocking(q: "queue.Queue", item, stop: threading.Event) -> None:
    """往队列里放一项，直到成功或收到 ``stop``。

    生产者收尾时用：消费者可能已经退出，此时普通 ``put`` 会永远阻塞在
    满队列上（daemon 线程不会阻止进程退出，但会一直占着内存与句柄）。
    """
    while True:
        if stop.is_set() and item is not None:
            # stop 之后我们只关心把 _DONE 送出去；__error__ 可以丢，
            # 因为主循环此时已经在退出路径上了。
            if item is not _DONE:
                return
        try:
            q.put(item, timeout=0.1)
            return
        except queue.Full:
            continue
