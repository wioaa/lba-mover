"""手动选择文件链路的临时验证：扫描 → 选文件 → 重新预演 → 只搬选中的。

    python scripts/check_scope.py
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from lbamover.contract.enums import JobState, Strategy  # noqa: E402
from lbamover.ui import main_window as mw  # noqa: E402
from lbamover.ui.widgets.file_select import FileTableModel  # noqa: E402


def pump(app, cond, timeout=30.0, label=""):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    raise TimeoutError(f"等待超时：{label}")


def main() -> int:
    app = QApplication([])
    w = mw.MainWindow()
    w.load_demo()
    pump(app, lambda: w._volume is not None, label="演示卷")

    w.scope_panel.set_manual(True)
    w._on_scan()
    pump(app, lambda: w._report is not None, label="扫描")
    total = w._report.file_count
    print(f"扫描：{total} 个文件，碎片文件 {len(w._report.fragmented_files)}")

    model = FileTableModel(w._report.files, w._volume.geometry.block_size)
    print(f"模型：默认只看碎片 {model.visible_count()} 行 / 共 {model.total_count()}")
    model.set_filter(only_fragmented=False)
    print(f"模型：全部 {model.visible_count()} 行")
    model.set_filter(needle="nested", only_fragmented=False)
    print(f"模型：过滤 'nested' → {model.visible_count()} 行")

    # 选前 3 个碎片文件
    model.set_filter(needle="", only_fragmented=True)
    ids = []
    from PySide6.QtCore import Qt

    for row in range(min(3, model.visible_count())):
        frag = model.frag_at(row)
        ids.append(frag.file_id)
        model.setData(
            model.index(row, 0),
            Qt.CheckState.Checked,
            Qt.ItemDataRole.CheckStateRole,
        )
    print(f"勾选 {model.selection_count()} 个，共 {model.selection_blocks()} 块")

    w._selection = tuple(ids)
    w._refresh_scope_summary()
    print("范围摘要：", w.scope_panel.summary.text())
    w.service.submit(mw.Replan(Strategy.COALESCE, w._scope_file_ids()))
    pump(app, lambda: w._plan is not None and w._plan.op_count >= 0, label="预演")
    time.sleep(0.3)
    app.processEvents()
    print(f"选中后预演：{w._plan.op_count} 个文件 / {w._plan.total_blocks} 块")
    assert w._plan.op_count <= len(ids), "预演里出现了没选中的文件"
    print("开始按钮：", w.control.btn_start.isEnabled())

    # 整卷口径对照
    w.scope_panel.set_manual(False)
    pump(app, lambda: w._plan is not None, label="整卷预演")
    time.sleep(0.3)
    app.processEvents()
    print(f"整卷预演：{w._plan.op_count} 个文件 / {w._plan.total_blocks} 块")
    w.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
