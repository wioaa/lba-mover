"""日志与异常提示面板。

三条规则：

1. **级别用颜色区分，且颜色之外还有字面标记**（``[错误]``）。只靠颜色等于
   对色弱用户不可用。
2. **有上限**。无上限日志会在长时间任务里吃掉几百 MB，最终把界面拖死。
   这里保留最近 3000 行，超出从头部丢。
3. **异常单独置顶提示**。错误埋在一屏滚动文字里，用户第一反应是"卡住了"。
"""

from __future__ import annotations

import html
import time
from collections import deque

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...contract.enums import LogLevel
from ..theme import DARK, Palette, mono_font

__all__ = ["LogView"]

_ORDER = {LogLevel.DEBUG: 0, LogLevel.INFO: 1, LogLevel.WARNING: 2, LogLevel.ERROR: 3}
_TAG = {
    LogLevel.DEBUG: ("调试", "dbg"),
    LogLevel.INFO: ("信息", "info"),
    LogLevel.WARNING: ("警告", "warn"),
    LogLevel.ERROR: ("错误", "err"),
}


class LogView(QWidget):
    """带级别过滤、异常横幅与"复制诊断"的日志视图。"""

    #: 用户点了「复制诊断」。生成报告要用到版本、卷、参数、结果，
    #: 这些只有主窗口知道，所以这里只发信号，不去拼内容。
    diagnoseRequested = Signal()

    max_lines = 3000

    def __init__(self, palette: Palette = DARK, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._p = palette
        self._min_level = LogLevel.DEBUG
        self._lines: deque[str] = deque(maxlen=self.max_lines)

        head = QHBoxLayout()
        head.setSpacing(8)
        title = QLabel("运行日志")
        title.setObjectName("cardTitle")
        head.addWidget(title)
        head.addStretch(1)

        self.filter = QComboBox()
        for label, lvl in (("全部", LogLevel.DEBUG), ("信息+", LogLevel.INFO),
                           ("警告+", LogLevel.WARNING), ("仅错误", LogLevel.ERROR)):
            self.filter.addItem(label, lvl)
        self.filter.setCurrentIndex(1)
        self.filter.currentIndexChanged.connect(self._on_filter)
        self.filter.setFixedWidth(96)
        head.addWidget(self.filter)

        self.diagnose_btn = QPushButton("复制诊断")
        self.diagnose_btn.setObjectName("ghost")
        self.diagnose_btn.setToolTip(
            "把版本、目标卷、参数、失败明细与最近日志打包成一段文本复制到剪贴板，"
            "可以直接贴给 AI 或贴进 issue"
        )
        self.diagnose_btn.clicked.connect(self.diagnoseRequested.emit)
        head.addWidget(self.diagnose_btn)

        self.clear_btn = QPushButton("清空")
        self.clear_btn.setObjectName("ghost")
        self.clear_btn.clicked.connect(self.clear)
        head.addWidget(self.clear_btn)

        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.banner.setVisible(False)
        self.banner.setStyleSheet(
            f"color: {palette.danger}; background: {palette.danger}18;"
            f"border: 1px solid {palette.danger}55; border-radius: 8px;"
            "padding: 6px 10px; font-size: 12px;"
        )

        self.text = QPlainTextEdit()
        self.text.setObjectName("log")
        self.text.setReadOnly(True)
        self.text.setMaximumBlockCount(self.max_lines)
        self.text.setFont(mono_font(9))
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addLayout(head)
        lay.addWidget(self.banner)
        lay.addWidget(self.text, 1)

    # -- 写入 -------------------------------------------------------------

    def append(self, level: LogLevel, message: str, detail: str = "") -> None:
        if _ORDER[level] < _ORDER[self._min_level]:
            self._lines.append("")
            self._lines.pop()
            return
        color = {
            LogLevel.DEBUG: self._p.text_faint,
            LogLevel.INFO: self._p.text_dim,
            LogLevel.WARNING: self._p.warn,
            LogLevel.ERROR: self._p.danger,
        }[level]
        label, _ = _TAG[level]
        ts = time.strftime("%H:%M:%S")
        line = (
            f'<span style="color:{self._p.text_faint}">{ts}</span> '
            f'<span style="color:{color}">[{label}]</span> '
            f'<span style="color:{color if level in (LogLevel.WARNING, LogLevel.ERROR) else self._p.text_dim}">'
            f"{html.escape(message)}</span>"
        )
        if detail:
            line += f' <span style="color:{self._p.text_faint}">· {html.escape(detail)}</span>'
        self._lines.append(line)
        self.text.appendHtml(line)
        if level is LogLevel.ERROR:
            self.show_banner(message)
        self.text.moveCursor(QTextCursor.MoveOperation.End)

    def plain_tail(self, lines: int = 40) -> list[str]:
        """最近若干行**纯文本**。

        诊断报告要的是能直接读的文字，而 ``_lines`` 里存的是带颜色的 HTML。
        从控件取纯文本还能顺带反映"用户当前看到的过滤条件"。
        """
        text = self.text.toPlainText().splitlines()
        return text[-lines:] if lines > 0 else text

    def show_banner(self, message: str) -> None:
        self.banner.setText(f"⚠ {message}")
        self.banner.setVisible(True)

    def clear_banner(self) -> None:
        self.banner.setVisible(False)

    def clear(self) -> None:
        self._lines.clear()
        self.text.clear()
        self.clear_banner()

    def _on_filter(self) -> None:
        self._min_level = self.filter.currentData()
        # 过滤条件变化时用缓存重建，避免"过滤后日志消失且再也回不来"
        self.text.clear()
        for line in self._lines:
            if line:
                self.text.appendHtml(line)
