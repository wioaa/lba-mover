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
from dataclasses import dataclass
from typing import Callable

from ..contract.models import JobParams, MoveOp
from ..devices.base import DeviceError
from .chunk import BlockSlice, slice_move_op
from .control import RunControl
from .errors import CancelledError, VerifyError
from .retry import RetryPolicy, run_with_retry

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

    # -- 单次操作 ---------------------------------------------------------

    def run_op(self, op: MoveOp, start_blocks: int = 0) -> OpResult:
        """执行一个 op。``start_blocks`` 用于断点续做：跳过已写入的块数。"""
        slices = slice_move_op(op, self.params.chunk_blocks)
        if not slices:
            return OpResult(op.op_id, 0, 0, ok=True, verified=self.params.verify)

        q: queue.Queue = queue.Queue(maxsize=max(1, self.params.queue_depth))
        done_blocks = 0
        skipped = 0
        cancelled = False
        failure: BaseException | None = None

        def producer() -> None:
            try:
                for sl in slices:
                    if sl.offset_blocks + sl.blocks <= start_blocks:
                        continue  # 断点之前的块已经写好了
                    self.control.checkpoint()
                    payload = self._read_slice(sl)
                    # put 可能阻塞：消费者慢时这是我们想要的背压
                    while True:
                        try:
                            q.put((sl, payload), timeout=0.1)
                            break
                        except queue.Full:
                            if self.control.cancelled:
                                raise CancelledError("读取阶段被取消") from None
            except BaseException as exc:  # noqa: BLE001 - 交给消费者统一处理
                q.put(("__error__", exc))
            finally:
                q.put(_DONE)

        th = threading.Thread(target=producer, name=f"lba-read-op{op.op_id}", daemon=True)
        th.start()

        try:
            while True:
                item = q.get()
                if item is _DONE:
                    break
                marker, payload = item
                if marker == "__error__":
                    failure = payload
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
                    cancelled = False
                    break
                done_blocks += sl.blocks
                if self.on_chunk is not None:
                    self.on_chunk(sl)
        finally:
            # 无论成功失败，都要把生产者收干净：消费剩余项直到 _DONE
            self._drain(q)
            th.join(timeout=5.0)

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
        def once() -> bytes:
            return self.src.read_blocks(sl.src_lba, sl.blocks)

        return run_with_retry(
            once,
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
    def _drain(q: queue.Queue) -> None:
        while True:
            try:
                item = q.get_nowait()
            except queue.Empty:
                return
            if item is _DONE:
                return
