"""契约层：引擎与 UI 之间唯一的共享词汇表。

本包**只依赖标准库**，不允许 import PySide6，也不允许 import engine 的任何
实现模块。这样保证：

* UI 可以在没有引擎实现的情况下单独编译、单独测试（用假引擎）；
* 引擎可以在无 GUI 环境（CI、CLI）下完整运行；
* 两侧的耦合面收敛为「数据类 + 枚举 + Protocol」，改一处必须两边同时看见。
"""

from __future__ import annotations

from .commands import (
    CancelJob,
    Command,
    PauseJob,
    ResumeJob,
    SetParams,
    Shutdown,
    StartJob,
    UpdateTarget,
)
from .enums import (
    ErrorClass,
    EventKind,
    JobState,
    LogLevel,
    Strategy,
    VolumeKind,
)
from .events import (
    Event,
    FragMapEvent,
    JobDoneEvent,
    LogEvent,
    PlanEvent,
    ProgressEvent,
    StateEvent,
)
from .models import (
    Extent,
    FileFrag,
    FragReport,
    Geometry,
    JobParams,
    JobSpec,
    MoveOp,
    Plan,
    Progress,
    VolumeInfo,
)
from .ports import BlockDevice, EventSink, FileCatalog, JobJournal, ListSink, NullSink

__all__ = [
    # enums
    "ErrorClass",
    "EventKind",
    "JobState",
    "LogLevel",
    "Strategy",
    "VolumeKind",
    # models
    "Extent",
    "FileFrag",
    "FragReport",
    "Geometry",
    "JobParams",
    "JobSpec",
    "MoveOp",
    "Plan",
    "Progress",
    "VolumeInfo",
    # events
    "Event",
    "FragMapEvent",
    "JobDoneEvent",
    "LogEvent",
    "PlanEvent",
    "ProgressEvent",
    "StateEvent",
    # commands
    "CancelJob",
    "Command",
    "PauseJob",
    "ResumeJob",
    "SetParams",
    "Shutdown",
    "StartJob",
    "UpdateTarget",
    # ports
    "BlockDevice",
    "EventSink",
    "FileCatalog",
    "JobJournal",
    "ListSink",
    "NullSink",
]
