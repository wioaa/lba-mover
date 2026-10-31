"""策略选择。

旧版把 ``range / auto_fill / compact / fragment / zone / relocate`` 六个
策略并列在一个下拉里，用户无法自解释"我到底要干什么"。新版按**优化目标**
命名，并且每个选项都带一句"它会动什么"，让选择变成可解释的动作：

* 先问"你的痛点是单个文件太碎，还是没有大块连续空间"；
* 再对应到策略。

只保留 4 个选项，且第一个是"只分析"，给用户一条不写盘的安全路径。
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QButtonGroup, QPushButton, QVBoxLayout, QWidget

from ...contract.commands import STRATEGY_LABELS
from ...contract.enums import Strategy
from ..theme import DARK, Palette

__all__ = ["StrategyPanel", "STRATEGY_HELP"]

#: 每个策略的"它会做什么"——比名字更能决定用户点哪个
STRATEGY_HELP: dict[Strategy, str] = {
    Strategy.ANALYZE_ONLY: "只扫描出报告，不写任何一块",
    Strategy.COALESCE: "把每个碎片文件重排成一段连续区",
    Strategy.COMPACT: "把文件往卷首挤，腾出尾部大块连续空间",
    Strategy.RELOCATE_TAIL: "把碎片文件搬到卷尾，为卷首让位",
}


class StrategyPanel(QWidget):
    strategyChanged = Signal(object)  # Strategy

    def __init__(self, palette: Palette = DARK, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._p = palette
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._index: list[Strategy] = []

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        for i, strategy in enumerate(
            (Strategy.ANALYZE_ONLY, Strategy.COALESCE, Strategy.COMPACT, Strategy.RELOCATE_TAIL)
        ):
            btn = QPushButton(
                f"{STRATEGY_LABELS[strategy]}\n{STRATEGY_HELP[strategy]}"
            )
            btn.setObjectName("option")
            btn.setCheckable(True)
            btn.setMinimumHeight(42)
            btn.setToolTip(STRATEGY_HELP[strategy])
            self._group.addButton(btn, i)
            self._index.append(strategy)
            lay.addWidget(btn)
            if strategy is Strategy.COALESCE:
                btn.setChecked(True)

        self._group.idClicked.connect(self._on_clicked)

    def _on_clicked(self, idx: int) -> None:
        self.strategyChanged.emit(self._index[idx])

    def strategy(self) -> Strategy:
        idx = max(0, self._group.checkedId())
        return self._index[idx]

    def set_strategy(self, strategy: Strategy) -> None:
        if strategy in self._index:
            self._group.button(self._index.index(strategy)).setChecked(True)

    def setEnabled(self, enabled: bool) -> None:  # noqa: N802 - Qt 命名
        super().setEnabled(enabled)
