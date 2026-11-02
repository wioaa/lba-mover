"""引擎内部异常。只放引擎自己会抛/会接的类型，不属于契约层。"""

from __future__ import annotations

__all__ = ["CancelledError", "PlanningCancelled", "VerifyError", "PlanRejected"]


class CancelledError(Exception):
    """用户请求停止（或引擎关闭）。**不是失败**，不计入 failed 统计。"""


class PlanningCancelled(CancelledError):
    """规划/筛选过程中被取消。

    单独一个子类，而不是让规划器直接抛 :class:`CancelledError`：规划是
    **纯函数**，``build_plan`` 的调用方包括 CLI 与测试，它们不该被迫处理一个
    "用户按了停止"的语义。规划器只报告"这次计算被中断了"，把中断翻译成
    CANCELLED 状态是 :class:`EngineCore` 的职责。

    继承 ``CancelledError`` 是为了让所有既有的 ``except CancelledError``
    仍然正确 —— 取消永远不是失败。
    """


class VerifyError(Exception):
    """回读校验不一致。默认按可重试处理（坏块多为随机）。"""

    def __init__(self, message: str, *, lba: int | None = None, blocks: int = 0) -> None:
        super().__init__(message)
        self.lba = lba
        self.blocks = blocks


class PlanRejected(Exception):
    """计划未通过静态自检，拒绝执行。带全部问题列表。"""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("计划未通过自检：" + "；".join(problems))
        self.problems = tuple(problems)
