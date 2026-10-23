"""大文件列表：``QTableView`` + 自定义 ``QStyledItemDelegate``。

视觉目标：**去表格化** —— 没有网格线、没有原生选中蓝，靠斑马纹、留白与
自绘徽章区分信息层级；行高 50px，悬停整行高亮。
"""

from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
    QWidget,
)

from app.core.models import FileEntry, human_bytes
from app.ui import icons, theme

COL_CHECK = 0
COL_NAME = 1
COL_SIZE = 2
COL_FRAG = 3
COL_LCN = 4
COL_STATUS = 5

HEADERS = ["", "文件", "大小", "碎片", "LCN 区间", "状态"]
ROW_HEIGHT = 50


def fragment_kind(fragments: int) -> str:
    """碎片数 → 语义色。"""
    if fragments <= 1:
        return "success"
    if fragments <= 5:
        return "warning"
    return "danger"


def status_kind(status: str) -> str:
    """状态文案 → 语义色。"""
    if status == "已迁移" or status == "已就位":
        return "success"
    if status == "不可移动":
        return "danger"
    if status == "待迁移":
        return "accent"
    return "neutral"


class FileTableModel(QAbstractTableModel):
    """文件列表模型（含勾选状态与排序）。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._rows: List[FileEntry] = []
        self._max_size = 1
        self._sort_column = COL_SIZE
        self._sort_order = Qt.SortOrder.DescendingOrder

    # ---------------- Qt 接口 ---------------- #
    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: D102
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: D102
        return 0 if parent.isValid() else len(HEADERS)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):  # noqa: D102
        if not index.isValid():
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return HEADERS[index.column()]
        if role == Qt.ItemDataRole.UserRole:
            return self._rows[index.row()]
        return None

    def headerData(self, section: int, orientation, role: int = Qt.ItemDataRole.DisplayRole):  # noqa: D102
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return HEADERS[section]
        return None

    def flags(self, index: QModelIndex):  # noqa: D102
        base = super().flags(index)
        if index.isValid():
            return base | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        return base

    # ---------------- 数据操作 ---------------- #
    def set_entries(self, entries: List[FileEntry]) -> None:
        """整体替换数据。"""
        self.beginResetModel()
        self._rows = list(entries)
        self._max_size = max([e.size for e in self._rows], default=1) or 1
        self._apply_sort()
        self.endResetModel()

    def append_entry(self, entry: FileEntry) -> None:
        """增量追加一行（扫描过程中使用）。"""
        row = len(self._rows)
        self.beginInsertRows(QModelIndex(), row, row)
        self._rows.append(entry)
        self._max_size = max(self._max_size, entry.size)
        self.endInsertRows()

    def clear(self) -> None:
        """清空数据。"""
        self.beginResetModel()
        self._rows = []
        self._max_size = 1
        self.endResetModel()

    def entry_at(self, row: int) -> Optional[FileEntry]:
        """按行号取条目。"""
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def rows(self) -> List[FileEntry]:
        """只读访问全部条目。"""
        return list(self._rows)

    def max_size(self) -> int:
        """当前条目中的最大字节数（用于占比条）。"""
        return self._max_size

    def toggle_row(self, row: int) -> None:
        """切换某一行的勾选状态。"""
        entry = self.entry_at(row)
        if entry is None or not entry.movable:
            return
        entry.checked = not entry.checked
        index = self.index(row, COL_CHECK)
        self.dataChanged.emit(index, index)

    def set_checked_rows(self, checked: bool) -> None:
        """全选 / 全不选（仅对可移动项生效）。"""
        if not self._rows:
            return
        for entry in self._rows:
            if entry.movable:
                entry.checked = checked
        top = self.index(0, COL_CHECK)
        bottom = self.index(len(self._rows) - 1, COL_CHECK)
        self.dataChanged.emit(top, bottom)

    def invert_checks(self) -> None:
        """反选。"""
        if not self._rows:
            return
        for entry in self._rows:
            if entry.movable:
                entry.checked = not entry.checked
        top = self.index(0, COL_CHECK)
        bottom = self.index(len(self._rows) - 1, COL_CHECK)
        self.dataChanged.emit(top, bottom)

    def checked_entries(self) -> List[FileEntry]:
        """返回已勾选且可移动的条目。"""
        return [e for e in self._rows if e.checked and e.movable]

    def checked_count(self) -> int:
        return len(self.checked_entries())

    def update_status(self, path: str, status: str) -> None:
        """更新某文件的状态文案并刷新对应行。"""
        for row, entry in enumerate(self._rows):
            if entry.path == path:
                entry.status = status
                index = self.index(row, COL_STATUS)
                self.dataChanged.emit(index, index)
                return

    def refresh(self) -> None:
        """整体刷新（不改变行数）。"""
        if not self._rows:
            return
        top = self.index(0, 0)
        bottom = self.index(len(self._rows) - 1, len(HEADERS) - 1)
        self.dataChanged.emit(top, bottom)

    # ---------------- 排序 ---------------- #
    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:  # noqa: D102
        self._sort_column = column
        self._sort_order = order
        self.layoutAboutToBeChanged.emit()
        self._apply_sort()
        self.layoutChanged.emit()

    def _apply_sort(self) -> None:
        keys = {
            COL_NAME: lambda e: e.name.lower(),
            COL_SIZE: lambda e: e.size,
            COL_FRAG: lambda e: e.fragments,
            COL_LCN: lambda e: e.first_lcn,
            COL_STATUS: lambda e: e.status,
        }
        key = keys.get(self._sort_column)
        if key is None:
            return
        self._rows.sort(key=key, reverse=self._sort_order == Qt.SortOrder.DescendingOrder)

    @property
    def sort_column(self) -> int:
        return self._sort_column

    @property
    def sort_order(self) -> Qt.SortOrder:
        return self._sort_order


class FileItemDelegate(QStyledItemDelegate):
    """行绘制：复选框 / 双行文件名 / 渐变占比条 / 碎片胶囊 / 状态徽章。"""

    def __init__(self, view: "FileTableView") -> None:
        super().__init__(view)
        self._view = view

    # ---------------- 绘制 ---------------- #
    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        model = self._view.model()
        if not isinstance(model, FileTableModel):
            return
        entry = model.entry_at(index.row())
        if entry is None:
            return

        palette = theme.palette()
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        rect = QRectF(option.rect)
        row = index.row()
        hovered = row == self._view.hover_row

        # 背景：斑马纹 / 悬停
        if hovered:
            background = QColor(palette["hover"])
        elif row % 2 == 1:
            background = QColor(palette["zebra"])
        else:
            background = QColor(0, 0, 0, 0)

        if background.alpha() > 0:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(background)
            band = QRectF(rect)
            band.setLeft(rect.left())
            band.setRight(rect.right() + (2 if index.column() == COL_STATUS else 0))
            painter.drawRect(band)

        column = index.column()
        if column == COL_CHECK:
            self._paint_check(painter, palette, rect, entry)
        elif column == COL_NAME:
            self._paint_name(painter, palette, rect, entry)
        elif column == COL_SIZE:
            self._paint_size(painter, palette, rect, entry, model.max_size())
        elif column == COL_FRAG:
            self._paint_badge(
                painter, palette, rect, f"{entry.fragments} 段", fragment_kind(entry.fragments)
            )
        elif column == COL_LCN:
            self._paint_lcn(painter, palette, rect, entry)
        elif column == COL_STATUS:
            self._paint_badge(painter, palette, rect, entry.status, status_kind(entry.status))

        painter.restore()

    # ---------------- 各列 ---------------- #
    def _paint_check(self, painter: QPainter, palette, rect: QRectF, entry: FileEntry) -> None:
        size = 16.0
        box = QRectF(
            rect.left() + 16,
            rect.center().y() - size / 2,
            size,
            size,
        )
        if entry.checked and entry.movable:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(palette["accent"]))
            painter.drawRoundedRect(box, 4.5, 4.5)
            icons.draw_check_mark(painter, box, QColor("#FFFFFF"), 1.9)
        else:
            border = QColor(palette["border_strong"] if entry.movable else palette["border"])
            painter.setPen(QPen(border, 1.3))
            painter.setBrush(QColor(palette["card"]))
            painter.drawRoundedRect(box, 4.5, 4.5)

    def _paint_name(self, painter: QPainter, palette, rect: QRectF, entry: FileEntry) -> None:
        left = rect.left() + 8
        width = rect.width() - 16

        font_name = QFont(theme.app_font(13, QFont.Weight.DemiBold))
        painter.setFont(font_name)
        painter.setPen(QColor(palette["text"] if entry.movable else palette["text_dim"]))
        metrics = QFontMetrics(font_name)
        name = metrics.elidedText(entry.name, Qt.TextElideMode.ElideRight, int(width))
        painter.drawText(
            QRectF(left, rect.top() + 7, width, 18),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            name,
        )

        font_path = QFont(theme.app_font(11))
        painter.setFont(font_path)
        painter.setPen(QColor(palette["text_faint"]))
        metrics_path = QFontMetrics(font_path)
        reason = entry.reason if (entry.reason and not entry.movable) else entry.path
        path_text = metrics_path.elidedText(reason, Qt.TextElideMode.ElideMiddle, int(width))
        painter.drawText(
            QRectF(left, rect.top() + 26, width, 16),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            path_text,
        )

    def _paint_size(
        self,
        painter: QPainter,
        palette,
        rect: QRectF,
        entry: FileEntry,
        max_size: int,
    ) -> None:
        track = QRectF(rect.left() + 8, rect.center().y() - 11, rect.width() - 22, 22)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(palette["grid"]))
        painter.drawRoundedRect(track, 6, 6)

        ratio = 0.0 if max_size <= 0 else max(0.02, min(1.0, entry.size / float(max_size)))
        fill = QRectF(track)
        fill.setWidth(max(6.0, track.width() * ratio))

        start_color = QColor(palette["accent"])
        start_color.setAlpha(58)
        end_color = QColor(palette["cyan"])
        end_color.setAlpha(34)
        gradient = QLinearGradient(track.left(), 0, track.right(), 0)
        gradient.setColorAt(0.0, start_color)
        gradient.setColorAt(1.0, end_color)
        painter.setBrush(gradient)
        painter.drawRoundedRect(fill, 6, 6)

        font = QFont(theme.mono_font(12))
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(QColor(palette["text"]))
        painter.drawText(
            track.adjusted(8, 0, -8, 0),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            human_bytes(entry.size),
        )

    def _paint_badge(
        self,
        painter: QPainter,
        palette,
        rect: QRectF,
        text: str,
        kind: str,
    ) -> None:
        fg_key, bg_key = {
            "accent": ("accent", "accent_soft"),
            "success": ("success", "success_soft"),
            "warning": ("warning", "warning_soft"),
            "danger": ("danger", "danger_soft"),
            "neutral": ("text_dim", "sunken"),
        }.get(kind, ("text_dim", "sunken"))

        font = QFont(theme.app_font(11, QFont.Weight.DemiBold))
        painter.setFont(font)
        metrics = QFontMetrics(font)
        width = metrics.horizontalAdvance(text) + 20
        height = 22.0
        left = rect.left() + 8
        if rect.width() - 16 < width:
            width = max(28.0, rect.width() - 16)

        pill = QRectF(left, rect.center().y() - height / 2, width, height)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(palette[bg_key]))
        painter.drawRoundedRect(pill, height / 2, height / 2)
        painter.setPen(QColor(palette[fg_key]))
        painter.drawText(pill, int(Qt.AlignmentFlag.AlignCenter), text)

    def _paint_lcn(self, painter: QPainter, palette, rect: QRectF, entry: FileEntry) -> None:
        font = QFont(theme.mono_font(11))
        painter.setFont(font)
        painter.setPen(QColor(palette["text_dim"] if entry.movable else palette["text_faint"]))
        metrics = QFontMetrics(font)
        text = metrics.elidedText(entry.lcn_text, Qt.TextElideMode.ElideRight, int(rect.width() - 18))
        painter.drawText(
            QRectF(rect.left() + 8, rect.top(), rect.width() - 16, rect.height()),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            text,
        )

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:  # noqa: D102
        return QSize(80, ROW_HEIGHT)


class FileTableView(QTableView):
    """文件列表视图：处理勾选点击、排序、空状态与扫描中占位。"""

    toggled = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._model = FileTableModel(self)
        self.setModel(self._model)
        self._delegate = FileItemDelegate(self)
        self.setItemDelegate(self._delegate)

        self.hover_row = -1
        self._placeholder = "empty"
        self._placeholder_title = "尚未扫描"
        self._placeholder_text = "选择卷并设置大小阈值后，点击「扫描」开始分析大文件"
        self._dots = 0

        self.setShowGrid(False)
        self.setFrameShape(QTableView.Shape.NoFrame)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setMouseTracking(True)
        self.setWordWrap(False)
        self.setCornerButtonEnabled(False)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)

        header = self.horizontalHeader()
        header.setHighlightSections(False)
        header.setSectionsMovable(False)
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        header.setFixedHeight(38)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setSortIndicator(COL_SIZE, Qt.SortOrder.DescendingOrder)
        header.sectionClicked.connect(self._on_header_clicked)

        self.setColumnWidth(COL_CHECK, 48)
        header.setSectionResizeMode(COL_CHECK, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        self.setColumnWidth(COL_SIZE, 148)
        header.setSectionResizeMode(COL_SIZE, QHeaderView.ResizeMode.Fixed)
        self.setColumnWidth(COL_FRAG, 78)
        header.setSectionResizeMode(COL_FRAG, QHeaderView.ResizeMode.Fixed)
        self.setColumnWidth(COL_LCN, 168)
        header.setSectionResizeMode(COL_LCN, QHeaderView.ResizeMode.Fixed)
        self.setColumnWidth(COL_STATUS, 96)
        header.setSectionResizeMode(COL_STATUS, QHeaderView.ResizeMode.Fixed)

        self._timer = QTimer(self)
        self._timer.setInterval(360)
        self._timer.timeout.connect(self._tick)

    # ---------------- 状态 ---------------- #
    @property
    def file_model(self) -> FileTableModel:
        return self._model

    def set_empty_state(self, title: str, text: str, icon_name: str = "folder") -> None:
        """切到空状态占位。"""
        self._placeholder = "empty"
        self._placeholder_title = title
        self._placeholder_text = text
        self._timer.stop()
        self.viewport().update()

    def set_scanning_state(self, text: str = "正在扫描卷上的大文件…") -> None:
        """切到扫描中占位（带动态省略号）。"""
        self._placeholder = "scanning"
        self._placeholder_title = text
        self._placeholder_text = "扫描在后台线程执行，可随时取消"
        self._timer.start()
        self.viewport().update()

    def set_ready_state(self) -> None:
        """切回表格模式。"""
        self._placeholder = "ready"
        self._timer.stop()
        self.viewport().update()

    def _tick(self) -> None:
        self._dots = (self._dots + 1) % 4
        self.viewport().update()

    # ---------------- 交互 ---------------- #
    def _row_at(self, position) -> int:
        index = self.indexAt(position)
        return index.row() if index.isValid() else -1

    def mousePressEvent(self, event) -> None:  # noqa: D102
        if event.button() == Qt.MouseButton.LeftButton:
            index = self.indexAt(event.position().toPoint())
            if index.isValid() and index.column() == COL_CHECK:
                self._model.toggle_row(index.row())
                self.toggled.emit()
                self.viewport().update()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: D102
        index = self.indexAt(event.position().toPoint())
        if index.isValid():
            self._model.toggle_row(index.row())
            self.toggled.emit()
            self.viewport().update()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: D102
        row = self._row_at(event.position().toPoint())
        if row != self.hover_row:
            self.hover_row = row
            self.setCursor(
                Qt.CursorShape.PointingHandCursor
                if row >= 0
                else Qt.CursorShape.ArrowCursor
            )
            self.viewport().update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: D102
        if self.hover_row != -1:
            self.hover_row = -1
            self.viewport().update()
        super().leaveEvent(event)

    def _on_header_clicked(self, column: int) -> None:
        if column in (COL_NAME, COL_SIZE, COL_FRAG, COL_LCN, COL_STATUS):
            order = Qt.SortOrder.AscendingOrder
            if self._model.sort_column == column:
                order = (
                    Qt.SortOrder.DescendingOrder
                    if self._model.sort_order == Qt.SortOrder.AscendingOrder
                    else Qt.SortOrder.AscendingOrder
                )
            self._model.sort(column, order)
            self.horizontalHeader().setSortIndicator(column, order)
            self.viewport().update()

    # ---------------- 空状态绘制 ---------------- #
    def paintEvent(self, event) -> None:  # noqa: D102
        super().paintEvent(event)
        # 有数据时永远显示真实行（扫描过程中也能看到增量结果）
        if self._model.rowCount() > 0:
            return

        palette = theme.palette()
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = QRectF(self.viewport().rect())
        center_x = rect.center().x()
        top = rect.center().y() - 70

        if self._placeholder == "scanning":
            ring = QRectF(center_x - 22, top, 44, 44)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(palette["accent"]), 3.0))
            painter.drawEllipse(ring)
            painter.setPen(QPen(QColor(palette["border"]), 3.0))
            painter.drawEllipse(ring)
            painter.end()
            return

        halo = QRectF(center_x - 40, top - 14, 80, 80)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(palette["sunken"]))
        painter.drawEllipse(halo)
        icons.paint_icon(
            painter,
            "folder",
            QRectF(center_x - 22, top + 8, 44, 44),
            QColor(palette["text_faint"]),
        )

        font_title = QFont(theme.app_font(14, QFont.Weight.DemiBold))
        painter.setFont(font_title)
        painter.setPen(QColor(palette["text_dim"]))
        painter.drawText(
            QRectF(0, top + 74, rect.width(), 22),
            int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop),
            self._placeholder_title,
        )

        font_text = QFont(theme.app_font(12))
        painter.setFont(font_text)
        painter.setPen(QColor(palette["text_faint"]))
        painter.drawText(
            QRectF(24, top + 98, rect.width() - 48, 40),
            int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop),
            self._placeholder_text,
        )
        painter.end()
