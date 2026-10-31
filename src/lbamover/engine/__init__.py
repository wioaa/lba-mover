"""引擎包。

对外只暴露三件东西：门面（:class:`EngineCore` / :class:`EngineService`）、
错误类型、以及构建计划的纯函数。其余模块是内部实现细节，可以自由重构。
"""

from __future__ import annotations

from .control import RunControl
from .errors import CancelledError, PlanRejected, VerifyError
from .engine import DeviceProviderError, EngineCore, EngineService
from .frag import DEFAULT_BUCKETS, analyze, bucketize, free_extents_from_files
from .planner import build_plan, select_files, validate_plan
from .scheduler import ExecutionResult, JobRunner, RateLimiter

__all__ = [
    "CancelledError",
    "DeviceProviderError",
    "DEFAULT_BUCKETS",
    "EngineCore",
    "EngineService",
    "ExecutionResult",
    "JobRunner",
    "PlanRejected",
    "RateLimiter",
    "RunControl",
    "VerifyError",
    "analyze",
    "bucketize",
    "build_plan",
    "free_extents_from_files",
    "select_files",
    "validate_plan",
]
