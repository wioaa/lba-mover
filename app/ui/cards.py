"""可复用的小控件集合：卡片、徽章、分段控件、自绘复选框 / 下拉框、进度条等。

这些控件全部自绘或半自绘，规避 Qt 默认的灰色外观；配色一律在 ``paintEvent``
中从当前调色板读取，因此主题切换后只需 ``update()`` 即生效。
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional

from PySide6.QtCore import (
    QEasingCurve,
    QPointF,
    QRectF,
    QSize,
    Qt,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.ui import icons, theme


# --------------------------------------------------------------------------- #
# 基础构件
# --------------------------------------------------------------------------- #
class Card(QFrame):
    """圆角卡片容器：白底 + 1px 描边 + 内部统一内边距。"""

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        padding: int = 16,
        spacing: int = 12,
        role: str = "card",
    ) -> None:
        super().__init__(parent)
        self.setProperty("role", role)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(padding, padding, padding, padding)
        self._layout.setSpacing(spacing)

    def body(self) -> QVBoxLayout:
        """返回内部垂直布局。"""
        return self._layout


class Divider(QFrame):
    """1px 分隔线。"""

    def __init__(self, vertical: bool = False, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "vline" if vertical else "hline")
        if vertical:
            self.setFixedWidth(1)
            self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        else:
            self.setFixedHeight(1)
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)


def label(
    text: str,
    role: str = "body",
    parent: Optional[QWidget] = None,
    wrap: bool = False,
) -> QLabel:
    """快捷创建带语义角色的 ``QLabel``。"""
    widget = QLabel(text, parent)
    widget.setProperty("role", role)
    widget.setWordWrap(wrap)
    return widget


class SectionHeader(QWidget):
    """小节标题：图标 + 标题 + 可选右侧说明。"""

    def __init__(
        self,
        title: str,
        icon_name: str = "",
        hint: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._icon_name = icon_name
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        if icon_name:
            icon_widget = IconLabel(icon_name, 18, "accent")
            layout.addWidget(icon_widget, 0, Qt.AlignmentFlag.AlignVCenter)

        self._title = label(title, "h2")
        layout.addWidget(self._title, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addStretch(1)

        self._hint = label(hint, "dim")
        layout.addWidget(self._hint, 0, Qt.AlignmentFlag.AlignVCenter)

    def set_hint(self, text: str) -> None:
        """更新右侧说明文字。"""
        self._hint.setText(text)


class IconLabel(QWidget):
    """只画一个矢量图标的固定尺寸小控件。"""

    def __init__(
        self,
        icon_name: str,
        size: int = 18,
        color_key: str = "text_dim",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._name = icon_name
        self._color_key = color_key
        self.setFixedSize(size, size)

    def set_icon(self, name: str, color_key: str = "") -> None:
        """切换图标。"""
        self._name = name
        if color_key:
            self._color_key = color_key
        self.update()

    def paintEvent(self, event) -> None:  # noqa: D102 - Qt 回调
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        icons.paint_icon(
            painter,
            self._name,
            QRectF(self.rect()),
            theme.color(self._color_key),
        )
        painter.end()


# --------------------------------------------------------------------------- #
# 按钮
# --------------------------------------------------------------------------- #
class IconButton(QAbstractButton):
    """自绘图标按钮（标题栏、工具条使用），支持危险悬停色。"""

    def __init__(
        self,
        icon_name: str,
        size: int = 34,
        icon_size: int = 16,
        tooltip: str = "",
        danger_hover: bool = False,
        color_key: str = "text_dim",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._name = icon_name
        self._icon_size = icon_size
        self._danger_hover = danger_hover
        self._color_key = color_key
        self._hover = False
        self.setFixedSize(size, size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        if tooltip:
            self.setToolTip(tooltip)

    def set_icon(self, name: str) -> None:
        """切换图标。"""
        self._name = name
        self.update()

    def enterEvent(self, event) -> None:  # noqa: D102
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: D102
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:  # noqa: D102
        palette = theme.palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        pressed = self.isDown()
        if self._danger_hover and self._hover:
            background = QColor(palette["danger"])
            icon_color = QColor("#FFFFFF")
        elif self._hover:
            background = QColor(palette["pressed"] if pressed else palette["hover"])
            icon_color = QColor(palette["text"])
        else:
            background = QColor(0, 0, 0, 0)
            icon_color = theme.color(self._color_key)

        if background.alpha() > 0:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(background)
            painter.drawRoundedRect(rect, 7, 7)

        inset = (self.width() - self._icon_size) / 2.0
        icons.paint_icon(
            painter,
            self._name,
            QRectF(inset, inset, self._icon_size, self._icon_size),
            icon_color,
        )
        painter.end()


class SoftCheckBox(QCheckBox):
    """自绘复选框：圆角方框 + 矢量对勾，避免使用 Qt 默认指示器。"""

    BOX = 18

    def __init__(self, text: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def sizeHint(self) -> QSize:  # noqa: D102
        metrics = QFontMetrics(self.font())
        width = self.BOX + (metrics.horizontalAdvance(self.text()) + 10 if self.text() else 0)
        return QSize(width + 2, max(self.BOX, metrics.height()) + 4)

    def minimumSizeHint(self) -> QSize:  # noqa: D102
        return self.sizeHint()

    def paintEvent(self, event) -> None:  # noqa: D102
        palette = theme.palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        box = QRectF(1, (self.height() - self.BOX) / 2.0, self.BOX, self.BOX)
        hovered = self.underMouse()
        enabled = self.isEnabled()

        if self.isChecked():
            fill = QColor(palette["accent"] if enabled else palette["border_strong"])
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill)
            painter.drawRoundedRect(box, 5, 5)
            icons.draw_check_mark(painter, box, QColor("#FFFFFF"), 2.0)
        else:
            border = QColor(
                palette["accent"] if (hovered and enabled) else
                (palette["border_strong"] if enabled else palette["border"])
            )
            painter.setPen(QPen(border, 1.4))
            painter.setBrush(QColor(palette["card"]))
            painter.drawRoundedRect(box, 5, 5)

        if self.text():
            painter.setPen(
                QColor(palette["text"] if enabled else palette["text_faint"])
            )
            font = QFont(self.font())
            font.setPixelSize(13)
            painter.setFont(font)
            text_rect = QRectF(
                box.right() + 10,
                0,
                self.width() - box.right() - 10,
                self.height(),
            )
            painter.drawText(
                text_rect,
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                self.text(),
            )
        painter.end()


class SoftComboBox(QComboBox):
    """在下拉框右侧自绘 chevron 箭头（QSS 已隐藏原生箭头）。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(38)

    def paintEvent(self, event) -> None:  # noqa: D102
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        size = 12
        rect = QRectF(
            self.width() - 26,
            (self.height() - size) / 2.0,
            size,
            size,
        )
        icons.paint_icon(painter, "chevron-down", rect, theme.color("text_dim"))
        painter.end()


# --------------------------------------------------------------------------- #
# 分段控件
# --------------------------------------------------------------------------- #
class SegmentedControl(QWidget):
    """自绘分段控件（替代 QTabWidget 的默认外观）。"""

    changed = Signal(int)

    def __init__(
        self,
        items: List[str],
        current: int = 0,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._items = list(items)
        self._current = max(0, min(current, len(self._items) - 1))
        self._hover = -1
        self._pos = float(self._current)
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(180)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._on_anim)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(40)
        self.setMinimumWidth(120)

    # ---------------- 状态 ---------------- #
    @property
    def current_index(self) -> int:
        return self._current

    def set_current(self, index: int, emit: bool = False) -> None:
        """切换当前项（``emit=True`` 时发出 ``changed``）。"""
        if not self._items:
            return
        index = max(0, min(index, len(self._items) - 1))
        if index == self._current:
            return
        self._current = index
        self._animate_to(float(index))
        if emit:
            self.changed.emit(index)
        self.update()

    def _animate_to(self, value: float) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(value)
        self._anim.start()

    def _on_anim(self, value) -> None:
        try:
            self._pos = float(value)
        except (TypeError, ValueError):
            self._pos = float(self._current)
        self.update()

    # ---------------- 交互 ---------------- #
    def _index_at(self, x: float) -> int:
        if not self._items:
            return -1
        width = self.width() / len(self._items)
        index = int(x // width)
        return max(0, min(index, len(self._items) - 1))

    def mousePressEvent(self, event) -> None:  # noqa: D102
        if event.button() == Qt.MouseButton.LeftButton:
            index = self._index_at(event.position().x())
            if index >= 0 and index != self._current:
                self.set_current(index, emit=True)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: D102
        index = self._index_at(event.position().x())
        if index != self._hover:
            self._hover = index
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: D102
        self._hover = -1
        self.update()
        super().leaveEvent(event)

    # ---------------- 绘制 ---------------- #
    def paintEvent(self, event) -> None:  # noqa: D102
        palette = theme.palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        outer = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(QColor(palette["border"]), 1))
        painter.setBrush(QColor(palette["sunken"]))
        painter.drawRoundedRect(outer, 9, 9)

        if not self._items:
            painter.end()
            return

        slot_width = (outer.width() - 6) / len(self._items)
        pill = QRectF(outer.left() + 3 + slot_width * self._pos, outer.top() + 3,
                      slot_width, outer.height() - 6)

        painter.setPen(QPen(QColor(palette["border"]), 1))
        painter.setBrush(QColor(palette["card"]))
        painter.drawRoundedRect(pill, 7, 7)

        font = QFont(self.font())
        font.setPixelSize(13)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)

        for index, text in enumerate(self._items):
            rect = QRectF(
                outer.left() + 3 + slot_width * index,
                outer.top() + 3,
                slot_width,
                outer.height() - 6,
            )
            if index == self._current:
                color = QColor(palette["accent"])
            elif index == self._hover:
                color = QColor(palette["text"])
            else:
                color = QColor(palette["text_dim"])
            painter.setPen(color)
            painter.drawText(
                rect,
                int(Qt.AlignmentFlag.AlignCenter),
                text,
            )
        painter.end()


# --------------------------------------------------------------------------- #
# 徽章
# --------------------------------------------------------------------------- #
class Badge(QWidget):
    """胶囊徽章，配色随主题与语义自动切换。"""

    KINDS: Dict[str, tuple] = {
        "neutral": ("text_dim", "sunken"),
        "accent": ("accent", "accent_soft"),
        "success": ("success", "success_soft"),
        "warning": ("warning", "warning_soft"),
        "danger": ("danger", "danger_soft"),
    }

    def __init__(
        self,
        text: str = "",
        kind: str = "neutral",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._text = text
        self._kind = kind if kind in self.KINDS else "neutral"
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def set_state(self, text: str, kind: str) -> None:
        """更新文案与配色。"""
        self._text = text
        self._kind = kind if kind in self.KINDS else "neutral"
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:  # noqa: D102
        font = QFont(theme.app_font(11, QFont.Weight.DemiBold))
        metrics = QFontMetrics(font)
        return QSize(metrics.horizontalAdvance(self._text) + 20, 22)

    def minimumSizeHint(self) -> QSize:  # noqa: D102
        return self.sizeHint()

    def paintEvent(self, event) -> None:  # noqa: D102
        palette = theme.palette()
        fg_key, bg_key = self.KINDS[self._kind]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(palette[bg_key]))
        painter.drawRoundedRect(rect, rect.height() / 2.0, rect.height() / 2.0)

        font = QFont(theme.app_font(11, QFont.Weight.DemiBold))
        painter.setFont(font)
        painter.setPen(QColor(palette[fg_key]))
        painter.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), self._text)
        painter.end()


# --------------------------------------------------------------------------- #
# 数值行 / 滑块字段
# --------------------------------------------------------------------------- #
class StatRow(QWidget):
    """左标签 + 右数值的一行统计。"""

    def __init__(
        self,
        name: str,
        value: str = "—",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self._name = label(name, "dim")
        self._value = label(value, "h3")
        self._value.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        layout.addWidget(self._name, 1)
        layout.addWidget(self._value, 0)

    def set_value(self, text: str, role: str = "h3") -> None:
        """更新数值文本。"""
        self._value.setText(text)
        self._value.setProperty("role", role)
        self._value.style().unpolish(self._value)
        self._value.style().polish(self._value)
        self._value.update()


class SliderField(QWidget):
    """滑块 + 数字框联动组件。"""

    valueChanged = Signal(int)

    def __init__(
        self,
        minimum: int,
        maximum: int,
        value: int,
        suffix: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._suffix = suffix
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self._slider = QSlider(Qt.Orientation.Horizontal, self)
        self._slider.setRange(minimum, maximum)
        self._slider.setValue(value)
        self._slider.setCursor(Qt.CursorShape.PointingHandCursor)

        self._spin = QSpinBox(self)
        self._spin.setRange(minimum, maximum)
        self._spin.setValue(value)
        self._spin.setFixedWidth(96)
        self._spin.setAlignment(Qt.AlignmentFlag.AlignRight)
        if suffix:
            self._spin.setSuffix(suffix)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(self._slider, 1)
        row.addWidget(self._spin, 0)

        layout.addLayout(row)

        self._slider.valueChanged.connect(self._on_slider)
        self._spin.valueChanged.connect(self._on_spin)

    # ---------------- 接口 ---------------- #
    def value(self) -> int:
        return int(self._slider.value())

    def set_value(self, value: int) -> None:
        """外部设置数值（不重复发信号）。"""
        value = max(self._slider.minimum(), min(value, self._slider.maximum()))
        for widget in (self._slider, self._spin):
            widget.blockSignals(True)
            widget.setValue(value)
            widget.blockSignals(False)

    def set_range(self, minimum: int, maximum: int) -> None:
        """调整取值范围。"""
        for widget in (self._slider, self._spin):
            widget.blockSignals(True)
            widget.setRange(minimum, maximum)
            widget.blockSignals(False)

    def setEnabled(self, enabled: bool) -> None:  # noqa: D102
        super().setEnabled(enabled)
        self._slider.setEnabled(enabled)
        self._spin.setEnabled(enabled)

    # ---------------- 内部 ---------------- #
    def _on_slider(self, value: int) -> None:
        self._spin.blockSignals(True)
        self._spin.setValue(value)
        self._spin.blockSignals(False)
        self.valueChanged.emit(value)

    def _on_spin(self, value: int) -> None:
        self._slider.blockSignals(True)
        self._slider.setValue(value)
        self._slider.blockSignals(False)
        self.valueChanged.emit(value)


# --------------------------------------------------------------------------- #
# 进度条
# --------------------------------------------------------------------------- #
class ProgressBarWidget(QWidget):
    """自绘圆角进度条：左侧主文案，右侧百分比，下方渐变条。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._value = 0
        self._maximum = 100
        self._title = "就绪"
        self._detail = ""
        self._indeterminate = False
        self._phase = 0.0
        self.setMinimumHeight(46)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._timer = None

    # ---------------- 接口 ---------------- #
    def set_state(
        self,
        value: int,
        maximum: int,
        title: str = "",
        detail: str = "",
    ) -> None:
        """更新进度。"""
        self._maximum = max(1, int(maximum))
        self._value = max(0, min(int(value), self._maximum))
        if title:
            self._title = title
        self._detail = detail
        self.update()

    def set_indeterminate(self, enabled: bool) -> None:
        """开启 / 关闭不确定进度动画。"""
        from PySide6.QtCore import QTimer

        self._indeterminate = enabled
        if enabled:
            if self._timer is None:
                self._timer = QTimer(self)
                self._timer.setInterval(28)
                self._timer.timeout.connect(self._tick)
            self._timer.start()
        else:
            if self._timer is not None:
                self._timer.stop()
            self._phase = 0.0
        self.update()

    def reset(self, title: str = "就绪") -> None:
        """回到初始状态。"""
        self.set_indeterminate(False)
        self._value = 0
        self._maximum = 100
        self._title = title
        self._detail = ""
        self.update()

    def _tick(self) -> None:
        self._phase = (self._phase + 0.022) % 1.0
        self.update()

    # ---------------- 绘制 ---------------- #
    def paintEvent(self, event) -> None:  # noqa: D102
        palette = theme.palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        percent = 0.0 if self._maximum <= 0 else self._value / self._maximum
        bar_height = 6.0
        bar_top = self.height() - bar_height - 4
        track = QRectF(0, bar_top, self.width(), bar_height)

        font = QFont(theme.app_font(12, QFont.Weight.DemiBold))
        painter.setFont(font)
        painter.setPen(QColor(palette["text"]))
        title_rect = QRectF(0, 0, self.width() * 0.62, 20)
        painter.drawText(
            title_rect,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            self._title,
        )

        font_percent = QFont(theme.app_font(12, QFont.Weight.Bold))
        painter.setFont(font_percent)
        painter.setPen(QColor(palette["accent"]))
        painter.drawText(
            QRectF(self.width() * 0.62, 0, self.width() * 0.38, 20),
            int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            f"{int(percent * 100)}%",
        )

        if self._detail:
            font_detail = QFont(theme.app_font(11))
            painter.setFont(font_detail)
            painter.setPen(QColor(palette["text_faint"]))
            metrics = QFontMetrics(font_detail)
            text = metrics.elidedText(
                self._detail, Qt.TextElideMode.ElideMiddle, int(self.width())
            )
            painter.drawText(
                QRectF(0, 20, self.width(), 15),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                text,
            )

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(palette["sunken"]))
        painter.drawRoundedRect(track, bar_height / 2, bar_height / 2)

        gradient = QLinearGradient(track.left(), 0, track.right(), 0)
        gradient.setColorAt(0.0, QColor(palette["accent"]))
        gradient.setColorAt(1.0, QColor(palette["cyan"]))

        if self._indeterminate:
            span = track.width() * 0.32
            x = track.left() + (track.width() + span) * self._phase - span
            chunk = QRectF(x, track.top(), span, track.height())
            chunk = chunk.intersected(track)
            if chunk.width() > 1:
                painter.setBrush(gradient)
                painter.drawRoundedRect(chunk, bar_height / 2, bar_height / 2)
        elif percent > 0:
            chunk = QRectF(
                track.left(),
                track.top(),
                max(bar_height, track.width() * min(1.0, percent)),
                track.height(),
            )
            painter.setBrush(gradient)
            painter.drawRoundedRect(chunk, bar_height / 2, bar_height / 2)

        painter.end()


# --------------------------------------------------------------------------- #
# 空状态
# --------------------------------------------------------------------------- #
class EmptyState(QWidget):
    """空状态占位：矢量插画 + 标题 + 说明。"""

    def __init__(
        self,
        icon_name: str = "disk",
        title: str = "",
        description: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._icon_name = icon_name
        self._title = title
        self._description = description

    def set_content(self, title: str, description: str, icon_name: str = "") -> None:
        """更新空状态文案。"""
        self._title = title
        self._description = description
        if icon_name:
            self._icon_name = icon_name
        self.update()

    def paintEvent(self, event) -> None:  # noqa: D102
        palette = theme.palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        center_x = self.width() / 2.0
        icon_size = 56.0
        icon_top = self.height() / 2.0 - 64

        halo = QRectF(center_x - 44, icon_top - 16, 88, 88)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(palette["sunken"]))
        painter.drawEllipse(halo)

        icons.paint_icon(
            painter,
            self._icon_name,
            QRectF(center_x - icon_size / 2, icon_top + 8, icon_size, icon_size),
            QColor(palette["text_faint"]),
        )

        font_title = QFont(theme.app_font(14, QFont.Weight.DemiBold))
        painter.setFont(font_title)
        painter.setPen(QColor(palette["text_dim"]))
        painter.drawText(
            QRectF(0, icon_top + 84, self.width(), 22),
            int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop),
            self._title,
        )

        font_desc = QFont(theme.app_font(12))
        painter.setFont(font_desc)
        painter.setPen(QColor(palette["text_faint"]))
        painter.drawText(
            QRectF(24, icon_top + 108, self.width() - 48, 44),
            int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop),
            self._description,
        )
        painter.end()


def set_role(widget: QWidget, role: str) -> None:
    """动态修改控件的 QSS 角色属性并刷新样式。"""
    widget.setProperty("role", role)
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()
