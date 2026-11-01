"""策略选择。

旧版把 ``range / auto_fill / compact / fragment / zone / relocate`` 六个
策略并列在一个下拉里，用户无法自解释"我到底要干什么"。新版按**优化目标**
命名，并且每个选项都带一句"它会动什么"，让选择变成可解释的动作：

* 先说清楚痛点：单个文件太碎、没有大块连续空间、还是"我要腾地方装东西"；
* 再对应到策略；第一个永远是"只分析"，给用户一条不写盘的安全路径。

**排版说明。** 五个策略若沿用"按钮里塞两行说明"，左栏会多出近 50px 而
本来就刚好装满一屏。所以按钮只放 4~6 个字（两列网格），说明搬到下面单独
一行随选择更新 —— 省了近 80px，说明还能写完整（按钮里那半句"为卷首让位"
其实说不清让多少）。

「可用空间整理」选中时会展开它自己的两个参数（多大 / 哪一侧），其余策略
不占这块高度。
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QGridLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...contract.commands import STRATEGY_LABELS
from ...contract.enums import FreeSpaceSide, Strategy
from ..theme import DARK, Palette

__all__ = [
    "StrategyPanel",
    "ReclaimPanel",
    "STRATEGY_HELP",
    "STRATEGY_SHORT",
    "STRATEGY_ORDER",
]

#: 每个策略的"它会做什么"——比名字更能决定用户点哪个
STRATEGY_HELP: dict[Strategy, str] = {
    Strategy.ANALYZE_ONLY: "只扫描出报告，不写任何一块",
    Strategy.COALESCE: "把每个碎片文件重排成一段连续区",
    Strategy.COMPACT: "把文件往卷首挤，腾出尾部大块连续空间",
    Strategy.RELOCATE_TAIL: "把碎片文件搬到卷尾，为卷首让位",
    Strategy.RECLAIM: "腾出指定大小的连续可用空间，位置可选卷首或卷尾",
}

#: 网格里显示的短名。4~6 个字，两列放得下，也不至于要靠猜。
STRATEGY_SHORT: dict[Strategy, str] = {
    Strategy.ANALYZE_ONLY: "只分析",
    Strategy.COALESCE: "合并碎片",
    Strategy.COMPACT: "向卷首聚拢",
    Strategy.RELOCATE_TAIL: "搬到卷尾",
    Strategy.RECLAIM: "腾出可用空间",
}

#: 按钮顺序。**索引是契约**：``set_strategy`` 与 UI 测试都按它定位，
#: 新增策略一律追加到末尾，不要插到中间。
STRATEGY_ORDER: tuple[Strategy, ...] = (
    Strategy.ANALYZE_ONLY,
    Strategy.COALESCE,
    Strategy.COMPACT,
    Strategy.RELOCATE_TAIL,
    Strategy.RECLAIM,
)


class ReclaimPanel(QWidget):
    """「可用空间整理」的两个参数：要多大、放在哪一侧。

    单位用 GiB 而不是块 —— 用户想的是"腾 200 G 给虚拟机"，不是"52428800 块"。
    换算只在边界处做一次，引擎内部一律按块走（不留第二套单位）。
    """

    changed = Signal()

    def __init__(self, palette: Palette = DARK, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from PySide6.QtWidgets import QSpinBox

        self._p = palette
        self._block_size = 4096
        self._capacity_gib = 1.0
        self._suppress = False

        self.caption = QLabel("腾出")
        self.caption.setObjectName("cardHint")

        self.size = QSpinBox()
        self.size.setRange(1, 1 << 20)
        self.size.setSuffix(" GiB")
        self.size.setSingleStep(10)
        self.size.setValue(10)
        self.size.setMinimumHeight(26)

        self.btn_head = QPushButton("卷首")
        self.btn_tail = QPushButton("卷尾")
        group = QButtonGroup(self)
        group.setExclusive(True)
        for btn in (self.btn_head, self.btn_tail):
            btn.setCheckable(True)
            btn.setObjectName("chip")
            btn.setMinimumHeight(26)
            btn.setMaximumWidth(50)
            group.addButton(btn)
        self.btn_tail.setChecked(True)
        self.btn_head.setToolTip("低地址一侧。系统通常从这里开始放文件，所以多半要让开一段")
        self.btn_tail.setToolTip("高地址一侧。放虚拟机磁盘、镜像这类大文件通常要这一侧")

        lay = QGridLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setHorizontalSpacing(6)
        lay.setVerticalSpacing(0)
        lay.addWidget(self.caption, 0, 0)
        lay.addWidget(self.size, 0, 1)
        lay.addWidget(self.btn_head, 0, 2)
        lay.addWidget(self.btn_tail, 0, 3)
        lay.setColumnStretch(1, 1)

        self.size.valueChanged.connect(self._on_changed)
        self.btn_head.clicked.connect(self._on_changed)
        self.btn_tail.clicked.connect(self._on_changed)
        self._update_tip()

    # -- 配置 -------------------------------------------------------------

    def set_geometry(self, block_size: int, block_count: int) -> None:
        """按目标卷校准单位换算与上限。**换卷必须重新调用**。"""
        self._block_size = max(1, block_size)
        self._capacity_gib = max(1.0, block_count * block_size / 1024**3)
        cap = max(1, int(self._capacity_gib))
        self._suppress = True
        self.size.setRange(1, cap)
        if self.size.value() > cap:
            self.size.setValue(cap)
        self._suppress = False
        self._update_tip()

    def blocks(self) -> int:
        return int(self.size.value() * 1024**3 // self._block_size)

    def side(self) -> FreeSpaceSide:
        return FreeSpaceSide.HEAD if self.btn_head.isChecked() else FreeSpaceSide.TAIL

    def set_values(self, blocks: int, side: FreeSpaceSide) -> None:
        self._suppress = True
        gib = max(1, round(blocks * self._block_size / 1024**3))
        self.size.setValue(min(gib, self.size.maximum()))
        (self.btn_head if side is FreeSpaceSide.HEAD else self.btn_tail).setChecked(True)
        self._suppress = False
        self._update_tip()

    # -- 内部 -------------------------------------------------------------

    def _on_changed(self, *_args) -> None:
        self._update_tip()
        if not self._suppress:
            self.changed.emit()

    def _update_tip(self) -> None:
        blocks = self.blocks()
        text = f"约 {blocks} 块 × {self._block_size} 字节"
        self.caption.setToolTip(text)
        self.size.setToolTip(
            f"卷容量 {self._capacity_gib:.1f} GiB。当前设置{text}；"
            "要得越多，需要搬走的文件越多、耗时越长"
        )


class StrategyPanel(QWidget):
    strategyChanged = Signal(object)  # Strategy
    #: 可用空间整理的大小/位置变了 —— 需要重算预演
    reclaimChanged = Signal()

    def __init__(self, palette: Palette = DARK, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._p = palette
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._index: list[Strategy] = list(STRATEGY_ORDER)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(5)
        for i, strategy in enumerate(self._index):
            btn = QPushButton(STRATEGY_SHORT[strategy])
            btn.setObjectName("option")
            btn.setCheckable(True)
            btn.setMinimumHeight(30)
            btn.setToolTip(f"{STRATEGY_LABELS[strategy]}\n{STRATEGY_HELP[strategy]}")
            self._group.addButton(btn, i)
            row, col = divmod(i, 2)
            # 最后一项是奇数位 → 独占整行，避免右半边空出一块
            span = 2 if (i == len(self._index) - 1 and col == 0) else 1
            grid.addWidget(btn, row, col, 1, span)
            if strategy is Strategy.COALESCE:
                btn.setChecked(True)
        lay.addLayout(grid)

        self.hint = QLabel(STRATEGY_HELP[Strategy.COALESCE])
        self.hint.setObjectName("cardHint")
        self.hint.setWordWrap(True)
        self.hint.setMinimumHeight(30)
        lay.addWidget(self.hint)

        self.reclaim = ReclaimPanel(palette)
        self.reclaim.changed.connect(self.reclaimChanged.emit)
        self.reclaim.setVisible(False)
        lay.addWidget(self.reclaim)

        self._group.idClicked.connect(self._on_clicked)

    # -- 对外 -------------------------------------------------------------

    def _on_clicked(self, idx: int) -> None:
        strategy = self._index[idx]
        self._apply_visual(strategy)
        self.strategyChanged.emit(strategy)

    def strategy(self) -> Strategy:
        idx = max(0, self._group.checkedId())
        return self._index[idx]

    def set_strategy(self, strategy: Strategy) -> None:
        """程序化切换（恢复状态用）。**不发** ``strategyChanged``。"""
        if strategy in self._index:
            self._group.button(self._index.index(strategy)).setChecked(True)
            self._apply_visual(strategy)

    def _apply_visual(self, strategy: Strategy) -> None:
        self.hint.setText(STRATEGY_HELP[strategy])
        self.reclaim.setVisible(strategy is Strategy.RECLAIM)

    def setEnabled(self, enabled: bool) -> None:  # noqa: N802 - Qt 命名
        super().setEnabled(enabled)
