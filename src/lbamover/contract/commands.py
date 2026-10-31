"""UI → 引擎的命令。

命令走**单生产者队列**：所有改造引擎状态的入口（按钮、快捷键、CLI）
都只是往队列里丢一个不可变命令对象，引擎在自己的线程里串行消费。
这样「暂停和停止同时到达」这类竞态从未发生过——旧版每个 worker 各自
收取消信号，才出现「取消没反应」「按钮永久禁用」这一族缺陷。
"""

from __future__ import annotations

from typing import Any

from .enums import Strategy
from .models import JobParams, JobSpec, VolumeInfo

__all__ = [
    "CancelJob",
    "Command",
    "PauseJob",
    "ResumeJob",
    "SetParams",
    "Shutdown",
    "StartJob",
    "UpdateTarget",
]


class Command:
    """命令基类。不可变，可带一个用于日志的名称。"""

    __slots__ = ()

    name: str = "command"

    def __setattr__(self, key: str, value: Any) -> None:
        raise AttributeError(f"{type(self).__name__} 是不可变命令，不能修改 {key!r}")

    def describe(self) -> str:
        return self.name


class UpdateTarget(Command):
    """切换目标卷，并（可选）跑一次「扫描 + 规划」预演。

    ``strategy`` 带进来是有意的：用户点「扫描」时想看到的不只是碎片报告，
    还有"如果按当前策略整理，会是多少个文件/多少数据"。把规划放进同一条
    命令，保证预览与随后的执行用的是同一套参数。
    """

    __slots__ = ("volume", "params", "strategy", "file_ids")
    name = "update_target"

    def __init__(
        self,
        volume: VolumeInfo,
        params: JobParams | None = None,
        strategy: Strategy | None = None,
        file_ids: tuple[str, ...] = (),
    ) -> None:
        object.__setattr__(self, "volume", volume)
        object.__setattr__(self, "params", params)
        object.__setattr__(self, "strategy", strategy)
        object.__setattr__(self, "file_ids", file_ids)

    def describe(self) -> str:
        return f"选择目标卷 {self.volume.mount_point}"


class StartJob(Command):
    """按当前目标与策略开始整理。``spec`` 为 None 时由引擎用现有扫描结果构建。"""

    __slots__ = ("spec",)
    name = "start_job"

    def __init__(self, spec: JobSpec | None = None) -> None:
        object.__setattr__(self, "spec", spec)

    def describe(self) -> str:
        if self.spec is None:
            return "开始整理（沿用当前扫描结果）"
        return f"开始整理：{self.spec.strategy.value} @ {self.spec.volume.mount_point}"


class PauseJob(Command):
    __slots__ = ()
    name = "pause"

    def describe(self) -> str:
        return "暂停"


class ResumeJob(Command):
    __slots__ = ()
    name = "resume"

    def describe(self) -> str:
        return "继续"


class CancelJob(Command):
    """停止任务。``keep_checkpoint=True`` 时保留断点，下次可续做。"""

    __slots__ = ("keep_checkpoint",)
    name = "cancel"

    def __init__(self, keep_checkpoint: bool = True) -> None:
        object.__setattr__(self, "keep_checkpoint", keep_checkpoint)

    def describe(self) -> str:
        return "停止任务（保留断点）" if self.keep_checkpoint else "停止任务（丢弃断点）"


class SetParams(Command):
    """运行期调整参数。只有部分参数支持热更新（限速、并发上限、校验开关）。"""

    __slots__ = ("params",)
    name = "set_params"

    def __init__(self, params: JobParams) -> None:
        object.__setattr__(self, "params", params)

    def describe(self) -> str:
        return f"更新参数（限速 {self.params.rate_limit_mbps:g} MiB/s）"


class Shutdown(Command):
    """引擎线程退出。会先请求取消当前任务。"""

    __slots__ = ()
    name = "shutdown"

    def describe(self) -> str:
        return "关闭引擎"


#: 供 UI 做类型提示/穷举测试用的策略命令映射说明。
STRATEGY_LABELS: dict[Strategy, str] = {
    Strategy.ANALYZE_ONLY: "只分析（不写盘）",
    Strategy.COALESCE: "合并碎片（每个文件合成一段）",
    Strategy.COMPACT: "向卷首聚拢（腾出尾部大块连续空间）",
    Strategy.RELOCATE_TAIL: "搬到卷尾（为卷首让出连续空间）",
}
