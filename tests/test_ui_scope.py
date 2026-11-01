"""手动选择文件（整理范围）的 UI 行为。

三件事必须成立，否则"只整理我选的这几个文件"就是空话：

1. **选中的人是 file_id，不是行号** —— 行号随筛选/排序变化，用它当身份
   会让用户勾了 A、引擎动了 B；
2. **手动模式下一个文件都没选时不许开始** —— 空 ``file_ids`` 在引擎那边的
   语义是"整卷"，一个疏忽就会把几十万个文件都搬了；
3. **选择必须真的传到引擎** —— 预演的数字（几个文件/多少块）与执行用的
   必须是同一份 file_ids。
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("PySide6", reason="未安装 PySide6，跳过 GUI 测试")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from lbamover.contract.enums import Strategy  # noqa: E402
from lbamover.contract.models import Extent, FileFrag  # noqa: E402
from lbamover.ui.theme import DARK  # noqa: E402


class StubBox:
    """替身：``QMessageBox`` 的静态方法，用来点亮"用户点了是"。

    注意保留 ``StandardButton``：真正的判定逻辑（``confirmed``）要走真实路径，
    只把"弹框"换掉，不把"怎么判返回值"绕过去。
    """

    StandardButton = QMessageBox.StandardButton
    Icon = QMessageBox.Icon

    answer: object = 0
    notes: list = []

    @classmethod
    def question(cls, *args, **kwargs):
        return cls.answer

    @classmethod
    def information(cls, *args, **kwargs):
        cls.notes.append(str(args[1]) if len(args) > 1 else "")
        return cls.StandardButton.Ok


@pytest.fixture()
def app():
    inst = QApplication.instance()
    if inst is None:
        inst = QApplication([])
    return inst


def pump(cond, timeout: float = 20.0) -> bool:
    """转事件循环直到条件成立。引擎在工作线程里，事件要 pump 才会到。"""
    inst = QApplication.instance()
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        inst.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    return False


def demo_window(monkeypatch, answer=None, wait_report: bool = True):
    from lbamover.ui import main_window as mw

    if answer is not None:
        StubBox.answer = answer
        StubBox.notes = []
        monkeypatch.setattr(mw, "QMessageBox", StubBox)
    w = mw.MainWindow(palette=DARK)
    w.load_demo()
    if wait_report:
        w._on_scan()
        assert pump(lambda: w._report is not None), "演示卷扫描没有完成"
    return w


class TestFileSelectModel:
    def _model(self):
        from lbamover.ui.widgets.file_select import FileTableModel

        files = (
            FileFrag("a", "/v/a.bin", (Extent(0, 4), Extent(40, 4)), 0),
            FileFrag("b", "/v/b.bin", (Extent(10, 8),), 0),
            FileFrag("c", "/v/sub/c.bin", (Extent(20, 2), Extent(60, 2), Extent(90, 2)), 0),
            FileFrag("d", "/v/sub/deep/d.bin", (Extent(30, 16),), 0),
        )
        return FileTableModel(files, 4096)

    def test_default_shows_only_fragmented(self, app):
        m = self._model()
        assert m.total_count() == 4
        assert m.visible_count() == 2, "默认视图应当只列碎片文件"

    def test_show_all(self, app):
        m = self._model()
        m.set_filter(only_fragmented=False)
        assert m.visible_count() == 4

    def test_search_is_case_insensitive_substring(self, app):
        m = self._model()
        m.set_filter(only_fragmented=False, needle="SUB/DEEP")
        assert m.visible_count() == 1
        assert m.frag_at(0).file_id == "d"

    def test_sort_by_runs_puts_worst_first(self, app):
        m = self._model()
        m.set_filter(sort="runs")
        assert m.frag_at(0).file_id == "c"  # 3 段

    def test_checking_tracks_file_id_not_row(self, app):
        m = self._model()
        m.setData(m.index(0, 0), Qt.CheckState.Checked, Qt.ItemDataRole.CheckStateRole)
        picked = m.selection_ids()
        assert len(picked) == 1
        chosen = picked[0]

        # 换排序：同一行号底下已经是另一个文件，选择不能跟着行号跑
        m.set_filter(sort="path")
        assert m.selection_ids() == (chosen,)

    def test_select_all_visible_and_clear(self, app):
        m = self._model()
        m.select_all_visible()
        assert m.selection_count() == m.visible_count()
        blocks = m.selection_blocks()
        assert blocks > 0 and m.selection_bytes() == blocks * 4096
        m.clear_selection()
        assert m.selection_count() == 0 and m.selection_blocks() == 0

    def test_preselect_survives_filter_change(self, app):
        m = self._model()
        m.set_selection(("b", "d"))
        assert m.selection_count() == 2
        m.set_filter(only_fragmented=True)  # b/d 被滤掉，但选择还在
        assert m.selection_count() == 2
        m.setData(m.index(0, 0), Qt.CheckState.Checked, Qt.ItemDataRole.CheckStateRole)
        assert m.selection_count() == 3

    def test_uncheck_removes_from_selection(self, app):
        m = self._model()
        m.setData(m.index(0, 0), Qt.CheckState.Checked, Qt.ItemDataRole.CheckStateRole)
        assert m.selection_count() == 1
        m.setData(m.index(0, 0), Qt.CheckState.Unchecked, Qt.ItemDataRole.CheckStateRole)
        assert m.selection_count() == 0


class TestScopePanel:
    def test_toggle_emits_and_tracks(self, app):
        from lbamover.ui.widgets.scope_panel import ScopePanel

        p = ScopePanel(DARK)
        seen: list[bool] = []
        p.scopeChanged.connect(seen.append)
        assert p.is_manual() is False
        p.set_manual(True)
        assert p.is_manual() is True and seen == [True]
        p.set_manual(False)
        assert seen == [True, False]

    def test_pick_disabled_without_report(self, app):
        from lbamover.ui.widgets.scope_panel import ScopePanel

        p = ScopePanel(DARK)
        p.set_manual(True)
        assert p.btn_pick.isEnabled() is False, "没有扫描结果时不该能选文件"
        p.set_available(True)
        assert p.btn_pick.isEnabled() is True

    def test_summary_mentions_stale_files(self, app):
        from lbamover.ui.widgets.scope_panel import ScopePanel

        p = ScopePanel(DARK)
        p.set_manual(True)
        p.set_selection(5, 100, 4096, stale=2)
        text = p.summary.text()
        assert "已选 5" in text and "2 个已不在" in text


class TestManualScopeFlow:
    def test_manual_without_selection_blocks_start(self, app, monkeypatch):
        w = demo_window(monkeypatch, int(QMessageBox.StandardButton.Yes))
        sent: list = []
        monkeypatch.setattr(w.service, "submit", sent.append)
        try:
            from lbamover.contract.commands import StartJob

            w._strategy = Strategy.COALESCE
            w.scope_panel.set_manual(True)  # 这一步会触发一次 Replan，属正常
            sent.clear()
            w._on_start()
            assert not [c for c in sent if isinstance(c, StartJob)], (
                "手动模式下一个文件都没选却提交了任务 —— 引擎会按整卷执行"
            )
            assert StubBox.notes, "应当提示用户先选文件"
            assert w.control.btn_start.isEnabled() is False
        finally:
            w.close()

    def test_selection_is_passed_to_start_job(self, app, monkeypatch):
        from lbamover.contract.commands import StartJob

        w = demo_window(monkeypatch, int(QMessageBox.StandardButton.Yes))
        sent: list = []
        monkeypatch.setattr(w.service, "submit", sent.append)
        try:
            assert w._report is not None
            chosen = tuple(f.file_id for f in w._report.files[:3])
            w.scope_panel.set_manual(True)
            sent.clear()
            w._selection = chosen
            w._refresh_scope_summary()
            assert w.control.btn_start.isEnabled() is True

            w._on_start()
            jobs = [c for c in sent if isinstance(c, StartJob)]
            assert jobs, "没提交 StartJob"
            assert jobs[0].spec.file_ids == chosen, "开始整理时没把选中文件传给引擎"
        finally:
            w.close()

    def test_switching_scope_replans_without_rescan(self, app, monkeypatch):
        from lbamover.contract.commands import Replan

        w = demo_window(monkeypatch)
        sent: list = []
        monkeypatch.setattr(w.service, "submit", sent.append)
        try:
            w._selection = tuple(f.file_id for f in w._report.files[:2])
            sent.clear()
            w.scope_panel.set_manual(True)
            replans = [c for c in sent if isinstance(c, Replan)]
            assert replans and len(replans[0].file_ids) == 2, "切到手动范围没有重新预演"

            sent.clear()
            w.scope_panel.set_manual(False)
            replans = [c for c in sent if isinstance(c, Replan)]
            assert replans and replans[0].file_ids == (), "切回整卷时没恢复整卷口径"
        finally:
            w.close()

    def test_stale_selection_pruned_on_new_report(self, app, monkeypatch):
        w = demo_window(monkeypatch)
        try:
            valid = w._report.files[0].file_id
            w._selection = (valid, "no-such-file")
            w._refresh_scope_summary()
            w._on_report(w._report)  # 同一份报告重放一次，模拟重新扫描
            assert w._selection == (valid,), "失效的选择没有被清掉"
            assert w._sel_stale == 0
        finally:
            w.close()

    def test_scope_ids_are_empty_for_whole_volume(self, app, monkeypatch):
        w = demo_window(monkeypatch)
        try:
            w._selection = tuple(f.file_id for f in w._report.files[:2])
            assert w._scope_file_ids() == (), "整卷模式下不该带 file_ids"
            w.scope_panel.set_manual(True)
            assert w._scope_file_ids() == w._selection
        finally:
            w.close()


class TestScanProgressCard:
    """扫描阶段的进度展示。

    扫描没有"总量"可算（要遍历完才知道有多少文件），所以这里**不编百分比**，
    用不确定进度条 + 已看条目数。假百分比比没有进度更糟：用户会按 90% 的
    剩余量估时间，而实际上扫描常常在最后一刻才发现一堆新文件。
    """

    def test_indeterminate_during_scan_then_restore(self, app):
        from lbamover.contract.enums import JobState
        from lbamover.contract.models import Progress
        from lbamover.ui.widgets.progress_card import ProgressCard

        card = ProgressCard(DARK)
        card.set_state(JobState.SCANNING)
        assert card.bar.maximum() == 0, "扫描阶段进度条应当是不确定模式 (0,0)"

        card.update_progress(Progress(scanned_items=123456, current_path="T:/a/b.bin"))
        assert "12" in card.percent_label.text(), card.percent_label.text()
        assert "b.bin" in card.current_label.text()

        card.set_state(JobState.RUNNING)
        assert card.bar.maximum() == 1000, "进入执行阶段必须恢复确定进度"
        card.update_progress(Progress(done_blocks=5, total_blocks=10))
        assert card.percent_label.text().startswith("50")

    def test_reset_restores_definite_mode(self, app):
        from lbamover.contract.enums import JobState
        from lbamover.ui.widgets.progress_card import ProgressCard

        card = ProgressCard(DARK)
        card.set_state(JobState.SCANNING)
        card.reset()
        assert card.bar.maximum() == 1000


class TestPartialState:
    """「部分完成」在界面上的呈现。

    这一档状态的唯一目的就是让用户不要误以为"整盘已经整理好了"，所以
    必须同时满足：文案写明"部分完成"、颜色是警告而不是成功、按钮恢复可用
    （否则用户没法再跑一次处理剩下的）。
    """

    def test_progress_card_chip_says_partial(self, app):
        from lbamover.contract.enums import JobState
        from lbamover.ui.widgets.progress_card import ProgressCard

        card = ProgressCard(DARK)
        card.set_state(JobState.PARTIAL)
        assert card.state_chip.text() == "部分完成"

    def test_progress_card_partial_uses_warn_not_ok(self, app):
        from lbamover.contract.enums import JobState
        from lbamover.ui.widgets.progress_card import ProgressCard

        card = ProgressCard(DARK)
        card.set_state(JobState.DONE)
        done_style = card.state_chip.styleSheet()
        card.set_state(JobState.PARTIAL)
        partial_style = card.state_chip.styleSheet()
        assert partial_style != done_style, "部分完成和已完成用了同一个颜色"
        assert DARK.warn in partial_style and DARK.ok not in partial_style

    def test_control_bar_recovers_buttons_after_partial(self, app):
        from lbamover.contract.enums import JobState
        from lbamover.ui.widgets.control_bar import ControlBar

        bar = ControlBar(DARK)
        bar.set_target_available(True)
        bar.sync_state(JobState.RUNNING)
        assert not bar.btn_start.isEnabled()

        bar.sync_state(JobState.PARTIAL)
        assert bar.btn_start.isEnabled(), "部分完成后必须能再次开始（处理没搬走的文件）"
        assert "部分完成" in bar.summary.text()
        assert "日志" in bar.summary.text(), "要告诉用户去哪儿看原因"


class TestDiagnoseCopy:
    """「复制诊断」按钮。

    这是用户唯一的求助出口：出问题时他手里只有这个程序的界面。点一下就能
    得到一段自带版本、卷、参数、失败阶段与系统错误码的文字，别人（或 AI）
    拿到就能直接开始定位，而不必先追问五个问题。
    """

    def test_button_exists_and_is_wired(self, app, monkeypatch):
        w = demo_window(monkeypatch)
        try:
            assert w.log_view.diagnose_btn.text() == "复制诊断"
            copied: list[bool] = []
            monkeypatch.setattr(w, "_copy_diagnostic", lambda: copied.append(True))
            w.log_view.diagnose_btn.click()
            assert copied, "按钮没有连到 _copy_diagnostic"
        finally:
            w.close()

    def test_clipboard_gets_a_self_contained_report(self, app, monkeypatch):
        w = demo_window(monkeypatch)
        try:
            QApplication.clipboard().clear()
            w._copy_diagnostic()
            text = QApplication.clipboard().text()

            assert "LBAMover" in text, "报告里没有版本，别人无法判断是不是老版本的问题"
            assert "目标卷" in text, "报告里没有目标卷，无法复现"
            assert "参数:" in text, "报告里没有参数，无法判断是不是配置导致"
            assert "报告结束" in text, "没有结束标记，用户不知道该复制到哪"
        finally:
            w.close()

    def test_copy_is_announced_in_log(self, app, monkeypatch):
        w = demo_window(monkeypatch)
        try:
            lines: list[str] = []
            monkeypatch.setattr(
                w.log_view, "append", lambda level, msg, *a, **k: lines.append(str(msg))
            )
            w._copy_diagnostic()
            assert any("已复制到剪贴板" in line for line in lines), (
                "复制之后没有任何反馈，用户不知道到底复制成功了没有"
            )
        finally:
            w.close()

    def test_tail_is_plain_text_not_html(self, app, monkeypatch):
        """报告里不能出现 HTML 标签 —— 贴进聊天窗口会变成一串尖括号。"""
        w = demo_window(monkeypatch)
        try:
            w.log_view.append(
                __import__("lbamover.contract.enums", fromlist=["LogLevel"]).LogLevel.INFO,
                "一条普通日志",
            )
            tail = w.log_view.plain_tail(10)
            assert any("一条普通日志" in line for line in tail)
            assert all("<span" not in line for line in tail)
        finally:
            w.close()


class TestReclaimPanel:
    """「可用空间整理」的两个参数控件。

    单位用 GiB（用户想的是"腾 200 G 给虚拟机"），引擎内部一律按块 ——
    换算只在边界做一次。这类换算最容易出的错是"少乘或多乘一个 1024"，
    结果是要 200 GiB 却只腾了 200 MiB，所以值得单独钉住。
    """

    def test_default_is_ten_gib_on_tail(self, app):
        from lbamover.contract.enums import FreeSpaceSide
        from lbamover.ui.widgets.strategy_panel import ReclaimPanel

        p = ReclaimPanel(DARK)
        assert p.size.value() == 10
        assert p.side() is FreeSpaceSide.TAIL

    def test_gib_is_converted_to_blocks(self, app):
        from lbamover.ui.widgets.strategy_panel import ReclaimPanel

        p = ReclaimPanel(DARK)
        p.set_geometry(4096, 100 * 1024**3 // 4096)  # 100 GiB 的卷
        p.size.setValue(2)
        assert p.blocks() == 2 * 1024**3 // 4096

    def test_capacity_caps_the_spinbox(self, app):
        from lbamover.ui.widgets.strategy_panel import ReclaimPanel

        p = ReclaimPanel(DARK)
        p.set_geometry(4096, 5 * 1024**3 // 4096)  # 只有 5 GiB
        assert p.size.maximum() == 5
        assert p.size.value() <= 5, "换到小卷后要腾的量必须自动收进来"

    def test_change_emits(self, app):
        from lbamover.ui.widgets.strategy_panel import ReclaimPanel

        p = ReclaimPanel(DARK)
        p.set_geometry(4096, 100 * 1024**3 // 4096)
        seen: list[int] = []
        p.changed.connect(lambda: seen.append(1))
        p.size.setValue(20)
        p.btn_head.click()
        assert len(seen) == 2, "改大小或换边都必须通知外面重算预演"


class TestReclaimStrategyWiring:
    def test_panel_reveals_only_for_reclaim(self, app):
        from lbamover.contract.enums import Strategy
        from lbamover.ui.widgets.strategy_panel import StrategyPanel

        s = StrategyPanel(DARK)
        assert s.strategy() is Strategy.COALESCE
        # 窗口没 show 过时 isVisible() 恒为 False，只能用 isHidden() 判断
        assert s.reclaim.isHidden()
        s.set_strategy(Strategy.RECLAIM)
        assert not s.reclaim.isHidden()
        assert "连续可用空间" in s.hint.text()

    def test_switching_to_reclaim_sets_a_nonzero_size(self, app, monkeypatch):
        """0 在引擎里等于"没设置"，会让功能一上手就判不可行。"""
        from lbamover.contract.enums import Strategy

        w = demo_window(monkeypatch)
        try:
            w._on_strategy(Strategy.RECLAIM)
            assert w._params.reclaim_blocks > 0
            assert w._params.reclaim_at is w.strategy_panel.reclaim.side()
        finally:
            w.close()

    def test_changing_size_updates_params(self, app, monkeypatch):
        w = demo_window(monkeypatch)
        try:
            w.strategy_panel.reclaim.set_geometry(4096, 100 * 1024**3 // 4096)
            w.strategy_panel.reclaim.size.setValue(30)
            assert w._params.reclaim_blocks == 30 * 1024**3 // 4096
        finally:
            w.close()

    def test_params_panel_edit_does_not_wipe_reclaim(self, app, monkeypatch):
        """参数面板不知道自己不该覆盖 reclaim 字段。

        用户改一次并发数，``ParamsPanel`` 会造一份全新的 ``JobParams``；
        不合并的话"腾 30 GiB"会静默变回"没设置"。
        """
        from lbamover.contract.enums import Strategy
        from lbamover.contract.models import JobParams

        w = demo_window(monkeypatch)
        try:
            w._on_strategy(Strategy.RECLAIM)
            before = w._params.reclaim_blocks
            assert before > 0
            w._on_params(JobParams(chunk_blocks=64, concurrency=3))
            assert w._params.reclaim_blocks == before
            assert w._params.concurrency == 3
        finally:
            w.close()
