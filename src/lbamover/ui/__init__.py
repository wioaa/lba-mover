"""UI 包。依赖 PySide6；引擎核心不依赖本包。"""

from __future__ import annotations

__all__ = ["MainWindow", "build_app", "main"]


def __getattr__(name: str):
    # 延迟导入：让 `import lbamover.ui` 在没装 PySide6 的机器上也不炸，
    # 只有真正要用窗口时才要求 Qt 存在。
    if name == "MainWindow":
        from .main_window import MainWindow

        return MainWindow
    if name in ("build_app", "main"):
        from . import app

        return getattr(app, name)
    raise AttributeError(name)
