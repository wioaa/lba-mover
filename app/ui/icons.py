"""全部图标都用 ``QPainter`` 矢量自绘（不使用 emoji、不依赖外部资源）。

统一在 24x24 的逻辑坐标系里描述路径，渲染时按目标尺寸缩放并开启抗锯齿；
线宽按尺寸等比缩放，保证线条风格统一。
"""

from __future__ import annotations

import math
from typing import Callable, Dict, Optional

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
)

_UNIT = 24.0


# --------------------------------------------------------------------------- #
# 内部绘制原语
# --------------------------------------------------------------------------- #
def _pen(color: QColor, width: float) -> QPen:
    pen = QPen(color)
    pen.setWidthF(width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def _draw_scan(p: QPainter, c: QColor) -> None:
    p.setPen(_pen(c, 1.7))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(QRectF(4.0, 4.0, 12.0, 12.0))
    p.drawLine(QPointF(15.0, 15.0), QPointF(20.5, 20.5))
    p.drawLine(QPointF(7.6, 10.0), QPointF(12.4, 10.0))
    p.drawLine(QPointF(10.0, 7.6), QPointF(10.0, 12.4))


def _draw_refresh(p: QPainter, c: QColor) -> None:
    p.setPen(_pen(c, 1.7))
    p.setBrush(Qt.BrushStyle.NoBrush)
    rect = QRectF(4.5, 4.5, 15.0, 15.0)
    p.drawArc(rect, 60 * 16, 280 * 16)
    head = QPolygonF(
        [QPointF(16.4, 2.6), QPointF(19.6, 7.2), QPointF(14.2, 7.8)]
    )
    p.setBrush(QBrush(c))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawPolygon(head)


def _draw_sun(p: QPainter, c: QColor) -> None:
    p.setPen(_pen(c, 1.7))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(QRectF(8.0, 8.0, 8.0, 8.0))
    for index in range(8):
        angle = math.radians(index * 45.0)
        x0 = 12.0 + math.cos(angle) * 8.6
        y0 = 12.0 + math.sin(angle) * 8.6
        x1 = 12.0 + math.cos(angle) * 10.6
        y1 = 12.0 + math.sin(angle) * 10.6
        p.drawLine(QPointF(x0, y0), QPointF(x1, y1))


def _draw_moon(p: QPainter, c: QColor) -> None:
    path = QPainterPath()
    path.setFillRule(Qt.FillRule.OddEvenFill)
    path.addEllipse(QRectF(4.4, 3.4, 15.2, 15.2))
    path.addEllipse(QRectF(9.0, 1.4, 15.2, 15.2))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(c))
    p.drawPath(path)


def _draw_minimize(p: QPainter, c: QColor) -> None:
    p.setPen(_pen(c, 1.6))
    p.drawLine(QPointF(6.5, 12.0), QPointF(17.5, 12.0))


def _draw_maximize(p: QPainter, c: QColor) -> None:
    p.setPen(_pen(c, 1.6))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(QRectF(6.5, 6.5, 11.0, 11.0), 2.4, 2.4)


def _draw_restore(p: QPainter, c: QColor) -> None:
    p.setPen(_pen(c, 1.5))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(QRectF(5.0, 8.0, 10.0, 10.0), 2.2, 2.2)
    p.drawLine(QPointF(8.6, 8.0), QPointF(8.6, 5.6))
    p.drawLine(QPointF(8.6, 5.6), QPointF(18.4, 5.6))
    p.drawLine(QPointF(18.4, 5.6), QPointF(18.4, 15.4))
    p.drawLine(QPointF(18.4, 15.4), QPointF(15.2, 15.4))


def _draw_close(p: QPainter, c: QColor) -> None:
    p.setPen(_pen(c, 1.7))
    p.drawLine(QPointF(7.0, 7.0), QPointF(17.0, 17.0))
    p.drawLine(QPointF(17.0, 7.0), QPointF(7.0, 17.0))


def _draw_warning(p: QPainter, c: QColor) -> None:
    p.setPen(_pen(c, 1.7))
    p.setBrush(Qt.BrushStyle.NoBrush)
    tri = QPolygonF([QPointF(12.0, 3.4), QPointF(21.2, 19.6), QPointF(2.8, 19.6)])
    p.drawPolygon(tri)
    p.drawLine(QPointF(12.0, 9.0), QPointF(12.0, 14.2))
    p.setBrush(QBrush(c))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(QRectF(11.1, 16.0, 1.8, 1.8))


def _draw_play(p: QPainter, c: QColor) -> None:
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(c))
    p.drawPolygon(QPolygonF([QPointF(8.4, 5.4), QPointF(19.0, 12.0), QPointF(8.4, 18.6)]))


def _draw_stop(p: QPainter, c: QColor) -> None:
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(c))
    p.drawRoundedRect(QRectF(6.8, 6.8, 10.4, 10.4), 2.4, 2.4)


def _draw_check(p: QPainter, c: QColor) -> None:
    p.setPen(_pen(c, 2.2))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPolyline(
        QPolygonF([QPointF(5.6, 12.6), QPointF(10.0, 17.0), QPointF(18.6, 7.4)])
    )


def _draw_chevron_down(p: QPainter, c: QColor) -> None:
    p.setPen(_pen(c, 1.8))
    p.drawPolyline(
        QPolygonF([QPointF(6.6, 9.6), QPointF(12.0, 15.0), QPointF(17.4, 9.6)])
    )


def _draw_chevron_up(p: QPainter, c: QColor) -> None:
    p.setPen(_pen(c, 1.8))
    p.drawPolyline(
        QPolygonF([QPointF(6.6, 14.4), QPointF(12.0, 9.0), QPointF(17.4, 14.4)])
    )


def _draw_chevron_right(p: QPainter, c: QColor) -> None:
    p.setPen(_pen(c, 1.8))
    p.drawPolyline(
        QPolygonF([QPointF(9.6, 6.6), QPointF(15.0, 12.0), QPointF(9.6, 17.4)])
    )


def _draw_shield(p: QPainter, c: QColor) -> None:
    p.setPen(_pen(c, 1.6))
    p.setBrush(Qt.BrushStyle.NoBrush)
    path = QPainterPath()
    path.moveTo(12.0, 3.2)
    path.lineTo(19.6, 6.0)
    path.lineTo(19.6, 12.2)
    path.cubicTo(19.6, 16.6, 16.4, 19.4, 12.0, 20.8)
    path.cubicTo(7.6, 19.4, 4.4, 16.6, 4.4, 12.2)
    path.lineTo(4.4, 6.0)
    path.closeSubpath()
    p.drawPath(path)
    p.drawPolyline(
        QPolygonF([QPointF(8.8, 12.0), QPointF(11.2, 14.4), QPointF(15.4, 9.6)])
    )


def _draw_folder(p: QPainter, c: QColor) -> None:
    p.setPen(_pen(c, 1.6))
    p.setBrush(Qt.BrushStyle.NoBrush)
    path = QPainterPath()
    path.moveTo(3.6, 7.4)
    path.lineTo(9.4, 7.4)
    path.lineTo(11.2, 9.6)
    path.lineTo(20.4, 9.6)
    path.lineTo(20.4, 18.4)
    path.lineTo(3.6, 18.4)
    path.closeSubpath()
    p.drawPath(path)


def _draw_trash(p: QPainter, c: QColor) -> None:
    p.setPen(_pen(c, 1.6))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawLine(QPointF(4.6, 7.0), QPointF(19.4, 7.0))
    p.drawLine(QPointF(9.6, 7.0), QPointF(9.6, 4.8))
    p.drawLine(QPointF(9.6, 4.8), QPointF(14.4, 4.8))
    p.drawLine(QPointF(14.4, 4.8), QPointF(14.4, 7.0))
    p.drawRoundedRect(QRectF(6.4, 7.0, 11.2, 12.6), 2.0, 2.0)
    p.drawLine(QPointF(10.4, 10.6), QPointF(10.4, 16.2))
    p.drawLine(QPointF(13.6, 10.6), QPointF(13.6, 16.2))


def _draw_drive(p: QPainter, c: QColor) -> None:
    p.setPen(_pen(c, 1.6))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(QRectF(3.4, 6.2, 17.2, 11.6), 2.6, 2.6)
    p.drawLine(QPointF(3.4, 13.4), QPointF(20.6, 13.4))
    p.setBrush(QBrush(c))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(QRectF(16.4, 15.0, 1.9, 1.9))


def _draw_layers(p: QPainter, c: QColor) -> None:
    p.setPen(_pen(c, 1.6))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPolygon(
        QPolygonF([QPointF(12.0, 3.6), QPointF(20.4, 8.2), QPointF(12.0, 12.8), QPointF(3.6, 8.2)])
    )
    p.drawPolyline(
        QPolygonF([QPointF(3.6, 12.6), QPointF(12.0, 17.2), QPointF(20.4, 12.6)])
    )
    p.drawPolyline(
        QPolygonF([QPointF(3.6, 16.4), QPointF(12.0, 21.0), QPointF(20.4, 16.4)])
    )


def _draw_arrow_right(p: QPainter, c: QColor) -> None:
    p.setPen(_pen(c, 1.7))
    p.drawLine(QPointF(4.4, 12.0), QPointF(18.0, 12.0))
    p.drawPolyline(
        QPolygonF([QPointF(13.4, 7.4), QPointF(18.6, 12.0), QPointF(13.4, 16.6)])
    )


def _draw_info(p: QPainter, c: QColor) -> None:
    p.setPen(_pen(c, 1.6))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(QRectF(3.8, 3.8, 16.4, 16.4))
    p.drawLine(QPointF(12.0, 11.0), QPointF(12.0, 16.6))
    p.setBrush(QBrush(c))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(QRectF(11.1, 7.2, 1.8, 1.8))


def _draw_disk(p: QPainter, c: QColor) -> None:
    p.setPen(_pen(c, 1.6))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(QRectF(3.6, 3.6, 16.8, 16.8))
    p.drawEllipse(QRectF(9.4, 9.4, 5.2, 5.2))
    p.drawArc(QRectF(6.2, 6.2, 11.6, 11.6), 20 * 16, 90 * 16)


def _draw_lock(p: QPainter, c: QColor) -> None:
    p.setPen(_pen(c, 1.6))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(QRectF(5.2, 10.6, 13.6, 9.6), 2.4, 2.4)
    p.drawArc(QRectF(8.0, 4.0, 8.0, 10.0), 0, 180 * 16)


_DRAWERS: Dict[str, Callable[[QPainter, QColor], None]] = {
    "scan": _draw_scan,
    "refresh": _draw_refresh,
    "sun": _draw_sun,
    "moon": _draw_moon,
    "minimize": _draw_minimize,
    "maximize": _draw_maximize,
    "restore": _draw_restore,
    "close": _draw_close,
    "warning": _draw_warning,
    "play": _draw_play,
    "stop": _draw_stop,
    "check": _draw_check,
    "chevron-down": _draw_chevron_down,
    "chevron-up": _draw_chevron_up,
    "chevron-right": _draw_chevron_right,
    "shield": _draw_shield,
    "folder": _draw_folder,
    "trash": _draw_trash,
    "drive": _draw_drive,
    "layers": _draw_layers,
    "arrow-right": _draw_arrow_right,
    "info": _draw_info,
    "disk": _draw_disk,
    "lock": _draw_lock,
}


# --------------------------------------------------------------------------- #
# 对外接口
# --------------------------------------------------------------------------- #
def paint_icon(
    painter: QPainter,
    name: str,
    rect: QRectF,
    color: QColor,
    stroke_scale: float = 1.0,
) -> None:
    """把图标绘制到给定矩形内（保持宽高比、居中）。"""
    drawer = _DRAWERS.get(name)
    if drawer is None:
        return
    side = min(rect.width(), rect.height())
    if side <= 0:
        return
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.translate(
        rect.center().x() - side / 2.0,
        rect.center().y() - side / 2.0,
    )
    painter.scale(side / _UNIT, side / _UNIT)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    drawer(painter, color)
    painter.restore()


def pixmap(
    name: str,
    size: int = 18,
    color: str | QColor = "#0F172A",
    dpr: float = 2.0,
) -> QPixmap:
    """生成指定尺寸的图标位图（自带 devicePixelRatio，高分屏不模糊）。"""
    physical = max(1, int(round(size * dpr)))
    canvas = QPixmap(physical, physical)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    paint_icon(painter, name, QRectF(0, 0, physical, physical), QColor(color))
    painter.end()
    canvas.setDevicePixelRatio(dpr)
    return canvas


def icon(
    name: str,
    size: int = 18,
    color: str | QColor = "#0F172A",
) -> QIcon:
    """生成 ``QIcon``（含普通 / 禁用两态）。"""
    result = QIcon()
    result.addPixmap(pixmap(name, size, color), QIcon.Mode.Normal)
    result.addPixmap(pixmap(name, size, "#94A3B8"), QIcon.Mode.Disabled)
    return result


def app_logo(size: int = 28) -> QPixmap:
    """应用图标：圆角渐变方块 + 磁盘线稿。"""
    dpr = 2.0
    physical = max(8, int(round(size * dpr)))
    canvas = QPixmap(physical, physical)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    gradient = QLinearGradient(0, 0, physical, physical)
    gradient.setColorAt(0.0, QColor("#2563EB"))
    gradient.setColorAt(1.0, QColor("#0EA5E9"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(gradient))
    radius = physical * 0.28
    painter.drawRoundedRect(QRectF(0, 0, physical, physical), radius, radius)

    inset = physical * 0.24
    paint_icon(
        painter,
        "disk",
        QRectF(inset, inset, physical - inset * 2, physical - inset * 2),
        QColor("#FFFFFF"),
    )
    painter.end()
    canvas.setDevicePixelRatio(dpr)
    return canvas


def draw_check_mark(painter: QPainter, rect: QRectF, color: QColor, width: float = 2.0) -> None:
    """在给定矩形内绘制一个对勾（复选框 / 徽章复用）。"""
    pen = _pen(color, width)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPolyline(
        QPolygonF(
            [
                QPointF(rect.left() + rect.width() * 0.24, rect.top() + rect.height() * 0.54),
                QPointF(rect.left() + rect.width() * 0.44, rect.top() + rect.height() * 0.74),
                QPointF(rect.left() + rect.width() * 0.78, rect.top() + rect.height() * 0.28),
            ]
        )
    )


def draw_lock_badge(
    painter: QPainter, rect: QRectF, color: QColor, scale: float = 0.52
) -> None:
    """在矩形中央绘制一个小锁形标记（保留区语义）。

    尺寸小于 9 px 时直接跳过 —— 硬画出来只会变成一个脏点。
    只改变 ``pen`` / ``brush``，不改 ``clip``，调用方可在循环里反复用。
    """
    size = min(rect.width(), rect.height())
    if size < 9:
        return
    size *= max(0.2, min(1.0, scale))
    center_x = rect.center().x()
    center_y = rect.center().y()

    body = QRectF(center_x - size * 0.26, center_y - size * 0.04,
                  size * 0.52, size * 0.40)
    painter.save()
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(_pen(color, max(1.0, size * 0.09)))
    shackle = QRectF(
        body.left() + size * 0.09,
        body.top() - size * 0.26,
        body.width() - size * 0.18,
        size * 0.34,
    )
    painter.drawArc(shackle, 0, 180 * 16)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    painter.drawRoundedRect(body, size * 0.08, size * 0.08)
    painter.restore()

