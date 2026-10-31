"""引擎 → UI 事件。

两个设计决定：

* **不可变快照**。事件不携带引擎内部对象引用，UI 即使延迟处理（事件队列
  积压）也读不到已变化的内部状态。旧版把这个坑踩成了「进度条偶尔回跳」。
* **不用 dataclass 生成 ``__init__``**。事件需要「子类只暴露业务字段」的
  构造签名，而 dataclass 的字段继承会把 ``kind`` 顶到参数列表最前面，
  调用点全变成 ``LogEvent(EventKind.LOG, ...)``，容易传错。这里显式写
  ``__init__``，代价是几行样板，收益是调用点自解释。
"""

from __future__ import annotations

from typing import Any

from .enums import EventKind, JobState, LogLevel
from .models import FragReport, Plan, Progress

__all__ = [
    "Event",
    "FragMapEvent",
    "JobDoneEvent",
    "LogEvent",
    "PlanEvent",
    "ProgressEvent",
    "StateEvent",
]


class Event:
    """所有事件的公共外壳。``seq`` 单调递增，UI 可用它丢弃乱序/过期事件。"""

    __slots__ = ("_seq", "_job_id")

    kind: EventKind = EventKind.LOG

    def __init__(self, seq: int = 0, job_id: str = "") -> None:
        object.__setattr__(self, "_seq", seq)
        object.__setattr__(self, "_job_id", job_id)

    # -- 只读访问 ---------------------------------------------------------

    @property
    def seq(self) -> int:
        return self._seq

    @property
    def job_id(self) -> str:
        return self._job_id

    # -- 不可变 -----------------------------------------------------------

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(f"{type(self).__name__} 是不可变事件，不能修改 {name!r}")

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "seq": self.seq, "job_id": self.job_id}

    def __repr__(self) -> str:  # pragma: no cover - 调试可读性
        fields = ", ".join(f"{k}={v!r}" for k, v in self.as_dict().items())
        return f"{type(self).__name__}({fields})"


class StateEvent(Event):
    """状态机迁移通知。UI 的启停按钮可用性完全由它驱动。"""

    __slots__ = ("state", "reason")
    kind = EventKind.STATE

    def __init__(self, state: JobState, reason: str = "", seq: int = 0, job_id: str = "") -> None:
        super().__init__(seq, job_id)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "reason", reason)

    def as_dict(self) -> dict[str, Any]:
        d = super().as_dict()
        d.update(state=self.state.value, reason=self.reason)
        return d


class ProgressEvent(Event):
    __slots__ = ("progress",)
    kind = EventKind.PROGRESS

    def __init__(self, progress: Progress, seq: int = 0, job_id: str = "") -> None:
        super().__init__(seq, job_id)
        object.__setattr__(self, "progress", progress)

    def as_dict(self) -> dict[str, Any]:
        p = self.progress
        d = super().as_dict()
        d.update(
            done_blocks=p.done_blocks,
            total_blocks=p.total_blocks,
            rate_bps=p.rate_bps,
            ratio=p.ratio,
        )
        return d


class LogEvent(Event):
    __slots__ = ("level", "message", "detail")
    kind = EventKind.LOG

    def __init__(
        self,
        level: LogLevel,
        message: str,
        detail: str = "",
        seq: int = 0,
        job_id: str = "",
    ) -> None:
        super().__init__(seq, job_id)
        object.__setattr__(self, "level", level)
        object.__setattr__(self, "message", message)
        object.__setattr__(self, "detail", detail)

    def as_dict(self) -> dict[str, Any]:
        d = super().as_dict()
        d.update(level=self.level.value, message=self.message)
        return d


class FragMapEvent(Event):
    __slots__ = ("report",)
    kind = EventKind.FRAG_MAP

    def __init__(self, report: FragReport, seq: int = 0, job_id: str = "") -> None:
        super().__init__(seq, job_id)
        object.__setattr__(self, "report", report)

    def as_dict(self) -> dict[str, Any]:
        r = self.report
        d = super().as_dict()
        d.update(
            files=r.file_count,
            free_frag=r.free_frag_score,
            file_frag=r.file_frag_score,
        )
        return d


class PlanEvent(Event):
    __slots__ = ("plan",)
    kind = EventKind.PLAN

    def __init__(self, plan: Plan, seq: int = 0, job_id: str = "") -> None:
        super().__init__(seq, job_id)
        object.__setattr__(self, "plan", plan)

    def as_dict(self) -> dict[str, Any]:
        p = self.plan
        d = super().as_dict()
        d.update(ops=p.op_count, blocks=p.total_blocks, feasible=p.feasible)
        return d


class JobDoneEvent(Event):
    __slots__ = (
        "state",
        "moved_ops",
        "failed_ops",
        "skipped_ops",
        "bytes_moved",
        "elapsed_s",
        "message",
    )
    kind = EventKind.JOB_DONE

    def __init__(
        self,
        state: JobState,
        moved_ops: int = 0,
        failed_ops: int = 0,
        skipped_ops: int = 0,
        bytes_moved: int = 0,
        elapsed_s: float = 0.0,
        message: str = "",
        seq: int = 0,
        job_id: str = "",
    ) -> None:
        super().__init__(seq, job_id)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "moved_ops", moved_ops)
        object.__setattr__(self, "failed_ops", failed_ops)
        object.__setattr__(self, "skipped_ops", skipped_ops)
        object.__setattr__(self, "bytes_moved", bytes_moved)
        object.__setattr__(self, "elapsed_s", elapsed_s)
        object.__setattr__(self, "message", message)

    def as_dict(self) -> dict[str, Any]:
        d = super().as_dict()
        d.update(
            state=self.state.value,
            moved_ops=self.moved_ops,
            failed_ops=self.failed_ops,
            skipped_ops=self.skipped_ops,
            bytes_moved=self.bytes_moved,
            elapsed_s=round(self.elapsed_s, 3),
        )
        return d
