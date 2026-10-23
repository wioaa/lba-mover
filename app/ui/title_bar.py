"""无边框自定义标题栏：应用标识 + 主题切换 + 自绘窗口按钮。

拖拽使用 ``QWindow.startSystemMove()``（Windows 上由系统接管，最顺滑），
失败时回退到手动的全局坐标位移；双击切换最大化。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPoint, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QMouseEvent, QPainter, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from app.ui import cards, icons, theme

TITLE_HEIGHT = 64


class AppMark(QWidget):
    """应用图标（渐变圆角方块 + 磁盘线稿）。"""

    def __init__(self, size: int = 34, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._pixmap: QPixmap = icons.app_logo(size)
        self.setFixedSize(size, size)

    def paintEvent(self, event) -> None:  # noqa: D102
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.drawPixmap(0, 0, self._pixmap)
        painter.end()


class TitleBar(QWidget):
    """自绘标题栏。"""

    minimizeRequested = Signal()
    maximizeRequested = Signal()
    closeRequested = Signal()
    themeToggleRequested = Signal()
    workModeChanged = Signal(int)     # 0 = LBA 迁移，1 = 分块流式

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(TITLE_HEIGHT)
        self.setObjectName("AppTitleBar")
        self._drag_offset: Optional[QPoint] = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 0, 12, 0)
        layout.setSpacing(12)

        self._mark = AppMark(34, self)
        layout.addWidget(self._mark, 0, Qt.AlignmentFlag.AlignVCenter)

        text_column = QVBoxLayout()
        text_column.setContentsMargins(0, 0, 0, 0)
        text_column.setSpacing(1)
        self._title = QLabel("LBA Tail Mover", self)
        self._title.setProperty("role", "h1")
        self._subtitle = QLabel("大文件末端 LBA 迁移", self)
        self._subtitle.setProperty("role", "caption")
        text_column.addWidget(self._title)
        text_column.addWidget(self._subtitle)
        layout.addLayout(text_column)
        layout.addStretch(1)

        # ---- 工作模式切换（LBA 迁移 / 分块流式）----
        self._work_switch = cards.SegmentedControl(["LBA 迁移", "分块流式"], 0, self)
        self._work_switch.changed.connect(self.workModeChanged.emit)
        layout.addWidget(self._work_switch, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addSpacing(10)

        self._mode_badge = cards.Badge("预览模式", "accent", self)
        layout.addWidget(self._mode_badge, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addSpacing(8)

        self._theme_button = cards.IconButton(
            "moon", size=34, icon_size=17, tooltip="切换深色 / 浅色主题"
        )
        self._theme_button.clicked.connect(self.themeToggleRequested.emit)
        layout.addWidget(self._theme_button, 0, Qt.AlignmentFlag.AlignVCenter)

        layout.addWidget(cards.Divider(vertical=True), 0)
        layout.addSpacing(4)

        self._btn_min = cards.IconButton("minimize", 34, 15, "最小化")
        self._btn_max = cards.IconButton("maximize", 34, 15, "最大化 / 还原")
        self._btn_close = cards.IconButton("close", 34, 15, "关闭", danger_hover=True)

        self._btn_min.clicked.connect(self.minimizeRequested.emit)
        self._btn_max.clicked.connect(self.maximizeRequested.emit)
        self._btn_close.clicked.connect(self.closeRequested.emit)

        for button in (self._btn_min, self._btn_max, self._btn_close):
            layout.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)

        self.sync_theme()
        self.sync_maximized(False)

    # ------------------------------------------------------------------ #
    def sync_theme(self) -> None:
        """根据当前主题切换主题按钮图标。"""
        self._theme_button.set_icon("sun" if theme.theme().is_dark else "moon")

    def sync_maximized(self, maximized: bool) -> None:
        """同步最大化按钮图标。"""
        self._btn_max.set_icon("restore" if maximized else "maximize")

    def set_mode_badge(self, text: str, kind: str) -> None:
        """更新左上角的模式徽章（预览 / 执行）。"""
        self._mode_badge.set_state(text, kind)

    def work_mode(self) -> int:
        """当前工作模式：0 = LBA 迁移，1 = 分块流式。"""
        return int(self._work_switch.current_index)

    def set_work_mode(self, index: int) -> None:
        self._work_switch.set_current(int(index), emit=False)

    def paintEvent(self, event) -> None:  # noqa: D102
        palette = theme.palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # 顶部极淡的色带，拉开层次
        painter.fillRect(self.rect(), QColor(palette["card"]))

        painter.setPen(QColor(palette["border"]))
        painter.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        painter.end()

    # ------------------------------------------------------------------ #
    # 拖动 / 双击最大化
    # ------------------------------------------------------------------ #
    def _in_button_area(self, position: QPoint) -> bool:
        for button in (self._theme_button, self._btn_min, self._btn_max, self._btn_close):
            if button.geometry().adjusted(-2, -2, 2, 2).contains(position):
                return True
        # 工作模式切换器：别让在它上面的按压触发窗口拖动
        if self._work_switch.geometry().adjusted(-2, -2, 2, 2).contains(position):
            return True
        return False

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: D102
        if event.button() == Qt.MouseButton.LeftButton and not self._in_button_area(
            event.position().toPoint()
        ):
            window = self.window()
            handle = window.windowHandle()
            if handle is not None and handle.startSystemMove():
                event.accept()
                return
            self._drag_offset = event.globalPosition().toPoint() - window.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: D102
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            window = self.window()
            if not window.isMaximized():
                window.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: D102
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: D102
        if event.button() == Qt.MouseButton.LeftButton and not self._in_button_area(
            event.position().toPoint()
        ):
            self.maximizeRequested.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)
