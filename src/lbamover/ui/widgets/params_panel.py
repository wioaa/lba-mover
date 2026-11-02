"""参数配置。

每个参数旁边都有"为什么调它"。没有解释的可调参数等于给用户一堆开关让
他自己背锅——尤其是"并发"和"队列深度"，调大反而更慢的情况很常见。

一个硬约束：**内存上界实时算给用户看**。它等于
``chunk_blocks × block_size × queue_depth × concurrency``，
改任何一项都会立刻反映。这是"内存可控"唯一诚实的表达方式。
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...contract.models import MAX_IGNORED_FAILURES, JobParams
from ..format import fmt_bytes
from ..theme import DARK, Palette

__all__ = ["ParamsPanel"]


class ParamsPanel(QWidget):
    paramsChanged = Signal(object)  # JobParams

    def __init__(self, palette: Palette = DARK, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._p = palette
        self._block_size = 4096
        self._suppress = False

        form = QFormLayout()
        form.setLabelAlignment(form.labelAlignment())
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)
        form.setContentsMargins(0, 0, 0, 0)

        self.chunk = _spin(1, 4096, 256, " 块", "单次连续读写的块数。大 → 吞吐高、单次延迟大")
        self.depth = _spin(1, 32, 4, " 块", "在途 chunk 数。≥2 才能形成读写重叠")
        self.concurrency = _spin(1, 8, 2, " 路", "同时处理的文件数。HDD 建议 1，SSD 可 2~4")
        self.retries = _spin(0, 10, 3, " 次", "单个 chunk 失败后的重试上限")
        self.rate = QDoubleSpinBox()
        self.rate.setRange(0.0, 4000.0)
        self.rate.setSingleStep(10.0)
        self.rate.setDecimals(0)
        self.rate.setSuffix(" MiB/s")
        self.rate.setToolTip("0 表示不限速。限速会明显降低吞吐，但能让机器保持可用")
        self.reserve = _spin(0, 1_000_000, 0, " 块", "卷尾保留块数，为系统预留活动空间")
        self.verify = QCheckBox("回读校验（慢一倍，但能发现坏块）")
        self.verify.setChecked(True)
        # 默认关闭：默认行为必须是"保守收尾"。打开它等于主动放弃熔断保护，
        # 所以要用户自己勾，且旁边把代价写清楚。
        self.ignore_errors = QCheckBox("忽略错误继续运行")
        self.ignore_errors.setChecked(False)
        self.ignore_errors.setToolTip(
            "不因连续失败停下，把整卷能搬的搬完；累计失败到 "
            f"{MAX_IGNORED_FAILURES} 个才会停。\n"
            "代价：设备真的出问题（掉线、只读）时会在废设备上空转更久，\n"
            "失败的条目最后统一列在日志与报告里。"
        )
        self._ignore_hint = QLabel(
            "不因连续失败停下，把整卷能搬的搬完；累计失败到 "
            f"{MAX_IGNORED_FAILURES} 个才会停。"
        )
        self._ignore_hint.setWordWrap(True)
        self._ignore_hint.setStyleSheet(f"color: {palette.text_faint}; font-size: 11px;")
        self._ignore_hint.setVisible(False)

        form.addRow("分块大小", self.chunk)
        form.addRow("队列深度", self.depth)
        form.addRow("并发", self.concurrency)
        form.addRow("重试上限", self.retries)
        form.addRow("限速", self.rate)
        form.addRow("尾部保留", self.reserve)
        form.addRow("", self.verify)
        # 独立占一行：它跟"回读校验"是两个正交的决策，不能挤在同一格，
        # 也不能靠布局间隙找位置（本项目踩过控件重叠、标题被压）。
        form.addRow("", self.ignore_errors)
        # 说明只在勾选时出现，平时不占面板高度（左栏在 900 高的窗口里很紧）
        form.addRow("", self._ignore_hint)
        form.setRowVisible(self._ignore_hint, False)

        # 三个"很少动、动了容易出事"的参数收进高级区，默认折叠。
        # 常驻 7 行会让左栏在 900 高的窗口里放不下，而其中 3 个参数
        # 用户一年也改不了一次。
        self._advanced_rows = (self.depth, self.retries, self.reserve)
        self.advanced_toggle = QPushButton("高级参数 ▸")
        self.advanced_toggle.setObjectName("ghost")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setChecked(False)
        self.advanced_toggle.clicked.connect(self._toggle_advanced)
        for widget in self._advanced_rows:
            form.setRowVisible(widget, False)

        self.budget = QLabel()
        self.budget.setWordWrap(True)
        self.budget.setStyleSheet(f"color: {palette.text_faint}; font-size: 11px;")
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setVisible(False)
        self.error.setStyleSheet(f"color: {palette.danger}; font-size: 11px;")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addLayout(form)
        lay.addWidget(self.advanced_toggle)
        lay.addWidget(self.budget)
        lay.addWidget(self.error)

        for widget in (self.chunk, self.depth, self.concurrency, self.retries, self.reserve):
            widget.valueChanged.connect(self._emit)
        self.rate.valueChanged.connect(self._emit)
        self.verify.toggled.connect(self._emit)
        self.ignore_errors.toggled.connect(self._on_ignore_errors_toggled)

        self._refresh_budget()

    # -- 状态 -------------------------------------------------------------

    def set_block_size(self, block_size: int) -> None:
        self._block_size = max(512, block_size)
        self._refresh_budget()

    def current(self) -> JobParams | None:
        try:
            return JobParams(
                block_size=self._block_size,
                chunk_blocks=self.chunk.value(),
                queue_depth=self.depth.value(),
                concurrency=self.concurrency.value(),
                max_retries=self.retries.value(),
                rate_limit_mbps=float(self.rate.value()),
                verify=self.verify.isChecked(),
                reserve_tail_blocks=self.reserve.value(),
                ignore_errors=self.ignore_errors.isChecked(),
            )
        except ValueError as exc:
            self.error.setText(str(exc))
            self.error.setVisible(True)
            return None

    def apply(self, params: JobParams) -> None:
        self._suppress = True
        try:
            self._block_size = params.block_size
            self.chunk.setValue(params.chunk_blocks)
            self.depth.setValue(params.queue_depth)
            self.concurrency.setValue(params.concurrency)
            self.retries.setValue(params.max_retries)
            self.rate.setValue(params.rate_limit_mbps)
            self.reserve.setValue(params.reserve_tail_blocks)
            self.verify.setChecked(params.verify)
            self.ignore_errors.setChecked(params.ignore_errors)
        finally:
            self._suppress = False
        self._sync_ignore_hint()
        self._refresh_budget()

    def _toggle_advanced(self) -> None:
        shown = self.advanced_toggle.isChecked()
        self.advanced_toggle.setText("高级参数 ▾" if shown else "高级参数 ▸")
        form = self.layout().itemAt(0).layout()
        for widget in self._advanced_rows:
            form.setRowVisible(widget, shown)
        # 隐藏的行不会自动让面板缩小，必须显式让上层重新算尺寸
        form.activate()
        self.updateGeometry()

    def set_locked(self, locked: bool) -> None:
        """任务运行期间锁住结构性参数（热更新只允许限速与校验）。"""
        for widget in (self.chunk, self.depth, self.concurrency, self.retries, self.reserve):
            widget.setEnabled(not locked)
        # 「忽略错误运行」也是结构性参数：它决定的是循环里的闸门，而闸门在
        # 构造 JobRunner 时就定了，中途改只会让界面与实际行为说法不一。
        self.ignore_errors.setEnabled(not locked)

    # -- 内部 -------------------------------------------------------------

    def _on_ignore_errors_toggled(self, checked: bool) -> None:
        self._sync_ignore_hint(checked)
        self._emit()

    def _sync_ignore_hint(self, show: bool | None = None) -> None:
        """说明文字只在勾选时出现，避免常驻多占一行高度。"""
        if show is None:
            show = self.ignore_errors.isChecked()
        form = self.layout().itemAt(0).layout()
        if form is not None:
            form.setRowVisible(self._ignore_hint, show)
            form.activate()
        self._ignore_hint.setVisible(show)
        self.updateGeometry()

    def _emit(self) -> None:
        if self._suppress:
            return
        self._refresh_budget()
        params = self.current()
        if params is None:
            return
        self.error.setVisible(False)
        self.paramsChanged.emit(params)

    def _refresh_budget(self) -> None:
        chunk_bytes = self.chunk.value() * self._block_size
        budget = chunk_bytes * self.depth.value() * self.concurrency.value()
        self.budget.setText(
            f"内存上界 ≈ {fmt_bytes(budget)}"
            f"（{self.chunk.value()} 块 × {fmt_bytes(self._block_size)}"
            f" × 深度 {self.depth.value()} × 并发 {self.concurrency.value()}），与卷大小无关"
        )


def _spin(lo: int, hi: int, val: int, suffix: str, tip: str) -> QSpinBox:
    box = QSpinBox()
    box.setRange(lo, hi)
    box.setValue(val)
    box.setSuffix(suffix)
    box.setToolTip(tip)
    box.setAccelerated(True)
    return box
