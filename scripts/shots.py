"""生成界面截图，用于人工评审与视觉回归。

用途：改 UI 前后各跑一次，肉眼比对布局有没有塌。不是自动化断言，因此
不会进测试结论；但它比"打开窗口看一眼"可靠，因为尺寸、时机都是固定的。

用法::

    QT_QPA_PLATFORM=offscreen PYTHONPATH=src python scripts/shots.py
输出到 ``.tmp/shots/``。
"""

from __future__ import annotations

import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
# 刻意**不**强制 offscreen：Qt 的 offscreen 平台字体库不全，中文会渲染成
# 方块，截图就失去评审价值。无头机器上自己 export QT_QPA_PLATFORM=offscreen。


OUT = os.path.join(_ROOT, ".tmp", "shots")


def pump(app, predicate, timeout=25.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def main() -> int:
    from PySide6.QtWidgets import QApplication

    from lbamover.contract.commands import StartJob
    from lbamover.contract.enums import JobState
    from lbamover.contract.models import JobSpec
    from lbamover.ui.app import build_app

    os.makedirs(OUT, exist_ok=True)
    app, w = build_app(["--demo"])
    w.resize(1360, 940)
    w.show()
    pump(app, lambda: w._volume is not None)

    def shot(name: str) -> None:
        app.processEvents()
        path = os.path.join(OUT, f"{name}.png")
        w.grab().save(path)
        print("saved", path)

    shot("01-empty")

    w._on_scan()
    pump(app, lambda: w._plan is not None)
    shot("02-scanned")

    # 跑一次真实执行，截一张运行中与一张完成
    spec = JobSpec(volume=w._volume, strategy=w._strategy, params=w._params)
    w.service.submit(StartJob(spec))
    pump(app, lambda: w.core.state is JobState.RUNNING, timeout=10)
    time.sleep(0.3)  # 等进度卡的自动滚动校正完成
    app.processEvents()
    shot("03-running")
    pump(app, lambda: w.core.state in (JobState.DONE, JobState.FAILED), timeout=60)
    shot("04-done")

    # 手动选择文件：范围卡 + 选择对话框
    w.scope_panel.set_manual(True)
    w._selection = tuple(f.file_id for f in w._report.fragmented_files[:3])
    w._refresh_scope_summary()
    app.processEvents()
    shot("05-manual-scope")

    from lbamover.ui.widgets.file_select import FileSelectDialog

    dlg = FileSelectDialog(
        w._report.files, w._volume.geometry.block_size, w._p,
        preselect=w._selection, parent=w,
    )
    dlg.resize(980, 640)
    dlg.show()
    for _ in range(3):
        app.processEvents()
        time.sleep(0.05)
    dlg.grab().save(os.path.join(OUT, "06-file-picker.png"))
    print("saved", os.path.join(OUT, "06-file-picker.png"))
    dlg.close()
    w.scope_panel.set_manual(False)

    # 窄窗口：左栏应自动收起
    w.resize(900, 720)
    app.processEvents()
    shot("07-narrow")

    w.close()
    QApplication.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
