"""速率曲线：最近 90 秒的吞吐走势。

为什么单独画一条曲线而不是只看数字：整理大卷时"速率掉到 0"是最重要的
信号（介质出错、设备掉线、被别的进程抢占），而一个不断刷新的平均值会
把这个信息抹平。曲线让"停了"这件事一眼可见。
"""

from __future__ import annotations

import time
from collections import deque

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..format import fmt_rate
from ..theme import DARK, Palette, mono_font

__all__ = ["RateChart"]


class RateChart(QWidget):
    def __init__(self, palette: Palette = DARK, parent: QWidget | None = None, window_s: float = 90.0) -> None:
        super().__init__(parent)
        self._p = palette
        self._window = window_s
        self._samples: deque[tuple[float, float]] = deque()
        self.setFixedHeight(56)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(320, 56)

    def push(self, rate_bps: float, now: float | None = None) -> None:
        t = time.monotonic() if now is None else now
        self._samples.append((t, max(0.0, rate_bps)))
        cutoff = t - self._window
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()
        self.update()

    def clear(self) -> None:
        self._samples.clear()
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pal = self._p
        r = QRectF(self.rect()).adjusted(0, 14, 0, 0)
        p.fillRect(self.rect(), QColor(pal.panel))

        p.setFont(mono_font(8))
        p.setPen(QColor(pal.text_faint))
        p.drawText(
            QRectF(0, 0, self.width(), 12),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            "吞吐走势 / THROUGHPUT (90s)",
        )

        if len(self._samples) < 2:
            p.setPen(QColor(pal.text_faint))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, "—")
            return

        peak = max(v for _, v in self._samples) or 1.0
        n = len(self._samples)
        step = r.width() / max(1, n - 1)

        path = QPainterPath()
        path.moveTo(r.left(), r.bottom())
        for i, (_, v) in enumerate(self._samples):
            x = r.left() + i * step
            y = r.bottom() - (v / peak) * (r.height() - 4)
            path.lineTo(QPointF(x, y))
        path.lineTo(r.right(), r.bottom())
        path.closeSubpath()

        fill = QColor(pal.accent)
        fill.setAlpha(48)
        p.fillPath(path, fill)

        line = QPainterPath()
        for i, (_, v) in enumerate(self._samples):
            x = r.left() + i * step
            y = r.bottom() - (v / peak) * (r.height() - 4)
            if i == 0:
                line.moveTo(QPointF(x, y))
            else:
                line.lineTo(QPointF(x, y))
        pen = QPen(QColor(pal.accent))
        pen.setWidthF(1.4)
        p.setPen(pen)
        p.drawPath(line)

        p.setPen(QColor(pal.text_dim))
        p.drawText(
            QRectF(0, 0, self.width(), 12),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            f"峰值 {fmt_rate(peak)}",
        )
