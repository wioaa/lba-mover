"""块设备层：以 LBA 为单位的裸读写抽象与两个安全实现（内存 / 镜像文件）。

真机 Windows 原始卷实现在 :mod:`lbamover.devices.win32`，导入口统一走
:func:`open_device`，由 :class:`DeviceSpec` 决定用哪一个——引擎代码里
不出现任何 ``if sys.platform``。
"""

from __future__ import annotations

from .base import DeviceError, DeviceSpec, open_device
from .image import ImageDevice
from .memory import MemoryDevice

__all__ = [
    "DeviceError",
    "DeviceSpec",
    "ImageDevice",
    "MemoryDevice",
    "open_device",
]
