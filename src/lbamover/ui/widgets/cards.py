"""通用容器：卡片、指标块、分隔线、徽标。

这些是"信息架构"的砖块。把它们抽出来是为了保证**同一类信息在任何页面
长得一样**——用户不用重新学习怎么看。旧版各面板各自画边框，同一个概念
在不同位置字重、间距都不一样，读起来很累。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

__all__ = ["Card", "MetricTile", "Badge", "Divider", "hint_label", "section_label"]


class Card(QFrame):
    """圆角卡片，带标题行与可选提示。"""

    def __init__(
        self,
        title: str = "",
        hint: str = "",
        parent: QWidget | None = None,
        *,
        spacing: int = 10,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(spacing)

        self.header = QHBoxLayout()
        self.header.setSpacing(8)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("cardTitle")
        self.hint_label = QLabel(hint)
        self.hint_label.setObjectName("cardHint")
        # 卡片标题行只放得下一小段字。提示太长时 Qt 会把它画到卡片边框外面
        # （实测"整卷 / 只整理勾选的文件"末尾就被裁掉了），所以既限制宽度，
        # 也把全文放进 tooltip —— 界面上短、悬停能看到完整的。
        self.hint_label.setToolTip(hint)
        self.hint_label.setMaximumWidth(150)
        self.header.addWidget(self.title_label)
        self.header.addStretch(1)
        self.header.addWidget(self.hint_label)
        if title:
            outer.addLayout(self.header)

        self.body = QVBoxLayout()
        self.body.setSpacing(spacing)
        outer.addLayout(self.body, 1)
        self._outer = outer

    def set_title(self, text: str) -> None:
        self.title_label.setText(text)

    def set_hint(self, text: str) -> None:
        self.hint_label.setText(text)

    def add(self, widget: QWidget, stretch: int = 0) -> None:
        self.body.addWidget(widget, stretch)

    def add_layout(self, layout, stretch: int = 0) -> None:
        self.body.addLayout(layout, stretch)

    def add_stretch(self) -> None:
        self.body.addStretch(1)


class MetricTile(QFrame):
    """一个「键 + 值」的小方块。放在流式布局里自动换行。"""

    def __init__(
        self,
        key: str,
        value: str = "—",
        parent: QWidget | None = None,
        *,
        min_width: int = 104,
    ) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self.value_label = QLabel(value)
        self.value_label.setObjectName("metricValue")
        self.key_label = QLabel(key)
        self.key_label.setObjectName("metricKey")
        lay.addWidget(self.value_label)
        lay.addWidget(self.key_label)
        self.setMinimumWidth(min_width)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

    def set_value(self, text: str) -> None:
        if self.value_label.text() != text:
            self.value_label.setText(text)

    def set_accent(self, color: str | None) -> None:
        self.value_label.setStyleSheet(f"color: {color};" if color else "")


class Badge(QLabel):
    """状态徽标。``set_clickable(True)`` 后变成可点的动作入口。

    需要做成可点，是因为「权限：普通用户」这种状态光写着没用——用户看到
    之后唯一的动作是去关掉程序、右键、选管理员身份。把这一步直接放在徽标上，
    比在日志里写一句"请以管理员身份运行"有用得多。
    """

    clicked = Signal()

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("badge")
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self._clickable = False

    def set_clickable(self, clickable: bool) -> None:
        if self._clickable == clickable:
            return
        self._clickable = clickable
        self.setCursor(
            Qt.CursorShape.PointingHandCursor if clickable else Qt.CursorShape.ArrowCursor
        )

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        # 用 release 而不是 press：按下后拖开再松手应当是"取消"，与按钮一致。
        # 比数值不比身份：Qt 若有枚举返回 int，`is` 会静默变 False（见 main_window.confirmed）。
        if self._clickable and event.button() == Qt.MouseButton.LeftButton:
            if self.rect().contains(event.position().toPoint()):
                self.clicked.emit()
                event.accept()
                return
        super().mouseReleaseEvent(event)


class Divider(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(1)
        self.setObjectName("divider")
        self.setStyleSheet("background: rgba(128,140,160,0.18);")


def hint_label(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("cardHint")
    lab.setWordWrap(True)
    lab.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
    return lab


def section_label(text: str) -> QLabel:
    lab = QLabel(text.upper())
    lab.setObjectName("sectionLabel")
    return lab
