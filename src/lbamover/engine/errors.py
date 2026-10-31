"""引擎内部异常。只放引擎自己会抛/会接的类型，不属于契约层。"""

from __future__ import annotations

__all__ = ["CancelledError", "VerifyError", "PlanRejected"]


class CancelledError(Exception):
    """用户请求停止（或引擎关闭）。**不是失败**，不计入 failed 统计。"""


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
