"""折叠式操作日志面板：等宽字体、时间戳、按级别着色。"""

from __future__ import annotations

import html
from datetime import datetime
from typing import List, Optional, Tuple

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import QHBoxLayout, QTextEdit, QVBoxLayout, QWidget

from app.ui import cards, theme

LEVEL_STYLE = {
    "INFO": ("text_dim", "INFO"),
    "WARN": ("warning", "WARN"),
    "ERROR": ("danger", "ERROR"),
    "SUCCESS": ("success", " OK "),
}

MAX_RECORDS = 1500


class LogPanel(QWidget):
    """可折叠的日志面板。"""

    collapsedChanged = Signal(bool)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._records: List[Tuple[str, str, str]] = []
        self._collapsed = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)

        self._toggle = cards.IconButton("chevron-down", size=24, icon_size=13, tooltip="折叠 / 展开日志")
        self._toggle.clicked.connect(self.toggle_collapsed)
        header.addWidget(self._toggle, 0, Qt.AlignmentFlag.AlignVCenter)

        header.addWidget(cards.label("操作日志", "h3"), 0, Qt.AlignmentFlag.AlignVCenter)
        self._counter = cards.label("0 条", "caption")
        header.addWidget(self._counter, 0, Qt.AlignmentFlag.AlignVCenter)
        header.addStretch(1)

        self._clear_button = cards.IconButton("trash", size=26, icon_size=14, tooltip="清空日志")
        self._clear_button.clicked.connect(self.clear)
        header.addWidget(self._clear_button, 0, Qt.AlignmentFlag.AlignVCenter)

        layout.addLayout(header)

        self._view = QTextEdit(self)
        self._view.setReadOnly(True)
        self._view.setFrameShape(QTextEdit.Shape.NoFrame)
        self._view.setFont(theme.mono_font(11))
        self._view.setFixedHeight(72)
        self._view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        layout.addWidget(self._view, 1)

        self.info("程序就绪，等待选择卷")

    # ---------------- 对外接口 ---------------- #
    def info(self, message: str) -> None:
        """记录一条 INFO。"""
        self.log("INFO", message)

    def warn(self, message: str) -> None:
        """记录一条 WARN。"""
        self.log("WARN", message)

    def error(self, message: str) -> None:
        """记录一条 ERROR。"""
        self.log("ERROR", message)

    def success(self, message: str) -> None:
        """记录一条 SUCCESS。"""
        self.log("SUCCESS", message)

    def log(self, level: str, message: str) -> None:
        """追加一条日志。"""
        level = level.upper()
        if level not in LEVEL_STYLE:
            level = "INFO"
        timestamp = datetime.now().strftime("%H:%M:%S")
        self._records.append((level, timestamp, message))
        if len(self._records) > MAX_RECORDS:
            del self._records[: len(self._records) - MAX_RECORDS]
            self._render_all()
            return
        self._append(level, timestamp, message)
        self._update_counter()

    def clear(self) -> None:
        """清空日志。"""
        self._records.clear()
        self._view.clear()
        self._update_counter()

    def messages(self) -> List[Tuple[str, str, str]]:
        """返回全部记录（供导出 / 测试使用）。"""
        return list(self._records)

    # ---------------- 折叠 ---------------- #
    @property
    def is_collapsed(self) -> bool:
        return self._collapsed

    def set_collapsed(self, collapsed: bool) -> None:
        """折叠 / 展开日志正文。"""
        self._collapsed = collapsed
        self._view.setVisible(not collapsed)
        self._toggle.set_icon("chevron-right" if collapsed else "chevron-down")
        self.collapsedChanged.emit(collapsed)

    def toggle_collapsed(self) -> None:
        """切换折叠状态。"""
        self.set_collapsed(not self._collapsed)

    def refresh_theme(self) -> None:
        """主题切换后重建 HTML，使颜色与新调色板一致。"""
        self._render_all()

    # ---------------- 内部 ---------------- #
    def _append(self, level: str, timestamp: str, message: str) -> None:
        palette = theme.palette()
        color_key, tag = LEVEL_STYLE[level]
        mono = theme.theme().mono_family()
        color = palette[color_key]
        dim = palette["text_faint"]
        body = palette["text"] if level == "INFO" else color

        fragment = (
            f'<span style="color:{dim};font-family:\'{mono}\';">{timestamp}</span>&nbsp;'
            f'<span style="color:{color};font-family:\'{mono}\';font-weight:600;">{tag}</span>&nbsp;'
            f'<span style="color:{body};font-family:\'{mono}\';">{html.escape(message)}</span>'
        )
        cursor = self._view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if not self._view.document().isEmpty():
            cursor.insertBlock()
        cursor.insertHtml(fragment)
        self._view.setTextCursor(cursor)
        bar = self._view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _render_all(self) -> None:
        self._view.clear()
        for level, timestamp, message in self._records:
            self._append(level, timestamp, message)
        self._update_counter()

    def _update_counter(self) -> None:
        self._counter.setText(f"{len(self._records)} 条")
