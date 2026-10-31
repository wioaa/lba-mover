"""平台层：错误分类、卷枚举、权限提升。

这一层是唯一允许出现 ``ctypes`` / ``win32`` 字样的地方（``devices.win32``
除外）。引擎只消费这里抛出的 :class:`lbamover.devices.base.DeviceError`，
因此引擎可以在 Linux CI 上跑通全部分支。
"""

from __future__ import annotations

from .errors import RETRYABLE_WINERRORS, DeviceUnavailable
from .volumes import enumerate_volumes, probe_volume

__all__ = [
    "DeviceUnavailable",
    "RETRYABLE_WINERRORS",
    "enumerate_volumes",
    "probe_volume",
]
