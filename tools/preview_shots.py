"""开发期预览脚本：用**真实**卷数据驱动主窗口并截图（不参与发布）。

输出 6 张（T05 视觉自查要求）：

1. ``light.png``     —— 浅色 · 可行态（目标区间 + 可行性 + 摘要 + IO）
2. ``dark.png``      —— 深色 · 同上
3. ``drag.png``      —— 目标区间拖选态（布局图上的选区 + 面板双向同步）
4. ``blockers.png``  —— 挡路者清单展开态
5. ``impossible.png``—— 不可行态（红色内联条 + 量化原因与建议）
6. ``dialog.png``    —— 危险确认框（目标区间/挡路者数量/预计写入/on_error）

全程只读，**绝不调用 FSCTL_MOVE_FILE**。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

if os.environ.get("LBA_PREVIEW_OFFSCREEN") == "1":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtGui import QFont  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core.models import human_bytes  # noqa: E402
from app.ui import theme  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402

OUT = BASE / "docs" / "screenshots"

#: 截图用的最小窗口尺寸（与 MainWindow.setMinimumSize 一致，便于检查"最小窗口不重叠"）
SHOT_W, SHOT_H = 1280, 950


def find_occupied_cluster(snapshot, near: int) -> int:
    """从 ``near`` 向后找第一个**被占用**的簇（用于构造真实的挡路场景）。"""
    bits = snapshot.bits
    total = snapshot.total_clusters
    step = 4096  # 按字节块跳跃，先用整字节判断再逐位定位
    lcn = max(0, min(int(near), total - 1))
    while lcn < total:
        index = lcn >> 3
        if index >= len(bits):
            break
        if bits[index]:
            for bit in range(8):
                probe = (index << 3) + bit
                if probe >= total:
                    break
                if bits[index] >> bit & 1:
                    if probe >= lcn:
                        return probe
        lcn += step
    # 回绕到卷首再找一次
    lcn = 0
    while lcn < total:
        index = lcn >> 3
        if index >= len(bits):
            break
        if bits[index]:
            for bit in range(8):
                probe = (index << 3) + bit
                if probe >= total:
                    break
                if bits[index] >> bit & 1:
                    return probe
        lcn += step
    return -1


def pump(rounds: int = 12) -> None:
    for _ in range(rounds):
        QCoreApplication.processEvents()


def wait_until(predicate, timeout: float = 180.0, label: str = "") -> bool:
    """事件循环等待条件成立（不 sleep，避免遮蔽竞态）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        pump(4)
        if predicate():
            return True
    print(f"!! 等待超时：{label}")
    return False


def main() -> int:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    manager = theme.theme()
    manager.prepare_fonts()
    font = QFont(manager.font_family())
    font.setPixelSize(13)
    app.setFont(font)
    app.setStyleSheet(theme.build_stylesheet(manager.palette(), manager.font_family()))

    OUT.mkdir(parents=True, exist_ok=True)
    window = MainWindow()
    window.resize(SHOT_W, SHOT_H)
    window.show()
    pump(40)

    if not wait_until(lambda: bool(window._volumes), 30, "枚举卷"):
        print("!! 没有枚举到任何卷")
        return 1

    # ------------------------------------------------------------------ #
    # 1) 挑一个「能读到卷结构 + 位图」的卷（真实判断，不硬编码盘符）
    # ------------------------------------------------------------------ #
    # 只挑「卷结构可读」的卷，并优先取**簇数最小**的那个：
    # 非提权下通常只有可移动介质可读，其中最小的卷才能让预览脚本在几十秒内跑完。
    candidates = sorted(
        (i for i in range(len(window._volumes)) if window._volumes[i].total_clusters > 0),
        key=lambda idx: window._volumes[idx].total_clusters,
    )
    if not candidates:
        print("!! 没有任何卷可读取结构信息（需要管理员权限）")
        return 1

    picked = -1
    for index in candidates:
        candidate = window._volumes[index]
        window._volume_combo.blockSignals(True)
        window._volume_combo.setCurrentIndex(index)
        window._volume_combo.blockSignals(False)
        window._on_volume_changed(index)
        wait_until(lambda: window._snapshot is not None, 90, f"{candidate.letter}: 位图")
        if window._snapshot is not None:
            picked = index
            break
    if picked < 0:
        print("!! 候选卷全部读取位图失败（需要管理员权限）")
        return 1

    info = window._volume
    print(
        f"pick {info.letter}: clusters={info.total_clusters:,} "
        f"bpc={info.bytes_per_cluster} fs={info.filesystem}"
    )
    print("media:", window._probe.media, "|", window._probe.describe())
    pump(20)

    # ------------------------------------------------------------------ #
    # 2) 真实 MFT 扫描（只读）—— 阈值降到 64 MB，保证小卷上也有命中
    # ------------------------------------------------------------------ #
    window._threshold_combo.setCurrentIndex(0)
    window._start_scan()
    wait_until(lambda: not window._busy, 240, "扫描 + 归属索引")
    entries = window._table.file_model.checked_entries()
    print(
        f"scan: {window._table.file_model.rowCount()} 行 / 选中 {len(entries)} 个"
        f" · 归属索引={'有' if window._ownership is not None else '无'}"
    )
    if not entries:
        print("!! 扫描没有命中文件，无法生成计划")
        return 1

    # ------------------------------------------------------------------ #
    # 3) 选一个有挡路者的目标区间（取卷中部，避开首尾保留区）
    # ------------------------------------------------------------------ #
    total = max(0, info.total_clusters)
    need = max(1, sum(e.allocated_clusters for e in entries))
    # 把窗口**压到刚好等于需求量**并压在一个真实占用簇上：
    # 这样窗口内只要有一个别人的文件挡路，就必须驱逐 → 稳定的琥珀态。
    pivot = find_occupied_cluster(window._snapshot, int(total * 0.42))
    if pivot < 0:
        pivot = int(total * 0.42)
    start = max(window._target_panel.reserve_head_clusters(), pivot - need // 2)
    end = min(total - window._target_panel.reserve_tail_clusters(), start + need)
    print(f"pivot={pivot:,} window=[{start:,}, {end:,}) size={end - start:,} need={need:,}")
    window._target_panel.set_window(start, end)
    window._refresh_target_visual()
    window._compute_feasibility_now()
    pump(20)
    report = window._feasibility
    if report is not None:
        print(
            f"feasibility: {report.verdict} · blockers={len(report.blockers)} "
            f"movable={len([b for b in report.blockers if b.movable])} "
            f"reasons={report.reasons[:2]}"
        )

    # ------------------------------------------------------------------ #
    # 4) 生成真实计划（区间模式）
    # ------------------------------------------------------------------ #
    window._generate_plan()
    wait_until(lambda: window._plan is not None and not window._plan_stale, 300, "规划")
    if window._plan is None:
        print("!! 规划没有返回结果")
        return 1
    print("plan:", window._plan.note, "| tasks", len(window._plan.tasks))

    def shoot(name: str) -> str:
        pump(14)
        pixmap = window.grab()
        target = OUT / name
        pixmap.save(str(target))
        print(f"saved {target}  {pixmap.width()} x {pixmap.height()}")
        return str(target)

    # ---------------- 1. 浅色 · 可行态 ---------------- #
    shoot("light.png")

    # ---------------- 2. 深色 ---------------- #
    window._toggle_theme()
    pump(24)
    shoot("dark.png")
    window._toggle_theme()
    pump(16)

    # ---------------- 3. 拖选态 ---------------- #
    disk = window._disk_map
    anchor = int(total * 0.55)
    disk._dragging = True
    disk._drag_anchor = anchor
    disk._drag_current = min(total, anchor + max(need, 4096))
    disk.update()
    window._target_panel.set_window(min(anchor, disk._drag_current),
                                    max(anchor, disk._drag_current))
    window._refresh_target_visual()
    window._compute_feasibility_now()
    shoot("drag.png")
    disk._dragging = False
    disk._drag_anchor = -1
    disk._drag_current = -1
    disk.update()

    # ---------------- 4. 挡路者展开态 ---------------- #
    window._compute_feasibility_now()
    window._target_panel.set_window(start, end)
    window._refresh_target_visual()
    window._compute_feasibility_now()
    report = window._feasibility
    if report is not None:
        print(
            f"blockers state: {report.verdict} · blockers={len(report.blockers)} "
            f"movable={len([b for b in report.blockers if b.movable])}"
        )
    section = window._target_panel._blockers_section
    section.set_expanded(True)
    pump(20)
    shoot("blockers.png")
    section.set_expanded(False)

    # ---------------- 5. 不可行态 ---------------- #
    tiny = max(64, int(need * 0.05))
    window._target_panel.set_window(int(total * 0.6), int(total * 0.6) + tiny)
    window._refresh_target_visual()
    window._compute_feasibility_now()
    pump(20)
    if window._feasibility is not None:
        print(
            f"impossible state: {window._feasibility.verdict} "
            f"shortfall={window._feasibility.shortfall_clusters:,}"
        )
    shoot("impossible.png")

    # ---------------- 6. 危险确认框 ---------------- #
    from app.ui import dialogs

    report_for_dialog = window._feasibility or report
    start2, end2 = window._target_panel.window()
    files = [item.entry for item in window._plan.all_items if item.entry.path]
    if not files:
        files = list(entries)
    dialog = dialogs.DangerConfirmDialog(
        volume_name=f"{info.letter}: ({info.filesystem or '未知'})",
        files=files,
        total_clusters=window._plan.total_move_clusters,
        bytes_per_cluster=info.bytes_per_cluster,
        target_range=(start2, end2),
        parent=window,
        blocker_count=len(
            [b for b in (report_for_dialog.blockers if report_for_dialog else []) if b.movable]
        ),
        evict_clusters=getattr(report_for_dialog, "evictable_clusters", 0),
        planned_tasks=len(window._plan.tasks),
    )
    dialog.show()
    pump(24)
    pixmap = dialog.grab()
    pixmap.save(str(OUT / "dialog.png"))
    print(f"saved {OUT / 'dialog.png'}  {pixmap.width()} x {pixmap.height()}")
    dialog.close()

    # ------------------------------------------------------------------ #
    # 7) 几何自检：控件是否越界 / 高度为 0 / 互相覆盖
    # ------------------------------------------------------------------ #
    print("\n--- geometry ---")
    checks = [
        "_disk_map", "_table", "_volume_combo", "_scan_panel", "_summary",
        "_target_panel", "_io_panel", "_recovery", "_mode_switch",
        "_action_button", "_progress", "_log", "_bottom_card",
    ]
    for name in checks:
        widget = getattr(window, name, None)
        if widget is None:
            continue
        rect = widget.geometry()
        flags = []
        if rect.width() <= 0 or rect.height() <= 0:
            flags.append("ZERO")
        bottom = rect.bottom()
        if widget.parentWidget() is not None:
            parent_rect = widget.parentWidget().rect()
            if bottom > parent_rect.bottom() + 1:
                flags.append("OVERFLOW-Y")
            if rect.right() > parent_rect.right() + 1:
                flags.append("OVERFLOW-X")
        hint = widget.sizeHint()
        print(
            f"{name:16s} geo={rect.getRect()} min={widget.minimumSize().toTuple()} "
            f"hint={hint.toTuple()} {' '.join(flags)}"
        )

    print("\n--- plan summary ---")
    stats = window._plan.stats
    print(
        f"place {stats.place_clusters:,} / evict {stats.evict_clusters:,} "
        f"/ relay {stats.relay_clusters:,} / total {stats.total_clusters:,} 簇"
        f" = {human_bytes(stats.total_clusters * info.bytes_per_cluster)}"
    )
    print("window:", start2, end2, "| journal:", window._journal_path or "(无)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
