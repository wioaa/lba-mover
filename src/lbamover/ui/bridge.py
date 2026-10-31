"""引擎 ⇄ UI 桥。

这是两侧唯一的接触点，也是**唯一**允许出现 ``QObject`` 的地方（引擎侧
完全不知道 Qt 存在）。

线程模型：引擎在工作线程里调 :meth:`emit`，桥把不可变事件翻译成 Qt 信号。
Qt 对跨线程信号自动走队列连接，因此 GUI 线程永远在事件循环里收件，不需要
任何锁——这正是"通过明确定义的接口通信"的实际收益：把跨线程同步问题
收敛成一个信号收发。
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from ..contract.enums import EventKind, JobState, LogLevel
from ..contract.events import (
    Event,
    FragMapEvent,
    JobDoneEvent,
    LogEvent,
    PlanEvent,
    ProgressEvent,
    StateEvent,
)

__all__ = ["EngineBridge"]


class EngineBridge(QObject):
    """实现 :class:`lbamover.contract.ports.EventSink`。"""

    stateChanged = Signal(object)      # JobState
    progressChanged = Signal(object)   # Progress
    logRecord = Signal(object, str, str)  # LogLevel, message, detail
    fragReportReady = Signal(object)   # FragReport
    planReady = Signal(object)         # Plan
    jobFinished = Signal(object)       # JobDoneEvent

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.count = 0
        self.last_state = JobState.IDLE

    # -- EventSink --------------------------------------------------------

    def emit(self, event: Event) -> None:  # noqa: A003 - 契约要求这个名字
        self.count += 1
        kind = event.kind
        if kind is EventKind.PROGRESS:
            self.progressChanged.emit(event.progress)
        elif kind is EventKind.STATE:
            self.last_state = event.state
            self.stateChanged.emit(event.state)
        elif kind is EventKind.LOG:
            self.logRecord.emit(event.level, event.message, event.detail)
        elif kind is EventKind.FRAG_MAP:
            self.fragReportReady.emit(event.report)
        elif kind is EventKind.PLAN:
            self.planReady.emit(event.plan)
        elif kind is EventKind.JOB_DONE:
            self.jobFinished.emit(event)

    # -- 测试/调试辅助 ----------------------------------------------------

    def drain(self) -> list[str]:
        """说明性方法：桥不做缓冲，事件即时转发。存在是为了让测试可读。"""
        return []
