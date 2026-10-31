"""应用启动：QApplication + 主题 + 主窗口。

启动参数的取舍：只有 ``--demo`` 一个开关会改变行为，其余都是展示性参数。
一个"整理磁盘"的工具不该有一堆需要读文档才能懂的启动参数。
"""

from __future__ import annotations

import argparse
import os
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from .. import VERSION_DISPLAY
from .theme import DARK, LIGHT, apply_theme
from .main_window import MainWindow

__all__ = ["main", "build_app"]


def _parse(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="lbamover", description="LBA 块级流式整理引擎与桌面控制台")
    p.add_argument("--demo", action="store_true", help="启动后立即载入内存合成卷（不接触真实磁盘）")
    p.add_argument("--light", action="store_true", help="使用浅色主题")
    p.add_argument("--version", action="version", version=f"LBAMover {VERSION_DISPLAY}")
    return p.parse_args(argv)


def build_app(argv: list[str] | None = None) -> tuple[QApplication, MainWindow]:
    """创建 QApplication 与主窗口。供 main() 与冒烟测试共用。"""
    args = _parse(argv if argv is not None else sys.argv[1:])
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
        os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.*=false")

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("LBAMover")
    app.setApplicationVersion(VERSION_DISPLAY)
    app.setAttribute(Qt.ApplicationAttribute.AA_DontShowIconsInMenus, True)

    palette = LIGHT if args.light else DARK
    apply_theme(app, palette)
    window = MainWindow(palette=palette)
    if args.demo:
        window.load_demo()
    return app, window


def main(argv: list[str] | None = None) -> int:
    app, window = build_app(argv)
    window.show()
    return app.exec()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
