"""契约层枚举。全部用 ``str`` 派生的枚举，便于跨线程 / 跨进程 / 落盘序列化。"""

from __future__ import annotations

from enum import Enum

__all__ = [
    "ErrorClass",
    "EventKind",
    "JobState",
    "LogLevel",
    "Strategy",
    "VolumeKind",
]


class JobState(str, Enum):
    """任务状态机。

    合法迁移（由 engine.state 强制校验）::

        IDLE ──start──> SCANNING ──> PLANNING ──> RUNNING ⇄ PAUSED
                                             │         │
                                             └─────────┴─> STOPPING ─> CANCELLED
                                                          └────────> DONE
                                                          └────────> FAILED
    """

    IDLE = "idle"
    SCANNING = "scanning"
    PLANNING = "planning"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in (JobState.DONE, JobState.FAILED, JobState.CANCELLED)

    @property
    def is_active(self) -> bool:
        """占用引擎线程、需要用户等待的状态。"""
        return self in (
            JobState.SCANNING,
            JobState.PLANNING,
            JobState.RUNNING,
            JobState.PAUSED,
            JobState.STOPPING,
        )


class Strategy(str, Enum):
    """整理策略。

    只保留三个**动作语义明确**的策略。旧版把 ``range/auto_fill/compact/
    fragment/zone/relocate`` 六个并列塞进一个下拉，用户无法自解释，
    因此新版按「优化目标」而不是「算法名」命名。
    """

    #: 只扫描、出报告，不动任何块。
    ANALYZE_ONLY = "analyze_only"
    #: 把每个碎片文件的多个 extent 合并到单段连续区（降低该文件碎片度）。
    COALESCE = "coalesce"
    #: 把选中文件向卷首聚拢，压缩自由空间的碎片（提升大块连续可用空间）。
    COMPACT = "compact"
    #: 把选中文件搬到卷尾，为卷首让出连续空间。
    RELOCATE_TAIL = "relocate_tail"

    @property
    def writes_blocks(self) -> bool:
        return self is not Strategy.ANALYZE_ONLY


class LogLevel(str, Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class EventKind(str, Enum):
    """引擎 → UI 的事件类型。UI 只按 kind 分派。"""

    STATE = "state"
    PROGRESS = "progress"
    LOG = "log"
    FRAG_MAP = "frag_map"
    PLAN = "plan"
    JOB_DONE = "job_done"


class ErrorClass(str, Enum):
    """错误分类决定「重试还是放弃」，是重试策略的唯一输入。

    分类错误会导致两种事故：把不可恢复错误重试到天荒地老，或把瞬时错误
    直接判死。因此分类必须显式、可测。
    """

    #: 瞬时错误，重试可能成功（设备忙、超时、共享冲突）。
    RETRYABLE = "retryable"
    #: 不可恢复（参数非法、越界、只读介质、权限不足）。
    FATAL = "fatal"
    #: 由于取消/停止产生，不算失败，不重试。
    CANCELLED = "cancelled"
    #: 数据校验不一致，重试一次通常能过（坏块多为随机）。
    VERIFY_MISMATCH = "verify_mismatch"


class VolumeKind(str, Enum):
    """卷/介质类型，决定默认参数与风险提示。"""

    FIXED = "fixed"
    REMOVABLE = "removable"
    NETWORK = "network"
    RAMDISK = "ramdisk"
    UNKNOWN = "unknown"

    @property
    def writable_by_default(self) -> bool:
        return self in (VolumeKind.FIXED, VolumeKind.REMOVABLE, VolumeKind.RAMDISK)
