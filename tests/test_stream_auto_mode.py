"""1.0.0F 分块流式「自动模式」回归钉（UI 接线 + 执行前挑选 + 确认闸门）。

钉住：
1. 自动模式的开关/容量属于**待定配置**（要「应用配置」才生效，不让未确认的
   参数进执行路径）；
2. 已提交后 `stream_auto()` / `stream_capacity_bytes()` 才变化，并发 `streamAutoChanged`；
3. 自动模式下主按钮不需要预先勾选（文案变「开始自动流式迁移」），切回手动则
   要求勾选；
4. 执行时由 `densify.auto_stream_selection` 挑选（**不是**勾选集），挑选结果
   写进日志/摘要，然后**仍走危险确认框**，取消则不启动 worker；
5. 自动模式挑不出文件时给出明确提示，不静默返回。
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
        free_clusters=800_000, mft_zone_start=1000, mft_zone_end=2000,
        mft_start_lcn=1000, is_ntfs=True,
    )
    window._snapshot = BitmapSnapshot(
        bits=bytes(TOTAL // 8), total_clusters=TOTAL, bytes_per_cluster=BPC
    )
    window._mode = mw.MODE_PREVIEW
    # 引擎切到「分块流式」并立即应用（走既有确认流程）
    window._io_panel._engine_switch.set_current(1, emit=True)
    window._apply_pending()
    assert window._io_panel.engine() == mw.ENGINE_STREAM
    return window, mw


def _seed(window, checked: bool) -> None:
    rows = [
        FileEntry(
            path=f"R:\\{name}.bin", name=f"{name}.bin", size=clusters * BPC,
            extents=[Extent(vcn=0, lcn=1000 * (i + 1), clusters=clusters)],
            movable=True, checked=checked,
        )
        for i, (name, clusters) in enumerate((("big", 40_000), ("mid", 10_000), ("small", 500)))
    ]
    window._table.file_model.set_entries(rows)


def test_auto_mode_is_pending_until_applied() -> None:
    window, mw = _make_window()
    panel = window._io_panel
    assert panel.stream_auto() is False
    assert panel.stream_capacity_bytes() == 0

    panel._stream_auto_switch.set_current(1, emit=True)
    panel._stream_capacity_spin.setValue(50)
    _app().processEvents()
    assert panel.is_pending(), "自动模式/容量属于需确认的配置"
    assert window._has_pending
    assert panel.stream_auto() is False, "未应用前必须仍是已提交值"

    seen: list = []
    panel.streamAutoChanged.connect(lambda on: seen.append(on))
    window._apply_pending()
    assert panel.stream_auto() is True
    assert panel.stream_capacity_bytes() == 50 * (1024 ** 3)
    assert seen == [True]
    assert not window._has_pending


def test_auto_mode_button_text_and_enablement() -> None:
    window, mw = _make_window()
    _seed(window, checked=False)          # 一个都没勾
    window._sync_buttons()
    assert window._action_button.text() == "开始分块流式迁移"
    assert not window._action_button.isEnabled(), "手动模式要求勾选"

    window._io_panel._stream_auto_switch.set_current(1, emit=True)
    window._apply_pending()
    window._sync_buttons()
    assert window._action_button.text() == "开始自动流式迁移"
    assert window._action_button.isEnabled(), "自动模式由程序挑选，不要求勾选"

    window._io_panel._stream_auto_switch.set_current(0, emit=True)
    window._apply_pending()
    window._sync_buttons()
    assert window._action_button.text() == "开始分块流式迁移"
    assert not window._action_button.isEnabled()


def test_auto_mode_picks_by_selection_function() -> None:
    """执行时用的是自动挑选结果（全池、大文件优先），不是勾选集。"""
    window, mw = _make_window()
    _seed(window, checked=False)
    window._io_panel._stream_auto_switch.set_current(1, emit=True)
    window._io_panel._stream_capacity_spin.setValue(1)   # 1 GB ≈ 262144 簇
    window._apply_pending()

    picked: dict = {}
    real = mw.densify_mod.auto_stream_selection

    def spy(entries, **kw):
        res = real(entries, **kw)
        picked["count"] = len(res.selected)
        picked["names"] = [e.name for e in res.selected]
        picked["capacity"] = kw.get("capacity_bytes")
        picked["free"] = kw.get("free_bytes")
        return res

    mw.densify_mod.auto_stream_selection = spy
    started: list = []

    class FakeWorker:
        def __init__(self, entries, **kw):
            started.append((len(entries), kw.get("mode")))

        def __getattr__(self, name):
            class Sig:
                def connect(self, *a, **k):
                    pass

            return Sig()

        def start(self):
            started.append("start")

    mw.BlockStreamWorker = FakeWorker
    mw.dialogs.DangerConfirmDialog = _AcceptDialog

    window._start_stream_move()

    mw.densify_mod.auto_stream_selection = real
    assert picked["names"] == ["big.bin", "mid.bin", "small.bin"], "大文件优先"
    assert picked["capacity"] == 1 * (1024 ** 3)
    assert picked["free"] == 800_000 * BPC, "未设容量时以卷可用空间兜底"
    assert started and started[-1] == "start", "确认后必须真正启动 worker"


def test_auto_mode_cancelled_confirm_does_not_start() -> None:
    window, mw = _make_window()
    _seed(window, checked=False)
    window._io_panel._stream_auto_switch.set_current(1, emit=True)
    window._apply_pending()

    started: list = []

    class FakeWorker:
        def __init__(self, *a, **kw):
            started.append("init")

        def __getattr__(self, name):
            class Sig:
                def connect(self, *a, **k):
                    pass

            return Sig()

        def start(self):
            started.append("start")

    mw.BlockStreamWorker = FakeWorker
    mw.dialogs.DangerConfirmDialog = _RejectDialog

    window._start_stream_move()
    assert started == [], "确认框取消 = 不启动 worker"
    assert window._stream_worker is None


def test_auto_mode_no_candidate_notice() -> None:
    """挑不出文件时必须有提示，不静默返回。"""
    window, mw = _make_window()
    window._table.file_model.set_entries([])     # 没有任何条目
    window._io_panel._stream_auto_switch.set_current(1, emit=True)
    window._apply_pending()

    window._start_stream_move()
    text = window._notice.text()
    assert "自动模式" in text and "没有挑出" in text


class _AcceptDialog:
    def __init__(self, **kw):
        self._kw = kw

    def exec(self):
        return 1

    def on_error(self):
        return "continue"


class _RejectDialog:
    def __init__(self, **kw):
        self._kw = kw

    def exec(self):
        return 0

    def on_error(self):
        return "continue"


if __name__ == "__main__":
    from tests._harness import exit_with

    exit_with(globals())
