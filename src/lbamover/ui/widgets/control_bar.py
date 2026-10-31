"""底部操作栏。

按钮可用性**只由状态机驱动**（一个 ``sync_state``），不允许任何一个面板
自己 ``setEnabled`` 某个按钮。旧版每个面板各自判断，才出现"两个面板都
觉得自己能点，结果同时触发两次任务"。
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from ...contract.enums import JobState
from ..theme import DARK, Palette

__all__ = ["ControlBar"]


class ControlBar(QWidget):
    scanRequested = Signal()
    startRequested = Signal()
    pauseRequested = Signal()
    resumeRequested = Signal()
    stopRequested = Signal()

    def __init__(self, palette: Palette = DARK, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._p = palette
        self._state = JobState.IDLE
        self._has_target = False

        self.summary = QLabel("选择目标卷后开始")
        self.summary.setStyleSheet(f"color: {palette.text_faint}; font-size: 12px;")

        self.btn_scan = QPushButton("扫描 / 预演")
        self.btn_scan.clicked.connect(self.scanRequested.emit)

        self.btn_start = QPushButton("开始整理")
        self.btn_start.setObjectName("primary")
        self.btn_start.clicked.connect(self.startRequested.emit)

        self.btn_pause = QPushButton("暂停")
        self.btn_pause.clicked.connect(self._on_pause)

        self.btn_stop = QPushButton("停止")
        self.btn_stop.setObjectName("danger")
        self.btn_stop.clicked.connect(self.stopRequested.emit)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addWidget(self.summary, 1)
        for b in (self.btn_scan, self.btn_pause, self.btn_stop, self.btn_start):
            lay.addWidget(b)

        self.sync_state(JobState.IDLE)

    # -- 状态 -------------------------------------------------------------

    def set_target_available(self, has_target: bool) -> None:
        self._has_target = has_target
        self.sync_state(self._state)

    def set_summary(self, text: str) -> None:
        self.summary.setText(text)

    def sync_state(self, state: JobState) -> None:
        self._state = state
        running = state in (JobState.RUNNING, JobState.PAUSED, JobState.STOPPING)
        busy = state in (JobState.SCANNING, JobState.PLANNING)
        idle = state in (JobState.IDLE, JobState.DONE, JobState.FAILED, JobState.CANCELLED)

        self.btn_scan.setEnabled(self._has_target and not running and not busy)
        self.btn_start.setEnabled(self._has_target and not running and not busy)
        self.btn_pause.setEnabled(state in (JobState.RUNNING, JobState.PAUSED))
        self.btn_stop.setEnabled(running or state is JobState.PAUSED)
        self.btn_pause.setText("继续" if state is JobState.PAUSED else "暂停")

        if busy:
            self.btn_scan.setText("扫描中…" if state is JobState.SCANNING else "规划中…")
        else:
            self.btn_scan.setText("扫描 / 预演")

        if not self._has_target and idle:
            self.summary.setText("请先选择目标卷")
        elif state is JobState.RUNNING:
            self.summary.setText("正在搬运数据 —— 请勿断电或强制关闭")
        elif state is JobState.PAUSED:
            self.summary.setText("已暂停，可随时继续或停止（断点已保存）")
        elif state is JobState.STOPPING:
            self.summary.setText("正在停止，等待在途 chunk 写完…")
        elif state is JobState.DONE:
            self.summary.setText("已完成。可再次扫描确认碎片度变化")
        elif state is JobState.FAILED:
            self.summary.setText("执行失败，详情见日志面板")
        elif state is JobState.CANCELLED:
            self.summary.setText("已停止，断点已保留，可再次开始续做")
        elif state is JobState.IDLE:
            # 有目标但还没扫过：给下一步动作，而不是留一句过期的"请先选择目标卷"
            self.summary.setText(
                "已选择目标，点击「扫描 / 预演」查看碎片分布" if self._has_target else "请先选择目标卷"
            )

    def _on_pause(self) -> None:
        if self._state is JobState.PAUSED:
            self.resumeRequested.emit()
        else:
            self.pauseRequested.emit()
