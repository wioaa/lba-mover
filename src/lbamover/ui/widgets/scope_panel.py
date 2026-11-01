"""整理范围：整卷 / 手动选择文件。

为什么把它单独做一张卡，而不是塞进策略里
----------------------------------------
"整理哪些文件"与"怎么整理"是两件事。整卷是默认（能治的都治），手动是
例外（只治我关心的）。把两者混成一个下拉，用户会以为"选了策略就限定了
范围"，而实际范围仍然是全卷 —— 这类误解的代价是几十分钟的意外写盘。

规则的唯一出口在这里：范围状态变了就发 :attr:`ScopePanel.scopeChanged`，
主窗口据此决定 ``StartJob`` 带不带 ``file_ids``，以及要不要拦住"手动但
一个文件都没选"这种空转。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QButtonGroup, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from ..format import fmt_bytes, fmt_count
from ..theme import DARK, Palette

__all__ = ["ScopePanel"]


class ScopePanel(QWidget):
    """Range 选择器 + 已选摘要 + 入口按钮。"""

    scopeChanged = Signal(bool)  # True = 手动选择
    pickRequested = Signal()
    clearRequested = Signal()

    def __init__(self, palette: Palette = DARK, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._p = palette
        self._manual = False
        self._has_report = False

        self.btn_whole = QPushButton("整卷")
        self.btn_manual = QPushButton("手动选择")
        for btn in (self.btn_whole, self.btn_manual):
            btn.setCheckable(True)
            btn.setObjectName("option")
            btn.setMinimumHeight(32)
        self.btn_whole.setToolTip("按策略处理卷内所有满足条件的文件")
        self.btn_manual.setToolTip("只处理你在「选择文件…」里勾选的文件")
        self.btn_whole.setChecked(True)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._group.addButton(self.btn_whole, 0)
        self._group.addButton(self.btn_manual, 1)
        self._group.idClicked.connect(self._on_clicked)

        self.summary = QLabel("整卷：扫描后按策略自动挑选文件")
        self.summary.setObjectName("cardHint")
        self.summary.setWordWrap(True)
        # 左栏是竖排卡片，范围卡每多占 10px 就要从别的卡片身上挤出来。
        # 摘要与操作按钮同一行：文字可以被截断，按钮必须完整可点。
        self.summary.setMinimumWidth(110)

        self.btn_pick = QPushButton("选择文件…")
        self.btn_pick.setObjectName("ghost")
        self.btn_pick.clicked.connect(self.pickRequested.emit)
        self.btn_clear = QPushButton("清空")
        self.btn_clear.setObjectName("ghost")
        self.btn_clear.clicked.connect(self.clearRequested.emit)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self.btn_whole, 1)
        row.addWidget(self.btn_manual, 1)
        lay.addLayout(row)

        inline = QHBoxLayout()
        inline.setSpacing(6)
        inline.addWidget(self.summary, 1)
        inline.addWidget(self.btn_pick)
        inline.addWidget(self.btn_clear)
        lay.addLayout(inline)

        self._sync_enabled()

    # -- 对外 -------------------------------------------------------------

    def is_manual(self) -> bool:
        return self._manual

    def set_manual(self, manual: bool) -> None:
        if manual == self._manual:
            return
        self._manual = manual
        self.btn_manual.setChecked(manual)
        self.btn_whole.setChecked(not manual)
        self._sync_enabled()
        self.scopeChanged.emit(manual)

    def set_available(self, has_report: bool) -> None:
        """没有扫描结果时不能选文件：选的是"扫描结果里的文件"，不是路径。"""
        self._has_report = has_report
        self._sync_enabled()

    def set_selection(self, count: int, blocks: int, block_size: int, stale: int = 0) -> None:
        """更新"已选 N 个"的摘要。``stale`` = 已不在扫描结果里的个数。"""
        if not self._manual:
            self.summary.setText("整卷：扫描后按策略自动挑选文件")
            return
        if count <= 0:
            self.summary.setText("还没有选中文件 —— 点「选择文件…」开始")
            return
        text = (
            f"已选 {fmt_count(count)} 个 · {fmt_bytes(blocks * block_size)} · "
            f"预计搬 {fmt_count(blocks)} 块"
        )
        if stale:
            text += f"（其中 {stale} 个已不在扫描结果里）"
        self.summary.setText(text)

    # -- 内部 -------------------------------------------------------------

    def _on_clicked(self, idx: int) -> None:
        manual = idx == 1
        if manual == self._manual:
            return
        self._manual = manual
        self._sync_enabled()
        self.scopeChanged.emit(manual)

    def _sync_enabled(self) -> None:
        # 选择器永远可点（用户可以先切到手动再扫描），但"选择文件"要先有报告
        self.btn_pick.setEnabled(self._has_report and self._manual)
        self.btn_clear.setEnabled(bool(self._manual))
        if not self._has_report:
            self.btn_pick.setToolTip("先点底部「扫描 / 预演」——可选的文件来自扫描结果")
        else:
            self.btn_pick.setToolTip("")
        if not self._manual:
            self.summary.setCursor(Qt.CursorShape.ArrowCursor)
