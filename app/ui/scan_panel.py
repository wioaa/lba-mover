"""扫描控制区（T05 要求 1）：通道选择 + 扫描统计条 + 回退显式告警。

三个必须成立的事实：

1. **通道可由用户显式选择**（MFT 快速扫描 / 句柄扫描），复用 v1 自绘分段控件；
2. **统计条必须常显**：记录总数 / 有效 / 文件数 / 跳过 / 解析异常 / 抽样复核；
   异常数 > 0 时用警告色，明细挂在 tooltip 上（不占版面，但一定看得见）；
3. **自动回退必须显式可见**（D5）：琥珀色徽章 + 一行中文原因 +
   能力差异说明（D8：句柄通道拿不到归属信息 → 挡路者自动驱逐不可用）。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.core.mft import fallback_message
from app.ui import cards, theme

CHANNEL_MFT = "mft"
CHANNEL_HANDLE = "handle"

#: D8：句柄通道的能力差异（必须如实展示，不允许隐藏）
D8_NOTE = (
    "挡路者自动驱逐不可用：句柄通道取不到「哪些文件占用了目标区间的簇」，"
    "因此规划阶段无法判断谁挡路、更无法自动迁出。仍可手工取消勾选后重试，"
    "或改用 MFT 快速扫描（需管理员权限 + NTFS）。"
)


class ScanStatChip(QWidget):
    """统计条中的一格：上方小字名称，下方数值。"""

    def __init__(self, name: str, value: str = "—", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._name = name
        self._value = value
        self._kind = "neutral"
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def set_value(self, value: str, kind: str = "neutral") -> None:
        self._value = value
        self._kind = kind if kind in ("neutral", "warning", "danger", "success", "accent") else "neutral"
        self.updateGeometry()
        self.update()

    def _fonts(self) -> tuple:
        font_name = QFont(theme.app_font(10))
        font_value = QFont(theme.app_font(12, QFont.Weight.DemiBold))
        return font_name, font_value, QFontMetrics(font_name), QFontMetrics(font_value)

    def sizeHint(self) -> QSize:  # noqa: D102
        _fn, _fv, name_m, value_m = self._fonts()
        width = max(name_m.horizontalAdvance(self._name), value_m.horizontalAdvance(self._value))
        return QSize(width + 20, name_m.height() + value_m.height() + 6)

    def minimumSizeHint(self) -> QSize:  # noqa: D102
        return self.sizeHint()

    def paintEvent(self, event) -> None:  # noqa: D102
        palette = theme.palette()
        font_name, font_value, name_m, value_m = self._fonts()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        rect = QRectF(self.rect())
        painter.setFont(font_name)
        painter.setPen(QColor(palette["text_faint"]))
        painter.drawText(
            QRectF(0, 0, rect.width(), name_m.height()),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            name_m.elidedText(
                self._name, Qt.TextElideMode.ElideRight, int(rect.width())
            ),
        )

        color_key = "text" if self._kind == "neutral" else self._kind
        painter.setFont(font_value)
        painter.setPen(QColor(palette.get(color_key, palette["text"])))
        painter.drawText(
            QRectF(0, name_m.height(), rect.width(), value_m.height() + 2),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            value_m.elidedText(
                self._value, Qt.TextElideMode.ElideRight, int(rect.width())
            ),
        )
        painter.end()


class ScanControlPanel(QWidget):
    """扫描控制区：通道选择 + 统计条 + 回退告警。"""

    channelChanged = Signal(str)

    STAT_NAMES = (
        ("records", "记录总数"),
        ("in_use", "有效记录"),
        ("files", "文件数"),
        ("skipped", "跳过"),
        ("anomalies", "解析异常"),
        ("sample", "抽样复核"),
    )

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._files_total = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # ---------------- 通道选择行 ---------------- #
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(10)
        head.addWidget(cards.IconLabel("scan", 16, "accent"), 0,
                       Qt.AlignmentFlag.AlignVCenter)
        head.addWidget(cards.label("扫描通道", "dim"), 0, Qt.AlignmentFlag.AlignVCenter)

        self._selector = cards.SegmentedControl(
            ["MFT 快速扫描（推荐）", "句柄扫描（兼容）"], 0, self
        )
        self._selector.setMinimumWidth(300)
        self._selector.setMaximumWidth(320)
        self._selector.changed.connect(self._on_channel_changed)
        head.addWidget(self._selector, 0)

        self._channel_badge = cards.Badge("MFT 通道", "success", self)
        head.addWidget(self._channel_badge, 0, Qt.AlignmentFlag.AlignVCenter)

        self._channel_hint = cards.label(
            "MFT 通道一次遍历同时得到文件表与归属索引，实测快 2 倍以上。",
            "caption",
            self,
        )
        head.addWidget(self._channel_hint, 1, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(head)

        # ---------------- 统计条 ---------------- #
        self._strip = QFrame(self)
        self._strip.setProperty("role", "sunken")
        self._strip.setVisible(False)
        strip_layout = QHBoxLayout(self._strip)
        strip_layout.setContentsMargins(12, 6, 12, 6)
        strip_layout.setSpacing(18)

        self._chips: dict = {}
        for key, name in self.STAT_NAMES:
            chip = ScanStatChip(name, "—", self._strip)
            self._chips[key] = chip
            strip_layout.addWidget(chip, 0)
        strip_layout.addStretch(1)

        self._strip_hint = cards.label("", "caption", self._strip)
        # 允许被压缩：统计条的 6 个 chip 已经承载了全部数字，
        # 这里只放通道与耗时，长文案一律进 tooltip（否则会把整条撑宽）
        self._strip_hint.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self._strip_hint.setMaximumWidth(180)
        strip_layout.addWidget(self._strip_hint, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self._strip)

        # ---------------- 回退告警 ---------------- #
        self._fallback_badge = cards.Badge("已回退句柄通道", "warning", self)
        self._fallback_badge.setVisible(False)

        banner = QFrame(self)
        banner.setProperty("role", "sunken")
        self._banner = banner
        banner_layout = QVBoxLayout(banner)
        banner_layout.setContentsMargins(12, 9, 12, 9)
        banner_layout.setSpacing(4)

        badge_row = QHBoxLayout()
        badge_row.setContentsMargins(0, 0, 0, 0)
        badge_row.setSpacing(8)
        badge_row.addWidget(self._fallback_badge, 0, Qt.AlignmentFlag.AlignVCenter)
        badge_row.addWidget(cards.label("能力差异（D8）", "h3"), 0,
                            Qt.AlignmentFlag.AlignVCenter)
        badge_row.addStretch(1)
        banner_layout.addLayout(badge_row)

        self._fallback_reason = cards.label("", "warning", banner, True)
        banner_layout.addWidget(self._fallback_reason)
        self._fallback_capability = cards.label(D8_NOTE, "caption", banner, True)
        banner_layout.addWidget(self._fallback_capability)

        banner.setVisible(False)
        layout.addWidget(banner)

    # ------------------------------------------------------------------ #
    # 接口
    # ------------------------------------------------------------------ #
    def channel(self) -> str:
        """当前选择的通道：``mft`` / ``handle``。"""
        return CHANNEL_HANDLE if self._selector.current_index == 1 else CHANNEL_MFT

    def set_channel(self, channel: str, emit: bool = False) -> None:
        """程序性设置通道。"""
        index = 1 if channel == CHANNEL_HANDLE else 0
        self._selector.set_current(index, emit=emit)
        self._sync_channel_visual()

    def set_busy(self, busy: bool) -> None:
        self._selector.setEnabled(not busy)

    def clear_stats(self) -> None:
        """清空统计条（切换卷 / 重新扫描前）。"""
        self._strip.setVisible(False)
        self._files_total = 0
        for chip in self._chips.values():
            chip.set_value("—")
            chip.setToolTip("")
        self._strip_hint.setText("")
        self.clear_fallback()

    def set_fallback(self, code: str, detail: str = "") -> None:
        """显示**自动回退**（琥珀色徽章 + 中文原因 + D8 能力差异）。"""
        if not code:
            self.clear_fallback()
            return
        message = fallback_message(code, detail)
        self._fallback_badge.set_state(f"已自动回退句柄通道（{code}）", "warning")
        self._fallback_badge.setVisible(True)
        self._fallback_reason.setText(message)
        self._banner.setVisible(True)

    def set_manual_handle_note(self) -> None:
        """用户**主动**选了句柄通道：同样展示 D8 能力差异，但不冒充「降级」。"""
        self._fallback_badge.set_state("用户选择句柄通道", "neutral")
        self._fallback_badge.setVisible(True)
        self._fallback_reason.setText(
            fallback_message("user_preferred_handle", "（主动选择，非自动降级）")
        )
        self._banner.setVisible(True)

    def set_stats(self, stats, matched: int = 0) -> None:
        """用 :class:`app.core.mft.MftScanStats` 填充统计条。"""
        if stats is None:
            self._strip.setVisible(False)
            return
        self._files_total = int(matched)
        anomalies = int(getattr(stats, "anomalies", 0))
        self._chips["records"].set_value(f"{getattr(stats, 'total_records', 0):,}")
        self._chips["in_use"].set_value(f"{getattr(stats, 'in_use_records', 0):,}")
        self._chips["files"].set_value(f"{int(matched):,}", "accent")
        self._chips["skipped"].set_value(f"{getattr(stats, 'skipped_total', 0):,}")
        if anomalies > 0:
            self._chips["anomalies"].set_value(f"{anomalies:,}", "warning")
            self._chips["anomalies"].setToolTip(stats.describe())
        else:
            self._chips["anomalies"].set_value("0", "success")
            self._chips["anomalies"].setToolTip(stats.describe())
        sample = stats.sample_text()
        mismatch = int(getattr(stats, "sample_mismatch", 0))
        kind = "neutral" if mismatch == 0 else "warning"
        self._chips["sample"].set_value(sample, kind)
        self._chips["sample"].setToolTip(stats.describe())
        self._strip_hint.setText(
            f"MFT 通道 · {getattr(stats, 'elapsed_seconds', 0.0):.1f} s"
        )
        self._strip.setToolTip(stats.describe())
        self._strip.setVisible(True)

    def set_handle_stats(self, files: int, extents: int, elapsed: float) -> None:
        """句柄通道没有 MFT 健康度，只报能干真的事（不假装有）。"""
        self._strip.setVisible(True)
        self._files_total = int(files)
        self._chips["records"].set_value("—")
        self._chips["in_use"].set_value(f"{int(files):,}")
        self._chips["files"].set_value(f"{int(files):,}", "accent")
        self._chips["skipped"].set_value("—")
        self._chips["anomalies"].set_value("—")
        self._chips["sample"].set_value("句柄通道无抽样")
        for key in ("records", "skipped", "anomalies"):
            self._chips[key].setToolTip("句柄通道不解析 MFT，该项无意义")
        self._strip_hint.setText(f"句柄通道 · {elapsed:.1f} s")
        self._strip.setToolTip(
            f"句柄通道：{int(files):,} 个文件 / {int(extents):,} 个 extent，"
            f"耗时 {elapsed:.1f} s。该通道不解析 MFT，没有解析健康度可报。"
        )

    def describe_channel(self) -> str:
        """一行中文，写日志用。"""
        if self.channel() == CHANNEL_MFT:
            return "MFT 快速扫描（直读 $MFT，只读）"
        return "句柄扫描（逐文件 CreateFile + retrieval pointers）"

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _on_channel_changed(self, _index: int) -> None:
        self._sync_channel_visual()
        self.channelChanged.emit(self.channel())

    def _sync_channel_visual(self) -> None:
        """只更新徽章与提示语；**不**动回退告警条。

        告警条由主窗口通过 :meth:`set_fallback` 控制 —— 自动降级与用户主动选择
        是两件事，视觉上不能混为一谈。
        """
        if self.channel() == CHANNEL_MFT:
            self._channel_badge.set_state("MFT 通道", "success")
            self._channel_hint.setText(
                "MFT 通道一次遍历同时得到文件表与归属索引，实测快 2 倍以上。"
            )
        else:
            self._channel_badge.set_state("句柄通道", "warning")
            self._channel_hint.setText(
                "句柄通道兼容性最好（非 NTFS / 非管理员也能用），但拿不到归属信息，"
                "且大卷需要更长时间。"
            )

    def clear_fallback(self) -> None:
        """清除回退告警（切卷 / 手动切回 MFT 时调用）。"""
        self._banner.setVisible(False)
        self._fallback_badge.setVisible(False)
        self._fallback_reason.setText("")
