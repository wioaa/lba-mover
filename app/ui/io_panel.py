"""IO 加速设置（T05 要求 5）与迁移摘要三行（要求 6）。

两项硬性「不许美化」的约束：

* 介质徽章照实写：``HDD（旋转介质）`` / ``SSD`` / ``未知（保守模式）``，
  来源说明挂在 tooltip 上（``seek_penalty`` / ``bus_type`` / ``fallback``）；
* 缓冲必须显示**实际生效值**：内核单次搬运有上限，用户选了 1024 MB 而实际
  只能生效 256 MB 时，界面必须写 ``1024 MB → 实际生效 256 MB``，
  绝不能让用户以为 1 GB 全用上了。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.core.io_profile import PREWARM_MIN_BUFFER_MB, IoProfile
from app.core.models import (
    MEDIA_HDD,
    MEDIA_SSD,
    MEDIA_UNKNOWN,
    PlanStats,
    human_bytes,
)
from app.ui import cards, widgets
from app.win32.storage import MediaProbe

#: 缓冲预设（MB）—— 与设计 §4.4 的三档一致
BUFFER_PRESETS = (64, 256, 1024)
CUSTOM_INDEX = 3

MEDIA_TEXT = {
    MEDIA_HDD: "HDD（旋转介质）",
    MEDIA_SSD: "SSD",
    MEDIA_UNKNOWN: "未知（保守模式）",
}
MEDIA_KIND = {
    MEDIA_HDD: "success",
    MEDIA_SSD: "accent",
    MEDIA_UNKNOWN: "warning",
}
SOURCE_TEXT = {
    "seek_penalty": "驱动报告 StorageDeviceSeekPenaltyProperty = 有/无寻道惩罚",
    "bus_type": "驱动未实现 SeekPenalty 查询，退按总线类型推断",
    "fallback": "两种查询都不可用，按未知处理（保守策略）",
}


class IoPanel(QWidget):
    """介质徽章 + 缓冲预设 + 预读预热开关 + 诚实边界说明。"""

    profileChanged = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._probe = MediaProbe()
        self._bytes_per_cluster = 0
        self._window_clusters = 0
        self._guard = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # ---------------- 介质 ---------------- #
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        head.addWidget(cards.IconLabel("drive", 16, "accent"), 0,
                       Qt.AlignmentFlag.AlignVCenter)
        head.addWidget(cards.label("IO 加速", "h2"), 0, Qt.AlignmentFlag.AlignVCenter)
        self._media_badge = cards.Badge(MEDIA_TEXT[MEDIA_UNKNOWN], "warning", self)
        head.addWidget(self._media_badge, 0, Qt.AlignmentFlag.AlignVCenter)
        head.addStretch(1)
        layout.addLayout(head)

        self._media_note = cards.label("", "caption", self, True)
        layout.addWidget(self._media_note)

        # ---------------- 缓冲 ---------------- #
        layout.addWidget(cards.Divider())
        layout.addWidget(cards.label("批次缓冲", "dim", self))
        self._buffer_switch = cards.SegmentedControl(
            [f"{value} MB" for value in BUFFER_PRESETS] + ["自定义"], 1, self
        )
        self._buffer_switch.changed.connect(self._on_buffer_preset)
        layout.addWidget(self._buffer_switch)

        self._custom_row = QWidget(self)
        custom_layout = QHBoxLayout(self._custom_row)
        custom_layout.setContentsMargins(0, 0, 0, 0)
        custom_layout.setSpacing(8)
        custom_layout.addWidget(cards.label("自定义值", "dim", self._custom_row), 0)
        self._custom_spin = QSpinBox(self._custom_row)
        self._custom_spin.setRange(16, 4096)
        self._custom_spin.setValue(512)
        self._custom_spin.setSuffix(" MB")
        self._custom_spin.setFixedWidth(112)
        self._custom_spin.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._custom_spin.valueChanged.connect(self._on_custom_value)
        custom_layout.addWidget(self._custom_spin, 0)
        custom_layout.addStretch(1)
        self._custom_row.setVisible(False)
        layout.addWidget(self._custom_row)

        self._effective_label = cards.label("", "caption", self, True)
        layout.addWidget(self._effective_label)

        # ---------------- 预读预热 ---------------- #
        self._prewarm_switch = widgets.ToggleSwitch("预读预热", self)
        self._prewarm_switch.set_checked_silent(True)
        self._prewarm_switch.toggled.connect(self._on_prewarm_toggled)
        layout.addWidget(self._prewarm_switch, 0, Qt.AlignmentFlag.AlignLeft)
        self._prewarm_hint = cards.label("", "caption", self, True)
        layout.addWidget(self._prewarm_hint)

        self._honest_note = cards.label("", "caption", self, True)
        self._honest_note.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum
        )
        layout.addWidget(self._honest_note)

        self._refresh()

    # ------------------------------------------------------------------ #
    # 接口
    # ------------------------------------------------------------------ #
    def set_volume(
        self,
        probe: MediaProbe,
        bytes_per_cluster: int,
        window_clusters: int = 0,
    ) -> None:
        """设置介质探测结果与卷参数。"""
        self._probe = probe or MediaProbe()
        self._bytes_per_cluster = max(0, int(bytes_per_cluster))
        self._window_clusters = max(0, int(window_clusters))
        self._refresh()

    def set_window_clusters(self, window_clusters: int) -> None:
        """目标区间变化时更新（批次上限会被区间大小夹紧）。"""
        self._window_clusters = max(0, int(window_clusters))
        self._refresh()

    def buffer_mb(self) -> int:
        index = self._buffer_switch.current_index
        if index == CUSTOM_INDEX:
            return int(self._custom_spin.value())
        if 0 <= index < len(BUFFER_PRESETS):
            return int(BUFFER_PRESETS[index])
        return 256

    def prefer_prewarm(self) -> bool:
        return bool(self._prewarm_switch.isChecked())

    def profile(self) -> IoProfile:
        """按当前 UI 状态构造 IO 策略（唯一可信来源）。"""
        return IoProfile.build(
            self._probe.media,
            self.buffer_mb(),
            self._bytes_per_cluster,
            prefer_prewarm=self.prefer_prewarm(),
            window_clusters=self._window_clusters or None,
        )

    def set_busy(self, busy: bool) -> None:
        self._buffer_switch.setEnabled(not busy)
        self._custom_spin.setEnabled(not busy)
        self._prewarm_switch.setEnabled(not busy)

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _on_buffer_preset(self, index: int) -> None:
        self._custom_row.setVisible(index == CUSTOM_INDEX)
        self._refresh()
        self.profileChanged.emit()

    def _on_custom_value(self, _value: int) -> None:
        self._refresh()
        self.profileChanged.emit()

    def _on_prewarm_toggled(self, _checked: bool) -> None:
        self._refresh()
        self.profileChanged.emit()

    def _refresh(self) -> None:
        if self._guard:
            return
        self._guard = True
        profile = self.profile()
        media = profile.media

        # ---- 介质徽章（照实写）----
        self._media_badge.set_state(MEDIA_TEXT.get(media, MEDIA_TEXT[MEDIA_UNKNOWN]),
                                    MEDIA_KIND.get(media, "warning"))
        source = SOURCE_TEXT.get(self._probe.source, self._probe.source)
        note = self._probe.describe()
        if source:
            note = f"{note}｜{source}"
        self._media_note.setText(note)

        # ---- 实际生效值（不美化）----
        requested = self.buffer_mb()
        self._effective_label.setText(
            f"请求 {requested} MB → {profile.cap_note}"
            f"（批次 {profile.batch_clusters:,} 簇 ≈ {human_bytes(profile.batch_bytes)}）"
        )

        # ---- 预读预热可用性 ----
        allowed, reason = self._prewarm_availability(profile)
        self._prewarm_switch.setEnabled(allowed)
        if allowed:
            self._prewarm_hint.setText(
                "把 MOVE 的读方向变成缓存命中，在 HDD 上能减少磁头换向；"
                "只读预热，不写一个字节。"
            )
        else:
            self._prewarm_hint.setText(reason)
        self._prewarm_hint.setToolTip(reason if reason else "")

        self._honest_note.setText(profile.honest_note)
        self._guard = False

    def _prewarm_availability(self, profile: IoProfile) -> tuple:
        """返回 ``(是否可用, 不可用原因)``。"""
        media = profile.media
        buffer_mb = self.buffer_mb()
        if media == MEDIA_SSD:
            return False, (
                "SSD 上额外的顺序读是纯成本、不减少任何寻道，已禁用预读预热。"
            )
        if media == MEDIA_UNKNOWN:
            return False, (
                "介质未知，无法判断预读是否有收益，按保守策略禁用（避免无谓 IO）。"
            )
        if buffer_mb < PREWARM_MIN_BUFFER_MB:
            return False, (
                f"缓冲需 ≥ {PREWARM_MIN_BUFFER_MB} MB 才启用预热"
                f"（当前 {buffer_mb} MB，预热块太小反而增加寻道）。"
            )
        return True, ""


class PlanSummaryPanel(QWidget):
    """迁移摘要：装入 / 驱逐 / 中转 三行 + 合计（R2 落点）。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._bytes_per_cluster = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        head.addWidget(cards.IconLabel("layers", 16, "accent"), 0,
                       Qt.AlignmentFlag.AlignVCenter)
        head.addWidget(cards.label("迁移摘要", "h2"), 0, Qt.AlignmentFlag.AlignVCenter)
        head.addStretch(1)
        layout.addLayout(head)

        self._stat_files = cards.StatRow("选中文件", "0 个", self)
        self._stat_bytes = cards.StatRow("净数据量", "0 B", self)
        self._stat_place = cards.StatRow("装入", "—", self)
        self._stat_evict = cards.StatRow("驱逐", "—", self)
        self._stat_relay = cards.StatRow("中转", "—", self)
        for stat in (
            self._stat_files,
            self._stat_bytes,
            self._stat_place,
            self._stat_evict,
            self._stat_relay,
        ):
            layout.addWidget(stat)

        layout.addWidget(cards.Divider())
        self._stat_total = cards.StatRow("合计需搬运", "—", self)
        layout.addWidget(self._stat_total)

        self._note = cards.label("", "caption", self, True)
        layout.addWidget(self._note)

        self.clear()

    # ------------------------------------------------------------------ #
    def set_bytes_per_cluster(self, value: int) -> None:
        self._bytes_per_cluster = max(0, int(value))

    def clear(self) -> None:
        """回到未规划状态。"""
        self._stat_place.set_value("—")
        self._stat_evict.set_value("—")
        self._stat_relay.set_value("—")
        self._stat_total.set_value("—")
        self._note.setText("")

    def set_selection(self, files: int, total_bytes: int) -> None:
        """选中文件与被选净数据量（不含驱逐与中转）。"""
        self._stat_files.set_value(f"{int(files)} 个")
        self._stat_bytes.set_value(human_bytes(int(total_bytes)))

    def set_stats(self, stats: PlanStats, note: str = "") -> None:
        """三行拆分 + 合计。``relay`` 是 ``place`` 的子集，合计不得重复计数。"""
        if stats is None:
            self.clear()
            return
        bpc = self._bytes_per_cluster

        def line(clusters: int, files: int) -> str:
            if clusters <= 0:
                return "0 簇"
            span = clusters * bpc if bpc > 0 else 0
            if files > 0:
                return f"{files} 个 · {clusters:,} 簇 ≈ {human_bytes(span)}"
            return f"{clusters:,} 簇 ≈ {human_bytes(span)}"

        self._stat_place.set_value(line(stats.place_clusters, stats.place_files))
        self._stat_evict.set_value(line(stats.evict_clusters, stats.evict_files))
        if stats.relay_clusters > 0:
            self._stat_relay.set_value(
                f"{stats.relay_files} 个 · {stats.relay_clusters:,} 簇"
                f"（已含在装入内，不重复计）"
            )
        else:
            self._stat_relay.set_value("0 簇")
        total = stats.total_clusters
        span = total * bpc if bpc > 0 else 0
        self._stat_total.set_value(
            f"{total:,} 簇 ≈ {human_bytes(span)}", "h2"
        )
        self._note.setText(note)
