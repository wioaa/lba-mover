"""规划策略回归钉（1.0.0F 修复：切换策略无效果 / 计划生成被区间条件误挡）。

用户实机反馈：「切换规划策略不产生实际效果，各种规划基本都是红色、不搬任何簇」。
根因两条：
1. 策略切换进了「应用配置」暂存（必须点应用才生效）——策略只决定「怎么算计划」，
   不是写盘参数，切换后计划必然重新生成，不该让用户再点一次；
2. `_sync_buttons` 一律要求「目标区间非空」，而「自动填满末端」策略只用卷尾保留区、
   根本不需要区间 → 切到该策略后主按钮一直禁用，计划都生成不了。

本文件钉住：策略立即生效、其余配置仍需确认、按钮的区间要求随策略变化。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core.models import (  # noqa: E402
    BitmapSnapshot,
    Extent,
    FileEntry,
    VolumeInfo,
)

TOTAL = 1_000_000
BPC = 4096


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _make_window():
    _ = _app()
    import app.ui.main_window as mw

    mw.MainWindow._load_volumes = lambda self: None
    window = mw.MainWindow()
    window._volume = VolumeInfo(
        letter="R", root="R:\\", label="T", filesystem="NTFS",
        total_bytes=TOTAL * BPC, bytes_per_cluster=BPC, total_clusters=TOTAL,
        mft_zone_start=1000, mft_zone_end=2000, mft_start_lcn=1000, is_ntfs=True,
    )
    window._snapshot = BitmapSnapshot(
        bits=bytes(TOTAL // 8), total_clusters=TOTAL, bytes_per_cluster=BPC
    )
    window._mode = mw.MODE_PREVIEW
    return window, mw


def _seed_selection(window):
    entry = FileEntry(
        path="R:\\a.bin", name="a.bin", size=64 * BPC,
        extents=[Extent(vcn=0, lcn=100, clusters=64)], movable=True, checked=True,
    )
    window._table.file_model.set_entries([entry])
    return entry


def test_strategy_switch_takes_effect_immediately() -> None:
    """切策略必须立刻改变已提交的 strategy，且不留 pending。"""
    window, mw = _make_window()
    panel = window._target_panel
    assert panel.strategy() == "range"

    seen: list = []
    panel.strategyChanged.connect(lambda mode: seen.append(mode))

    panel._strategy_combo.setCurrentIndex(1)      # 「自动填满末端」
    _app().processEvents()
    assert panel.strategy() == "auto_fill", "策略切换后已提交值必须立刻变化"
    assert seen == ["auto_fill"], "必须发出 strategyChanged（让计划失效并刷新按钮态）"
    assert not panel.is_pending(), "策略是直通项，不该进入「应用配置」暂存"
    assert not window._has_pending


def test_other_config_still_requires_apply() -> None:
    """区间 / 保留区仍走确认流程（策略直通不等于全部直通）。"""
    window, mw = _make_window()
    panel = window._target_panel
    pending: list = []
    panel.pendingChanged.connect(lambda dirty, count: pending.append((dirty, count)))

    panel._range_input._start_box.setValue(12345)   # 改区间起点
    _app().processEvents()
    assert panel.is_pending(), "区间改动仍应进入暂存"
    assert window._has_pending
    assert pending and pending[-1][0] is True


def test_auto_fill_strategy_does_not_require_window() -> None:
    """「自动填满末端」不需要目标区间：没有区间时主按钮也必须可用。"""
    window, mw = _make_window()
    _seed_selection(window)
    panel = window._target_panel

    # 区间模式、起止都为 0 → has_window() 为 False
    panel._range_input._start_box.setValue(0)
    panel._range_input._end_box.setValue(0)
    panel._strategy_combo.setCurrentIndex(1)      # 自动填满末端
    _app().processEvents()
    assert not panel.has_window()
    window._sync_buttons()
    assert window._action_button.isEnabled(), "自动填满末端不该被区间条件挡住"


def test_window_strategies_still_require_window() -> None:
    """区间装箱 / 预密实化 / 分区段仍必须要有区间。"""
    window, mw = _make_window()
    _seed_selection(window)
    panel = window._target_panel
    panel._range_input._start_box.setValue(0)
    panel._range_input._end_box.setValue(0)
    _app().processEvents()

    for index, mode in enumerate(("range", "compact", "zone")):
        panel._strategy_combo.setCurrentIndex(index if mode == "range" else (2 if mode == "compact" else 3))
        _app().processEvents()
        assert panel.strategy() == mode
        window._sync_buttons()
        assert not window._action_button.isEnabled(), f"{mode} 策略必须要求区间非空"


def test_switching_to_window_strategy_restores_requirement() -> None:
    """从「自动填满末端」切回区间类策略后，区间要求必须重新生效。"""
    window, mw = _make_window()
    _seed_selection(window)
    panel = window._target_panel
    panel._strategy_combo.setCurrentIndex(1)      # 自动填满末端（无区间也可用）
    _app().processEvents()
    window._sync_buttons()
    assert window._action_button.isEnabled()

    panel._strategy_combo.setCurrentIndex(0)      # 区间装箱（需区间）
    _app().processEvents()
    window._sync_buttons()
    assert not window._action_button.isEnabled()


if __name__ == "__main__":
    from tests._harness import exit_with

    exit_with(globals())
