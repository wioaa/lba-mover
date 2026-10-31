"""LBAMover —— LBA 块级流式整理引擎与桌面控制台。

版本号约定（见 pyproject.toml）：三段写在 PEP 440 的 version 里，
小版本字母放 [tool.lbamover].revision，展示时拼成 ``2.0.0A`` 形式。
"""

from __future__ import annotations

__all__ = [
    "VERSION",
    "REVISION",
    "VERSION_DISPLAY",
    "__version__",
]

#: PEP 440 合法的三段版本号。改这里时同步改 pyproject.toml。
VERSION = "2.0.0"

#: 小版本字母（A/B/C…），空字符串表示基线版本。与 VERSION 分开存放，
#: 因为 ``2.0.0A`` 不是合法 PEP 440，pip / setuptools 会直接拒绝。
REVISION = "A"

#: 对外展示用版本号。
VERSION_DISPLAY = f"{VERSION}{REVISION}"

#: 兼容 ``__version__`` 惯例。
__version__ = VERSION_DISPLAY
