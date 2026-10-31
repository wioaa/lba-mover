"""重试策略：指数退避 + 抖动 + 错误分类。

三条规则，每一条都来自具体的失败模式：

1. **只重试可重试错误。** 参数非法、越界、只读介质重试一万次也是失败，
   而重试会放大写入次数——在块设备上这是危险的。
2. **指数退避 + 抖动。** 纯指数退避会让多个并发 worker 同步撞击同一个
   故障点（惊群）；抖动把它们的重试时刻错开。
3. **重试有上限。** 无上限重试等于把"任务失败"变成"任务永远不结束"，
   用户只能强杀进程，而定点了断点的任务强杀后不可续做。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable, TypeVar

from ..contract.enums import ErrorClass
from ..devices.base import DeviceError
from .errors import CancelledError, VerifyError

__all__ = ["RetryPolicy", "run_with_retry", "classify_exception"]

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """退避参数。``max_retries`` 不含首次尝试。"""

    max_retries: int = 3
    base_delay: float = 0.05
    factor: float = 2.0
    max_delay: float = 2.0
    jitter: float = 0.35

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries 不能为负")
        if self.base_delay < 0 or self.max_delay < self.base_delay:
            raise ValueError("退避参数非法")
        if not (0.0 <= self.jitter <= 1.0):
            raise ValueError("jitter 必须在 0..1")

    @property
    def max_attempts(self) -> int:
        return self.max_retries + 1

    def delay_for(self, attempt: int, rng: random.Random | None = None) -> float:
        """第 ``attempt`` 次失败后应等待的秒数（``attempt`` 从 0 开始）。"""
        base = min(self.max_delay, self.base_delay * (self.factor**attempt))
        if self.jitter <= 0:
            return base
        r = rng or random
        # ±jitter 的均匀抖动：范围 [base*(1-j), base*(1+j)]
        return max(0.0, base * (1.0 + r.uniform(-self.jitter, self.jitter)))


def classify_exception(exc: BaseException) -> ErrorClass:
    """把异常映射成重试语义。未知异常一律 FATAL。"""
    if isinstance(exc, CancelledError):
        return ErrorClass.CANCELLED
    if isinstance(exc, VerifyError):
        return ErrorClass.VERIFY_MISMATCH
    if isinstance(exc, DeviceError):
        return exc.error_class
    return ErrorClass.FATAL


def _should_retry(exc: BaseException) -> bool:
    return classify_exception(exc) in (ErrorClass.RETRYABLE, ErrorClass.VERIFY_MISMATCH)


def run_with_retry(
    fn: Callable[[], T],
    policy: RetryPolicy,
    *,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
    control=None,
    sleep: Callable[[float], None] | None = None,
    rng: random.Random | None = None,
) -> T:
    """执行 ``fn``，按策略重试。

    ``on_retry(attempt, exc, delay)`` 用于上报——UI 的"重试中"提示与日志
    都靠它，没有它用户只会看到进度不动却不知道在重试。
    """
    attempts = 0
    while True:
        if control is not None:
            control.checkpoint()
        try:
            return fn()
        except BaseException as exc:  # noqa: BLE001 - 分类后立即决定去留
            kind = classify_exception(exc)
            if kind is ErrorClass.CANCELLED:
                raise
            if not _should_retry(exc) or attempts >= policy.max_retries:
                raise
            delay = policy.delay_for(attempts, rng)
            attempts += 1
            if on_retry is not None:
                on_retry(attempts, exc, delay)
            if control is not None:
                control.sleep(delay)
            elif sleep is not None:
                sleep(delay)
            else:  # pragma: no cover - 测试路径通常注入 sleep
                time.sleep(delay)
