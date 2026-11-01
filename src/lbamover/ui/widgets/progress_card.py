"""进度卡：一眼看到「到哪了 / 多快 / 还要多久 / 有没有出错」。

排版按视觉权重排：百分比最大，进度条第二，速率与 ETA 并排，异常计数在
最右侧单独用颜色标出。异常计数不塞进日志里是有意的——日志会滚，进度卡
不会，用户扫一眼就知道"这次跑得干不干净"。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QProgressBar, QVBoxLayout, QWidget

from ...contract.enums import JobState
from ...contract.models import Progress
from ..format import fmt_bytes, fmt_count, fmt_eta, fmt_percent, fmt_rate
from ..theme import DARK, Palette
from .cards import MetricTile
from .flow import FlowContainer

__all__ = ["ProgressCard"]

_STATE_TEXT = {
    JobState.IDLE: "待机",
    JobState.SCANNING: "扫描中",
    JobState.PLANNING: "规划中",
    JobState.RUNNING: "执行中",
    JobState.PAUSED: "已暂停",
    JobState.STOPPING: "正在停止",
    JobState.DONE: "已完成",
    JobState.PARTIAL: "部分完成",
    JobState.FAILED: "失败",
    JobState.CANCELLED: "已停止",
}


class ProgressCard(QWidget):
    def __init__(self, palette: Palette = DARK, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._p = palette
        self._state = JobState.IDLE

        self.percent_label = QLabel("0.0%")
        self.percent_label.setObjectName("heroValue")
        self.state_chip = QLabel("待机")
        self.state_chip.setObjectName("chip")

        top = QHBoxLayout()
        top.setSpacing(10)
        top.addWidget(self.percent_label)
        top.addWidget(self.state_chip, 0, Qt.AlignmentFlag.AlignBottom)
        top.addStretch(1)

        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(10)

        self.current_label = QLabel("等待任务…")
        self.current_label.setStyleSheet(f"color: {palette.text_dim}; font-size: 11px;")
        self.current_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self.tiles = FlowContainer(spacing=18)
        self.t_rate = MetricTile("速率", "—")
        self.t_eta = MetricTile("剩余时间", "—")
        self.t_blocks = MetricTile("已处理块", "0")
        self.t_bytes = MetricTile("数据量", "0 B")
        self.t_retry = MetricTile("重试", "0")
        self.t_fail = MetricTile("失败块", "0")
        for tile in (self.t_rate, self.t_eta, self.t_blocks, self.t_bytes, self.t_retry, self.t_fail):
            self.tiles.add(tile)

        # 显式最小高度：进度卡里的指标块一旦被压扁就会互相重叠，
        # 而 QWidget 默认的最小高度是所有子项的 max，流式布局算不准。
        self.setMinimumHeight(140)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        lay.addLayout(top)
        lay.addWidget(self.bar)
        lay.addWidget(self.current_label)
        lay.addWidget(self.tiles)

    # -- 更新 -------------------------------------------------------------

    def set_state(self, state: JobState) -> None:
        self._state = state
        self.state_chip.setText(_STATE_TEXT.get(state, state.value))
        # 扫描/规划没有"总量"可算，进度条切成不确定模式（持续滚动），
        # 数字位改显示已看条目数。给个假百分比是骗人的：扫描到 90% 才发
        # 现有 300 万个文件的事经常发生。
        if state in (JobState.SCANNING, JobState.PLANNING):
            self.bar.setRange(0, 0)
            if state is JobState.PLANNING:
                self.percent_label.setText("规划中")
        else:
            self.bar.setRange(0, 1000)
        color = self._p.text_dim
        if state in (JobState.RUNNING, JobState.SCANNING, JobState.PLANNING):
            color = self._p.accent
        elif state is JobState.PAUSED:
            color = self._p.warn
        elif state is JobState.DONE:
            color = self._p.ok
        elif state is JobState.PARTIAL:
            # 部分完成必须是警告色，不能是成功色：用户扫一眼"绿的"就走了，
            # 而那些没搬走的文件还躺在碎片里。
            color = self._p.warn
        elif state is JobState.FAILED:
            color = self._p.danger
        elif state is JobState.CANCELLED:
            color = self._p.warn
        self.state_chip.setStyleSheet(
            f"color: {color}; border: 1px solid {color}66; background: {color}18;"
            "border-radius: 9px; padding: 2px 9px; font-size: 11px;"
        )

    def update_progress(self, pr: Progress) -> None:
        if self._state is JobState.SCANNING:
            # 扫描：只报"看了多少条"和"看到哪了"，不给百分比
            if pr.scanned_items:
                self.percent_label.setText(f"{fmt_count(pr.scanned_items)} 项")
            if pr.current_path:
                self.current_label.setText(f"已扫描：{_elide(pr.current_path, 90)}")
            return
        if self._state is not JobState.RUNNING:
            self.bar.setRange(0, 1000)
        self.percent_label.setText(fmt_percent(pr.ratio))
        self.bar.setValue(int(round(pr.ratio * 1000)))
        self.t_rate.set_value(fmt_rate(pr.rate_bps))
        self.t_eta.set_value(fmt_eta(pr.eta_s))
        self.t_blocks.set_value(fmt_count(pr.done_blocks))
        self.t_bytes.set_value(f"{fmt_bytes(pr.bytes_done)} / {fmt_bytes(pr.bytes_total)}")
        self.t_retry.set_value(fmt_count(pr.retried_chunks))
        self.t_fail.set_value(fmt_count(pr.failed_chunks))
        self.t_fail.set_accent(self._p.danger if pr.failed_chunks else None)
        self.t_retry.set_accent(self._p.warn if pr.retried_chunks else None)
        if pr.current_path:
            self.current_label.setText(f"当前：{_elide(pr.current_path, 90)}")
        elif pr.total_ops and pr.done_ops >= pr.total_ops:
            self.current_label.setText("全部文件处理完毕")

    def reset(self) -> None:
        self.bar.setRange(0, 1000)
        self.percent_label.setText("0.0%")
        self.bar.setValue(0)
        for tile in (self.t_rate, self.t_eta, self.t_retry, self.t_fail):
            tile.set_value("—")
            tile.set_accent(None)
        self.t_blocks.set_value("0")
        self.t_bytes.set_value("0 B")
        self.current_label.setText("等待任务…")


def _elide(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = limit // 2 - 2
    tail = limit - head - 3
    return f"{text[:head]}…{text[-tail:]}"
