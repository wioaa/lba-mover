"""Win32 底层封装层。

本包是项目中**唯一**允许出现 ``ctypes`` 的地方；UI 层与 core 层只能通过
这里暴露的纯 Python 接口访问系统能力。
"""

__all__ = ["api", "volume", "bitmap", "retrieval", "mover", "privilege"]
