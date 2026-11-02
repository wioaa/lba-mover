"""UI 层契约测试。

不测"看起来对不对"，只测三件可断言的事：

1. **事件 → 信号的映射完整**（漏一种事件 = 某块界面永远不更新）；
2. **按钮可用性只由状态机决定**（这是旧版最常出 bug 的地方）；
3. **控件能吃下契约层数据并画出来**（自绘控件最容易在空数据/极端数据上崩）。
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6", reason="未安装 PySide6，跳过 GUI 测试")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QSignalSpy, QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from lbamover.contract.enums import JobState, LogLevel, Strategy, VolumeKind  # noqa: E402
from lbamover.contract.events import (  # noqa: E402
    FragMapEvent,
    JobDoneEvent,
    LogEvent,
    PlanEvent,
    ProgressEvent,
    StateEvent,
)
from lbamover.contract.models import (  # noqa: E402
    Extent,
    FileFrag,
    Geometry,
    JobParams,
    Plan,
    Progress,
    VolumeInfo,
)
from lbamover.engine.frag import analyze, free_extents_from_files  # noqa: E402
from lbamover.ui.bridge import EngineBridge  # noqa: E402
from lbamover.ui.theme import DARK, apply_theme  # noqa: E402
from lbamover.ui.widgets.cards import Badge  # noqa: E402
from lbamover.ui.widgets.control_bar import ControlBar  # noqa: E402
from lbamover.ui.widgets.frag_map import FragMap  # noqa: E402
from lbamover.ui.widgets.log_view import LogView  # noqa: E402
from lbamover.ui.widgets.params_panel import ParamsPanel  # noqa: E402
from lbamover.ui.widgets.progress_card import ProgressCard  # noqa: E402
from lbamover.ui.widgets.strategy_panel import StrategyPanel  # noqa: E402
from lbamover.ui.widgets.volume_picker import VolumePicker  # noqa: E402


def spy_count(spy) -> int:
    """PySide6 的 QSignalSpy 没实现 ``__len__``，统一走 count()。"""
    return int(spy.count())


def spy_at(spy, index: int):
    """取第 index 次触发的参数列表。"""
    return spy.at(index)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    apply_theme(app, DARK)
    yield app


def sample_report() -> object:
    geo = Geometry(4096, 512)
    files = [
        FileFrag("a", "C\\a.bin", (Extent(0, 8), Extent(40, 8))),
        FileFrag("b", "C\\b.bin", (Extent(100, 16),)),
    ]
    return analyze(files, free_extents_from_files(geo, files), geo, bucket_count=48)


def sample_volume() -> VolumeInfo:
    return VolumeInfo(
        device_path="memory://demo",
        mount_point="D:\\",
        label="测试卷",
        filesystem="NTFS",
        kind=VolumeKind.FIXED,
        geometry=Geometry(4096, 1024),
        free_bytes=1024 * 2048,
        writable=True,
    )


class TestBridge:
    def test_each_event_kind_maps_to_signal(self, qapp):
        bridge = EngineBridge()
        spies = {
            "state": QSignalSpy(bridge.stateChanged),
            "progress": QSignalSpy(bridge.progressChanged),
            "log": QSignalSpy(bridge.logRecord),
            "frag": QSignalSpy(bridge.fragReportReady),
            "plan": QSignalSpy(bridge.planReady),
            "done": QSignalSpy(bridge.jobFinished),
        }
        report = sample_report()
        plan = Plan(job_id="j", strategy=Strategy.COALESCE)
        bridge.emit(StateEvent(JobState.RUNNING, "go", seq=1))
        bridge.emit(ProgressEvent(Progress(done_blocks=1, total_blocks=10), seq=2))
        bridge.emit(LogEvent(LogLevel.INFO, "hello", seq=3))
        bridge.emit(FragMapEvent(report, seq=4))
        bridge.emit(PlanEvent(plan, seq=5))
        bridge.emit(JobDoneEvent(JobState.DONE, moved_ops=2, seq=6))

        assert spy_count(spies["state"]) == 1 and spy_at(spies["state"], 0)[0] is JobState.RUNNING
        assert spy_count(spies["progress"]) == 1
        assert spy_at(spies["progress"], 0)[0].done_blocks == 1
        assert spy_count(spies["log"]) == 1 and spy_at(spies["log"], 0)[1] == "hello"
        assert spy_count(spies["frag"]) == 1 and spy_at(spies["frag"], 0)[0] is report
        assert spy_count(spies["plan"]) == 1 and spy_at(spies["plan"], 0)[0] is plan
        assert spy_count(spies["done"]) == 1 and spy_at(spies["done"], 0)[0].moved_ops == 2
        assert bridge.count == 6
        # JobDone 不走 stateChanged，状态由 StateEvent 单独维护
        assert bridge.last_state is JobState.RUNNING

    def test_unknown_event_is_ignored(self, qapp):
        bridge = EngineBridge()
        spy = QSignalSpy(bridge.stateChanged)

        class Weird:
            kind = None

        bridge.emit(Weird())  # type: ignore[arg-type]
        assert spy_count(spy) == 0
        assert bridge.count == 1


class TestControlBar:
    def test_restricted_scan_blocks_start(self, qapp):
        """受限扫描（未提权）只能预览：开始按钮必须锁住，并说明原因。"""
        bar = ControlBar(DARK)
        bar.set_target_available(True)
        bar.set_restricted(True)
        bar.sync_state(JobState.IDLE)
        assert bar.btn_scan.isEnabled(), "扫描本身还要能重试"
        assert not bar.btn_start.isEnabled()
        assert "提权" in bar.btn_start.toolTip()
        assert "受限模式" in bar.summary.text()

    def test_clearing_restriction_restores_start(self, qapp):
        bar = ControlBar(DARK)
        bar.set_target_available(True)
        bar.set_restricted(True)
        bar.sync_state(JobState.IDLE)
        bar.set_restricted(False)
        assert bar.btn_start.isEnabled()
        assert bar.btn_start.toolTip() == ""

    def test_idle_enables_scan_and_start(self, qapp):
        bar = ControlBar(DARK)
        bar.set_target_available(True)
        bar.sync_state(JobState.IDLE)
        assert bar.btn_scan.isEnabled()
        assert bar.btn_start.isEnabled()
        assert not bar.btn_pause.isEnabled()
        assert not bar.btn_stop.isEnabled()

    def test_no_target_disables_actions(self, qapp):
        bar = ControlBar(DARK)
        bar.set_target_available(False)
        bar.sync_state(JobState.IDLE)
        assert not bar.btn_scan.isEnabled()
        assert not bar.btn_start.isEnabled()

    def test_running_state(self, qapp):
        bar = ControlBar(DARK)
        bar.set_target_available(True)
        bar.sync_state(JobState.RUNNING)
        assert not bar.btn_scan.isEnabled()
        assert not bar.btn_start.isEnabled()
        assert bar.btn_pause.isEnabled()
        assert bar.btn_stop.isEnabled()
        assert bar.btn_pause.text() == "暂停"

    def test_paused_shows_resume(self, qapp):
        bar = ControlBar(DARK)
        bar.set_target_available(True)
        bar.sync_state(JobState.PAUSED)
        assert bar.btn_pause.text() == "继续"
        assert bar.btn_stop.isEnabled()

    def test_busy_disables_scan(self, qapp):
        bar = ControlBar(DARK)
        bar.set_target_available(True)
        bar.sync_state(JobState.SCANNING)
        assert not bar.btn_scan.isEnabled()
        assert "扫描中" in bar.btn_scan.text()

    def test_stop_available_while_planning(self, qapp):
        """规划阶段「停止」必须可用。

        真实卷上规划要几十秒，旧版这个按钮在这段时间里是灰的 —— 用户点了
        没反应，而界面上又没有任何"不能停"的说明，只能干等。按钮可用性只在
        ``sync_state`` 一处决定，所以这一条只能靠状态断言，不能靠点。
        """
        bar = ControlBar(DARK)
        bar.set_target_available(True)
        for state in (JobState.SCANNING, JobState.PLANNING):
            bar.sync_state(state)
            assert bar.btn_stop.isEnabled(), f"{state.value} 阶段必须能停"
            assert bar.btn_stop.text() == "停止"

    def test_stopping_disables_stop_and_says_so(self, qapp):
        bar = ControlBar(DARK)
        bar.set_target_available(True)
        bar.sync_state(JobState.STOPPING)
        assert not bar.btn_stop.isEnabled(), "已经在停了，再点没有意义"
        assert bar.btn_stop.text() == "停止中…"
        assert "当前步骤" in bar.summary.text(), "规划阶段没有 chunk，不能说'等 chunk 写完'"

    def test_terminal_state_reenables(self, qapp):
        bar = ControlBar(DARK)
        bar.set_target_available(True)
        for state in (JobState.DONE, JobState.FAILED, JobState.CANCELLED):
            bar.sync_state(state)
            assert bar.btn_scan.isEnabled(), state
            assert bar.btn_start.isEnabled(), state

    def test_pause_button_emits_correct_signal(self, qapp):
        bar = ControlBar(DARK)
        bar.set_target_available(True)
        pause = QSignalSpy(bar.pauseRequested)
        resume = QSignalSpy(bar.resumeRequested)
        bar.sync_state(JobState.RUNNING)
        bar._on_pause()
        assert spy_count(pause) == 1 and spy_count(resume) == 0
        bar.sync_state(JobState.PAUSED)
        bar._on_pause()
        assert spy_count(resume) == 1


class TestBadge:
    """徽标不只是状态显示，也可能是一个动作入口（例如"点此提权"）。"""

    def test_not_clickable_by_default(self, qapp):
        b = Badge("权限：管理员")
        spy = QSignalSpy(b.clicked)
        b.resize(120, 22)
        QTest.mouseClick(b, Qt.MouseButton.LeftButton)
        assert spy_count(spy) == 0

    def test_clickable_emits_once_per_click(self, qapp):
        b = Badge("权限：普通用户 · 点此提权")
        b.set_clickable(True)
        spy = QSignalSpy(b.clicked)
        b.resize(160, 22)
        QTest.mouseClick(b, Qt.MouseButton.LeftButton)
        assert spy_count(spy) == 1

    def test_cursor_follows_clickable(self, qapp):
        b = Badge("x")
        assert b.cursor().shape() is Qt.CursorShape.ArrowCursor
        b.set_clickable(True)
        assert b.cursor().shape() is Qt.CursorShape.PointingHandCursor
        b.set_clickable(False)
        assert b.cursor().shape() is Qt.CursorShape.ArrowCursor


class TestFragMap:
    def test_renders_without_report(self, qapp):
        w = FragMap(DARK)
        w.resize(400, 160)
        assert not w.grab().isNull()

    def test_renders_with_report(self, qapp):
        w = FragMap(DARK)
        w.resize(480, 200)
        w.set_report(sample_report())
        assert not w.grab().isNull()

    def test_hover_maps_x_to_bucket(self, qapp):
        w = FragMap(DARK)
        w.resize(480, 200)
        w.set_report(sample_report())
        assert w._bucket_at(w.width() // 2) >= 0
        assert w._bucket_at(-50) == -1

    def test_extreme_bucket_counts(self, qapp):
        geo = Geometry(4096, 10_000)
        report = analyze([], [Extent(0, 10_000)], geo, bucket_count=512)
        w = FragMap(DARK)
        w.resize(300, 150)
        w.set_report(report)
        assert not w.grab().isNull()


class TestParamsPanel:
    def test_defaults_are_valid(self, qapp):
        p = ParamsPanel(DARK)
        params = p.current()
        assert isinstance(params, JobParams)
        assert params.block_size == 4096

    def test_budget_label_reflects_inputs(self, qapp):
        p = ParamsPanel(DARK)
        p.set_block_size(8192)
        p.chunk.setValue(16)
        p.depth.setValue(2)
        p.concurrency.setValue(1)
        text = p.budget.text()
        assert "256.0 KiB" in text or "256 KiB" in text or "262144" not in text
        assert p.current().buffer_budget_bytes == 8192 * 16 * 2

    def test_emits_on_change(self, qapp):
        p = ParamsPanel(DARK)
        spy = QSignalSpy(p.paramsChanged)
        p.chunk.setValue(64)
        assert spy_count(spy) >= 1
        assert spy_at(spy, spy_count(spy) - 1)[0].chunk_blocks == 64

    def test_apply_roundtrip(self, qapp):
        p = ParamsPanel(DARK)
        target = JobParams(chunk_blocks=32, concurrency=3, rate_limit_mbps=50.0, verify=False)
        p.apply(target)
        got = p.current()
        assert got.chunk_blocks == 32 and got.concurrency == 3
        assert got.rate_limit_mbps == 50.0 and not got.verify

    def test_advanced_rows_hidden_by_default(self, qapp):
        p = ParamsPanel(DARK)
        assert p.depth.isHidden() and p.retries.isHidden() and p.reserve.isHidden()
        assert not p.chunk.isHidden()

    def test_advanced_toggle_shows_and_hides(self, qapp):
        p = ParamsPanel(DARK)
        p.advanced_toggle.click()
        qapp.processEvents()
        assert not p.depth.isHidden()
        assert "▾" in p.advanced_toggle.text()
        # 隐藏的参数仍在 JobParams 里生效，不会因为折叠而丢值
        p.depth.setValue(6)
        assert p.current().queue_depth == 6
        p.advanced_toggle.click()
        qapp.processEvents()
        assert p.depth.isHidden()
        assert "▸" in p.advanced_toggle.text()

    def test_lock_toggles_structural_params(self, qapp):
        p = ParamsPanel(DARK)
        p.set_locked(True)
        assert not p.chunk.isEnabled()
        p.set_locked(False)
        assert p.chunk.isEnabled()


class TestStrategyPanel:
    def test_default_is_coalesce(self, qapp):
        s = StrategyPanel(DARK)
        assert s.strategy() is Strategy.COALESCE

    def test_selection_emits(self, qapp):
        s = StrategyPanel(DARK)
        spy = QSignalSpy(s.strategyChanged)
        s.set_strategy(Strategy.RELOCATE_TAIL)
        assert s.strategy() is Strategy.RELOCATE_TAIL
        # 真实点击第三个按钮（互斥组会同时更新选中态并发出 idClicked）
        s._group.button(2).click()
        assert spy_count(spy) == 1
        assert spy_at(spy, 0)[0] is Strategy.COMPACT
        assert s.strategy() is Strategy.COMPACT


class TestLogView:
    def test_append_and_banner(self, qapp):
        v = LogView(DARK)
        v.append(LogLevel.INFO, "普通消息")
        assert v.banner.isHidden()
        v.append(LogLevel.ERROR, "出错了")
        assert not v.banner.isHidden()
        assert "出错了" in v.banner.text()
        v.clear()
        assert v.banner.isHidden()

    def test_level_filter_suppresses_debug(self, qapp):
        v = LogView(DARK)
        v.filter.setCurrentIndex(3)  # 仅错误
        v.append(LogLevel.INFO, "被过滤")
        assert "被过滤" not in v.text.toPlainText()
        v.append(LogLevel.ERROR, "保留")
        assert "保留" in v.text.toPlainText()

    def test_html_is_escaped(self, qapp):
        v = LogView(DARK)
        v.append(LogLevel.INFO, "<script>alert(1)</script>")
        raw = v._lines[-1]
        assert "&lt;script&gt;" in raw, "日志消息必须做 HTML 转义"
        assert "<script>" not in raw


class TestProgressCard:
    def test_update_and_reset(self, qapp):
        c = ProgressCard(DARK)
        c.update_progress(
            Progress(
                done_blocks=512,
                total_blocks=1024,
                done_ops=1,
                total_ops=2,
                bytes_done=512 * 4096,
                bytes_total=1024 * 4096,
                rate_bps=8 * 1024 * 1024,
                eta_s=12.0,
                retried_chunks=2,
                failed_chunks=1,
                current_path="C\\very\\long\\path\\file.bin",
            )
        )
        assert c.percent_label.text() == "50.0%"
        assert c.bar.value() == 500
        assert "MiB/s" in c.t_rate.value_label.text()
        c.reset()
        assert c.percent_label.text() == "0.0%"

    def test_state_chip_colors(self, qapp):
        c = ProgressCard(DARK)
        for state in JobState:
            c.set_state(state)
            assert c.state_chip.text()


class TestVolumePicker:
    def test_empty_state_explains_itself(self, qapp):
        p = VolumePicker(DARK)
        p.set_volumes([])
        assert not p._empty.isHidden()
        assert "没有找到" in p._empty.text()

    def test_selection_emits(self, qapp):
        p = VolumePicker(DARK)
        spy = QSignalSpy(p.volumeSelected)
        p.set_volumes([sample_volume()])
        assert spy_count(spy) >= 1
        assert p.current().mount_point == "D:\\"
        assert p.count() == 1

    def test_select_by_letter(self, qapp):
        p = VolumePicker(DARK)
        p.set_volumes([sample_volume()])
        assert p.select_letter("D")
        assert not p.select_letter("Z")


class TestMainWindowSmoke:
    def test_refresh_keeps_engine_target(self, qapp, monkeypatch):
        """刷新卷列表不能把用户选的目标换回第一块盘。

        这是"界面显示 R:、实际去动 C:"的成因：延迟加载重建列表时若以
        picker 的高亮为依据，程序化切换目标（没有经过点击）就会丢选择。
        """
        from lbamover.ui import main_window as mw

        vols = []
        for letter in ("C:", "R:"):
            vol = sample_volume()
            object.__setattr__(vol, "mount_point", letter + "\\")
            object.__setattr__(vol, "device_path", "\\\\.\\" + letter[:-1])
            vols.append(vol)
        monkeypatch.setattr(mw, "enumerate_volumes", lambda: list(vols))

        w = mw.MainWindow(palette=DARK)
        try:
            w._load_volumes()
            w.picker.select_letter("R:")
            assert w._volume is not None and w._volume.mount_point.startswith("R:")
            w._load_volumes()  # 模拟延迟加载 / 用户点「刷新」
            assert w._volume is not None
            assert w._volume.mount_point.startswith("R:"), "刷新后目标被换成了别的盘"
            assert w.picker.current() is not None
            assert w.picker.current().mount_point.startswith("R:")
        finally:
            w.deleteLater()

    def test_window_constructs_in_demo_mode(self, qapp):
        from lbamover.ui.main_window import DEMO_DEVICE_PATH, MainWindow

        w = MainWindow(palette=DARK)
        try:
            w.load_demo()
            qapp.processEvents()
            assert w.picker.count() >= 1
            assert w._demo_device is not None
            assert w._volume is not None
            assert w._volume.device_path == DEMO_DEVICE_PATH
            # 主区域控件都在
            assert w.frag_map is not None and w.progress is not None
            assert w.log_view is not None and w.control is not None
            assert not w.grab().isNull()
        finally:
            w.close()

    def test_narrow_window_autohides_left_panel(self, qapp):
        from lbamover.ui.main_window import MainWindow

        w = MainWindow(palette=DARK)
        try:
            w.resize(1400, 800)
            qapp.processEvents()
            assert w.left_panel.isVisible() or not w.isVisible()
            w.resize(900, 700)
            qapp.processEvents()
            assert not w.left_panel.isVisible()
        finally:
            w.close()


class _StubBox:
    """替换 ``QMessageBox`` 的静态方法，用来断言"用户点了是/否"的分支。

    **不要 monkeypatch Qt 类本身**：shiboken 类型基本不让改属性，而且真正要
    测的是"我们把返回值怎么判"，不是 Qt 怎么弹框。这里保留 ``StandardButton``
    让 ``confirmed()`` 走真实代码路径。
    """

    StandardButton = QMessageBox.StandardButton
    Icon = QMessageBox.Icon

    answer: object = 0
    asked: list[str] = []

    @classmethod
    def question(cls, *args, **kwargs):
        cls.asked.append(str(args[1]) if len(args) > 1 else "")
        return cls.answer

    @classmethod
    def information(cls, *args, **kwargs):
        cls.asked.append(str(args[1]) if len(args) > 1 else "")
        return cls.StandardButton.Ok


class TestConfirmDialogs:
    """「点了是却什么都没发生」的回归测试。

    2026-11 的真实故障：``QMessageBox.question()`` 在 PySide6 里返回**普通
    int**（16384），而代码写的是 ``ret is QMessageBox.StandardButton.Yes``，
    身份比较恒为 False。后果是两个功能同时"坏掉"且不报错：

    * 提权确认永远被当成取消 → 点提权没反应；
    * 写盘确认永远被当成取消 → 点开始整理没反应。

    当时的冒烟脚本刻意"绕过 _on_start 的确认闸门"，所以这条链路没有任何
    用例覆盖。这里把闸门本身测起来。
    """

    def test_confirmed_accepts_pyside_int_return(self):
        from lbamover.ui.main_window import confirmed

        # 实测的 question() 返回值就是这个整数
        assert confirmed(int(QMessageBox.StandardButton.Yes)) is True
        assert confirmed(int(QMessageBox.StandardButton.No)) is False
        # 枚举成员同样要认（不同 PySide 版本返回类型不一致）
        assert confirmed(QMessageBox.StandardButton.Yes) is True
        assert confirmed(QMessageBox.StandardButton.No) is False
        # 意外输入不能当成"是"
        assert confirmed(None) is False
        assert confirmed("yes") is False

    def _window(self, monkeypatch, answer):
        from lbamover.ui import main_window as mw

        _StubBox.answer = answer
        _StubBox.asked = []
        monkeypatch.setattr(mw, "QMessageBox", _StubBox)
        w = mw.MainWindow(palette=DARK)
        w.load_demo()
        return w

    def test_confirm_write_returns_true_on_yes(self, qapp, monkeypatch):
        w = self._window(monkeypatch, int(QMessageBox.StandardButton.Yes))
        try:
            assert w._volume is not None
            assert w._confirm_write(w._volume) is True
        finally:
            w.close()

    def test_confirm_write_returns_false_on_no(self, qapp, monkeypatch):
        w = self._window(monkeypatch, int(QMessageBox.StandardButton.No))
        try:
            assert w._confirm_write(w._volume) is False
        finally:
            w.close()

    def test_start_submits_job_when_confirmed(self, qapp, monkeypatch):
        from lbamover.contract.commands import StartJob

        w = self._window(monkeypatch, int(QMessageBox.StandardButton.Yes))
        sent: list = []
        monkeypatch.setattr(w.service, "submit", sent.append)
        try:
            from lbamover.contract.enums import Strategy

            w._strategy = Strategy.COALESCE
            w._on_start()
            assert sent, "确认后没有提交 StartJob —— 「开始整理」等于没反应"
            assert isinstance(sent[0], StartJob)
        finally:
            w.close()

    def test_start_does_nothing_when_cancelled(self, qapp, monkeypatch):
        w = self._window(monkeypatch, int(QMessageBox.StandardButton.No))
        sent: list = []
        monkeypatch.setattr(w.service, "submit", sent.append)
        try:
            w._on_start()
            assert not sent, "用户点了否，却仍然提交了任务"
        finally:
            w.close()

    def test_elevate_reports_reason_when_launch_fails(self, qapp, monkeypatch):
        from lbamover.platform import privilege

        w = self._window(monkeypatch, int(QMessageBox.StandardButton.Yes))
        monkeypatch.setattr(
            privilege, "relaunch_as_admin", lambda argv=None: (False, "UAC 授权被取消")
        )
        lines: list[str] = []
        monkeypatch.setattr(
            w.log_view, "append", lambda level, msg, *a, **k: lines.append(str(msg))
        )
        try:
            w._elevate()
            assert any("UAC 授权被取消" in line for line in lines), (
                "提权失败必须把原因写进日志，否则用户只知道「没反应」"
            )
        finally:
            w.close()

    def test_elevate_closes_window_when_launch_succeeds(self, qapp, monkeypatch):
        from lbamover.platform import privilege

        w = self._window(monkeypatch, int(QMessageBox.StandardButton.Yes))
        calls: list = []
        monkeypatch.setattr(
            privilege,
            "relaunch_as_admin",
            lambda argv=None: (calls.append(argv) or (True, "")),
        )
        closed: list[bool] = []
        monkeypatch.setattr(w, "close", lambda: closed.append(True))
        try:
            w._elevate()
            assert calls, "确认后没有调用 relaunch_as_admin"
            assert closed, "提权已发起却没有关闭旧窗口"
        finally:
            w.deleteLater()

    def test_relaunch_params_include_script_when_not_frozen(self, monkeypatch):
        import sys as _sys

        from lbamover.platform import privilege

        monkeypatch.delattr(_sys, "frozen", raising=False)
        params = privilege._relaunch_params(["--demo"])
        assert _sys.argv[0] in params, "开发态重启必须带上脚本路径，否则 python 会去执行 --demo"
        assert "--demo" in params

    def test_relaunch_params_omit_script_when_frozen(self, monkeypatch):
        import sys as _sys

        from lbamover.platform import privilege

        monkeypatch.setattr(_sys, "frozen", True, raising=False)
        assert privilege._relaunch_params(["--demo"]) == "--demo"
