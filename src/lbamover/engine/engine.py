"""引擎门面。

分两层，职责不重叠：

* :class:`EngineCore` —— **同步**的状态机 + 编排。扫、规划、执行都是阻塞调用，
  没有线程，因此可以被 CLI、测试、UI 三种宿主复用，且行为完全一致。
* :class:`EngineService` —— 给 UI 用的线程宿主。持一个命令队列和一条工作线程，
  把阻塞调用搬离 GUI 线程。

为什么要这么切：旧版把「谁在跑、能不能取消」分散在八个 worker 里，每加一个
能力就复制一遍生命周期判定，最终演化出"取消没反应""按钮永久禁用"这一族
缺陷。现在只有一处状态机（:class:`EngineCore`），只有一处线程（Service），
按钮可用性由 ``StateEvent`` 单一驱动。
"""

from __future__ import annotations

import os
import queue
import threading
import time
import uuid
from dataclasses import replace
from typing import Callable

from ..contract.commands import (
    CancelJob,
    Command,
    PauseJob,
    ResumeJob,
    SetParams,
    Shutdown,
    StartJob,
    UpdateTarget,
)
from ..contract.enums import JobState, LogLevel, Strategy
from ..contract.events import (
    FragMapEvent,
    JobDoneEvent,
    LogEvent,
    PlanEvent,
    ProgressEvent,
    StateEvent,
)
from ..contract.models import FragReport, JobParams, JobSpec, Plan, Progress, VolumeInfo
from ..contract.ports import EventSink, NullSink
from .catalog import CatalogError, StaticCatalog
from .checkpoint import Checkpoint, CheckpointStore, JsonlJournal, default_state_dir
from .control import RunControl
from .errors import CancelledError, PlanRejected
from .frag import DEFAULT_BUCKETS, analyze, free_extents_from_files
from .planner import build_plan, validate_plan
from .scheduler import ExecutionResult, JobRunner

__all__ = ["EngineCore", "EngineService", "DeviceProviderError"]


class DeviceProviderError(RuntimeError):
    """打不开目标设备（权限不足、卷被占用、非 Windows）。"""


#: 状态机允许的迁移。写死在这里，非法迁移直接抛——避免 UI 把按钮逻辑写错
#: 之后引擎进入一个"看起来在跑其实没跑"的状态。
_ALLOWED: dict[JobState, set[JobState]] = {
    JobState.IDLE: {JobState.SCANNING, JobState.PLANNING, JobState.RUNNING, JobState.FAILED},
    JobState.SCANNING: {JobState.IDLE, JobState.PLANNING, JobState.FAILED, JobState.CANCELLED},
    JobState.PLANNING: {JobState.IDLE, JobState.RUNNING, JobState.FAILED, JobState.CANCELLED},
    JobState.RUNNING: {
        JobState.PAUSED,
        JobState.STOPPING,
        JobState.DONE,
        JobState.FAILED,
        JobState.IDLE,
        JobState.PLANNING,
    },
    JobState.PAUSED: {JobState.RUNNING, JobState.STOPPING, JobState.CANCELLED, JobState.IDLE},
    JobState.STOPPING: {JobState.CANCELLED, JobState.DONE, JobState.FAILED, JobState.IDLE},
    JobState.DONE: {JobState.IDLE, JobState.SCANNING, JobState.PLANNING},
    JobState.FAILED: {JobState.IDLE, JobState.SCANNING, JobState.PLANNING},
    JobState.CANCELLED: {JobState.IDLE, JobState.SCANNING, JobState.PLANNING},
}


class EngineCore:
    """同步引擎。所有公开方法都可从单一宿主线程调用。"""

    def __init__(
        self,
        sink: EventSink | None = None,
        catalog=None,
        state_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        self.sink: EventSink = sink or NullSink()
        self.catalog = catalog or StaticCatalog()
        self._state_dir = state_dir
        self._seq = 0
        self._seq_lock = threading.Lock()

        self.state = JobState.IDLE
        self.volume: VolumeInfo | None = None
        self.params = JobParams()
        self.report: FragReport | None = None
        self.plan: Plan | None = None
        self.job_id = ""
        self.control = RunControl()
        self.last_result: ExecutionResult | None = None
        self._last_progress_at = 0.0
        self._progress_interval = 0.08

    # -- 事件 -------------------------------------------------------------

    def _next_seq(self) -> int:
        with self._seq_lock:
            self._seq += 1
            return self._seq

    def _emit(self, event) -> None:
        self.sink.emit(event)

    def _log(self, level: LogLevel, message: str, detail: str = "") -> None:
        self._emit(LogEvent(level, message, detail, seq=self._next_seq(), job_id=self.job_id))

    def _set_state(self, new: JobState, reason: str = "") -> None:
        if new is self.state:
            return
        allowed = _ALLOWED.get(self.state)
        if allowed is not None and new not in allowed:
            # 不抛异常：状态机作者出错不应该让用户的任务崩掉，但要留痕
            self._log(
                LogLevel.WARNING,
                f"非法状态迁移 {self.state.value} -> {new.value}，已强制切换",
                detail=reason,
            )
        self.state = new
        self._emit(StateEvent(new, reason=reason, seq=self._next_seq(), job_id=self.job_id))

    # -- 目标 -------------------------------------------------------------

    def set_target(self, volume: VolumeInfo, params: JobParams | None = None) -> None:
        self.volume = volume
        self.params = params or self.params
        self.report = None
        self.plan = None
        self._emit(
            StateEvent(JobState.IDLE, reason=f"目标已切换：{volume.mount_point}", seq=self._next_seq())
        )

    # -- 扫描 -------------------------------------------------------------

    def scan(
        self,
        volume: VolumeInfo | None = None,
        *,
        bucket_count: int = DEFAULT_BUCKETS,
    ) -> FragReport:
        """扫描目标卷的文件分布，产出 :class:`FragReport`。"""
        vol = volume or self.volume
        if vol is None:
            raise ValueError("尚未选择目标卷")

        self._set_state(JobState.SCANNING, f"扫描 {vol.mount_point}")
        t0 = time.monotonic()
        try:
            files = tuple(self.catalog.list_files(vol))
            free = tuple(self.catalog.free_extents(vol))
        except CatalogError as exc:
            self._log(LogLevel.ERROR, str(exc))
            self._set_state(JobState.IDLE, "扫描失败")
            raise
        if not free:
            # 目录服务没给自由空间时用补集推算，两条路径互相校验
            free = free_extents_from_files(vol.geometry, files)
            self._log(LogLevel.DEBUG, "目录未提供自由区间，已由已用区间反推")

        report = analyze(
            files,
            free,
            vol.geometry,
            bucket_count=bucket_count,
            elapsed_s=time.monotonic() - t0,
        )
        self.volume = vol
        self.report = report
        self._emit(FragMapEvent(report, seq=self._next_seq()))
        self._log(
            LogLevel.INFO,
            f"扫描完成：{report.file_count} 个文件 / {len(report.fragmented_files)} 个碎片文件，"
            f"文件碎片度 {report.file_frag_score:.1%}，自由空间碎片度 {report.free_frag_score:.1%}",
            detail=f"耗时 {report.elapsed_s * 1000:.0f} ms",
        )
        self._set_state(JobState.IDLE, "扫描完成")
        return report

    # -- 规划 -------------------------------------------------------------

    def make_plan(
        self,
        strategy: Strategy,
        file_ids: tuple[str, ...] = (),
        *,
        params: JobParams | None = None,
    ) -> Plan:
        if self.report is None or self.volume is None:
            raise ValueError("请先扫描目标卷")
        p = params or self.params
        self.job_id = f"job-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        self._set_state(JobState.PLANNING, f"规划 {strategy.value}")
        plan = build_plan(self.job_id, strategy, self.report, file_ids=file_ids, params=p)
        problems = validate_plan(plan, self.volume.geometry.block_count)
        if problems:
            plan = replace(plan, feasible=False, reasons=tuple(problems))
        self.plan = plan
        self.params = p
        self._emit(PlanEvent(plan, seq=self._next_seq(), job_id=self.job_id))
        for reason in plan.reasons:
            self._log(LogLevel.WARNING, reason)
        for note in plan.notes:
            self._log(LogLevel.INFO, note)
        self._set_state(JobState.IDLE, "规划完成")
        return plan

    # -- 执行 -------------------------------------------------------------

    def execute(
        self,
        plan: Plan | None = None,
        device=None,
        *,
        resume: bool = True,
        halt_on_error: bool = False,
        open_device_fn: Callable[[VolumeInfo], object] | None = None,
    ) -> ExecutionResult:
        """执行计划。``device`` 与 ``open_device_fn`` 二选一。

        返回 :class:`ExecutionResult`；取消不是失败，用 ``result.cancelled`` 区分。
        """
        plan = plan or self.plan
        if plan is None:
            raise ValueError("没有可执行的计划")
        if self.volume is None:
            raise ValueError("尚未选择目标卷")
        # 顺序要紧：先判"计划本身是否允许执行"，再判"有没有活可干"。
        # 反过来写的话，一份明确标记为不可行的空计划会被当成"没事可做"
        # 静默通过 —— 用户以为跑完了，实际上什么都没做。
        problems = validate_plan(plan, self.volume.geometry.block_count)
        if problems:
            for p in problems:
                self._log(LogLevel.ERROR, p)
            raise PlanRejected(problems)
        if not plan.feasible:
            for reason in plan.reasons:
                self._log(LogLevel.ERROR, reason)
            raise PlanRejected(list(plan.reasons) or ["计划标记为不可行"])

        if not plan.ops:
            self._log(LogLevel.WARNING, "计划里没有可执行的操作")
            result = ExecutionResult()
            self.last_result = result
            self._emit(JobDoneEvent(JobState.DONE, message="没有需要搬运的数据", seq=self._next_seq(), job_id=plan.job_id))
            return result

        self.job_id = plan.job_id
        self.control = RunControl()
        dev = device if device is not None else (open_device_fn or _default_open)(self.volume)

        state_dir = self._state_dir if self._state_dir is not None else default_state_dir()
        store = CheckpointStore(os.path.join(state_dir, f"{plan.job_id}.checkpoint.json"))
        journal = JsonlJournal(os.path.join(state_dir, f"{plan.job_id}.jsonl"))

        checkpoint: Checkpoint | None = None
        if resume and store.exists:
            checkpoint = store.load()
            if checkpoint is not None and checkpoint.plan_hash != plan.plan_hash:
                self._log(
                    LogLevel.WARNING,
                    "断点与当前计划不匹配（计划已变化），将从头执行",
                )
                checkpoint = None
            elif checkpoint is not None:
                self._log(
                    LogLevel.INFO,
                    f"发现断点：已完成 {len(checkpoint.completed_ops)}/{plan.op_count} 个文件，将续做",
                )
        elif not resume:
            store.clear()

        self._set_state(JobState.RUNNING, "开始执行")
        runner = JobRunner(
            dev,
            dev,
            self.params,
            self.control,
            store=store,
            journal=journal,
            on_progress=self._on_progress,
            on_log=lambda m: self._log(LogLevel.INFO, m),
            halt_on_error=halt_on_error,
        )

        try:
            result = runner.run(plan, resume=checkpoint)
        except CancelledError:
            self._set_state(JobState.CANCELLED, "已停止")
            result = ExecutionResult(cancelled=True)
        except BaseException as exc:  # noqa: BLE001 - 任何异常都要把状态机推进到终态
            self._log(LogLevel.ERROR, f"执行失败：{type(exc).__name__}: {exc}")
            self._set_state(JobState.FAILED, str(exc))
            journal.append({"event": "abort", "error": f"{type(exc).__name__}: {exc}"})
            journal.close()
            raise
        finally:
            journal.close()

        self.last_result = result
        if result.cancelled:
            self._set_state(JobState.CANCELLED, "用户停止")
        elif result.failed_ops and not result.moved_ops:
            self._set_state(JobState.FAILED, "全部操作失败")
        else:
            self._set_state(JobState.DONE, "执行完成")

        self._emit(
            JobDoneEvent(
                self.state,
                moved_ops=result.moved_ops,
                failed_ops=result.failed_ops,
                skipped_ops=result.skipped_ops,
                bytes_moved=result.bytes_moved,
                elapsed_s=result.elapsed_s,
                message=("已停止" if result.cancelled else f"完成 {result.moved_ops} 个文件"),
                seq=self._next_seq(),
                job_id=plan.job_id,
            )
        )
        if not result.cancelled and result.failed_ops == 0:
            store.clear()  # 全部成功才清断点；有失败则留着便于续做
        return result

    def _on_progress(self, snapshot: Progress) -> None:
        now = time.monotonic()
        if now - self._last_progress_at < self._progress_interval and snapshot.ratio < 1.0:
            return
        self._last_progress_at = now
        self._emit(ProgressEvent(snapshot, seq=self._next_seq(), job_id=self.job_id))


def _default_open(volume: VolumeInfo):
    """默认设备打开策略：真机走 win32，其余直接报错。"""
    from ..devices import DeviceSpec, open_device

    if not os.name == "nt":
        raise DeviceProviderError("原始卷设备仅在 Windows 上可用")
    return open_device(
        DeviceSpec(
            kind="win32",
            path=volume.device_path,
            block_size=volume.geometry.block_size,
            writable=True,
            lock_volume=not volume.is_mounted,
        )
    )


# --------------------------------------------------------------------------
# 线程宿主
# --------------------------------------------------------------------------


class EngineService:
    """把 :class:`EngineCore` 搬到工作线程，供 GUI 使用。

    命令分发规则（重要）：**暂停 / 继续 / 停止 / 改参数不走命令队列**，
    它们在 :meth:`submit` 里直接作用于线程安全的 :class:`RunControl`。
    原因：队列由正在执行任务的这条线程消费，任务跑起来之后队列就没人读了，
    暂停命令会永远排在后面。旧版的"取消没反应"正是这个结构造成的。
    """

    def __init__(self, core: EngineCore, device_provider: Callable | None = None) -> None:
        self.core = core
        self.device_provider = device_provider
        self._queue: queue.Queue[Command] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._device = None
        self._device_owned = False
        self._busy = threading.Event()

    # -- 生命周期 ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._running.set()
        self._thread = threading.Thread(target=self._loop, name="lba-engine", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self.core.control.request_cancel()
        self._running.clear()
        self._queue.put(Shutdown())
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        self._close_device()

    @property
    def busy(self) -> bool:
        return self._busy.is_set()

    # -- 提交 -------------------------------------------------------------

    def submit(self, cmd: Command) -> None:
        if isinstance(cmd, PauseJob):
            self.core.control.request_pause()
            self.core._set_state(JobState.PAUSED, "用户暂停")
            return
        if isinstance(cmd, ResumeJob):
            self.core.control.request_resume()
            self.core._set_state(JobState.RUNNING, "用户继续")
            return
        if isinstance(cmd, CancelJob):
            self.core.control.request_cancel()
            self.core._set_state(JobState.STOPPING, "用户停止")
            if not cmd.keep_checkpoint:
                self._queue.put(Shutdown())  # 复用队列做"尽快收工"信号
            return
        if isinstance(cmd, SetParams):
            # 只有限速与校验开关支持热更新；其余参数改动需要重新规划
            self.core.params = replace(
                self.core.params,
                rate_limit_mbps=cmd.params.rate_limit_mbps,
                verify=cmd.params.verify,
            )
            return
        self._queue.put(cmd)

    # -- 工作线程 ---------------------------------------------------------

    def _loop(self) -> None:
        while self._running.is_set():
            try:
                cmd = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self._handle(cmd)
            except Exception as exc:  # noqa: BLE001 - 工作线程不能死
                self.core._log(LogLevel.ERROR, f"命令 {cmd.name} 执行失败：{exc}")
                self.core._set_state(JobState.IDLE, "命令失败")
            finally:
                self._busy.clear()

        self._close_device()

    def _handle(self, cmd: Command) -> None:
        if isinstance(cmd, Shutdown):
            self._running.clear()
            return
        self._busy.set()
        if isinstance(cmd, UpdateTarget):
            if cmd.params is not None:
                self.core.params = cmd.params
            self._close_device()
            self.core.set_target(cmd.volume, cmd.params)
            self.core.scan(cmd.volume)
            if cmd.strategy is not None and cmd.strategy.writes_blocks:
                # 预览：让用户在点「开始」之前就看到会动多少数据
                self.core.make_plan(cmd.strategy, cmd.file_ids)
            return
        if isinstance(cmd, StartJob):
            spec = cmd.spec
            if spec is not None:
                self.core.params = spec.params
                self.core.scan(spec.volume)
                plan = self.core.make_plan(spec.strategy, spec.file_ids)
            else:
                if self.core.report is None:
                    raise ValueError("请先扫描目标卷")
                plan = self.core.plan or self.core.make_plan(Strategy.COALESCE)
            if not plan.ops:
                for reason in plan.reasons:
                    self.core._log(LogLevel.WARNING, reason)
                self.core._log(LogLevel.WARNING, "没有可执行的整理操作")
                return
            self._device = self._open(self.core.volume)
            self.core.execute(plan, self._device)
            self._release_device()
            return

    def _open(self, volume):
        """打开设备。``device_provider`` 提供的是**宿主所有**的设备（例如
        演示卷），引擎只有使用权，任务结束后不能替宿主关掉。"""
        if self.device_provider is not None:
            self._device_owned = False
            return self.device_provider(volume)
        self._device_owned = True
        return _default_open(volume)

    def _release_device(self) -> None:
        """任务结束释放设备：只关自己打开的。"""
        if not self._device_owned:
            self._device = None
            return
        self._close_device()

    def _close_device(self) -> None:
        dev, self._device = self._device, None
        if dev is not None:
            try:
                dev.close()
            except OSError:
                pass
