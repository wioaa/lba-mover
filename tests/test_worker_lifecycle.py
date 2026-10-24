"""B-3 回归钉：主窗口 closeEvent 的 QThread 取消清单必须**无遗漏**。

背景（QA 实锤 P2）：1.0.0C 引入的 ``_stream_worker`` 没进 closeEvent 的取消
清单，关窗时流式迁移线程不会被请求取消。本文件用 **AST 扫描**
``MainWindow`` 源码里所有 ``self._x_worker`` 形态的属性，断言：

1. 已知的 worker 槽位确实存在（防止扫描器自身失效）；
2. 运行时这些属性的值要么是 ``None`` 要么是 ``QThread``（类型契约）；
3. ``closeEvent`` 源码里逐个出现（清单完整性）。

离屏运行（不加载真实卷），不启动任何线程。
"""

from __future__ import annotations

import ast
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_KNOWN_WORKERS = {
    "_bitmap_worker",
    "_scan_worker",
    "_index_worker",
    "_plan_worker",
    "_move_worker",
    "_rollback_worker",
    "_stream_worker",
    "_feasibility_worker",
}


def _worker_attributes() -> set:
    """AST 扫描 ``MainWindow`` 源码，收集所有 ``self._x_worker`` 属性名。"""
    import app.ui.main_window as mw

    tree = ast.parse(inspect.getsource(mw.MainWindow))
    names: set = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and node.attr.endswith("_worker")
        ):
            names.add(node.attr)
    return names


def test_close_event_cancels_every_worker() -> None:
    """closeEvent 必须逐个覆盖 ``self._x_worker`` 属性（含 _stream_worker）。"""
    from PySide6.QtWidgets import QApplication

    _ = QApplication.instance() or QApplication([])
    import app.ui.main_window as mw

    attrs = _worker_attributes()
    # ① 扫描器自检：已知 worker 必须都被扫出来
    missing = _KNOWN_WORKERS - attrs
    assert not missing, f"AST 扫描漏掉已知 worker 属性：{sorted(missing)}"
    # ② closeEvent 源码必须逐个引用（取消清单完整性）
    close_source = inspect.getsource(mw.MainWindow.closeEvent)
    for name in sorted(attrs):
        assert name in close_source, (
            f"closeEvent 取消清单缺 {name}（B-3 回归钉：1.0.0C 曾漏 _stream_worker）"
        )


def test_worker_slots_hold_qthread_or_none() -> None:
    """运行时契约：``_x_worker`` 属性要么是 ``None``，要么是 QThread 实例。"""
    from PySide6.QtCore import QThread
    from PySide6.QtWidgets import QApplication

    _ = QApplication.instance() or QApplication([])
    import app.ui.main_window as mw

    window = mw.MainWindow()
    for name in sorted(_worker_attributes()):
        value = getattr(window, name, "missing")
        assert value != "missing", f"MainWindow 缺少槽位 {name}"
        assert value is None or isinstance(value, QThread), (
            f"{name} 应为 None 或 QThread，实际 {type(value).__name__}"
        )


def test_cancel_current_covers_every_worker() -> None:
    """D-002 回归钉：``_cancel_current``（Esc 取消）的取消清单必须与 closeEvent
    一样覆盖**所有** ``self._x_worker`` 属性，否则按 Esc 无法中止某个在跑的线程
    （1.0.0F 实锤：曾漏 ``_stream_worker`` → 流式迁移按 Esc 无反应）。"""
    import app.ui.main_window as mw

    attrs = _worker_attributes()
    cancel_source = inspect.getsource(mw.MainWindow._cancel_current)
    for name in sorted(attrs):
        assert name in cancel_source, (
            f"_cancel_current 取消清单缺 {name}（Esc 将无法中止该线程）"
        )


if __name__ == "__main__":
    from tests._harness import exit_with

    exit_with(globals())
