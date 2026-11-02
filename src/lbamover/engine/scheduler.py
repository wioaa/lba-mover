"""任务调度：并发、限速、重试汇聚、断点落盘。

调度器只做四件事，每件事都必须有明确的上限或终态：

====================  ==================================================
并发                  ``max_workers = 1 if plan.requires_serial else concurrency``
限速                  令牌桶（字节/秒），0 表示不限
重试                  委托给 :class:`ChunkPipeline`（chunk 粒度）
停止                  收到取消后不再提交新 op，已提交的靠 ``RunControl`` 退出
====================  ==================================================

关于并发的一个坑：**并发数不是越大越好**。机械盘上并发 >1 会让磁头来回
寻道，实测吞吐反而下降；SSD 上才有收益。因此默认 2，且在报告里明确标注
``requires_serial`` 的计划强制降为 1——那不是性能选择，是正确性要求。
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Callable

from ..contract.models import (
    MAX_IGNORED_FAILURES,
    FailureDetail,
    JobParams,
    MoveOp,
    Plan,
)
from .checkpoint import Checkpoint, CheckpointStore, JsonlJournal, merge_op_progress
from .chunk import BlockSlice
from .control import RunControl
from .executor import BlockCopyExecutor, Executor
from ..diagnostics import hint_for
from .pipeline import OpResult
from .progress import ProgressTracker

__all__ = ["RateLimiter", "ExecutionResult", "JobRunner"]

#: 连续失败到多少个就熔断。
#:
#: **单个文件失败绝不中止**（被独占打开、权限不足都是常态，几十个文件里
#: 有几个失败完全正常）。但如果连着这么多个都失败，说明是系统性故障 ——
#: 卷被别的进程写保护、句柄失效、盘开始掉块。这时候继续跑只是在把剩下的
#: 操作一个个撞死，还白白写日志、拖时间，用户看到的是"进度条飞快走完但
#: 什么都没搬"。
#:
#: 20 这个值是"够多到不可能是巧合、够少到不用等一小时才反应过来"。
#:
#: 注意它只统计**系统级**失败（搬簇、读写、复核布局），不统计"打不开文件"，
#: 后者见 :data:`MAX_CONSECUTIVE_UNOPENABLE`。
MAX_CONSECUTIVE_FAILURES = 20

#: 连续多少个文件**打不开**才熔断。
#:
#: 为什么是 20 的 10 倍：规划按物理 LCN 排序（见 planner 的 ``_order_by_lcn``），
#: 同一目录下同时创建的小文件在盘上是紧挨着的，所以在任务队列里也连在一起。
#: 真机实测（F: 卷 / relocate_tail / 整卷）：Steam 开着，``Steam\logs`` 下
#: 二十个 .txt 连续 winerror=32，加上 3 个正在被写入的文件，**连续 20 个失败
#: 直接熔断，整卷一个文件都没搬成**。
#:
#: 打不开是"这一个文件现在搬不了"，不是"整卷不能搬"。200 的意思是：真的整卷
#: 都被别的程序占满了才值得放弃。
MAX_CONSECUTIVE_UNOPENABLE = 200

#: 只影响单个文件、跳过就该继续的失败阶段。
#:
#: 用阶段而不是错误分类来分档，因为分类回答"要不要重试这个文件"，而熔断要
#: 回答"还要不要继续搬别的文件"——这是两个问题。同是 RETRYABLE，打开文件
#: 时的共享冲突只坑一个文件，写盘时的 IO 超时则说明设备已经不可靠。
_LOCAL_FAILURE_STAGES = frozenset({"打开文件"})

#: 连续 n 次失败时，n 该往哪个闸门累加。
_FAILURE_KIND_SYSTEM = "system"
_FAILURE_KIND_LOCAL = "local"



class RateLimiter:
    """令牌桶限速。``bytes_per_sec <= 0`` 时完全旁路（零开销）。"""

    __slots__ = ("rate", "burst", "_tokens", "_last", "_lock")

    def __init__(self, bytes_per_sec: float, burst_seconds: float = 0.25) -> None:
        self.rate = max(0.0, bytes_per_sec)
        self.burst = max(1.0, self.rate * burst_seconds) if self.rate > 0 else 0.0
        self._tokens = self.burst
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def update_rate(self, bytes_per_sec: float) -> None:
        """运行期改限速（UI 的滑块）。"""
        with self._lock:
            self.rate = max(0.0, bytes_per_sec)
            self.burst = max(1.0, self.rate * 0.25) if self.rate > 0 else 0.0
            self._tokens = min(self._tokens, self.burst) if self.burst else 0.0
            self._last = time.monotonic()

    def acquire(self, n_bytes: int, control: RunControl | None = None) -> None:
        if self.rate <= 0 or n_bytes <= 0:
            return
        # 桶容量必须至少能装下「一次请求」，否则 token 永远攒不够，acquire
        # 会永久空转。这不是边界情况：限速 1 MiB/s 而 chunk 是 2 MiB 时，
        # 每个 chunk 都会撞上它。
        cap = max(self.burst, float(n_bytes))
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(cap, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= n_bytes:
                    self._tokens -= n_bytes
                    return
                need = (n_bytes - self._tokens) / self.rate
            # 分片等待：既能及时响应取消，又不会把 CPU 打满
            wait = min(need, 0.05)
            if control is not None:
                control.sleep(wait)
            else:  # pragma: no cover
                time.sleep(wait)


@dataclass(slots=True)
class ExecutionResult:
    moved_ops: int = 0
    failed_ops: int = 0
    skipped_ops: int = 0
    blocks_done: int = 0
    bytes_moved: int = 0
    elapsed_s: float = 0.0
    cancelled: bool = False
    #: 单行文本，保留给"扫一眼日志"的场合
    errors: list[str] = field(default_factory=list)
    #: 结构化失败明细。用户要把它贴给 AI 时，单行文本里没有阶段与错误码，
    #: 光凭"拒绝访问"这四个字谁都判断不了是权限还是占用。
    failures: list[FailureDetail] = field(default_factory=list)
    op_results: list[OpResult] = field(default_factory=list)
    #: 非用户主动停止的中止原因（例如连续失败熔断）。空串表示没发生。
    aborted: str = ""

    @property
    def ok(self) -> bool:
        return self.failed_ops == 0 and not self.cancelled


class JobRunner:
    """执行一份 :class:`Plan`。"""

    def __init__(
        self,
        src,
        dst,
        params: JobParams,
        control: RunControl,
        *,
        store: CheckpointStore | None = None,
        journal: JsonlJournal | None = None,
        on_progress: Callable | None = None,
        on_log: Callable[[str], None] | None = None,
        on_op_done: Callable[[OpResult], None] | None = None,
        halt_on_error: bool = False,
        executor_factory: Callable[["JobRunner"], Executor] | None = None,
    ) -> None:
        self.src = src
        self.dst = dst
        self.params = params
        self.control = control
        self.store = store
        self.journal = journal
        self.on_progress = on_progress
        self.on_log = on_log
        self.on_op_done = on_op_done
        self.halt_on_error = halt_on_error
        #: 由调用方决定"用哪种方式落盘"（块拷贝 / FSCTL_MOVE_FILE）。
        #: 用工厂而不是实例：执行器需要拿到 runner 的回调与限速器，
        #: 而那两样只有在 runner 构造完之后才存在。
        self.executor_factory = executor_factory
        self._executor: Executor | None = None

        self.tracker = ProgressTracker(0, params.block_size, 0)
        self.limiter = RateLimiter(params.rate_limit_mbps * 1024 * 1024)
        self._lock = threading.Lock()
        self._chunks_since_cp = 0
        self._cp: Checkpoint | None = None
        self._plan: Plan | None = None

    # -- 主流程 -----------------------------------------------------------

    def run(self, plan: Plan, resume: Checkpoint | None = None) -> ExecutionResult:
        self._plan = plan
        self.tracker = ProgressTracker(plan.total_blocks, self.params.block_size, plan.op_count)
        result = ExecutionResult()
        t0 = time.monotonic()

        completed: set[int] = set(resume.completed_ops) if resume else set()
        self._cp = Checkpoint(
            job_id=plan.job_id,
            plan_hash=plan.plan_hash,
            completed_ops=tuple(sorted(completed)),
            op_blocks=resume.op_blocks if resume else (),
        )

        pending: list[MoveOp] = []
        for op in plan.ops:
            if op.op_id in completed:
                # 续做时已完成的部分也要计入进度，否则进度条会从 0 开始"白跳"
                self.tracker.advance(op.blocks)
                self.tracker.complete_op()
                result.skipped_ops += 1
                result.blocks_done += op.blocks
            else:
                pending.append(op)

        workers = 1 if plan.requires_serial else max(1, self.params.concurrency)
        if plan.requires_serial and self.params.concurrency > 1:
            self._log("计划需要复用腾出的空间，并发强制降为 1 以保证读前写后")
        self._log(
            f"开始执行：{len(pending)} 个待搬文件，{plan.total_blocks} 块，"
            f"并发 {workers}，chunk {self.params.chunk_blocks} 块"
        )
        if self.params.ignore_errors:
            # 说在前面：后面"连续失败却不停"是用户自己选的，不是漏了熔断。
            # 不解释的话，日志看起来就像一个该停没停的 bug。
            self._log(
                "已开启「忽略错误运行」：不因连续失败熔断，"
                f"累计失败到 {MAX_IGNORED_FAILURES} 个才停止"
            )

        self._executor = self._make_executor()
        prepare = getattr(self._executor, "prepare", None)
        if prepare is not None and pending:
            try:
                prepare(pending)
            except Exception as exc:  # noqa: BLE001 - 预占失败不该阻止搬运
                self._log(f"目标区预占失败（按原样继续）：{exc}")
            note = getattr(self._executor, "_preoccupy_note", "")
            if note:
                self._log(note)
        try:
            if pending:
                self._run_pool(pending, workers, result)
        finally:
            closer = getattr(self._executor, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:  # noqa: BLE001 - 释放失败不改变任务结论
                    pass
            self._executor = None

        if self.control.cancelled:
            result.cancelled = True
        # 走 FSCTL_MOVE_FILE 时没有裸块设备，落盘一致性由文件系统自己保证
        if self.dst is not None:
            self.dst.flush()
        result.elapsed_s = time.monotonic() - t0
        result.bytes_moved = result.blocks_done * self.params.block_size
        self._save_checkpoint(force=True)
        self._emit_progress()
        return result

    # -- 并发池 -----------------------------------------------------------

    def _run_pool(self, ops: list[MoveOp], workers: int, result: ExecutionResult) -> None:
        executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="lba-op")
        futures: dict[Future, MoveOp] = {}
        consecutive_system = 0
        consecutive_local = 0
        # 「忽略错误运行」的闸门：统计**所有**失败（不分阶段），成功不清零。
        # 关掉开关时它只累加不判断，代价可以忽略。
        total_failures = 0
        try:
            for op in ops:
                if self.control.cancelled:
                    break
                futures[executor.submit(self._run_one, op)] = op
            for fut in _as_completed(futures):
                op = futures[fut]
                try:
                    res = fut.result()
                except BaseException as exc:  # noqa: BLE001 - worker 内部已兜底，这里防漏
                    res = OpResult(
                        op_id=op.op_id,
                        blocks_done=0,
                        bytes_done=0,
                        ok=False,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                self._account(op, res, result)
                if self.control.cancelled:
                    result.cancelled = True
                    break
                if res.ok:
                    consecutive_system = 0
                    consecutive_local = 0
                    continue
                if res.cancelled:
                    continue
                total_failures += 1
                # 两条独立的连续计数：失败性质不同，混在一起数就会让"一整批
                # 文件被同一个程序占着"把真正的系统故障线索淹掉（真机事故见
                # MAX_CONSECUTIVE_UNOPENABLE 的注释）。
                if res.stage in _LOCAL_FAILURE_STAGES:
                    consecutive_local += 1
                    consecutive_system = 0
                else:
                    consecutive_system += 1
                    consecutive_local = 0
                if self.halt_on_error:
                    self._log(f"遇到错误且开启了 fail-fast，停止后续任务：{res.error}")
                    self.control.request_cancel()
                    break
                if self.params.ignore_errors:
                    # 用户显式选择了"一次跑完"：跳过两条连续熔断。它们是保守
                    # 收尾的判断，而这里的前提恰好是"连续失败不算理由"。
                    # 但忽略错误不等于无限容忍：设备真的掉线时每个 op 都要走
                    # 完重试与退避才判失败，不设上限会在废设备上空转几小时。
                    # 上限本身定义在契约层（UI 要对用户承诺同一个数）。
                    if total_failures >= MAX_IGNORED_FAILURES:
                        # 按剩余量说话：第 N 个失败恰好是最后一个 op 时，
                        # "停止剩余任务"这句话就是假的，用户会去找那些根本
                        # 不存在的"剩余任务"。
                        remaining = len(ops) - len(result.op_results)
                        suffix = (
                            f"，剩余 {remaining} 个未执行"
                            if remaining > 0
                            else "，已无剩余任务"
                        )
                        result.aborted = (
                            f"已按「忽略错误运行」跳过所有连续失败，但累计失败已达 "
                            f"{total_failures} 个（多半是设备或整个目录被占用）{suffix}"
                        )
                        self._log(result.aborted)
                        break
                    continue
                if consecutive_system >= MAX_CONSECUTIVE_FAILURES:
                    # 熔断的门槛是"连续"，不是"一个"：单个文件被独占打开、
                    # 权限不足都是常态，绝不能因此停掉整卷整理。
                    result.aborted = (
                        f"连续 {consecutive_system} 个文件在「{res.stage or '执行'}」阶段失败，"
                        f"判断为系统性故障，已中止剩余任务"
                    )
                    self._log(result.aborted)
                    break
                if consecutive_local >= MAX_CONSECUTIVE_UNOPENABLE:
                    # 到这个数已经不只是"几个文件被占用"了，而是整卷都没法
                    # 拿到句柄，继续跑只会把剩下的几十万次打开操作逐一撞死。
                    result.aborted = (
                        f"连续 {consecutive_local} 个文件打不开（被占用或权限不足），"
                        f"整卷可能正被其他程序大量使用，已中止剩余任务"
                    )
                    self._log(result.aborted)
                    break
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

    def _run_one(self, op: MoveOp) -> OpResult:
        if self.control.cancelled:
            return OpResult(op.op_id, 0, 0, ok=False, cancelled=True, error="已取消")
        with self._lock:
            self.tracker.current_path = op.path
        start_blocks = self._cp.blocks_of(op.op_id) if self._cp else 0
        if start_blocks >= op.blocks:
            start_blocks = 0  # 记录异常时不冒险跳块，整体重做

        assert self._executor is not None
        return self._executor.run_op(op, start_blocks)

    def _make_executor(self) -> Executor:
        if self.executor_factory is not None:
            return self.executor_factory(self)
        # 默认：逐块拷贝（内存盘、镜像文件、离线镜像）
        return BlockCopyExecutor(
            self.src,
            self.dst,
            self.params,
            self.control,
            limiter=self.limiter,
            on_chunk=self._on_chunk,
            on_retry=self._on_retry,
        )

    # -- 给执行器工厂用的回调 ---------------------------------------------

    @property
    def chunk_callback(self) -> Callable:
        return self._on_chunk

    @property
    def retry_callback(self) -> Callable:
        return self._on_retry

    # -- 回调 -------------------------------------------------------------

    def _on_chunk(self, sl: BlockSlice) -> None:
        with self._lock:
            self.tracker.advance(sl.blocks)
            self._chunks_since_cp += 1
            need_cp = (
                self.params.checkpoint_every_chunks > 0
                and self._chunks_since_cp >= self.params.checkpoint_every_chunks
            )
            if need_cp:
                self._chunks_since_cp = 0
        if need_cp:
            self._save_checkpoint()
        if self.on_progress is not None:
            self.on_progress(self.tracker.snapshot())

    def _on_retry(self, attempt: int, exc: BaseException, delay: float) -> None:
        with self._lock:
            self.tracker.note_retry()
        self._log(f"chunk 重试 #{attempt}（等待 {delay * 1000:.0f} ms）：{exc}")

    def _account(self, op: MoveOp, res: OpResult, result: ExecutionResult) -> None:
        with self._lock:
            result.op_results.append(res)
            if res.ok:
                result.moved_ops += 1
                result.blocks_done += res.blocks_done
                self.tracker.complete_op()
                if self._cp is not None:
                    done = set(self._cp.completed_ops) | {op.op_id}
                    self._cp = replace(
                        self._cp,
                        completed_ops=tuple(sorted(done)),
                        op_blocks=merge_op_progress(
                            self._cp.op_blocks, op.op_id, op.blocks
                        ),
                        bytes_done=self._cp.bytes_done + res.bytes_done,
                    )
            elif res.cancelled:
                if self._cp is not None and res.blocks_done > 0:
                    self._cp = replace(
                        self._cp,
                        op_blocks=merge_op_progress(
                            self._cp.op_blocks,
                            op.op_id,
                            (self._cp.blocks_of(op.op_id)) + res.blocks_done,
                        ),
                        bytes_done=self._cp.bytes_done + res.bytes_done,
                    )
                self.tracker.skip_op()
            else:
                result.failed_ops += 1
                detail = FailureDetail(
                    path=op.path,
                    op_id=op.op_id,
                    stage=res.stage or "未知阶段",
                    reason=res.error or "（没有错误描述）",
                    error_type=res.error_type,
                    error_class=res.error_class,
                    winerror=res.winerror,
                )
                result.failures.append(detail)
                result.errors.append(f"{op.path}: {res.error}")
                self.tracker.note_failure()
                self.tracker.skip_op()
        self._save_checkpoint(force=True)
        if self.journal is not None:
            self.journal.append(
                {
                    "op_id": op.op_id,
                    "path": op.path,
                    "ok": res.ok,
                    "cancelled": res.cancelled,
                    "blocks": res.blocks_done,
                    "retries": res.retries,
                    "error": res.error,
                }
            )
        if self.on_op_done is not None:
            self.on_op_done(res)
        if not res.ok and not res.cancelled:
            detail = result.failures[-1] if result.failures else None
            if detail is not None:
                # 一行摘要 + 一行建议。摘要里带阶段与错误码，够用户转述；
                # 建议给出"下一步做什么"，否则等于只说了"不行"。
                self._log(f"op{op.op_id} 失败：{op.path} —— {detail.one_line()}")
                self._log(f"  建议：{hint_for(detail)}")
            else:  # pragma: no cover - failures 与 errors 同步追加，理论到不了
                self._log(f"op{op.op_id} 失败：{op.path} — {res.error}")

    # -- 断点 -------------------------------------------------------------

    def _save_checkpoint(self, force: bool = False) -> None:
        if self.store is None or self._cp is None:
            return
        if not force and self.params.checkpoint_every_chunks <= 0:
            return
        with self._lock:
            cp = replace(self._cp, updated_at=time.time())
            self._cp = cp
        try:
            self.store.save(cp)
        except OSError as exc:
            self._log(f"断点保存失败（不影响继续执行）：{exc}")

    def _emit_progress(self) -> None:
        if self.on_progress is not None:
            self.on_progress(self.tracker.snapshot())

    def _log(self, message: str) -> None:
        if self.on_log is not None:
            self.on_log(message)


def _as_completed(futures: dict[Future, MoveOp]):
    """按完成顺序产出（不依赖 concurrent.futures.as_completed 的兼容性）。"""
    from concurrent.futures import as_completed

    yield from as_completed(list(futures))
