"""日志与异常提示面板。

三条规则：

1. **级别用颜色区分，且颜色之外还有字面标记**（``[错误]``）。只靠颜色等于
   对色弱用户不可用。
2. **有上限**。无上限日志会在长时间任务里吃掉几百 MB，最终把界面拖死。
   这里保留最近 3000 行，超出从头部丢。
3. **异常单独置顶提示**。错误埋在一屏滚动文字里，用户第一反应是"卡住了"。

还有一条容易被写错的：**先存全量、渲染时才过滤**。反过来做（写入时按当前
级别丢弃）会有两个后果 —— 换回更宽的级别时之前被丢的行再也回不来，而且
"复制诊断"拿到的日志会缺掉恰好最需要的那部分。级别筛选是**看**的开关，
不是**留**的开关。
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
        #: ``(级别, 富文本, 纯文本)``。**保存全部级别**，过滤只作用于渲染 ——
        #: 见模块 docstring。
        self._lines: deque[tuple[LogLevel, str, str]] = deque(maxlen=self.max_lines)

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
        self.filter.setFixedWidth(96)
        # **先设默认项、connect 放到 self.text 建好之后**。两处都有讲究：
        # 默认项在 connect 之前设，不会触发 _on_filter（那时 text 还不存在）；
        # 而 _min_level 必须**显式**跟着默认项走 —— 忘了这一步，下拉框写着
        # 「信息+」而内部阈值还是 DEBUG，界面一切正常但筛选从没生效过。
        self.filter.setCurrentIndex(1)
        # currentData() 回来的是**字符串**（LogLevel 是 str 枚举，Qt 存的原值），
        # 必须转回枚举再用作 _ORDER 的键 —— 字符串恰好能查到，但那靠的是
        # str 混入的哈希，换个枚举类型就会静默失配。
        self._min_level = LogLevel(self.filter.currentData())
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
        # 到这里 text 才存在，现在可以把下拉框接上（见上面的注释）。
        self.filter.currentIndexChanged.connect(self._on_filter)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addLayout(head)
        lay.addWidget(self.banner)
        lay.addWidget(self.text, 1)

    # -- 写入 -------------------------------------------------------------

    def append(self, level: LogLevel, message: str, detail: str = "") -> None:
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
        plain = f"{ts} [{label}] {message}" + (f" · {detail}" if detail else "")
        self._lines.append((level, line, plain))
        if _ORDER[level] >= _ORDER[self._min_level]:
            self._render(line)
        if level is LogLevel.ERROR:
            # 横幅不受级别筛选影响：被筛掉的是"翻不到"，不是"没发生"。
            self.show_banner(message)

    def _render(self, line: str) -> None:
        self.text.appendHtml(line)
        self.text.moveCursor(QTextCursor.MoveOperation.End)

    def plain_tail(self, lines: int = 40) -> list[str]:
        """最近若干行**纯文本**，取自全量缓存，不受级别筛选影响。

        诊断报告要的是能直接读的文字，而且**必须是完整的那一份**：用户把
        日志筛到「仅错误」正是因为他只想看错误，但求助时需要的是上下文。
        按当前筛选取的话，报告里会恰好缺掉定位问题的那几行。
        """
        text = [plain for _lvl, _html, plain in self._lines]
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

    def set_min_level(self, level: LogLevel) -> None:
        """程序化设置级别阈值（与下拉框双向同步）。"""
        index = self.filter.findData(level)
        if index >= 0 and index != self.filter.currentIndex():
            self.filter.setCurrentIndex(index)  # 会触发 _on_filter
            return
        self._min_level = level
        self._rerender()

    def _on_filter(self) -> None:
        self._min_level = LogLevel(self.filter.currentData())
        self._rerender()

    def _rerender(self) -> None:
        """按当前阈值重画。**从全量缓存重画**，所以放宽级别能找回旧行。"""
        self.text.clear()
        for level, line, _plain in self._lines:
            if _ORDER[level] >= _ORDER[self._min_level]:
                self.text.appendHtml(line)
        self.text.moveCursor(QTextCursor.MoveOperation.End)
