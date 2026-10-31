"""碎片分布图：把卷的 LBA 空间画成两条带。

信息编码刻意用**两条独立的带**，而不是"一种颜色编码两件事"：

* **上带（占用）**：深色 = 空闲，蓝色 = 已用。回答"这块地方有没有东西"。
* **下带（碎片热力）**：蓝 → 黄 → 红，表示该区间的 extent 密度。
  回答"这块地方被切得多碎"。

把两者叠成一种颜色是可视化里最常见的错误：用户无法区分"这里占用率高"
和"这里碎片严重"，而这两件事对应的操作完全不同。
"""

from __future__ import annotations

from PySide6.QtCore import QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

from ...contract.models import FragReport
from ..format import fmt_bytes, fmt_count, fmt_percent
from ..theme import DARK, Palette, heat_color, mono_font

__all__ = ["FragMap"]


class FragMap(QWidget):
    """卷空间两带视图。只读，可悬停查看区间明细。"""

    bucketHovered = Signal(int)
    bucketClicked = Signal(int)

    def __init__(self, palette: Palette = DARK, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._p = palette
        self._report: FragReport | None = None
        self._hover = -1
        # 固定高度：这是"带状"可视化，拉高只会让它变成两块巨大的色块，
        # 信息密度不会增加，反而把下面的指标挤走。
        self.setFixedHeight(168)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self.setToolTip("")

    # -- 数据 -------------------------------------------------------------

    def set_report(self, report: FragReport | None) -> None:
        self._report = report
        self._hover = -1
        self.update()

    def set_palette(self, palette: Palette) -> None:
        self._p = palette
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(720, 190)

    # -- 几何 -------------------------------------------------------------

    def _plot_rect(self) -> QRect:
        m = 2
        return QRect(m, 26, max(1, self.width() - 2 * m), max(1, self.height() - 26 - 26))

    def _split(self, plot: QRect) -> tuple[QRect, QRect]:
        gap = 8
        used_h = int((plot.height() - gap) * 0.64)
        top = QRect(plot.x(), plot.y(), plot.width(), used_h)
        bottom = QRect(plot.x(), plot.y() + used_h + gap, plot.width(), plot.height() - used_h - gap)
        return top, bottom

    def _bucket_at(self, x: int) -> int:
        if not self._report or not self._report.buckets:
            return -1
        plot = self._plot_rect()
        if x < plot.x() or x > plot.right():
            return -1
        n = len(self._report.buckets)
        idx = int((x - plot.x()) / max(1, plot.width()) * n)
        return max(0, min(n - 1, idx))

    # -- 绘制 -------------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        pal = self._p
        plot = self._plot_rect()
        top, bottom = self._split(plot)

        p.fillRect(self.rect(), QColor(pal.panel))

        if self._report is None or not self._report.buckets:
            p.setPen(QColor(pal.text_faint))
            p.setFont(mono_font(9))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "尚未扫描 · 选择目标卷后点击「扫描」")
            # 空态也要有个"这里将出现图表"的边界，否则整张卡片看起来是坏了
            pen = QPen(QColor(pal.border))
            pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.drawRoundedRect(plot.adjusted(0, 0, -1, -1), 6, 6)
            return

        report = self._report
        buckets = report.buckets
        n = len(buckets)
        usable_w = plot.width()
        # 桶数多于像素时按列聚合，避免画几万个 1px 矩形拖垮重绘
        col_w = max(1.0, usable_w / n)

        p.fillRect(top, QColor(pal.free_block))
        p.fillRect(bottom, QColor(pal.track))

        for i, b in enumerate(buckets):
            x0 = top.x() + int(i * usable_w / n)
            x1 = top.x() + int((i + 1) * usable_w / n)
            w = max(1, x1 - x0)
            if b.used_blocks > 0:
                p.fillRect(QRect(x0, top.y(), w, top.height()), QColor(pal.used_block))
            heat = b.frag_ratio
            if heat > 0:
                p.fillRect(QRect(x0, bottom.y(), w, bottom.height()), heat_color(pal, heat))

        # 悬停高亮
        if 0 <= self._hover < n:
            x0 = top.x() + int(self._hover * usable_w / n)
            x1 = top.x() + int((self._hover + 1) * usable_w / n)
            pen = QPen(QColor(pal.text))
            pen.setWidth(1)
            p.setPen(pen)
            p.drawRect(QRect(x0, top.y(), max(2, x1 - x0), top.height() + bottom.height() + 8))

        # 边框：把两条带框起来，避免"空闲色"与卡片底色糊成一片
        pen = QPen(QColor(pal.border))
        pen.setWidth(1)
        p.setPen(pen)
        p.drawRect(QRect(top.x(), top.y(), top.width() - 1, bottom.bottom() - top.y()))

        # 行标题
        p.setFont(mono_font(8))
        p.setPen(QColor(pal.text_faint))
        p.drawText(QRect(plot.x(), plot.y() - 18, 200, 14),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "占用 / OCCUPANCY")
        p.drawText(QRect(plot.x(), bottom.y() - 15, 220, 13),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "碎片热力 / FRAGMENTATION")

        # 右侧刻度：Max
        total_bytes = report.geometry.total_bytes
        p.setPen(QColor(pal.text_faint))
        p.drawText(QRect(plot.right() - 160, plot.y() - 18, 160, 14),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   f"{fmt_bytes(total_bytes)} · {fmt_count(report.geometry.block_count)} 块")

        # 底部轴标签
        axis_y = plot.bottom() + 6
        p.setFont(mono_font(8))
        for frac, text in ((0.0, "0"), (0.25, "25%"), (0.5, "50%"), (0.75, "75%"), (1.0, "100%")):
            tx = plot.x() + int(usable_w * frac)
            label = text
            w = QFontMetrics(p.font()).horizontalAdvance(label)
            if frac == 0.0:
                rect = QRect(tx, axis_y, w + 4, 14)
                align = Qt.AlignmentFlag.AlignLeft
            elif frac == 1.0:
                rect = QRect(tx - w - 4, axis_y, w + 4, 14)
                align = Qt.AlignmentFlag.AlignRight
            else:
                rect = QRect(tx - w, axis_y, w * 2, 14)
                align = Qt.AlignmentFlag.AlignHCenter
            p.drawText(rect, align | Qt.AlignmentFlag.AlignVCenter, label)

    # -- 交互 -------------------------------------------------------------

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        idx = self._bucket_at(int(event.position().x()))
        if idx != self._hover:
            self._hover = idx
            self.update()
            if idx >= 0:
                self.bucketHovered.emit(idx)
        if idx >= 0 and self._report is not None:
            b = self._report.buckets[idx]
            bs = self._report.geometry.block_size
            tip = (
                f"区间 {fmt_bytes(b.start * bs)} – {fmt_bytes(b.end * bs)}\n"
                f"占用 {fmt_percent(b.usage_ratio)} · {fmt_count(b.used_blocks)} 块\n"
                f"区间内 extent 数 {b.extent_count} · 碎片指数 {fmt_percent(b.frag_ratio, 0)}"
            )
            QToolTip.showText(event.globalPosition().toPoint(), tip, self)
        else:
            QToolTip.hideText()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._hover != -1:
            self._hover = -1
            self.update()
        QToolTip.hideText()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        idx = self._bucket_at(int(event.position().x()))
        if idx >= 0:
            self.bucketClicked.emit(idx)
        super().mousePressEvent(event)
