"""LBA Tail Mover 程序入口。

职责：
* 高 DPI 策略与字体；
* 单实例互斥（避免两个进程同时操作同一块磁盘）；
* 未捕获异常兜底（写日志 + 弹窗提示，而不是静默崩溃）；
* 应用全局主题样式表。
"""

from __future__ import annotations

import ctypes
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QFont  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from app.ui import theme  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402

MUTEX_NAME = "Global\\LBA_Tail_Mover_SingleInstance"
LOG_PATH = BASE_DIR / "lba-mover.log"

_mutex_handle: Optional[int] = None


def acquire_single_instance() -> bool:
    """创建命名互斥体，检测是否已有实例在运行。"""
    global _mutex_handle
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        _mutex_handle = handle
        last_error = ctypes.get_last_error()
        if last_error == 183:  # ERROR_ALREADY_EXISTS
            return False
        return bool(handle)
    except (AttributeError, OSError):
        # 非 Windows 或调用失败时不阻塞启动
        return True


def write_crash_log(text: str) -> None:
    """把崩溃信息追加写入日志文件。"""
    try:
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"\n===== {datetime.now().isoformat()} =====\n")
            handle.write(text)
            handle.write("\n")
    except OSError:
        pass


def install_exception_hook(app: QApplication) -> None:
    """安装全局异常钩子。"""

    def _hook(exc_type, exc_value, exc_tb) -> None:
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        write_crash_log(text)
        sys.stderr.write(text)
        try:
            box = QMessageBox()
            box.setIcon(QMessageBox.Icon.Critical)
            box.setWindowTitle("LBA Tail Mover —— 未捕获异常")
            box.setText(f"{exc_type.__name__}: {exc_value}")
            box.setInformativeText(f"详细信息已写入：\n{LOG_PATH}")
            box.exec()
        except Exception:  # noqa: BLE001 - 钩子内部不得再抛
            pass

    sys.excepthook = _hook
    _ = app


def configure_application(app: QApplication) -> None:
    """应用级字体与调色板。"""
    app.setApplicationName("LBA Tail Mover")
    app.setApplicationDisplayName("LBA Tail Mover")
    app.setOrganizationName("LBATailMover")
    app.setStyle("Fusion")

    manager = theme.theme()
    manager.prepare_fonts()

    font = QFont(manager.font_family())
    font.setPixelSize(13)
    app.setFont(font)

    app.setStyleSheet(theme.build_stylesheet(manager.palette(), manager.font_family()))


def _selftest() -> int:
    """打包完整性自检：逐个 import 关键模块（含唯一写路径），只读、不起界面。

    供用户在目标机器上验证 EXE 是否打包完整——尤其 ``app.win32.mover``
    这类只在「执行模式」运行时才按需加载的模块。用法：
    ``LBA-Tail-Mover.exe --selftest``。返回 0 = 全部就绪，任一缺失返回 1。
    绝不触碰磁盘、不创建任何窗口。
    """
    import importlib

    from app import display_version

    targets = [
        ("app.win32.mover", "唯一写路径（FSCTL_MOVE_FILE，执行模式才加载）"),
        ("app.win32.mft_reader", "$MFT 原始读（只读）"),
        ("app.core.mft", "MFT FILE 记录解析"),
        ("app.core.scheduler", "依赖调度 + relay 中转"),
        ("app.ui.main_window", "主窗口 / 界面装配"),
    ]
    failures = 0
    for name, desc in targets:
        try:
            importlib.import_module(name)
            print(f"[ ok ] {name} - {desc}")
        except Exception as exc:  # noqa: BLE001 - 自检要把任何缺失都报出来
            failures += 1
            print(f"[FAIL] {name} - {exc}")
    print(f"version: {display_version()} (FileVersion 1.0.0.0)")
    if failures:
        print(f"---- selftest FAILED: {failures} module(s) missing ----")
        return 1
    print("---- selftest OK: bundle complete ----")
    return 0


def main() -> int:
    """程序入口。"""
    if "--selftest" in sys.argv:
        return _selftest()
    if hasattr(Qt, "HighDpiScaleFactorRoundingPolicy"):
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )

    QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    configure_application(app)

    if not acquire_single_instance():
        QMessageBox.warning(
            None,
            "LBA Tail Mover",
            "已有一个 LBA Tail Mover 实例正在运行。\n"
            "同时运行多个实例可能造成磁盘操作冲突，本次启动已取消。",
        )
        return 0

    install_exception_hook(app)

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
