"""策略选择：**模式**与**强度**是两行，互不干扰。

旧版把两件事绑死在策略里 —— ``COALESCE`` 只能作用于碎片文件，想动全部
文件就得换成 ``COMPACT``，而换策略会顺手把落点方向也改掉。于是"我想更
彻底一点"没有任何入口，用户只能猜，猜错就是几小时的重排。

现在拆成：上面一行选**强度**（动多少文件），下面选**模式**（往哪搬）。
五种模式 × 五档强度全部组合都成立，其中 ``RECLAIM`` / ``ANALYZE_ONLY``
是例外：前者是约束式任务（挡路的必须全搬，挑三拣四就会给出假结果），
后者根本不产生操作，所以它们选中时强度整行禁用并说明原因。

**排版说明。** 旧版五个策略若沿用"按钮里塞两行说明"，左栏会多出近 50px
而本来就刚好装满一屏。所以按钮只放 4~6 个字（两列网格），说明搬到下面
单独一行随选择更新。强度行同理：五个两字短名排一行，说明另占一行。
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...contract.commands import STRATEGY_LABELS
from ...contract.enums import FreeSpaceSide, Intensity, Strategy
from ..theme import DARK, Palette

__all__ = [
    "StrategyPanel",
    "ReclaimPanel",
    "PackPanel",
    "STRATEGY_HELP",
    "STRATEGY_SHORT",
    "STRATEGY_ORDER",
    "INTENSITY_HELP",
    "PACK_TAIL_INTENSITY_HELP",
    "INTENSITY_ORDER",
    "DEFAULT_INTENSITY",
]

#: 每个策略的"它会做什么"——比名字更能决定用户点哪个
STRATEGY_HELP: dict[Strategy, str] = {
    Strategy.ANALYZE_ONLY: "只扫描出报告，不写任何一块",
    Strategy.COALESCE: "把每个碎片文件重排成一段连续区",
    Strategy.COMPACT: "把文件往卷首挤，腾出尾部大块连续空间",
    Strategy.RELOCATE_TAIL: "把文件搬到卷尾，为卷首让位",
    Strategy.RECLAIM: "腾出指定大小的连续可用空间，位置可选卷首或卷尾",
    Strategy.PACK_TAIL: "把卷尾数据紧贴末尾重排，在它前面腾出一整块连续空闲",
}

#: 网格里显示的短名。4~6 个字，两列放得下，也不至于要靠猜。
STRATEGY_SHORT: dict[Strategy, str] = {
    Strategy.ANALYZE_ONLY: "只分析",
    Strategy.COALESCE: "合并碎片",
    Strategy.COMPACT: "向卷首聚拢",
    Strategy.RELOCATE_TAIL: "搬到卷尾",
    Strategy.RECLAIM: "腾出可用空间",
    Strategy.PACK_TAIL: "压实卷尾",
}

#: 按钮顺序。**索引是契约**：``set_strategy`` 与 UI 测试都按它定位，
#: 新增策略一律追加到末尾，不要插到中间。
STRATEGY_ORDER: tuple[Strategy, ...] = (
    Strategy.ANALYZE_ONLY,
    Strategy.COALESCE,
    Strategy.COMPACT,
    Strategy.RELOCATE_TAIL,
    Strategy.RECLAIM,
    Strategy.PACK_TAIL,
)

#: 强度顺序：从最省到最狠，界面按这个顺序排。
INTENSITY_ORDER: tuple[Intensity, ...] = (
    Intensity.FAST,
    Intensity.STANDARD,
    Intensity.DEEP,
    Intensity.THOROUGH,
    Intensity.EXTREME,
)

#: 每档"动多少文件"。这是用户真正在选的东西，所以写在可见的一行里，
#: 不是塞进 tooltip 就完事。
INTENSITY_HELP: dict[Intensity, str] = {
    Intensity.FAST: "只做最碎的 20%（最多 500 个），几分钟出结果",
    Intensity.STANDARD: "全部碎片文件，和旧版默认行为一致",
    Intensity.DEEP: "碎片文件 + 零散小文件，顺手消掉卷首的空洞",
    Intensity.THOROUGH: "全部文件重排，连本来连续的也一起挪",
    Intensity.EXTREME: "只动大文件，按体积从大到小依次归位",
}

#: 压实模式下强度的含义**与其它模式不同**，说明行必须换成这一套。
#:
#: 其它模式里强度回答"挑多少文件才有收益"，压实回答"为了完整性愿意付多少
#: 搬运量"。这不是文案差异：同一个"快速"在合并碎片里是"只做前 20%"，
#: 在压实里是"只搬多段文件"——沿用原文案会让用户按错误的预期点按钮。
PACK_TAIL_INTENSITY_HELP: dict[Intensity, str] = {
    Intensity.FAST: "只压多段（碎片）文件 —— 只有它们在自身地址空间里留洞",
    Intensity.STANDARD: "压实范围内全部数据，贴不紧时改放前面最近的空位",
    Intensity.DEEP: "同「标准」：压实不区分文件大小，多搬大文件只会更慢",
    Intensity.THOROUGH: "全部数据，且必须紧贴 —— 放不进去的文件就不动，不留空隙",
    Intensity.EXTREME: "只压不小于「大文件下限」的文件，一次性贴到卷尾",
}

#: 切模式时带出的默认强度。**不是绑定**——切完仍可自由改档位，这里只负责
#: 让"切换到某个模式"的初始结果符合直觉：
#: ``COMPACT`` 的旧语义是"全部文件都往卷首挤"，对应彻底档；
#: ``COALESCE`` / ``RELOCATE_TAIL`` 旧语义就是碎片文件，对应标准档。
DEFAULT_INTENSITY: dict[Strategy, Intensity] = {
    Strategy.ANALYZE_ONLY: Intensity.STANDARD,
    Strategy.COALESCE: Intensity.STANDARD,
    Strategy.COMPACT: Intensity.THOROUGH,
    Strategy.RELOCATE_TAIL: Intensity.STANDARD,
    Strategy.RECLAIM: Intensity.STANDARD,
    Strategy.PACK_TAIL: Intensity.STANDARD,
}

#: 这几档不看强度：两个约束式任务（必须动到范围内的**全部**数据，挑一部
#: 分就等于没做），一个不产生操作。
#:
#: ``PACK_TAIL`` **不在这里**（2.0.0K 起）。旧版把它一并禁用，理由是"压实
#: 要看范围内全部数据"。但实测下来，那个禁用让用户唯一能调的只剩"范围"，
#: 而真正需要调的恰恰是"要不要动大文件" —— 大文件贴不上去的时候，用户
#: 没有别的入口。现在它在压实下的语义另行定义，见
#: :data:`PACK_TAIL_INTENSITY_HELP`。
_INTENSITY_FREE = (Strategy.RECLAIM, Strategy.ANALYZE_ONLY)


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


class PackPanel(QWidget):
    """「压实卷尾」的范围：从卷尾往前算多少 GiB。

    0 用 ``setSpecialValueText`` 显示成「整卷」，而不是让用户猜"填 0 是
    什么意思"。整卷压实等于把全盘数据都搬到卷尾，几小时的重写 —— 所以它
    必须是能选到的，但不能是看起来像默认值的那个。
    """

    changed = Signal()

    def __init__(self, palette: Palette = DARK, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._p = palette
        self._block_size = 4096
        self._capacity_gib = 1.0
        self._suppress = False

        self.caption = QLabel("压实范围")
        self.caption.setObjectName("cardHint")

        self.size = QSpinBox()
        self.size.setRange(0, 1 << 20)
        self.size.setSuffix(" GiB")
        self.size.setSpecialValueText("整卷")
        self.size.setSingleStep(10)
        self.size.setValue(0)
        self.size.setMinimumHeight(26)
        self.size.setToolTip(
            "从卷尾往前算多少 GiB 参与压实。选「整卷」会把整个盘的数据都重排"
            "到卷尾，耗时以小时计"
        )

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(self.caption)
        lay.addWidget(self.size)
        lay.addStretch(1)

        self.size.valueChanged.connect(self._on_changed)
        self._update_tip()

    def set_geometry(self, block_size: int, block_count: int) -> None:
        """按目标卷校准换算与上限。**换卷必须重新调用**。"""
        self._block_size = max(1, block_size)
        self._capacity_gib = max(1.0, block_count * block_size / 1024**3)
        cap = max(1, int(self._capacity_gib))
        self._suppress = True
        self.size.setRange(0, cap)
        if self.size.value() > cap:
            self.size.setValue(cap)
        self._suppress = False
        self._update_tip()

    def blocks(self) -> int:
        """0 表示整卷（引擎里 ``pack_span_blocks=0`` 就是这个语义）。"""
        return int(self.size.value() * 1024**3 // self._block_size)

    def set_blocks(self, blocks: int) -> None:
        self._suppress = True
        gib = 0 if blocks <= 0 else max(1, round(blocks * self._block_size / 1024**3))
        self.size.setValue(min(gib, self.size.maximum()))
        self._suppress = False
        self._update_tip()

    def _on_changed(self, *_args) -> None:
        self._update_tip()
        if not self._suppress:
            self.changed.emit()

    def _update_tip(self) -> None:
        if self.size.value() == 0:
            self.caption.setToolTip(f"整卷（{self._capacity_gib:.1f} GiB）都参与压实")
        else:
            self.caption.setToolTip(
                f"卷容量 {self._capacity_gib:.1f} GiB，约 {self.blocks()} 块参与压实"
            )


class StrategyPanel(QWidget):
    strategyChanged = Signal(object)  # Strategy
    #: 强度档位变了 —— 影响"挑哪些文件"，必须重算预演
    intensityChanged = Signal(object)  # Intensity
    #: 极端档的"大文件下限"变了
    extremeMinChanged = Signal()
    #: 可用空间整理的大小/位置变了 —— 需要重算预演
    reclaimChanged = Signal()
    #: 压实范围变了
    packChanged = Signal()

    def __init__(self, palette: Palette = DARK, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._p = palette
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._index: list[Strategy] = list(STRATEGY_ORDER)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        # -- 强度行：一行五个两字短名 ----------------------------------
        # 单独包一层是为了整行一起禁用（约束式任务时强度不适用），
        # 而不是逐个按钮 setEnabled —— 那种写法漏一个就出现"半禁用"。
        #
        # **不放「强度」这个标签**：左栏只有 ~240px，加一个 30px 的标签
        # 就会把第 5 个按钮挤出可视区（实测：极端档被整个切掉，用户根本
        # 选不到）。维度名改由下面那行说明的前缀承担，那里是整行宽度。
        self.intensity_row = QWidget()
        irow = QHBoxLayout(self.intensity_row)
        irow.setContentsMargins(0, 0, 0, 0)
        irow.setSpacing(4)

        self._intensity_group = QButtonGroup(self)
        self._intensity_group.setExclusive(True)
        self._intensity_buttons: list[QPushButton] = []
        for i, inten in enumerate(INTENSITY_ORDER):
            btn = QPushButton(inten.label)
            btn.setObjectName("chip")
            btn.setCheckable(True)
            btn.setMinimumHeight(26)
            # 五档等宽铺满一行：宽度一致才像"刻度"，按文字宽度自适应会
            # 左边挤成一坨、右边空一大块，看不出这是一条从轻到重的谱。
            #
            # 必须显式压 ``minimumSize``：QSS 的 padding 会让每个按钮的
            # ``minimumSizeHint`` 撑到 81px，五个加起来正好跟面板可用宽度
            # 打平（421 vs 421），DPI 一缩放第 5 个就被裁掉 —— 用户看得见
            # "彻底"却选不到"极端"，而界面上没有任何提示。
            btn.setMinimumSize(40, 26)
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            btn.setToolTip(f"{inten.label}：{INTENSITY_HELP[inten]}")
            self._intensity_group.addButton(btn, i)
            self._intensity_buttons.append(btn)
            irow.addWidget(btn)
        self._intensity_buttons[INTENSITY_ORDER.index(Intensity.STANDARD)].setChecked(True)
        lay.addWidget(self.intensity_row)

        self.intensity_hint = QLabel(f"强度 · {INTENSITY_HELP[Intensity.STANDARD]}")
        self.intensity_hint.setObjectName("cardHint")
        self.intensity_hint.setWordWrap(True)
        self.intensity_hint.setMinimumHeight(22)
        self.intensity_hint.setToolTip(
            "强度决定动多少文件，与下面的模式正交：模式决定往哪搬，强度决定搬多大一片"
        )
        lay.addWidget(self.intensity_hint)

        # -- 模式网格 --------------------------------------------------
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

        self.pack = PackPanel(palette)
        self.pack.changed.connect(self.packChanged.emit)
        self.pack.setVisible(False)
        lay.addWidget(self.pack)

        # -- 极端档的"大文件下限"（只在极端档出现）--------------------
        self.extreme_row = QWidget()
        erow = QHBoxLayout(self.extreme_row)
        erow.setContentsMargins(0, 0, 0, 0)
        erow.setSpacing(6)
        elabel = QLabel("大文件下限")
        elabel.setObjectName("cardHint")
        self.extreme_min = QSpinBox()
        self.extreme_min.setRange(1, 1 << 20)
        self.extreme_min.setSuffix(" MiB")
        self.extreme_min.setSingleStep(64)
        self.extreme_min.setValue(256)
        self.extreme_min.setMinimumHeight(26)
        self.extreme_min.setToolTip(
            "只搬不小于这个大小的文件。卷越大越该往上调 —— 1 TiB 盘上 256 MiB "
            "只算中等文件，而 128 GiB 盘上它就是巨型文件"
        )
        erow.addWidget(elabel)
        erow.addWidget(self.extreme_min)
        erow.addStretch(1)
        self.extreme_row.setVisible(False)
        lay.addWidget(self.extreme_row)

        self._group.idClicked.connect(self._on_clicked)
        self._intensity_group.idClicked.connect(self._on_intensity_clicked)
        self.extreme_min.valueChanged.connect(self._on_extreme_min_changed)

    # -- 对外 -------------------------------------------------------------

    def _on_clicked(self, idx: int) -> None:
        strategy = self._index[idx]
        # 切模式时带出该模式的默认强度：旧语义里 ``COMPACT`` 就是"全部文件
        # 往卷首挤"，这里停在同一档才不会让升级变成一个静默的行为变更。
        # 带出之后用户仍可自由改档位，这不是绑定。
        self.set_intensity(DEFAULT_INTENSITY[strategy])
        self._apply_visual(strategy)
        self.strategyChanged.emit(strategy)
        self.intensityChanged.emit(self.intensity())

    def _on_intensity_clicked(self, idx: int) -> None:
        inten = INTENSITY_ORDER[idx]
        self._apply_intensity_visual(inten)
        self.intensityChanged.emit(inten)

    def _on_extreme_min_changed(self, _value: int) -> None:
        self.extremeMinChanged.emit()

    def strategy(self) -> Strategy:
        idx = max(0, self._group.checkedId())
        return self._index[idx]

    def intensity(self) -> Intensity:
        idx = max(0, self._intensity_group.checkedId())
        return INTENSITY_ORDER[idx]

    def extreme_min_mib(self) -> int:
        return int(self.extreme_min.value())

    def set_extreme_min_mib(self, value: int) -> None:
        self.extreme_min.blockSignals(True)
        self.extreme_min.setValue(max(1, int(value)))
        self.extreme_min.blockSignals(False)

    def set_strategy(self, strategy: Strategy) -> None:
        """程序化切换（恢复状态用）。**不发** ``strategyChanged``。"""
        if strategy in self._index:
            self._group.button(self._index.index(strategy)).setChecked(True)
            self._apply_visual(strategy)

    def set_intensity(self, intensity: Intensity) -> None:
        """程序化切换强度。**不发** ``intensityChanged``。"""
        if intensity in INTENSITY_ORDER:
            idx = INTENSITY_ORDER.index(intensity)
            self._intensity_buttons[idx].setChecked(True)
            self._apply_intensity_visual(intensity)

    def _apply_visual(self, strategy: Strategy) -> None:
        self.hint.setText(STRATEGY_HELP[strategy])
        self.reclaim.setVisible(strategy is Strategy.RECLAIM)
        self.pack.setVisible(strategy is Strategy.PACK_TAIL)
        self._apply_intensity_visual(self.intensity())

    def _apply_intensity_visual(self, intensity: Intensity) -> None:
        strategy = self.strategy()
        if strategy in _INTENSITY_FREE:
            # 这两档不适用强度。**禁用并说清原因**，不要留个能点但无效的控件 ——
            # 用户会以为"快速腾出空间"真的只搬 20%，然后按这个结果去规划下一步。
            self.intensity_row.setEnabled(False)
            if strategy is Strategy.RECLAIM:
                why = "强度 · 这项任务必须搬开所有挡路文件，档位不适用"
            else:
                why = "强度 · 只分析不搬任何文件，档位不适用"
            self.intensity_hint.setText(why)
            self.extreme_row.setVisible(False)
            return

        self.intensity_row.setEnabled(True)
        if strategy is Strategy.PACK_TAIL:
            # 压实有自己的一套档位语义，用通用文案会让人按错预期点按钮
            self.intensity_hint.setText(
                f"强度 · {PACK_TAIL_INTENSITY_HELP[intensity]}"
            )
        else:
            self.intensity_hint.setText(f"强度 · {INTENSITY_HELP[intensity]}")
        self.extreme_row.setVisible(intensity is Intensity.EXTREME)

    def setEnabled(self, enabled: bool) -> None:  # noqa: N802 - Qt 命名
        super().setEnabled(enabled)
