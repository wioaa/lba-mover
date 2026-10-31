"""流式布局：子控件按可用宽度自动换行。

这是"响应式"在 Qt 里的落地方式。Qt 没有 flexbox，``QHBoxLayout`` 只会
无限压缩子控件直到文字被截断。指标块、图例这类"数量不定、宽度不一"的
内容必须能换行，否则窗口一窄就变成一排省略号。
"""

from __future__ import annotations

from PySide6.QtCore import QMargins, QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QLayout, QLayoutItem, QSizePolicy, QWidget

__all__ = ["FlowLayout"]


class FlowLayout(QLayout):
    def __init__(
        self,
        parent: QWidget | None = None,
        margin: int = 0,
        h_spacing: int = 8,
        v_spacing: int = 8,
    ) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self._h = h_spacing
        self._v = v_spacing
        self.setContentsMargins(QMargins(margin, margin, margin, margin))

    def __del__(self) -> None:  # pragma: no cover - Qt 生命周期兜底
        while self._items:
            self._items.pop()

    # -- QLayout 契约 -----------------------------------------------------

    def addItem(self, item: QLayoutItem) -> None:  # noqa: N802 - Qt 命名
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:  # noqa: N802
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int) -> QLayoutItem | None:  # noqa: N802
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self) -> Qt.Orientation:  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:  # noqa: N802
        # 只报"一排"的尺寸。报成所有子项的外接框会让上层以为这里需要
        # 几百像素高，结果卡片被压扁、子控件互相重叠 —— 这个坑踩过一次。
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for item in self._items:
            s = item.sizeHint()
            size = QSize(max(size.width(), s.width()), max(size.height(), s.height()))
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    # -- 排版 -------------------------------------------------------------

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        m = self.contentsMargins()
        effective = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x = effective.x()
        y = effective.y()
        line_height = 0

        for item in self._items:
            widget = item.widget()
            if widget is not None and widget.isHidden():
                continue
            hint = item.sizeHint()
            next_x = x + hint.width() + self._h
            if next_x - self._h > effective.right() and line_height > 0:
                x = effective.x()
                y = y + line_height + self._v
                next_x = x + hint.width() + self._h
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())

        return y + line_height - rect.y() + m.bottom()


class FlowContainer(QWidget):
    """带 FlowLayout 的容器，方便直接当控件用。

    必须显式转发 ``sizeHint`` / ``minimumSizeHint`` / ``heightForWidth``：
    QWidget 默认不会把自己的尺寸请求交给布局代理，上层布局于是只能按
    "一排"估高，内容换行后就被裁掉。
    """

    def __init__(self, parent: QWidget | None = None, spacing: int = 8) -> None:
        super().__init__(parent)
        self._flow = FlowLayout(self, margin=0, h_spacing=spacing, v_spacing=spacing)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)

    def add(self, widget: QWidget) -> None:
        self._flow.addWidget(widget)

    def clear(self) -> None:
        while self._flow.count():
            item = self._flow.takeAt(0)
            if item is not None and item.widget() is not None:
                item.widget().deleteLater()

    # -- 尺寸代理 ---------------------------------------------------------

    def sizeHint(self) -> QSize:  # noqa: N802
        return self._flow.sizeHint()

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return self._flow.minimumSize()

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._flow.heightForWidth(width)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._flow.setGeometry(self.rect())
