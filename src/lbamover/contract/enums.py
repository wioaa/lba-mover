"""契约层枚举。全部用 ``str`` 派生的枚举，便于跨线程 / 跨进程 / 落盘序列化。"""

from __future__ import annotations

from enum import Enum

__all__ = [
    "ErrorClass",
    "EventKind",
    "FreeSpaceSide",
    "Intensity",
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
                                                          └────────> PARTIAL
                                                          └────────> FAILED

    ``DONE`` / ``PARTIAL`` / ``FAILED`` 三者的区别只在失败比例：全成功是
    ``DONE``，有成功也有失败是 ``PARTIAL``，一个都没成功是 ``FAILED``。
    合并成"完成/失败"两档会让"19 个成功 1 个失败"这种最常见的结局无家可归。
    """

    IDLE = "idle"
    SCANNING = "scanning"
    PLANNING = "planning"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    DONE = "done"
    #: 大部分文件搬完了，但有若干个失败。
    #:
    #: 单独列出来是因为把它并进 ``DONE`` 会造成一个具体的误导：用户看到
    #: 「已完成」，就认为"这块盘整理好了"，而实际上有些文件原样躺在碎片里。
    #: 也不能一律 ``FAILED``：19 个成功 1 个失败说成"失败"同样不诚实。
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in (JobState.DONE, JobState.PARTIAL, JobState.FAILED, JobState.CANCELLED)

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

    只保留**动作语义明确**的策略。旧版把 ``range/auto_fill/compact/
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
    #: 腾出**指定大小**的连续可用空间，位置可由 ``FreeSpaceSide`` 指定卷首/卷尾。
    #: 与 ``COMPACT`` 的差别是它不关心碎片度，只关心"目标区最后是不是全空"。
    RECLAIM = "reclaim"

    @property
    def writes_blocks(self) -> bool:
        return self is not Strategy.ANALYZE_ONLY


class Intensity(str, Enum):
    """整理强度：同一种模式下，决定**动多少文件**。

    与 :class:`Strategy` 正交。策略回答"往哪搬、怎么搬"，强度回答"动多大一片"。
    旧版把两件事绑死在策略里（``COALESCE`` 只能作用于碎片文件，想动全部文件
    就得换策略，连落点方向也一起变了），于是"我想更彻底一点"这个需求没有
    任何入口 —— 换策略会顺手改掉目标，用户只能猜。

    五档是**体积维度上的递进**，从最省到最狠::

        FAST      碎片收益前 20%（≤500 个）
        STANDARD  全部碎片文件                ← 默认，与 2.0.0H 之前完全一致
        DEEP      碎片文件 + 不足 1 MiB 的零散小文件
        THOROUGH  全部文件，无下限
        EXTREME   只动大文件（默认 ≥256 MiB），按体积降序

    ``EXTREME`` 不在"包含链"上：它是从体积维度切最大的一批，可能漏掉一堆
    小碎片。这样切是刻意的 —— 极端档的用途是"把占了大半空间的那些文件
    一次性归位，腾出大片连续区"，而不是"治所有碎片"。
    """

    FAST = "fast"
    STANDARD = "standard"
    DEEP = "deep"
    THOROUGH = "thorough"
    EXTREME = "extreme"

    @property
    def label(self) -> str:
        """短名。写成属性是为了让界面、CLI、诊断报告共用一个来源 ——
        文案写三份就是等它们漂移（这个项目在 ``to_dict`` 上已经吃过一次）。"""
        return _INTENSITY_LABELS[self]


_INTENSITY_LABELS: dict["Intensity", str] = {
    Intensity.FAST: "快速",
    Intensity.STANDARD: "标准",
    Intensity.DEEP: "深度",
    Intensity.THOROUGH: "彻底",
    Intensity.EXTREME: "极端",
}


class FreeSpaceSide(str, Enum):
    """「可用空间整理」把连续空闲区放在卷的哪一侧。"""

    #: 卷首（低 LBA）。
    HEAD = "head"
    #: 卷尾（高 LBA）。
    TAIL = "tail"

    @property
    def label(self) -> str:
        return "卷首" if self is FreeSpaceSide.HEAD else "卷尾"


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

    @property
    def label(self) -> str:
        """中文短标签。

        英文枚举值不该出现在给用户看的文本里：``retryable`` 对程序员是
        信息，对用户是噪音 —— 而这一档最重要的信息恰恰是"还要不要再点一次"。
        """
        return {
            ErrorClass.RETRYABLE: "可重试",
            ErrorClass.FATAL: "不可重试",
            ErrorClass.VERIFY_MISMATCH: "校验不一致",
            ErrorClass.CANCELLED: "已取消",
        }[self]


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
