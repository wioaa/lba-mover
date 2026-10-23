"""自定义对话框：危险操作二次确认（不使用 ``QMessageBox``）。"""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from PySide6.QtCore import QPoint, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core.models import FileEntry, human_bytes
from app.ui import cards, icons, theme


class _ShadowCard(QWidget):
    """带圆角与柔和阴影的对话框外框。"""

    RADIUS = 14
    MARGIN = 16

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def paintEvent(self, event) -> None:  # noqa: D102
        palette = theme.palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        body = QRectF(self.rect()).adjusted(
            self.MARGIN, self.MARGIN, -self.MARGIN, -self.MARGIN
        )

        shadow = QColor(palette["shadow"])
        layers = 10
        for index in range(layers, 0, -1):
            alpha = int(3 + 26 * (index / layers) ** 2)
            shadow.setAlpha(alpha)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(shadow)
            painter.drawRoundedRect(
                body.adjusted(-index * 0.8, -index * 0.8 + 2, index * 0.8, index * 0.8 + 2),
                self.RADIUS + index * 0.4,
                self.RADIUS + index * 0.4,
            )

        painter.setBrush(QColor(palette["card"]))
        painter.setPen(QPen(QColor(palette["border"]), 1.0))
        painter.drawRoundedRect(body, self.RADIUS, self.RADIUS)
        painter.end()


class DangerConfirmDialog(QDialog):
    """真实移动前的二次确认对话框。

    v2 扩展（T05 要求 7）：除了文件清单，还必须摊开

    * 目标区间（``[start, end)``，含簇数与字节数）；
    * 挡路者数量与被驱逐的簇数；
    * 预计写入字节数（= 搬运簇数 × 簇大小）；
    * ``on_error`` 策略选择（失败继续 / 失败即止）。

    确认按钮默认禁用，用户必须勾选「我已知晓风险」后才可点击。
    """

    ON_ERROR_CONTINUE = 0
    ON_ERROR_HALT = 1

    def __init__(
        self,
        volume_name: str,
        files: Sequence[FileEntry],
        total_clusters: int,
        bytes_per_cluster: int,
        target_range: Tuple[int, int],
        parent: Optional[QWidget] = None,
        *,
        blocker_count: int = 0,
        evict_clusters: int = 0,
        planned_tasks: int = 0,
    ) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setModal(True)
        self._drag_offset: Optional[QPoint] = None

        self._volume_name = volume_name
        self._files = list(files)
        self._total_clusters = total_clusters
        self._bytes_per_cluster = bytes_per_cluster
        self._target_range = target_range
        self._blocker_count = max(0, int(blocker_count))
        self._evict_clusters = max(0, int(evict_clusters))
        self._planned_tasks = max(0, int(planned_tasks))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card = _ShadowCard(self)
        outer.addWidget(card)

        margin = _ShadowCard.MARGIN
        layout = QVBoxLayout(card)
        layout.setContentsMargins(margin + 24, margin + 22, margin + 24, margin + 22)
        layout.setSpacing(16)

        layout.addWidget(self._build_header())
        layout.addWidget(cards.Divider())
        layout.addWidget(self._build_summary())
        layout.addWidget(self._build_file_list(), 1)
        layout.addWidget(cards.Divider())
        layout.addWidget(self._build_on_error())

        self._checkbox = cards.SoftCheckBox("我已知晓风险，确认在真实磁盘上移动上述文件", card)
        self._checkbox.toggled.connect(self._on_acknowledged)
        layout.addWidget(self._checkbox)

        layout.addLayout(self._build_buttons())

        self.setMinimumWidth(620)
        self.setFixedHeight(640)


    # ------------------------------------------------------------------ #
    def _build_header(self) -> QWidget:
        wrapper = QWidget(self)
        row = QHBoxLayout(wrapper)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(14)

        icon_holder = QWidget(wrapper)
        icon_holder.setFixedSize(46, 46)
        row.addWidget(icon_holder, 0, Qt.AlignmentFlag.AlignTop)

        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)
        title = cards.label("确认执行真实数据迁移", "h1", wrapper)
        subtitle = cards.label(
            "此操作会直接改写磁盘上的簇分配，无法通过「撤销」恢复。", "dim", wrapper
        )
        column.addWidget(title)
        column.addWidget(subtitle)
        row.addLayout(column, 1)

        class _WarningIcon(QWidget):
            def paintEvent(inner_self, event) -> None:  # noqa: N805
                palette = theme.palette()
                painter = QPainter(inner_self)
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(palette["danger_soft"]))
                painter.drawEllipse(QRectF(0, 0, 46, 46))
                icons.paint_icon(
                    painter, "warning", QRectF(11, 11, 24, 24), QColor(palette["danger"])
                )
                painter.end()

        warning = _WarningIcon(icon_holder)
        warning.setGeometry(0, 0, 46, 46)
        return wrapper

    def _build_summary(self) -> QWidget:
        wrapper = QWidget(self)
        layout = QVBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        total_bytes = self._total_clusters * self._bytes_per_cluster
        start, end = self._target_range
        clusters = max(0, end - start)
        rows = [
            ("目标卷", self._volume_name),
            ("文件数量", f"{len(self._files)} 个"),
            (
                "目标区间",
                f"LCN {start:,} – {max(start, end - 1):,}"
                f"（{clusters:,} 簇 ≈ {human_bytes(clusters * self._bytes_per_cluster)}）",
            ),
            ("挡路文件", f"{self._blocker_count} 个（需迁出 {self._evict_clusters:,} 簇）"),
            ("计划任务", f"{self._planned_tasks} 个（装入 + 驱逐 + 中转）"),
            (
                "预计写入",
                f"{self._total_clusters:,} 簇 ≈ {human_bytes(total_bytes)}",
            ),
        ]
        for name, value in rows:
            layout.addWidget(cards.StatRow(name, value, wrapper))

        warn = cards.label(
            "「预计写入」是 MOVE 实际搬运的簇数，与本卷容量无关；"
            "搬运在卷内进行，不产生跨卷数据。",
            "caption",
            wrapper,
            True,
        )
        layout.addWidget(warn)
        return wrapper

    def _build_on_error(self) -> QWidget:
        wrapper = QWidget(self)
        layout = QVBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(cards.label("出错时的处理策略", "dim", wrapper))
        self._on_error_switch = cards.SegmentedControl(
            ["单段失败后继续", "任意失败即中止"], self.ON_ERROR_CONTINUE, wrapper
        )
        layout.addWidget(self._on_error_switch)

        self._on_error_hint = cards.label(
            "继续：跳过失败段、把能搬的都搬完（结果面板会列出失败明细）。",
            "caption",
            wrapper,
            True,
        )
        layout.addWidget(self._on_error_hint)
        self._on_error_switch.changed.connect(self._on_error_mode_changed)
        return wrapper

    def _on_error_mode_changed(self, index: int) -> None:
        if index == self.ON_ERROR_HALT:
            self._on_error_hint.setText(
                "中止：遇到第一个失败立即停止，剩余任务标记为「跳过」。"
                "适合对连续性要求高的场景。"
            )
        else:
            self._on_error_hint.setText(
                "继续：跳过失败段、把能搬的都搬完（结果面板会列出失败明细）。"
            )

    def on_error(self) -> str:
        """``continue`` / ``halt``，直接喂给 :class:`MoveWorker`。"""
        return (
            "halt"
            if self._on_error_switch.current_index == self.ON_ERROR_HALT
            else "continue"
        )

    def _build_file_list(self) -> QWidget:
        panel = QFrame(self)
        panel.setProperty("role", "sunken")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(6)

        class _FileList(QWidget):
            def __init__(inner_self, files: List[FileEntry], owner: QWidget) -> None:  # noqa: N805
                super().__init__(owner)
                inner_self._files = files
                inner_self.setMinimumHeight(120)

            def paintEvent(inner_self, event) -> None:  # noqa: N805
                palette = theme.palette()
                painter = QPainter(inner_self)
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

                font_name = QFont(theme.app_font(12, QFont.Weight.DemiBold))
                font_size = QFont(theme.mono_font(11))
                y = 2.0
                line_height = 22.0
                available = max(1, int((inner_self.height() - 4) / line_height))
                total = len(inner_self._files)
                if total <= available:
                    shown = inner_self._files
                else:
                    # 留一行给「…… 还有 N 个文件」
                    shown = inner_self._files[: max(1, available - 1)]
                for entry in shown:
                    painter.setFont(font_name)
                    painter.setPen(QColor(palette["text"]))
                    metrics = QFontMetrics(font_name)
                    text = metrics.elidedText(
                        entry.name, Qt.TextElideMode.ElideMiddle, inner_self.width() - 110
                    )
                    painter.drawText(
                        QRectF(0, y, inner_self.width() - 100, 20),
                        int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                        text,
                    )
                    painter.setFont(font_size)
                    painter.setPen(QColor(palette["text_dim"]))
                    painter.drawText(
                        QRectF(inner_self.width() - 96, y, 96, 20),
                        int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                        human_bytes(entry.size),
                    )
                    y += line_height

                remaining = len(inner_self._files) - len(shown)
                if remaining > 0:
                    painter.setFont(font_name)
                    painter.setPen(QColor(palette["text_faint"]))
                    painter.drawText(
                        QRectF(0, y, inner_self.width(), 20),
                        int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                        f"…… 还有 {remaining} 个文件",
                    )
                painter.end()

        file_list = _FileList(self._files, panel)
        layout.addWidget(file_list, 1)
        return panel

    def _build_buttons(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        row.addStretch(1)

        cancel = QPushButton("取消", self)
        cancel.setProperty("role", "secondary")
        cancel.setMinimumWidth(104)
        cancel.setMinimumHeight(38)
        cancel.clicked.connect(self.reject)
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)

        self._confirm = QPushButton("我确认，开始迁移", self)
        self._confirm.setProperty("role", "danger")
        self._confirm.setMinimumWidth(160)
        self._confirm.setMinimumHeight(38)
        self._confirm.setEnabled(False)
        self._confirm.setCursor(Qt.CursorShape.PointingHandCursor)
        self._confirm.clicked.connect(self.accept)

        row.addWidget(cancel)
        row.addWidget(self._confirm)
        return row

    # ------------------------------------------------------------------ #
    def _on_acknowledged(self, checked: bool) -> None:
        self._confirm.setEnabled(checked)

    def sizeHint(self) -> QSize:  # noqa: D102
        return QSize(660, 640)

    # 拖动对话框
    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: D102
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self.windowHandle()
            if handle is not None and handle.startSystemMove():
                event.accept()
                return
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: D102
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: D102
        self._drag_offset = None
        super().mouseReleaseEvent(event)


class InfoDialog(QDialog):
    """轻量信息提示对话框（用于必须中断流程的致命错误）。"""

    def __init__(
        self,
        title: str,
        message: str,
        kind: str = "info",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setModal(True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card = _ShadowCard(self)
        outer.addWidget(card)

        margin = _ShadowCard.MARGIN
        layout = QVBoxLayout(card)
        layout.setContentsMargins(margin + 24, margin + 22, margin + 24, margin + 20)
        layout.setSpacing(14)

        color_key = {"info": "accent", "warn": "warning", "error": "danger"}.get(kind, "accent")
        header_row = QHBoxLayout()
        header_row.setSpacing(12)

        class _Icon(QWidget):
            def __init__(inner_self, name: str, key: str, owner: QWidget) -> None:  # noqa: N805
                super().__init__(owner)
                inner_self._name = name
                inner_self._key = key
                inner_self.setFixedSize(26, 26)

            def paintEvent(inner_self, event) -> None:  # noqa: N805
                painter = QPainter(inner_self)
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                icons.paint_icon(
                    painter,
                    inner_self._name,
                    QRectF(0, 0, 26, 26),
                    theme.color(inner_self._key),
                )
                painter.end()

        icon_name = "warning" if kind in ("warn", "error") else "info"
        header_row.addWidget(_Icon(icon_name, color_key, card), 0,
                             Qt.AlignmentFlag.AlignVCenter)
        header_row.addWidget(cards.label(title, "h2", card), 1)
        layout.addLayout(header_row)

        body = cards.label(message, "dim", card, wrap=True)
        body.setMinimumWidth(420)
        layout.addWidget(body)

        row = QHBoxLayout()
        row.addStretch(1)
        button = QPushButton("知道了", card)
        button.setProperty("role", "primary")
        button.setMinimumWidth(110)
        button.setMinimumHeight(36)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(self.accept)
        row.addWidget(button)
        layout.addLayout(row)
