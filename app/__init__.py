"""LBA Tail Mover 应用包。

包结构：
    app.win32   —— 全部 ctypes / Win32 调用（只在这一层出现）
    app.core    —— 纯业务逻辑（模型、扫描、规划）
    app.ui      —— PySide6 界面层
"""

__all__ = ["win32", "core", "ui"]
__version__ = "1.0.0"

#: 修订字母：与 ``__version__`` 组合成对外显示版本（如 ``v1.0.0A``）。
#: 约定见用户规范——三段号只写在这里，字母另存字段；Windows 资源里的
#: FileVersion 用三段转成的数字四元组（1.0.0.0），字母只在产品字符串里体现。
__revision__ = "G"


def display_version() -> str:
    """对外展示用版本串，例如 ``1.0.0A``（无修订号时退化为 ``__version__``）。"""
    rev = globals().get("__revision__") or ""
    return f"{__version__}{rev}"
