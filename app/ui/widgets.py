"""v2 新增的基础控件：开关、折叠面板、内联状态条、LCN 区间输入。

设计约定与 ``cards.py`` 一致：**全部自绘或半自绘**，配色一律在 ``paintEvent``
里从 ``theme.palette()`` 现取，因此深浅色切换后只需 ``update()`` 即生效。

这些控件刻意不依赖任何业务模型（不 import ``app.core.models``），
以保证 ``app/ui`` 内部层次清晰：控件 ← 面板 ← 主窗口。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import (
    QEasingCurve,
    QRectF,
    QSize,
    Qt,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.ui import icons, theme


# --------------------------------------------------------------------------- #
# 开关
# --------------------------------------------------------------------------- #
class ToggleSwitch(QAbstractButton):
    """自绘滑动开关（替代 ``QCheckBox`` 的勾选框语义）。

    用于「预读预热」这类**功能性开关**：开/关状态用滑块位置 + 颜色双重表达，
    比勾选框更容易在紧凑的行内被识别。
    """

    TRACK_W = 36
    TRACK_H = 20
    KNOB = 14

    def __init__(self, text: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setText(text)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pos = 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(140)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._on_anim)
        self.toggled.connect(self._on_toggled)

    # ---------------- 状态 ---------------- #
    def _on_toggled(self, checked: bool) -> None:
        self._animate(1.0 if checked else 0.0)

    def _animate(self, target: float) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(target)
        self._anim.start()

    def _on_anim(self, value) -> None:
        try:
            self._pos = float(value)
        except (TypeError, ValueError):
            self._pos = 1.0 if self.isChecked() else 0.0
        self.update()

    def set_checked_silent(self, checked: bool) -> None:
        """外部设置状态且**不触发** ``toggled``（用于程序性同步）。"""
        self.blockSignals(True)
        self.setChecked(checked)
        self.blockSignals(False)
        self._pos = 1.0 if checked else 0.0
        self.update()

    # ---------------- 尺寸 ---------------- #
    def _text_enabled(self) -> bool:
        return bool(self.text())

    def sizeHint(self) -> QSize:  # noqa: D102
        width = self.TRACK_W
        if self._text_enabled():
            metrics = QFontMetrics(theme.app_font(12))
            width += 9 + metrics.horizontalAdvance(self.text())
        return QSize(width + 2, max(self.TRACK_H, 20) + 4)

    def minimumSizeHint(self) -> QSize:  # noqa: D102
        return self.sizeHint()

    # ---------------- 绘制 ---------------- #
    def paintEvent(self, event) -> None:  # noqa: D102
        palette = theme.palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        enabled = self.isEnabled()
        track = QRectF(1, (self.height() - self.TRACK_H) / 2.0, self.TRACK_W, self.TRACK_H)

        if self.isChecked():
            fill = QColor(palette["accent"] if enabled else palette["border_strong"])
        else:
            fill = QColor(palette["sunken"] if enabled else palette["disabled_bg"])
        painter.setPen(
            QPen(QColor(palette["border_strong"] if enabled else palette["border"]), 1.0)
        )
        painter.setBrush(fill)
        painter.drawRoundedRect(track, self.TRACK_H / 2.0, self.TRACK_H / 2.0)

        travel = track.width() - self.KNOB - 6
        knob_x = track.left() + 3 + travel * max(0.0, min(1.0, self._pos))
        knob = QRectF(knob_x, track.top() + 3, self.KNOB, self.KNOB)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#FFFFFF" if enabled else palette["border"]))
        painter.drawEllipse(knob)

        if self._text_enabled():
            painter.setFont(QFont(theme.app_font(12)))
            painter.setPen(
                QColor(palette["text"] if enabled else palette["text_faint"])
            )
            painter.drawText(
                QRectF(
                    track.right() + 9,
                    0,
                    self.width() - track.right() - 9,
                    self.height(),
                ),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                self.text(),
            )
        painter.end()


# --------------------------------------------------------------------------- #
# 折叠面板
# --------------------------------------------------------------------------- #
class CollapsibleSection(QWidget):
    """可展开 / 收起的子区块（用于挡路者清单与执行结果）。

    收起时只留标题行（约 26 px），展开时把 ``content`` 显示出来 ——
    这是「最小窗口下纵向空间不够」的主要解法。
    """

    expandedChanged = Signal(bool)

    def __init__(
        self,
        title: str,
        parent: Optional[QWidget] = None,
        *,
        expanded: bool = False,
        icon_name: str = "chevron-right",
    ) -> None:
        super().__init__(parent)
        self._title = title
        self._expanded = bool(expanded)
        self._hover = False
        self._icon_name = icon_name

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self._header = _CollapseHeader(self)
        self._header.clicked.connect(self.toggle)
        self._header.hoverChanged.connect(self._on_hover)
        layout.addWidget(self._header)

        self._content = QWidget(self)
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.setSpacing(6)
        layout.addWidget(self._content)
        self._content.setVisible(self._expanded)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

    # ---------------- 接口 ---------------- #
    def content_layout(self) -> QVBoxLayout:
        """内容区布局（调用方往里塞控件）。"""
        return self._content_layout

    def content(self) -> QWidget:
        return self._content

    def set_title(self, title: str) -> None:
        self._title = title
        self._header.set_title(title)

    def set_expanded(self, expanded: bool, *_args) -> None:
        expanded = bool(expanded)
        if expanded == self._expanded:
            return
        self._expanded = expanded
        self._content.setVisible(expanded)
        self._header.set_expanded(expanded)
        self.expandedChanged.emit(expanded)
        self.updateGeometry()

    def is_expanded(self) -> bool:
        return self._expanded

    def toggle(self) -> None:
        self.set_expanded(not self._expanded)

    def _on_hover(self, hovered: bool) -> None:
        self._hover = hovered

    def sizeHint(self) -> QSize:  # noqa: D102
        hint = self.layout().sizeHint()
        return QSize(max(160, hint.width()), hint.height())


class _CollapseHeader(QWidget):
    """折叠面板的标题行（可点击）。"""

    clicked = Signal()
    hoverChanged = Signal(bool)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._title = ""
        self._expanded = False
        self._hover = False
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(26)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

    def set_title(self, title: str) -> None:
        self._title = title
        self.update()

    def set_expanded(self, expanded: bool) -> None:
        self._expanded = expanded
        self.update()

    def enterEvent(self, event) -> None:  # noqa: D102
        self._hover = True
        self.hoverChanged.emit(True)
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: D102
        self._hover = False
        self.hoverChanged.emit(False)
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: D102
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:  # noqa: D102
        palette = theme.palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = QRectF(self.rect())
        if self._hover:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(palette["hover"]))
            painter.drawRoundedRect(rect.adjusted(0, 1, 0, -1), 6, 6)

        icons.paint_icon(
            painter,
            "chevron-up" if self._expanded else "chevron-down",
            QRectF(4, (self.height() - 12) / 2.0, 12, 12),
            QColor(palette["text_dim"]),
        )

        font = QFont(theme.app_font(12, QFont.Weight.DemiBold))
        painter.setFont(font)
        painter.setPen(QColor(palette["text"] if self._hover else palette["text_dim"]))
        metrics = QFontMetrics(font)
        text = metrics.elidedText(
            self._title, Qt.TextElideMode.ElideRight, max(20, self.width() - 26)
        )
        painter.drawText(
            QRectF(20, 0, self.width() - 22, self.height()),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            text,
        )
        painter.end()


# --------------------------------------------------------------------------- #
# 内联状态条
# --------------------------------------------------------------------------- #
class InlineStatus(QWidget):
    """三档内联状态条：绿（可行）/ 琥珀（需驱逐）/ 红（不可行）。

    **不弹窗**。原因与建议以第二行小字呈现，允许换行；高度随内容自适应。
    """

    KINDS = {
        "ok": ("success", "success_soft", "check"),
        "warn": ("warning", "warning_soft", "warning"),
        "danger": ("danger", "danger_soft", "warning"),
        "idle": ("text_dim", "sunken", "info"),
    }

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._kind = "idle"
        self._headline = ""
        self._detail = ""
        self._reason = ""
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

    def set_state(
        self,
        kind: str,
        headline: str,
        detail: str = "",
        reason: str = "",
    ) -> None:
        """更新状态条。

        Args:
            kind: ``ok`` / ``warn`` / ``danger`` / ``idle``。
            headline: 一行结论（含量化数字）。
            detail: 第二行补充（建议）。
            reason: 悬停明细（放进 tooltip，长文案不占版面）。
        """
        self._kind = kind if kind in self.KINDS else "idle"
        self._headline = headline or ""
        self._detail = detail or ""
        self._reason = reason or ""
        self.setToolTip(self._reason)
        self.setVisible(bool(self._headline))
        self.updateGeometry()
        self.update()

    def clear(self) -> None:
        self.set_state("idle", "")

    def _lines(self) -> list:
        lines = [self._headline]
        if self._detail:
            for chunk in self._detail.split("\n"):
                lines.append(chunk)
        return [line for line in lines if line]

    def _metrics(self) -> tuple:
        font_head = QFont(theme.app_font(12, QFont.Weight.DemiBold))
        font_body = QFont(theme.app_font(11))
        head_m = QFontMetrics(font_head)
        body_m = QFontMetrics(font_body)
        return font_head, font_body, head_m, body_m

    def sizeHint(self) -> QSize:  # noqa: D102
        if not self._headline:
            return QSize(0, 0)
        _fh, _fb, head_m, body_m = self._metrics()
        available = max(120, self.width() - 40)
        height = 10 + head_m.height()
        if self._detail:
            for chunk in self._detail.split("\n"):
                rect = body_m.boundingRect(
                    0, 0, available, 2000, int(Qt.TextFlag.TextWordWrap), chunk
                )
                height += rect.height() + 1
        return QSize(180, height + 10)

    def minimumSizeHint(self) -> QSize:  # noqa: D102
        return QSize(140, self.sizeHint().height())

    def resizeEvent(self, event) -> None:  # noqa: D102
        super().resizeEvent(event)
        self.updateGeometry()

    def paintEvent(self, event) -> None:  # noqa: D102
        if not self._headline:
            return
        palette = theme.palette()
        fg_key, bg_key, icon_name = self.KINDS[self._kind]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(palette[bg_key]))
        painter.drawRoundedRect(rect, 8, 8)

        icons.paint_icon(
            painter,
            icon_name,
            QRectF(rect.left() + 9, rect.top() + 8, 14, 14),
            QColor(palette[fg_key]),
        )

        font_head, font_body, head_m, body_m = self._metrics()
        text_left = rect.left() + 29
        text_width = max(60.0, rect.width() - 29 - 10)
        y = rect.top() + 6

        painter.setFont(font_head)
        painter.setPen(QColor(palette[fg_key]))
        head = head_m.elidedText(
            self._headline, Qt.TextElideMode.ElideRight, int(text_width)
        )
        painter.drawText(
            QRectF(text_left, y, text_width, head_m.height()),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            head,
        )
        y += head_m.height()

        if self._detail:
            painter.setFont(font_body)
            painter.setPen(QColor(palette["text_dim"]))
            for chunk in self._detail.split("\n"):
                bounds = body_m.boundingRect(
                    0, 0, int(text_width), 2000, int(Qt.TextFlag.TextWordWrap), chunk
                )
                painter.drawText(
                    QRectF(text_left, y, text_width, bounds.height()),
                    int(Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignLeft),
                    chunk,
                )
                y += bounds.height() + 1
        painter.end()


# --------------------------------------------------------------------------- #
# LCN 区间输入
# --------------------------------------------------------------------------- #
class LCNRangeInput(QWidget):
    """起点 / 终点 LCN 双数字框，右侧实时换算 GB 偏移。

    两端互相约束（``start <= end``），换算文案由外部通过 :meth:`set_unit` 提供，
    这样控件本身不需要知道簇大小。
    """

    changed = Signal(int, int)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._bytes_per_cluster = 0
        self._guard = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        row_start = QHBoxLayout()
        row_start.setContentsMargins(0, 0, 0, 0)
        row_start.setSpacing(8)
        self._start_label = QLabel("起点 LCN", self)
        self._start_label.setProperty("role", "dim")
        self._start_box = QSpinBox(self)
        self._start_box.setRange(0, 2_000_000_000)
        self._start_box.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._start_box.setFixedWidth(128)
        self._start_hint = QLabel("", self)
        self._start_hint.setProperty("role", "caption")
        row_start.addWidget(self._start_label, 0)
        row_start.addWidget(self._start_box, 0)
        row_start.addWidget(self._start_hint, 1)

        row_end = QHBoxLayout()
        row_end.setContentsMargins(0, 0, 0, 0)
        row_end.setSpacing(8)
        self._end_label = QLabel("终点 LCN", self)
        self._end_label.setProperty("role", "dim")
        self._end_box = QSpinBox(self)
        self._end_box.setRange(0, 2_000_000_000)
        self._end_box.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._end_box.setFixedWidth(128)
        self._end_hint = QLabel("", self)
        self._end_hint.setProperty("role", "caption")
        row_end.addWidget(self._end_label, 0)
        row_end.addWidget(self._end_box, 0)
        row_end.addWidget(self._end_hint, 1)

        self._span_hint = QLabel("", self)
        self._span_hint.setProperty("role", "caption")

        layout.addLayout(row_start)
        layout.addLayout(row_end)
        layout.addWidget(self._span_hint)

        self._start_box.valueChanged.connect(self._on_start)
        self._end_box.valueChanged.connect(self._on_end)

    # ---------------- 接口 ---------------- #
    def set_unit(self, bytes_per_cluster: int) -> None:
        """设置簇大小，用于 GB 换算。"""
        self._bytes_per_cluster = max(0, int(bytes_per_cluster))
        self._refresh_hints()

    def set_bounds(self, maximum: int) -> None:
        """设置 LCN 上限（= 总簇数）。"""
        maximum = max(0, int(maximum))
        self._guard = True
        for box in (self._start_box, self._end_box):
            box.setRange(0, maximum)
        self._guard = False
        self._refresh_hints()

    def set_range_values(self, start: int, end: int) -> None:
        """外部设置区间（不发信号）。"""
        self._guard = True
        self._start_box.setValue(max(0, int(start)))
        self._end_box.setValue(max(0, int(end)))
        self._guard = False
        self._refresh_hints()

    def values(self) -> tuple:
        return int(self._start_box.value()), int(self._end_box.value())

    def setEnabled(self, enabled: bool) -> None:  # noqa: D102
        super().setEnabled(enabled)
        for widget in (
            self._start_box,
            self._end_box,
            self._start_label,
            self._end_label,
        ):
            widget.setEnabled(enabled)

    # ---------------- 内部 ---------------- #
    def _on_start(self, value: int) -> None:
        if self._guard:
            return
        if value > self._end_box.value():
            self._guard = True
            self._end_box.setValue(value)
            self._guard = False
        self._refresh_hints()
        self.changed.emit(*self.values())

    def _on_end(self, value: int) -> None:
        if self._guard:
            return
        if value < self._start_box.value():
            self._guard = True
            self._start_box.setValue(value)
            self._guard = False
        self._refresh_hints()
        self.changed.emit(*self.values())

    def _gb_text(self, lcn: int) -> str:
        if self._bytes_per_cluster <= 0:
            return ""
        # 用 GiB（1024^3）保证与布局图刻度尺口径一致
        gib = lcn * self._bytes_per_cluster / float(1024 ** 3)
        if gib >= 1.0:
            return f"≈ {gib:.2f} GiB"
        mib = lcn * self._bytes_per_cluster / float(1024 ** 2)
        return f"≈ {mib:.1f} MiB"

    def _refresh_hints(self) -> None:
        start, end = self.values()
        self._start_hint.setText(self._gb_text(start))
        self._end_hint.setText(self._gb_text(end))
        clusters = max(0, end - start)
        if self._bytes_per_cluster > 0:
            span_bytes = clusters * self._bytes_per_cluster
            self._span_hint.setText(
                f"区间长度 {clusters:,} 簇 ≈ {_fmt_bytes(span_bytes)}"
            )
        else:
            self._span_hint.setText(f"区间长度 {clusters:,} 簇")


def _fmt_bytes(value: int) -> str:
    """本地字节格式化（不依赖 models，避免控件层反向依赖）。"""
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    size = float(max(0, int(value)))
    index = 0
    while size >= 1024.0 and index < len(units) - 1:
        size /= 1024.0
        index += 1
    if index == 0:
        return f"{int(size)} B"
    return f"{size:.2f} {units[index]}"
