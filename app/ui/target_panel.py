"""目标区间面板（T05 要求 2 / 3 / 4 / 8）。

四块内容：

1. **目标区间两种设定方式并存**：``任意区间``（起点/终点 LCN + 实时 GB 换算）
   与 ``快捷``（卷尾倒数 N GB / 从 LCN X 起连续 N GB）；
2. **可行性三档内联反馈**（绿/琥珀/红 + 量化原因与建议，**不弹窗**）；
3. **挡路者可展开清单**（文件名/大小/碎片数/搬迁成本，支持逐个排除；
   句柄通道下如实显示「归属信息不可用 + 原因」，绝不隐藏）；
4. **保留区设置**（卷首/卷尾滑块 + 数字框联动，下界由
   :func:`app.core.safety.clamp_reserve_mb` 给出，物理上无法输入 0）。
"""

from __future__ import annotations

from typing import FrozenSet, Iterable, List, Optional, Sequence, Tuple

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QHBoxLayout,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.core.models import (
    GB,
    MB,
    VERDICT_DIRECT,
    VERDICT_EVICT,
    Blocker,
    FeasibilityReport,
    human_bytes,
)
from app.core.packer import gb_to_clusters, window_from_length, window_from_tail
from app.core.safety import clamp_reserve_mb, min_reserve_clusters
from app.ui import cards, widgets

MODE_ANY = "any"
MODE_QUICK = "quick"

QUICK_TAIL = 0
QUICK_FROM = 1

#: 挡路者清单最多列出多少个（其余折叠为「还有 N 个」）
MAX_BLOCKER_ROWS = 8


class TargetPanel(QWidget):
    """目标区间 + 可行性 + 挡路者 + 保留区。"""

    windowChanged = Signal()
    excludeChanged = Signal(object)      # FrozenSet[str]
    reserveChanged = Signal(int, int)    # 卷首 MB, 卷尾 MB
    modeChanged = Signal(str)            # MODE_ANY / MODE_QUICK
    strategyChanged = Signal(str)        # range / auto_fill / compact / zone

    #: 规划策略：标签 → PlanWorker mode（1.0.0C 新增）
    _STRATEGIES = (
        ("区间装箱", "range"),
        ("自动填满末端", "auto_fill"),
        ("区间预密实化", "compact"),
        ("按大小分区", "zone"),
    )

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._total_clusters = 0
        self._bytes_per_cluster = 0
        self._excluded: set = set()
        self._blockers: List[Blocker] = []
        self._guard = False
        self._report: Optional[FeasibilityReport] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        # ================= 目标区间 ================= #
        layout.addWidget(self._build_window_block())
        # ================= 可行性 ================= #
        layout.addWidget(self._build_feasibility_block())
        # ================= 挡路者 ================= #
        layout.addWidget(self._build_blocker_block())
        # ================= 保留区 ================= #
        layout.addWidget(self._build_reserve_block())

    # ------------------------------------------------------------------ #
    # 构建
    # ------------------------------------------------------------------ #
    def _build_window_block(self) -> QWidget:
        holder = QWidget(self)
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        head.addWidget(cards.IconLabel("layers", 16, "accent"), 0,
                       Qt.AlignmentFlag.AlignVCenter)
        head.addWidget(cards.label("目标区间", "h2"), 0, Qt.AlignmentFlag.AlignVCenter)
        self._window_badge = cards.Badge("未设定", "neutral", holder)
        head.addWidget(self._window_badge, 0, Qt.AlignmentFlag.AlignVCenter)
        head.addStretch(1)
        column.addLayout(head)

        # ---- 规划策略（1.0.0C 新增）----
        strat_row = QHBoxLayout()
        strat_row.setContentsMargins(0, 0, 0, 0)
        strat_row.setSpacing(8)
        strat_row.addWidget(cards.label("规划策略", "dim", holder), 0)
        self._strategy_combo = cards.SoftComboBox(holder)
        for _label, _mode in self._STRATEGIES:
            self._strategy_combo.addItem(_label)
        self._strategy_combo.currentIndexChanged.connect(self._on_strategy_changed)
        strat_row.addWidget(self._strategy_combo, 1)
        column.addLayout(strat_row)
        self._strategy_hint = cards.label(
            "「自动填满末端」按卷尾保留区自动挑选、无需区间；其余策略用上方目标区间。",
            "caption", holder, True,
        )
        column.addWidget(self._strategy_hint)

        self._mode_switch = cards.SegmentedControl(["任意区间", "快捷"], 0, holder)
        self._mode_switch.changed.connect(self._on_mode_changed)
        column.addWidget(self._mode_switch)

        # ---- 任意区间 ----
        self._any_box = QWidget(holder)
        any_layout = QVBoxLayout(self._any_box)
        any_layout.setContentsMargins(0, 0, 0, 0)
        any_layout.setSpacing(6)
        self._range_input = widgets.LCNRangeInput(self._any_box)
        self._range_input.changed.connect(self._on_range_input)
        any_layout.addWidget(self._range_input)
        tip = cards.label(
            "也可以在下方磁盘布局图上直接拖选 —— 两端会与这里双向同步。",
            "caption",
            self._any_box,
            True,
        )
        any_layout.addWidget(tip)
        column.addWidget(self._any_box)

        # ---- 快捷 ----
        self._quick_box = QWidget(holder)
        quick_layout = QVBoxLayout(self._quick_box)
        quick_layout.setContentsMargins(0, 0, 0, 0)
        quick_layout.setSpacing(6)

        row_quick = QHBoxLayout()
        row_quick.setContentsMargins(0, 0, 0, 0)
        row_quick.setSpacing(8)
        self._quick_combo = cards.SoftComboBox(self._quick_box)
        self._quick_combo.addItem("卷尾倒数 N GB")
        self._quick_combo.addItem("从 LCN X 起连续 N GB")
        self._quick_combo.currentIndexChanged.connect(self._recompute_quick)
        row_quick.addWidget(self._quick_combo, 1)

        self._quick_gb = _gb_spin(self._quick_box, 24)
        row_quick.addWidget(self._quick_gb, 0)
        quick_layout.addLayout(row_quick)

        row_from = QHBoxLayout()
        row_from.setContentsMargins(0, 0, 0, 0)
        row_from.setSpacing(8)
        self._quick_start_label = cards.label("起始 LCN", "dim", self._quick_box)
        self._quick_start = _lcn_spin(self._quick_box)
        row_from.addWidget(self._quick_start_label, 0)
        row_from.addWidget(self._quick_start, 1)
        quick_layout.addLayout(row_from)

        self._quick_result = cards.label("", "caption", self._quick_box, True)
        quick_layout.addWidget(self._quick_result)
        column.addWidget(self._quick_box)
        self._quick_box.setVisible(False)

        self._quick_gb.valueChanged.connect(self._recompute_quick)
        self._quick_start.valueChanged.connect(self._recompute_quick)
        return holder

    def _build_feasibility_block(self) -> QWidget:
        self._status = widgets.InlineStatus(self)
        self._status.clear()
        return self._status

    def _build_blocker_block(self) -> QWidget:
        """挡路者：默认收起，展开后列出前 N 个并可逐个排除。"""
        self._blockers_section = widgets.CollapsibleSection(
            "挡路者（0 个）", self, expanded=False
        )
        self._blocker_list = BlockerList(self._blockers_section.content())
        self._blocker_list.excludeToggled.connect(self._on_exclude_toggled)
        self._blockers_section.content_layout().addWidget(self._blocker_list)
        self._blockers_section.setVisible(False)
        return self._blockers_section

    def _build_reserve_block(self) -> QWidget:
        holder = QWidget(self)
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        head.addWidget(cards.IconLabel("lock", 16, "accent"), 0,
                       Qt.AlignmentFlag.AlignVCenter)
        head.addWidget(cards.label("保留区", "h2"), 0, Qt.AlignmentFlag.AlignVCenter)
        self._reserve_badge = cards.Badge("", "neutral", holder)
        head.addWidget(self._reserve_badge, 0, Qt.AlignmentFlag.AlignVCenter)
        head.addStretch(1)
        column.addLayout(head)

        self._head_field = cards.SliderField(1, 1024, 16, " MB", holder)
        self._head_field.setToolTip(
            "卷首保留区：留给分区表备份、引导代码等。下界由钳制函数给出，"
            "本工具不允许把它调到 0 —— 那是把自己逼进危险区。"
        )
        self._head_field.valueChanged.connect(self._on_reserve_changed)
        column.addWidget(_labelled("卷首保留", self._head_field, holder))

        self._tail_field = cards.SliderField(1, 1024, 16, " MB", holder)
        self._tail_field.setToolTip(
            "卷尾保留区：文件不会被安排到这里，可用于后续扩容 / 快照。"
        )
        self._tail_field.valueChanged.connect(self._on_reserve_changed)
        column.addWidget(_labelled("卷尾保留", self._tail_field, holder))

        self._reserve_hint = cards.label("", "caption", holder, True)
        column.addWidget(self._reserve_hint)

        self._partition_note = cards.label(
            "分区表位于卷之外（卷起始偏移 > 0），本工具物理上无法触及，"
            "因此不存在改坏分区表的风险。",
            "caption",
            holder,
            True,
        )
        column.addWidget(self._partition_note)
        return holder

    # ------------------------------------------------------------------ #
    # 卷参数
    # ------------------------------------------------------------------ #
    def set_volume(self, total_clusters: int, bytes_per_cluster: int) -> None:
        """切换卷时重置全部与卷相关的上下界。"""
        self._total_clusters = max(0, int(total_clusters))
        self._bytes_per_cluster = max(0, int(bytes_per_cluster))
        self._excluded.clear()
        self._blockers = []
        self._report = None
        self._status.clear()
        self._blockers_section.setVisible(False)
        self._blocker_list.set_blockers([], set())

        self._range_input.set_unit(self._bytes_per_cluster)
        self._range_input.set_bounds(self._total_clusters)
        self._range_input.set_range_values(0, self._total_clusters)

        capacity_gb = max(
            1,
            int(self._total_clusters * self._bytes_per_cluster / GB)
            if self._bytes_per_cluster
            else 1,
        )
        self._guard = True
        self._quick_gb.setRange(1, capacity_gb)
        self._quick_start.setRange(0, max(0, self._total_clusters - 1))
        self._guard = False

        # 保留区上下界：下界来自 clamp_reserve_mb（不允许 0）
        min_mb = clamp_reserve_mb(0, self._bytes_per_cluster, self._total_clusters)
        max_mb = max(
            min_mb,
            int(self._total_clusters * self._bytes_per_cluster / MB) if self._bytes_per_cluster else 1024,
        )
        max_mb = max(1, min(max_mb, 1_048_576))
        for field in (self._head_field, self._tail_field):
            field.set_range(min_mb, max_mb)
            if field.value() < min_mb or field.value() > max_mb:
                field.set_value(min(min_mb, max_mb))
        self._refresh_reserve_hint()
        self._refresh_window_badge()

    def set_reserve_defaults(self, head_mb: int, tail_mb: int) -> None:
        """设置初始保留区（来自 ``safety.default_head_mb`` 等）。"""
        self._guard = True
        self._head_field.set_value(max(1, int(head_mb)))
        self._tail_field.set_value(max(1, int(tail_mb)))
        self._guard = False
        self._refresh_reserve_hint()

    # ------------------------------------------------------------------ #
    # 目标区间
    # ------------------------------------------------------------------ #
    def mode(self) -> str:
        return MODE_QUICK if self._mode_switch.current_index == 1 else MODE_ANY

    def set_mode(self, mode: str) -> None:
        index = 1 if mode == MODE_QUICK else 0
        self._mode_switch.set_current(index, emit=False)
        self._sync_mode_visual()

    def strategy(self) -> str:
        """当前规划策略对应的 PlanWorker mode。"""
        index = self._strategy_combo.currentIndex()
        if 0 <= index < len(self._STRATEGIES):
            return self._STRATEGIES[index][1]
        return "range"

    def set_strategy(self, mode: str) -> None:
        for i, (_label, m) in enumerate(self._STRATEGIES):
            if m == mode:
                self._strategy_combo.setCurrentIndex(i)
                return

    def _on_strategy_changed(self, _index: int) -> None:
        self.strategyChanged.emit(self.strategy())

    def window(self) -> Tuple[int, int]:
        """当前目标区间 ``[start, end)``。"""
        if self.mode() == MODE_QUICK:
            window = self._quick_window()
        else:
            start, end = self._range_input.values()
            window = (start, end)
        start, end = window
        start = max(0, min(start, self._total_clusters))
        end = max(start, min(end, self._total_clusters))
        return start, end

    def set_window(self, start: int, end: int) -> None:
        """程序性设置目标区间（布局图拖选 → 面板）。"""
        start, end = int(start), int(end)
        if end < start:
            start, end = end, start
        self._range_input.set_range_values(start, end)
        if self.mode() == MODE_QUICK:
            # 拖选代表了更明确的意图，自动切回「任意区间」
            self._mode_switch.set_current(0, emit=False)
            self._sync_mode_visual()
        self._refresh_window_badge()

    def _quick_window(self) -> Tuple[int, int]:
        clusters = gb_to_clusters(int(self._quick_gb.value()), self._bytes_per_cluster)
        if self._quick_combo.currentIndex() == QUICK_TAIL:
            tail_guard = self.reserve_tail_clusters()
            win = window_from_tail(self._total_clusters, clusters, tail_guard)
        else:
            win = window_from_length(
                int(self._quick_start.value()), clusters, self._total_clusters
            )
        return win.start_lcn, win.end_lcn

    def _recompute_quick(self, *_args) -> None:
        if self._guard:
            return
        is_tail = self._quick_combo.currentIndex() == QUICK_TAIL
        self._quick_start_label.setVisible(not is_tail)
        self._quick_start.setVisible(not is_tail)
        start, end = self._quick_window()
        clusters = max(0, end - start)
        span = clusters * self._bytes_per_cluster
        self._quick_result.setText(
            f"= LCN {start:,} – {max(start, end - 1):,} · {clusters:,} 簇 ≈ {human_bytes(span)}"
        )
        self._refresh_window_badge()
        if not self._guard:
            self.windowChanged.emit()

    def _on_mode_changed(self, _index: int) -> None:
        self._sync_mode_visual()
        if self.mode() == MODE_QUICK:
            self._recompute_quick()
        self.modeChanged.emit(self.mode())
        self.windowChanged.emit()

    def _sync_mode_visual(self) -> None:
        quick = self.mode() == MODE_QUICK
        self._any_box.setVisible(not quick)
        self._quick_box.setVisible(quick)
        if quick:
            self._recompute_quick()

    def _on_range_input(self, _start: int, _end: int) -> None:
        self._refresh_window_badge()
        self.windowChanged.emit()

    def _refresh_window_badge(self) -> None:
        if self._total_clusters <= 0:
            self._window_badge.set_state("未设定", "neutral")
            return
        start, end = self.window()
        clusters = max(0, end - start)
        if clusters <= 0:
            self._window_badge.set_state("区间为空", "danger")
            return
        span = clusters * self._bytes_per_cluster
        self._window_badge.set_state(
            f"{clusters:,} 簇 · {human_bytes(span)}", "accent"
        )

    # ------------------------------------------------------------------ #
    # 可行性
    # ------------------------------------------------------------------ #
    def set_feasibility(self, report: Optional[FeasibilityReport]) -> None:
        """三档内联反馈：绿 / 琥珀 / 红 + 量化原因 + 建议。"""
        self._report = report
        if report is None:
            self._status.clear()
            return

        if report.verdict == VERDICT_DIRECT:
            kind = "ok"
            head = (
                f"直接可行：区间可用 {report.usable_clusters:,} 簇，"
                f"选中文件需 {report.selected_clusters:,} 簇，无需驱逐"
            )
        elif report.verdict == VERDICT_EVICT:
            movable = len([b for b in report.blockers if b.movable])
            kind = "warn"
            head = (
                f"可行，需驱逐 {movable} 个文件：区间可用 {report.usable_clusters:,} 簇，"
                f"其中空闲 {report.free_in_window:,} 簇、可腾出 {report.evictable_clusters:,} 簇"
            )
        else:
            kind = "danger"
            head = (
                f"不可行：还差 {report.shortfall_clusters:,} 簇"
                f"（需 {report.selected_clusters:,} 簇，"
                f"最多可腾出 {report.usable_clusters:,} 簇）"
            )

        detail_parts: List[str] = []
        if report.reasons:
            detail_parts.append("原因：" + "；".join(report.reasons[:3]))
        if report.suggestions:
            detail_parts.append("建议：" + "；".join(report.suggestions[:2]))
        if report.unknown_owner_clusters > 0:
            detail_parts.append(
                f"另有 {report.unknown_owner_clusters:,} 簇归属未知（索引缺失），未计入可腾出空间"
            )
        self._status.set_state(
            kind,
            head,
            "\n".join(detail_parts),
            reason="\n".join(list(report.reasons) + list(report.suggestions)),
        )

    # ------------------------------------------------------------------ #
    # 挡路者
    # ------------------------------------------------------------------ #
    def excluded_paths(self) -> FrozenSet[str]:
        return frozenset(self._excluded)

    def set_blockers(self, blockers: Sequence[Blocker]) -> None:
        """填充挡路者清单（可展开面板 + 逐个排除）。"""
        self._blockers = list(blockers)
        movable = [b for b in self._blockers if b.movable]
        clusters = sum(b.clusters_in_window for b in movable)
        if not self._blockers:
            self._blockers_section.setVisible(False)
            self._blocker_list.set_blockers([], self._excluded)
            return
        span = clusters * self._bytes_per_cluster
        self._blockers_section.setVisible(True)
        self._blockers_section.set_title(
            f"挡路者（{len(self._blockers)} 个 · 可驱逐 {len(movable)} 个"
            f" · 需搬 {human_bytes(span)}）"
        )
        self._blocker_list.set_blockers(self._blockers, self._excluded)

    def set_blockers_unavailable(self, reason: str) -> None:
        """归属信息不可用（句柄通道）：**展示原因**而不是隐藏整块。"""
        self._blockers = []
        self._blockers_section.setVisible(True)
        self._blockers_section.set_title("挡路者（归属信息不可用）")
        self._blocker_list.set_unavailable(reason)

    def _on_exclude_toggled(self, path: str, excluded: bool) -> None:
        if excluded:
            self._excluded.add(path)
        else:
            self._excluded.discard(path)
        self.excludeChanged.emit(self.excluded_paths())

    # ------------------------------------------------------------------ #
    # 保留区
    # ------------------------------------------------------------------ #
    def reserve_mb(self) -> Tuple[int, int]:
        return int(self._head_field.value()), int(self._tail_field.value())

    def reserve_head_clusters(self) -> int:
        if self._bytes_per_cluster <= 0:
            return 0
        return int(self._head_field.value() * MB / self._bytes_per_cluster)

    def reserve_tail_clusters(self) -> int:
        if self._bytes_per_cluster <= 0:
            return 0
        return int(self._tail_field.value() * MB / self._bytes_per_cluster)

    def _on_reserve_changed(self, _value: int) -> None:
        self._refresh_reserve_hint()
        if self._guard:
            return
        head, tail = self.reserve_mb()
        self.reserveChanged.emit(head, tail)
        if self.mode() == MODE_QUICK:
            self._recompute_quick()

    def _refresh_reserve_hint(self) -> None:
        head, tail = self.reserve_mb()
        head_clusters = self.reserve_head_clusters()
        tail_clusters = self.reserve_tail_clusters()
        minimum = min_reserve_clusters(self._bytes_per_cluster)
        self._reserve_hint.setText(
            f"实际生效：卷首 {head_clusters:,} 簇 / 卷尾 {tail_clusters:,} 簇"
            f"（下界 {minimum:,} 簇，不允许为 0）"
        )
        self._reserve_badge.set_state(f"{head} + {tail} MB", "neutral")

    def set_partition_note(self, start_offset: int) -> None:
        """用真实的卷起始偏移替换分区表说明（``-1`` 表示取不到）。"""
        if start_offset > 0:
            self._partition_note.setText(
                f"分区表位于卷之外（卷起始偏移 {human_bytes(start_offset)}），"
                "本工具物理上无法触及。"
            )
        else:
            self._partition_note.setText(
                "分区表位于卷之外（卷起始偏移取不到时按“卷外”保守说明），"
                "本工具物理上无法触及。"
            )

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #
    def set_busy(self, busy: bool) -> None:
        self._mode_switch.setEnabled(not busy)
        self._range_input.setEnabled(not busy)
        self._quick_combo.setEnabled(not busy)
        self._quick_gb.setEnabled(not busy)
        self._quick_start.setEnabled(not busy)
        self._head_field.setEnabled(not busy)
        self._tail_field.setEnabled(not busy)

    def has_window(self) -> bool:
        start, end = self.window()
        return end - start > 0


def _gb_spin(parent: QWidget, value: int) -> QSpinBox:
    """GB 数字框（整数 GB，够用且不容易输错）。"""
    box = QSpinBox(parent)
    box.setRange(1, 100_000)
    box.setValue(value)
    box.setSuffix(" GB")
    box.setFixedWidth(104)
    box.setAlignment(Qt.AlignmentFlag.AlignRight)
    return box


def _lcn_spin(parent: QWidget) -> QSpinBox:
    """LCN 数字框。"""
    box = QSpinBox(parent)
    box.setRange(0, 2_000_000_000)
    box.setFixedWidth(128)
    box.setAlignment(Qt.AlignmentFlag.AlignRight)
    return box


def _labelled(title: str, field: QWidget, parent: QWidget) -> QWidget:
    holder = QWidget(parent)
    column = QVBoxLayout(holder)
    column.setContentsMargins(0, 0, 0, 0)
    column.setSpacing(2)
    column.addWidget(cards.label(title, "dim", holder))
    column.addWidget(field)
    return holder


class BlockerList(QWidget):
    """挡路者清单：文件名 / 大小 / 碎片数 / 搬迁成本 + 逐个排除。

    句柄通道下拿不到归属索引，此时**不隐藏**这一块，而是明确写出
    「归属信息不可用 + 原因」—— 隐藏会让人误以为"没有挡路者"。
    """

    excludeToggled = Signal(str, bool)

    HEADERS = ("文件", "大小", "碎片", "成本")

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._rows: List[QWidget] = []
        self._excluded: set = set()

        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)

        self._header = QWidget(self)
        header_row = QHBoxLayout(self._header)
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(6)
        for index, text in enumerate(self.HEADERS):
            item = cards.label(text, "caption", self._header)
            if index == 0:
                item.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
                header_row.addWidget(item, 1)
            else:
                item.setFixedWidth(64 if index < 3 else 52)
                item.setAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                )
                header_row.addWidget(item, 0)
        spacer = QWidget(self._header)
        spacer.setFixedWidth(46)
        header_row.addWidget(spacer, 0)
        column.addWidget(self._header)

        self._rows_box = QWidget(self)
        self._rows_layout = QVBoxLayout(self._rows_box)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(2)
        column.addWidget(self._rows_box)

        self._unavailable = cards.label("", "caption", self, True)
        self._unavailable.setVisible(False)
        column.addWidget(self._unavailable)

        self._more = cards.label("", "caption", self)
        self._more.setVisible(False)
        column.addWidget(self._more)
        self.set_blockers([], set())

    # ------------------------------------------------------------------ #
    def set_unavailable(self, reason: str) -> None:
        """归属信息不可用时调用（句柄通道）。"""
        self._clear_rows()
        self._header.setVisible(False)
        self._more.setVisible(False)
        self._unavailable.setText(reason)
        self._unavailable.setVisible(bool(reason))

    def set_blockers(self, blockers: Sequence[Blocker], excluded: Iterable[str] = ()) -> None:
        """重建行（最多 :data:`MAX_BLOCKER_ROWS` 行）。"""
        self._excluded = set(excluded)
        self._clear_rows()
        self._unavailable.setVisible(False)
        if not blockers:
            self._header.setVisible(False)
            self._more.setVisible(False)
            self._unavailable.setText("")
            return

        self._header.setVisible(True)
        shown = list(blockers)[:MAX_BLOCKER_ROWS]
        for blocker in shown:
            self._rows_layout.addWidget(self._build_row(blocker))
        remaining = len(blockers) - len(shown)
        if remaining > 0:
            self._more.setText(f"…… 还有 {remaining} 个挡路文件未列出")
            self._more.setVisible(True)
        else:
            self._more.setVisible(False)

    def _build_row(self, blocker: Blocker) -> QWidget:
        row = QWidget(self._rows_box)
        row.setFixedHeight(24)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        name = cards.label("", "body", row)
        name.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        name.setToolTip(
            f"{blocker.path}\n"
            f"大小 {human_bytes(blocker.size)} · 卷内共 {blocker.total_clusters:,} 簇\n"
            f"挡路 {blocker.clusters_in_window:,} 簇 / {blocker.fragments_in_window} 段\n"
            f"搬迁成本 {blocker.cost:,.0f} 簇当量"
        )
        metrics = QFontMetrics(name.font())
        name.setText(
            metrics.elidedText(
                blocker.name or blocker.path, Qt.TextElideMode.ElideMiddle, 160
            )
        )
        layout.addWidget(name, 1)

        for text, width in (
            (human_bytes(blocker.size), 64),
            (f"{blocker.fragments_in_window} 段", 64),
            (f"{blocker.cost:,.0f}", 52),
        ):
            item = cards.label(text, "dim", row)
            item.setFixedWidth(width)
            item.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            layout.addWidget(item, 0)

        is_excluded = blocker.path in self._excluded
        button = QPushButton("恢复" if is_excluded else "排除", row)
        button.setProperty("role", "ghost")
        button.setFixedWidth(46)
        button.setFixedHeight(22)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        if not blocker.movable and not is_excluded:
            button.setEnabled(False)
            button.setToolTip(blocker.reason or "该文件不可搬运")
        elif not is_excluded:
            button.setToolTip("把这个文件从「可驱逐」里排除，可行性会立即重算")
        button.clicked.connect(
            lambda _checked=False, path=blocker.path, state=is_excluded: self._toggle(
                path, not state
            )
        )
        layout.addWidget(button, 0)
        self._rows.append(row)
        return row

    def _toggle(self, path: str, excluded: bool) -> None:
        if excluded:
            self._excluded.add(path)
        else:
            self._excluded.discard(path)
        self.excludeToggled.emit(path, excluded)

    def _clear_rows(self) -> None:
        for row in self._rows:
            self._rows_layout.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()
