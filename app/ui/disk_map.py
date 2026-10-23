"""核心视觉控件：自绘卷 LBA 物理布局图。

* 横向条带从左到右对应 LCN 递增；
* 分段渲染：已占用簇 / 空闲簇 / 待迁移文件 / 末端预留区 / MFT 保留区；
* 上方刻度尺随宽度自适应密度，同时标注 LCN 与 GB 偏移；
* 超长卷按像素聚合（每个像素代表多个簇），用占用密度决定颜色深浅；
* 鼠标悬停自绘气泡提示（LCN、所属文件、状态）。
"""

from __future__ import annotations

import bisect
from typing import List, Optional, Sequence, Tuple

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from app.core.models import GB, human_bytes
from app.ui import icons, theme

# 聚合桶数量（决定密度分辨率）
BUCKETS = 3000
# 每桶最多采样字节数（大卷时做等距抽样，避免 O(位图大小) 的 Python 循环）
MAX_SAMPLE_PER_BUCKET = 1024

_POPCOUNT = bytes(bin(i).count("1") for i in range(256))

_GUTTER = 62.0     # 左侧"迁移前/迁移后"标签宽度
_RIGHT_PAD = 4.0
_RULER_H = 32.0
_BAND_H = 40.0
_BAND_GAP = 10.0
_LEGEND_H = 20.0


def _blend(a: QColor, b: QColor, t: float) -> QColor:
    """线性混合两个颜色（``t=0`` 返回 ``a``）。"""
    t = max(0.0, min(1.0, t))
    return QColor(
        int(a.red() + (b.red() - a.red()) * t),
        int(a.green() + (b.green() - a.green()) * t),
        int(a.blue() + (b.blue() - a.blue()) * t),
        int(a.alpha() + (b.alpha() - a.alpha()) * t),
    )


def _nice_step(units_per_pixel: float, min_pixels: float) -> int:
    """挑选"好看"的刻度步长（1/2/5 × 10^k），使标签间距不小于 ``min_pixels``。"""
    if units_per_pixel <= 0:
        return 1
    need = units_per_pixel * min_pixels
    exponent = 0
    base = 1.0
    while base * 10 <= need:
        base *= 10
        exponent += 1
    for multiplier in (1.0, 2.0, 5.0, 10.0):
        step = base * multiplier
        if step >= need:
            return max(1, int(step))
    return max(1, int(base * 10))


def _hatch(
    painter: QPainter,
    band: QRectF,
    color: QColor,
    region: Optional[QRectF] = None,
    step: float = 7.0,
) -> None:
    """在 ``region``（缺省整条带）内画 45° 斜纹，用 ``band`` 做裁剪上界。

    斜纹是「保留区 / MFT 区」的**第二重编码** —— 只靠颜色区分在深色主题下
    容易和「已占用簇」混淆，加上斜纹后即使色觉受限也能分辨。
    """
    area = (region if region is not None else band).intersected(band)
    if area.width() < 1.0 or area.height() < 1.0:
        return
    painter.save()
    painter.setClipRect(area)
    painter.setPen(QPen(color, 1.0))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    offset = -area.height()
    while offset < area.width() + area.height():
        painter.drawLine(
            QPointF(area.left() + offset, area.bottom()),
            QPointF(area.left() + offset + area.height(), area.top()),
        )
        offset += step
    painter.restore()



class DiskMapWidget(QWidget):
    """卷 LBA 布局图。"""

    lcnHovered = Signal(int)
    #: 用户在条带上拖选出一个区间（``start``, ``end`` 均为 LCN，``end`` 开区间）
    windowSelected = Signal(int, int)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._total_clusters: int = 0
        self._bytes_per_cluster: int = 0
        self._density: List[float] = []
        self._sources: List[Tuple[int, int]] = []
        self._targets: List[Tuple[int, int]] = []
        self._reserve_floor: int = 0
        self._mft: List[Tuple[int, int]] = []
        self._file_ranges: List[Tuple[int, int, str]] = []
        self._file_starts: List[int] = []
        self._has_plan = False
        self._hover = QPointF(-1, -1)
        self._hover_lcn = -1
        self._hover_text: List[str] = []
        # ---- v2 新增的三类元素 ---- #
        self._window: Optional[Tuple[int, int]] = None    # 目标区间
        self._reserve_head: int = 0                        # 卷首保留簇数
        self._reserve_tail: int = -1                       # 卷尾保留起始 LCN
        self._blockers: List[Tuple[int, int]] = []         # 挡路者占用区间
        # ---- 拖选状态 ---- #
        self._drag_anchor: int = -1
        self._drag_current: int = -1
        self._dragging: bool = False
        self._selectable: bool = True
        # ---- 渲染缓存（1.0.0D 性能优化）---- #
        #: 条带图像缓存：键 = (主题模式, 像素宽, 是否"迁移后")。
        #: ``_build_band_image`` 每次要跑 width 次 Python 循环（数千像素），
        #: 悬停 / 拖动时的重绘若都重建会白白烧 CPU。失效时机见
        #: :meth:`_invalidate_band_cache`（数据代次变化）与 :meth:`resizeEvent`
        #: （宽度变化）。主题切换由键里的 ``theme.theme().mode`` 自动区分。
        self._band_cache: dict = {}
        self.setMouseTracking(True)
        self.setMinimumHeight(200)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)


    # ------------------------------------------------------------------ #
    # 数据接口
    # ------------------------------------------------------------------ #
    def set_volume(self, total_clusters: int, bytes_per_cluster: int, bits: bytes) -> None:
        """设置卷信息与位图，并重算聚合密度。"""
        self._total_clusters = max(0, int(total_clusters))
        self._bytes_per_cluster = max(0, int(bytes_per_cluster))
        self._density = self._compute_density(bits, self._total_clusters)
        self._invalidate_band_cache()
        self.update()

    def _invalidate_band_cache(self) -> None:
        """清空条带图像缓存（任何影响密度 / 迁移高亮的数据变化都必须调用）。"""
        self._band_cache.clear()

    def clear(self) -> None:
        """清空全部数据（回到空状态）。"""
        self._total_clusters = 0
        self._density = []
        self._sources = []
        self._targets = []
        self._mft = []
        self._file_ranges = []
        self._file_starts = []
        self._reserve_floor = 0
        self._has_plan = False
        self._hover_lcn = -1
        self._hover_text = []
        self._window = None
        self._reserve_head = 0
        self._reserve_tail = -1
        self._blockers = []
        self._drag_anchor = -1
        self._drag_current = -1
        self._dragging = False
        self._band_cache.clear()
        self.update()

    def set_mft_zone(self, start_lcn: int, end_lcn: int) -> None:
        """标注 MFT 保留区（斜纹）。"""
        self._mft = [(start_lcn, end_lcn)] if end_lcn > start_lcn else []
        self.update()

    def set_reserve(self, floor_lcn: int) -> None:
        """设置末端预留区起始 LCN（保留接口，等价于只设卷尾）。"""
        self._reserve_tail = max(0, int(floor_lcn)) if floor_lcn > 0 else -1
        self.update()

    def set_target_window(self, start_lcn: int, end_lcn: int) -> None:
        """设置目标区间 ``[start, end)``；``end <= start`` 视为未设定。"""
        start, end = int(start_lcn), int(end_lcn)
        if end <= start:
            self._window = None
        else:
            self._window = (max(0, start), max(0, end))
        self.update()

    def set_reserve_zones(self, head_clusters: int, tail_start_lcn: int) -> None:
        """设置卷首 / 卷尾保留区（斜纹 + 锁形标记）。"""
        self._reserve_head = max(0, int(head_clusters))
        self._reserve_tail = int(tail_start_lcn) if tail_start_lcn > 0 else -1
        self.update()

    def set_blocker_ranges(self, ranges: Sequence[Tuple[int, int]]) -> None:
        """设置挡路者占用的区间（第三色）。"""
        self._blockers = self._merge(ranges)
        self.update()

    def set_selectable(self, selectable: bool) -> None:
        """是否允许拖选（卷不可用时关闭）。"""
        self._selectable = bool(selectable)


    def set_plan_ranges(
        self,
        sources: Sequence[Tuple[int, int]],
        targets: Sequence[Tuple[int, int]],
        file_ranges: Sequence[Tuple[int, int, str]] = (),
    ) -> None:
        """设置迁移前后的高亮区间。"""
        self._sources = self._merge(sources)
        self._targets = self._merge(targets)
        ordered = sorted(file_ranges, key=lambda item: item[0])
        self._file_ranges = list(ordered)
        self._file_starts = [item[0] for item in ordered]
        self._has_plan = bool(self._sources or self._targets)
        self._invalidate_band_cache()
        self.update()

    def clear_plan_ranges(self) -> None:
        """清除高亮（回到纯布局视图）。"""
        self._sources = []
        self._targets = []
        self._file_ranges = []
        self._file_starts = []
        self._has_plan = False
        self._invalidate_band_cache()
        self.update()

    # ------------------------------------------------------------------ #
    # 密度聚合
    # ------------------------------------------------------------------ #
    @staticmethod
    def _compute_density(bits: bytes, total_clusters: int) -> List[float]:
        """把位图聚合成 ``BUCKETS`` 个 0..1 的占用密度。"""
        if total_clusters <= 0 or not bits:
            return []
        total_bytes = len(bits)
        density: List[float] = [0.0] * BUCKETS
        for index in range(BUCKETS):
            byte_start = int(total_bytes * index / BUCKETS)
            byte_end = int(total_bytes * (index + 1) / BUCKETS)
            if byte_end <= byte_start:
                byte_end = min(total_bytes, byte_start + 1)
            length = byte_end - byte_start
            if length <= 0:
                continue
            if length > MAX_SAMPLE_PER_BUCKET:
                stride = max(1, length // MAX_SAMPLE_PER_BUCKET)
                sample = bits[byte_start:byte_end:stride]
            else:
                sample = bits[byte_start:byte_end]
            if not sample:
                continue
            occupied = sum(sample.translate(_POPCOUNT))
            density[index] = occupied / float(len(sample) * 8)
        return density

    @staticmethod
    def _merge(ranges: Sequence[Tuple[int, int]]) -> List[Tuple[int, int]]:
        """合并重叠 / 相邻区间。"""
        cleaned = sorted((int(s), int(e)) for s, e in ranges if e > s)
        merged: List[Tuple[int, int]] = []
        for start, end in cleaned:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        return merged

    # ------------------------------------------------------------------ #
    # 坐标换算
    # ------------------------------------------------------------------ #
    def _band_rect(self, index: int) -> QRectF:
        top = _RULER_H + 6 + index * (_BAND_H + _BAND_GAP)
        return QRectF(_GUTTER, top, max(1.0, self.width() - _GUTTER - _RIGHT_PAD), _BAND_H)

    def _rect_to_lcn(self, rect: QRectF, x: float) -> int:
        if self._total_clusters <= 0 or rect.width() <= 0:
            return -1
        ratio = (x - rect.left()) / rect.width()
        ratio = max(0.0, min(0.999999, ratio))
        return int(ratio * self._total_clusters)

    def _lcn_to_x(self, rect: QRectF, lcn: int) -> float:
        if self._total_clusters <= 0:
            return rect.left()
        return rect.left() + rect.width() * (lcn / float(self._total_clusters))

    def _band_clip(self, rect: QRectF) -> QPainterPath:
        """条带圆角裁剪路径（保证所有叠加元素不越出圆角）。"""
        path = QPainterPath()
        path.addRoundedRect(rect, 8, 8)
        return path

    def _preview_window(self) -> Optional[Tuple[int, int]]:
        """返回当前要绘制的目标区间：拖选预览优先于已设定的区间。"""
        if self._dragging and self._drag_anchor >= 0 and self._drag_current >= 0:
            start = min(self._drag_anchor, self._drag_current)
            end = max(self._drag_anchor, self._drag_current)
            if end > start:
                return start, end
        return self._window


    # ------------------------------------------------------------------ #
    # 绘制
    # ------------------------------------------------------------------ #
    def paintEvent(self, event) -> None:  # noqa: D102
        palette = theme.palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        if self._total_clusters <= 0:
            self._paint_empty(painter, palette)
            painter.end()
            return

        self._paint_ruler(painter, palette)
        self._paint_band(painter, palette, 0, "迁移前", after=False)
        self._paint_band(painter, palette, 1, "迁移后", after=True)
        self._paint_legend(painter, palette)

        if self._hover_lcn >= 0 and self._hover_text:
            self._paint_hover_bubble(painter, palette)
        painter.end()

    # ---------------- 空状态 ---------------- #
    def _paint_empty(self, painter: QPainter, palette) -> None:
        rect = QRectF(self.rect())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(palette["sunken"]))
        painter.drawRoundedRect(rect.adjusted(0, 0, 0, 0), 10, 10)

        # 虚线示意条
        line = QRectF(24, rect.center().y() - 10, max(10.0, rect.width() - 48), 20)
        pen = QPen(QColor(palette["border_strong"]), 1.4, Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(line, 10, 10)

        font = QFont(theme.app_font(12))
        painter.setFont(font)
        painter.setPen(QColor(palette["text_faint"]))
        painter.drawText(
            rect,
            int(Qt.AlignmentFlag.AlignCenter),
            "选择卷并点击「加载布局」后，这里会显示物理簇分布",
        )

    # ---------------- 刻度尺 ---------------- #
    def _paint_ruler(self, painter: QPainter, palette) -> None:
        rect = QRectF(
            _GUTTER,
            2.0,
            max(1.0, self.width() - _GUTTER - _RIGHT_PAD),
            _RULER_H - 6.0,
        )
        units_per_pixel = self._total_clusters / max(1.0, rect.width())
        step = _nice_step(units_per_pixel, 96.0)

        font_main = QFont(theme.mono_font(10))
        font_sub = QFont(theme.app_font(9))
        painter.setFont(font_main)

        pen = QPen(QColor(palette["border_strong"]), 1.0)
        painter.setPen(pen)

        lcn = 0
        while lcn <= self._total_clusters:
            x = self._lcn_to_x(rect, lcn)
            painter.setPen(QPen(QColor(palette["border_strong"]), 1.0))
            painter.drawLine(QPointF(x, rect.bottom() - 5), QPointF(x, rect.bottom()))

            painter.setFont(font_main)
            painter.setPen(QColor(palette["text_dim"]))
            label = f"{lcn:,}"
            label_rect = QRectF(x - 44, 0, 88, 14)
            # 首尾标签夹紧在条带范围内，避免被控件边缘裁切
            if label_rect.left() < rect.left():
                label_rect.moveLeft(rect.left())
            if label_rect.right() > rect.right():
                label_rect.moveRight(rect.right())
            painter.drawText(
                label_rect,
                int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop),
                label,
            )

            if self._bytes_per_cluster > 0:
                offset_gb = lcn * self._bytes_per_cluster / GB
                painter.setFont(font_sub)
                painter.setPen(QColor(palette["text_faint"]))
                sub_rect = QRectF(label_rect.left(), 14, 88, 12)
                if sub_rect.left() < rect.left():
                    sub_rect.moveLeft(rect.left())
                if sub_rect.right() > rect.right():
                    sub_rect.moveRight(rect.right())
                painter.drawText(
                    sub_rect,
                    int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop),
                    f"{offset_gb:.1f} GB",
                )
            lcn += step

        painter.setPen(QPen(QColor(palette["border"]), 1.0))
        painter.drawLine(
            QPointF(rect.left(), rect.bottom()),
            QPointF(rect.right(), rect.bottom()),
        )

    # ---------------- 条带 ---------------- #
    def _paint_band(
        self,
        painter: QPainter,
        palette,
        index: int,
        caption: str,
        after: bool,
    ) -> None:
        rect = self._band_rect(index)
        width = int(rect.width())
        if width <= 0:
            return

        image = self._build_band_image(width, after)
        if image is not None:
            path = QPainterPath()
            path.addRoundedRect(rect, 8, 8)
            painter.save()
            painter.setClipPath(path)
            painter.drawImage(rect, image)
            painter.restore()

        # ---- 挡路者（橙）：先画，随后被目标区间压在上面 ---- #
        if self._blockers:
            painter.save()
            painter.setClipPath(self._band_clip(rect))
            for start, end in self._blockers:
                x0 = self._lcn_to_x(rect, start)
                x1 = self._lcn_to_x(rect, end)
                if x1 - x0 < 1.0:
                    x1 = x0 + 1.0
                painter.setPen(Qt.PenStyle.NoPen)
                overlay = QColor(palette["blocker"])
                overlay.setAlpha(150)
                painter.setBrush(overlay)
                painter.drawRect(QRectF(x0, rect.top(), x1 - x0, rect.height()))
            painter.restore()

        # ---- MFT 保留区（斜纹）---- #
        for start, end in self._mft:
            x0 = self._lcn_to_x(rect, start)
            x1 = self._lcn_to_x(rect, end)
            if x1 - x0 < 1.0:
                x1 = x0 + 1.0
            _hatch(painter, QRectF(x0, rect.top(), x1 - x0, rect.height()),
                   QColor(palette["mft"]), rect.intersected(
                       QRectF(x0, rect.top(), x1 - x0, rect.height())))

        # ---- 保留区（卷首 / 卷尾）：斜纹 + 锁标 ---- #
        reserve_color = QColor(palette["reserve"])
        zones: List[Tuple[int, int]] = []
        if self._reserve_head > 0:
            zones.append((0, min(self._reserve_head, self._total_clusters)))
        if 0 <= self._reserve_tail < self._total_clusters:
            zones.append((self._reserve_tail, self._total_clusters))
        for start, end in zones:
            x0 = self._lcn_to_x(rect, start)
            x1 = self._lcn_to_x(rect, end)
            if x1 - x0 < 1.0:
                x1 = x0 + 1.0
            zone = QRectF(x0, rect.top(), x1 - x0, rect.height()).intersected(rect)
            painter.save()
            painter.setClipPath(self._band_clip(rect))
            fill = QColor(palette["reserve_soft"])
            fill.setAlpha(200)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill)
            painter.drawRect(zone)
            painter.restore()
            _hatch(painter, rect, reserve_color, zone)
            painter.setPen(QPen(reserve_color, 1.0, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawLine(QPointF(zone.left(), rect.top()),
                             QPointF(zone.left(), rect.bottom()))
            painter.drawLine(QPointF(zone.right(), rect.top()),
                             QPointF(zone.right(), rect.bottom()))
            if zone.width() >= 22:
                icons.draw_lock_badge(painter, zone, reserve_color)

        # ---- 目标区间（紫）：填充 + 实线边框 + 上限标签 ---- #
        window = self._preview_window()
        if window is not None:
            start, end = window
            x0 = self._lcn_to_x(rect, start)
            x1 = self._lcn_to_x(rect, end)
            if x1 - x0 < 2.0:
                x1 = x0 + 2.0
            zone = QRectF(x0, rect.top(), x1 - x0, rect.height()).intersected(rect)
            painter.save()
            painter.setClipPath(self._band_clip(rect))
            fill = QColor(palette["target_soft"])
            fill.setAlpha(230)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill)
            painter.drawRect(zone)
            painter.restore()
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(palette["target"]), 1.6))
            painter.drawRoundedRect(zone.adjusted(0.8, 0.8, -0.8, -0.8), 5, 5)
            label = f"目标区间 {start:,} – {max(start, end - 1):,}"
            font = QFont(theme.app_font(10, QFont.Weight.DemiBold))
            painter.setFont(font)
            metrics = QFontMetrics(font)
            text_width = metrics.horizontalAdvance(label) + 12
            if zone.width() >= text_width:
                chip = QRectF(
                    zone.left() + 3,
                    rect.top() + 3,
                    min(text_width, zone.width() - 6),
                    15,
                )
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(palette["target"]))
                painter.drawRoundedRect(chip, 4, 4)
                painter.setPen(QColor("#FFFFFF"))
                painter.drawText(
                    chip,
                    int(Qt.AlignmentFlag.AlignCenter),
                    metrics.elidedText(
                        label, Qt.TextElideMode.ElideRight, int(chip.width() - 6)
                    ),
                )

        # ---- 外框 ---- #
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(palette["border"]), 1.0))
        painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)


        # 左侧标签
        font = QFont(theme.app_font(11, QFont.Weight.DemiBold))
        painter.setFont(font)
        painter.setPen(
            QColor(palette["accent"] if after and self._has_plan else palette["text_dim"])
        )
        painter.drawText(
            QRectF(0, rect.top(), _GUTTER - 10, rect.height()),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            caption,
        )

    def _build_band_image(self, width: int, after: bool) -> Optional[QImage]:
        """按像素生成条带图像（每像素对多簇取占用密度）。

        结果按 ``(主题模式, width, after)`` 缓存 —— 生成一次要跑 ``width`` 次
        Python 循环，而重绘（悬停 / 拖动 / 局部失效）远比数据变化频繁。
        缓存失效：数据变化走 :meth:`_invalidate_band_cache`；宽度变化走
        :meth:`resizeEvent`；主题切换体现在键里，天然区分。
        """
        key = (theme.theme().mode, width, after)
        cached = self._band_cache.get(key)
        if cached is not None:
            return cached

        image = QImage(width, 1, QImage.Format.Format_RGB32)
        if not self._density:
            image.fill(QColor(theme.palette()["free"]))
            self._band_cache[key] = image
            return image

        palette = theme.palette()
        free = QColor(palette["free"])
        occupied = QColor(palette["occupied_deep"])
        accent = QColor(palette["accent"])
        cyan = QColor(palette["cyan"])

        total = self._total_clusters
        bucket_count = len(self._density)
        sources = self._sources
        targets = self._targets

        src_index = 0
        tgt_index = 0
        src_len = len(sources)
        tgt_len = len(targets)

        for x in range(width):
            l0 = total * x // width
            l1 = total * (x + 1) // width
            if l1 <= l0:
                l1 = l0 + 1
            span = float(l1 - l0)

            bucket = min(bucket_count - 1, int(x * bucket_count / width))
            density = self._density[bucket]

            while src_index < src_len and sources[src_index][1] <= l0:
                src_index += 1
            src_cov = 0.0
            probe = src_index
            while probe < src_len and sources[probe][0] < l1:
                start, end = sources[probe]
                src_cov += max(0, min(l1, end) - max(l0, start))
                probe += 1
            src_cov = min(1.0, src_cov / span)

            while tgt_index < tgt_len and targets[tgt_index][1] <= l0:
                tgt_index += 1
            tgt_cov = 0.0
            probe = tgt_index
            while probe < tgt_len and targets[probe][0] < l1:
                start, end = targets[probe]
                tgt_cov += max(0, min(l1, end) - max(l0, start))
                probe += 1
            tgt_cov = min(1.0, tgt_cov / span)

            if after:
                base_density = density * (1.0 - src_cov)
            else:
                base_density = density

            color = _blend(free, occupied, max(0.0, min(1.0, base_density)))
            # 单个像素可能只覆盖文件的一小部分，这里放大强度，
            # 保证 1~2 像素宽的小文件也能被肉眼分辨出来。
            if after:
                if tgt_cov > 0.0:
                    strength = min(1.0, tgt_cov * 3.0)
                    color = _blend(color, _blend(accent, cyan, x / max(1, width)), strength)
            elif src_cov > 0.0:
                strength = min(1.0, src_cov * 3.0)
                color = _blend(color, _blend(accent, cyan, x / max(1, width)), strength)
            image.setPixel(x, 0, color.rgb())
        self._band_cache[key] = image
        return image

    def resizeEvent(self, event) -> None:  # noqa: D102
        # 宽度变了 → 条带图像按像素宽度生成，旧缓存全部作废
        if self._band_cache:
            self._band_cache.clear()
        super().resizeEvent(event)

    # ---------------- 图例 ---------------- #
    def _paint_legend(self, painter: QPainter, palette) -> None:
        top = self._band_rect(1).bottom() + 8
        font = QFont(theme.app_font(11))
        painter.setFont(font)
        metrics = QFontMetrics(font)

        items: List[Tuple[QColor, QColor, str]] = [
            (QColor(palette["occupied"]), QColor(palette["border"]), "已占用簇"),
            (QColor(palette["free"]), QColor(palette["border_strong"]), "空闲簇"),
            (QColor(palette["accent"]), QColor(palette["accent"]), "待迁移文件"),
            (QColor(palette["target"]), QColor(palette["target"]), "目标区间"),
            (QColor(palette["blocker"]), QColor(palette["blocker"]), "挡路者"),
            (QColor(palette["reserve"]), QColor(palette["reserve"]), "保留区"),
            (QColor(palette["mft"]), QColor(palette["border_strong"]), "MFT 保留区"),
        ]

        x = _GUTTER
        y = top
        for fill, border, text in items:
            swatch = QRectF(x, y + 5, 10, 10)
            painter.setPen(QPen(border, 1.0))
            painter.setBrush(fill)
            painter.drawRoundedRect(swatch, 3, 3)
            painter.setPen(QColor(palette["text_dim"]))
            painter.drawText(
                QRectF(x + 15, y, metrics.horizontalAdvance(text) + 6, _LEGEND_H),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                text,
            )
            x += 15 + metrics.horizontalAdvance(text) + 20
            if x > self.width() - 200:
                break

        # 右侧统计
        if self._total_clusters > 0 and self._bytes_per_cluster > 0:
            total_bytes = self._total_clusters * self._bytes_per_cluster
            info = f"共 {self._total_clusters:,} 簇 · {human_bytes(total_bytes)}"
            painter.setPen(QColor(palette["text_faint"]))
            painter.drawText(
                QRectF(_GUTTER, y, self.width() - _GUTTER - _RIGHT_PAD, _LEGEND_H),
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                info,
            )

    # ---------------- 悬停气泡 ---------------- #
    def _hover_bubble_rect(self) -> Optional[QRectF]:
        """计算悬停气泡矩形（绘制与"局部失效"共用同一套几何，保证不漂移）。"""
        if self._hover_lcn < 0 or not self._hover_text:
            return None
        font = QFont(theme.app_font(11))
        metrics = QFontMetrics(font)
        line_height = metrics.height() + 2
        padding = 9
        width = max(metrics.horizontalAdvance(line) for line in self._hover_text) + padding * 2
        height = line_height * len(self._hover_text) + padding * 2

        x = self._hover.x() + 14
        y = self._hover.y() - height - 10
        if x + width > self.width() - 4:
            x = self.width() - width - 4
        if y < 4:
            y = self._hover.y() + 18
        if y + height > self.height() - 4:
            y = self.height() - height - 4
        return QRectF(max(4.0, x), y, width, height)

    def _paint_hover_bubble(self, painter: QPainter, palette) -> None:
        bubble = self._hover_bubble_rect()
        if bubble is None:
            return
        font = QFont(theme.app_font(11))
        painter.setFont(font)
        metrics = QFontMetrics(font)
        line_height = metrics.height() + 2
        padding = 9
        width = bubble.width()
        height = bubble.height()

        painter.setPen(QPen(QColor(palette["border_strong"]), 1.0))
        painter.setBrush(QColor(palette["elevated"]))
        painter.drawRoundedRect(bubble, 8, 8)

        painter.setPen(QColor(palette["text"]))
        for index, line in enumerate(self._hover_text):
            color = QColor(palette["text"] if index == 0 else palette["text_dim"])
            painter.setPen(color)
            painter.drawText(
                QRectF(
                    bubble.left() + padding,
                    bubble.top() + padding + index * line_height,
                    width - padding * 2,
                    line_height,
                ),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                line,
            )

    # ------------------------------------------------------------------ #
    # 交互
    # ------------------------------------------------------------------ #
    def mousePressEvent(self, event) -> None:  # noqa: D102
        if event.button() == Qt.MouseButton.LeftButton and self._selectable:
            position = event.position()
            for index in (0, 1):
                rect = self._band_rect(index)
                if rect.contains(position):
                    lcn = self._rect_to_lcn(rect, position.x())
                    if lcn >= 0:
                        self._drag_anchor = lcn
                        self._drag_current = lcn
                        self._dragging = True
                        self.update()
                        event.accept()
                        return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: D102
        position = event.position()
        old_bubble = self._hover_bubble_rect()
        self._hover = position
        self._hover_lcn = -1
        self._hover_text = []

        if self._total_clusters > 0:
            for index in (0, 1):
                rect = self._band_rect(index)
                if rect.left() <= position.x() <= rect.right() and (
                    rect.top() <= position.y() <= rect.bottom()
                ):
                    lcn = self._rect_to_lcn(rect, position.x())
                    if lcn >= 0:
                        self._hover_lcn = lcn
                        if self._dragging:
                            # 拖选时把末端推到下一簇，避免"零宽区间"
                            self._drag_current = min(
                                self._total_clusters, lcn + 1
                            )
                            # ★ 1.0.0D：拖动中**不再**发 windowSelected ——
                            # 信号会触发主窗口连锁重算（2-3 次全图重绘）。
                            # 实时反馈交给 `_preview_window()` 画的紫色预览框，
                            # 一次性同步推迟到 mouseReleaseEvent。
                        self._hover_text = self._describe(lcn, index == 1)
                        self.lcnHovered.emit(lcn)
                    break

        if self._dragging:
            # 拖动中：预览框跨两条条带，直接整幅重绘（条带图像有缓存，开销可控）
            self.update()
        else:
            # ★ 1.0.0D：悬停只重绘「旧气泡 ∪ 新气泡」两块矩形，
            # 条带 / 刻度 / 图例从 backing store 拷贝，不再整图重画。
            new_bubble = self._hover_bubble_rect()
            region: Optional[QRectF] = None
            for rect in (old_bubble, new_bubble):
                if rect is None:
                    continue
                region = rect if region is None else region.united(rect)
            if region is not None:
                self.update(region.toRect().adjusted(-2, -2, 2, 2))
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: D102
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            start = min(self._drag_anchor, self._drag_current)
            end = max(self._drag_anchor, self._drag_current)
            self._dragging = False
            self._drag_anchor = -1
            self._drag_current = -1
            if end > start:
                self._window = (start, end)
                self.windowSelected.emit(start, end)
            self.update()
            event.accept()
            return
        super().mouseReleaseEvent(event)


    def leaveEvent(self, event) -> None:  # noqa: D102
        self._hover = QPointF(-1, -1)
        self._hover_lcn = -1
        self._hover_text = []
        self.update()
        super().leaveEvent(event)

    def _describe(self, lcn: int, after: bool) -> List[str]:
        """生成悬停提示文案。"""
        palette = theme.palette()
        lines: List[str] = [f"LCN {lcn:,}"]

        if self._bytes_per_cluster > 0:
            offset = lcn * self._bytes_per_cluster
            lines.append(f"偏移 {human_bytes(offset)}")

        state = "空闲" if self._density_at(lcn) < 0.5 else "已占用"

        # 语义优先级：保留区 > MFT > 挡路者 > 目标区间 > 迁移源/目标
        if self._reserve_head > 0 and lcn < self._reserve_head:
            state = "卷首保留区（工具不会使用）"
        elif 0 <= self._reserve_tail <= lcn:
            state = "卷尾保留区（工具不会使用）"
        else:
            for start, end in self._mft:
                if start <= lcn < end:
                    state = "MFT 保留区"
                    break
        for start, end in self._blockers:
            if start <= lcn < end:
                state = "挡路者占用（需迁出）"
                break
        window = self._window
        if window is not None and window[0] <= lcn < window[1]:
            state = f"目标区间内 · {state}"
        if self._has_plan:
            for start, end in self._sources:
                if start <= lcn < end:
                    state = "迁移后释放" if after else "待迁移文件（源）"
                    break
            for start, end in self._targets:
                if start <= lcn < end:
                    state = "迁移目标（新位置）" if after else "迁移目标（预留）"
                    break

        lines.append(f"状态 {state}")

        name = self._file_at(lcn)
        if name:
            lines.append(name)
        _ = palette  # 配色由调用方决定，这里只产文本
        return lines

    def _density_at(self, lcn: int) -> float:
        if not self._density or self._total_clusters <= 0:
            return 0.0
        bucket = int(lcn * len(self._density) / self._total_clusters)
        bucket = max(0, min(bucket, len(self._density) - 1))
        return self._density[bucket]

    def _file_at(self, lcn: int) -> str:
        if not self._file_ranges:
            return ""
        index = bisect.bisect_right(self._file_starts, lcn) - 1
        for probe in range(max(0, index), min(len(self._file_ranges), index + 3)):
            start, end, name = self._file_ranges[probe]
            if start <= lcn < end:
                return name
        return ""

    def sizeHint(self) -> QSize:  # noqa: D102
        return QSize(900, 210)

    def minimumSizeHint(self) -> QSize:  # noqa: D102
        return QSize(420, 156)
