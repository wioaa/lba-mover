"""执行与恢复（T05 要求 7）：部分完成结果面板 + 手动回滚。

两条纪律：

* **分段统计必须诚实**：成功 / 跳过 / 失败三段分开写，失败项可在折叠面板里
  逐条展开看到 Win32 错误码 —— 不允许只给一句"部分失败"。
* **回滚必须手动**：只有「本次会话确实写过流水」且「确实执行过」时按钮才可用；
  点击后仍需二次确认。工具绝不自动回滚（自动回滚可能把已就位的数据再搬乱）。
"""

from __future__ import annotations

from typing import Optional, Set

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QHBoxLayout,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.core.models import MoveOutcome, human_bytes
from app.ui import cards, theme, widgets


class SegmentBar(QWidget):
    """成功 / 跳过 / 失败 三段比例条（自绘）。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._parts = (0, 0, 0)   # (ok, skipped, failed)
        self.setFixedHeight(38)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_parts(self, ok: int, skipped: int, failed: int) -> None:
        self._parts = (max(0, ok), max(0, skipped), max(0, failed))
        self.update()

    def paintEvent(self, event) -> None:  # noqa: D102
        palette = theme.palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        ok, skipped, failed = self._parts
        total = ok + skipped + failed
        bar = QRectF(self.rect()).adjusted(0.5, 3.0, -0.5, -15.0)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(palette["sunken"]))
        painter.drawRoundedRect(bar, 4, 4)

        if total > 0:
            painter.save()
            clip = QPainterPath()
            clip.addRoundedRect(bar, 4, 4)
            painter.setClipPath(clip)
            x = bar.left()
            for value, key in zip(
                (ok, skipped, failed),
                ("success", "text_faint", "danger"),
            ):
                if value <= 0:
                    continue
                width = bar.width() * value / total
                chunk = QRectF(x, bar.top(), width, bar.height())
                painter.setBrush(QColor(palette[key]))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawRect(chunk)
                x += width
            painter.restore()

        font = QFont(theme.app_font(11))
        painter.setFont(font)
        metrics = QFontMetrics(font)
        painter.setPen(QColor(palette["text_dim"]))
        text = (
            f"成功 {ok} 个 · 跳过 {skipped} 个 · 失败 {failed} 个"
            if total > 0
            else "尚未执行"
        )
        painter.drawText(
            QRectF(0, bar.bottom() + 1, self.width(), 14),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            metrics.elidedText(text, Qt.TextElideMode.ElideRight, max(20, self.width())),
        )
        painter.end()


class RecoveryPanel(QWidget):
    """执行结果 + 手动回滚。"""

    rollbackRequested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._journal_path = ""
        self._journal_clusters = 0
        self._executed = False
        self._locked = True

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        head.addWidget(cards.IconLabel("shield", 16, "accent"), 0,
                       Qt.AlignmentFlag.AlignVCenter)
        head.addWidget(cards.label("执行与恢复", "h2"), 0, Qt.AlignmentFlag.AlignVCenter)
        self._state_badge = cards.Badge("未执行", "neutral", self)
        head.addWidget(self._state_badge, 0, Qt.AlignmentFlag.AlignVCenter)
        head.addStretch(1)
        layout.addLayout(head)

        layout.addWidget(cards.label("本次执行结果", "dim", self))
        self._segments = SegmentBar(self)
        layout.addWidget(self._segments)

        self._summary = cards.label("尚未执行任何迁移。", "caption", self, True)
        layout.addWidget(self._summary)

        self._failures = widgets.CollapsibleSection("失败明细", self, expanded=False)
        self._failures.setVisible(False)
        self._failures_box = QWidget(self._failures.content())
        self._failures_layout = QVBoxLayout(self._failures_box)
        self._failures_layout.setContentsMargins(4, 0, 0, 0)
        self._failures_layout.setSpacing(3)
        self._failures.content_layout().addWidget(self._failures_box)
        layout.addWidget(self._failures)

        layout.addWidget(cards.Divider())
        self._journal_label = cards.label("", "caption", self, True)
        layout.addWidget(self._journal_label)

        self._rollback_button = QPushButton("回滚本次迁移", self)
        self._rollback_button.setMinimumHeight(38)
        self._rollback_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._rollback_button.clicked.connect(self.rollbackRequested.emit)
        layout.addWidget(self._rollback_button)

        self._rollback_hint = cards.label("", "caption", self, True)
        layout.addWidget(self._rollback_hint)

        self.clear()

    # ------------------------------------------------------------------ #
    # 接口
    # ------------------------------------------------------------------ #
    def clear(self) -> None:
        """回到「未执行」状态。"""
        self._executed = False
        self._segments.set_parts(0, 0, 0)
        self._state_badge.set_state("未执行", "neutral")
        self._summary.setText("尚未执行任何迁移。")
        self._clear_failures()
        self._failures.setVisible(False)
        self._sync_rollback()

    def set_journal(self, path: str, clusters: int) -> None:
        """告知本次会话的流水文件（``path`` 为空表示没有流水）。"""
        self._journal_path = path or ""
        self._journal_clusters = max(0, int(clusters))
        self._sync_rollback()

    def mark_executed(self) -> None:
        """真实执行过一次迁移（即便全部失败也算"执行过"）。"""
        self._executed = True
        self._sync_rollback()

    def set_rollback_busy(self, busy: bool, message: str = "") -> None:
        self._locked = busy
        self._rollback_button.setEnabled(not busy and self._can_rollback())
        if busy:
            self._rollback_hint.setText(message or "正在回滚…")
        else:
            self._sync_rollback()

    def disable_rollback(self, reason: str) -> None:
        """显式禁用回滚并说明原因（例如已经回滚过）。"""
        self._executed = False
        self._journal_path = ""
        self._rollback_hint.setText(reason)
        self._sync_rollback()

    def set_outcome(self, outcome: MoveOutcome, planned_tasks: int = 0) -> None:
        """渲染部分完成结果。"""
        if not isinstance(outcome, MoveOutcome):
            return
        failed_paths: Set[str] = {failure.path for failure in outcome.failures}
        succeeded = int(outcome.moved_files)
        failed = len(failed_paths)
        planned = max(planned_tasks, succeeded + failed)
        skipped = max(0, planned - succeeded - failed)
        self._segments.set_parts(succeeded, skipped, failed)

        if outcome.cancelled:
            self._state_badge.set_state("已取消", "warning")
        elif failed:
            self._state_badge.set_state("部分完成", "danger")
        else:
            self._state_badge.set_state("已完成", "success")

        clusters = int(outcome.moved_clusters)
        self._summary.setText(
            f"已搬运 {clusters:,} 簇（{human_bytes(clusters * self._bytes_per_cluster())}）"
            f" · 取消 {'是' if outcome.cancelled else '否'}"
        )

        self._clear_failures()
        if outcome.failures:
            for failure in outcome.failures:
                self._failures_layout.addWidget(self._failure_row(failure))
            self._failures.set_title(
                f"失败明细 · {len(outcome.failures)} 段（点开看错误码）"
            )
            self._failures.setVisible(True)
        else:
            self._failures.setVisible(False)
        self._executed = True
        self._sync_rollback()

    def has_journal(self) -> bool:
        return bool(self._journal_path)

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _bytes_per_cluster(self) -> int:
        # 由主窗口通过 set_cluster_size 注入；缺省 0 时 human_bytes(0) = "0 B"
        return int(getattr(self, "_bpc", 0) or 0)

    def set_cluster_size(self, bytes_per_cluster: int) -> None:
        self._bpc = max(0, int(bytes_per_cluster))

    def _failure_row(self, failure) -> QWidget:
        holder = QWidget(self._failures_box)
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(1)

        title = cards.label(
            f"{failure.path}", "body", holder, True
        )
        title.setToolTip(failure.message)
        column.addWidget(title)

        meta = cards.label(
            f"Win32 {failure.winerror} · 源 LCN {failure.src_lcn:,} → 目标 "
            f"{failure.dst_lcn:,} · {failure.clusters:,} 簇\n{failure.message}",
            "caption",
            holder,
            True,
        )
        column.addWidget(meta)
        return holder

    def _clear_failures(self) -> None:
        while self._failures_layout.count() > 0:
            item = self._failures_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def _can_rollback(self) -> bool:
        return bool(self._journal_path) and self._journal_clusters > 0 and not self._locked

    def _sync_rollback(self) -> None:
        can = self._can_rollback()
        self._rollback_button.setEnabled(can)
        cards.set_role(self._rollback_button, "danger" if can else "ghost")

        if self._journal_path:
            self._journal_label.setText(
                f"流水：{self._journal_path}\n"
                f"可回滚 {self._journal_clusters:,} 簇（逆序、src↔dst 互换）"
            )
        else:
            self._journal_label.setText("本次会话尚无流水文件，无内容可回滚。")

        if can:
            self._rollback_hint.setText(
                "回滚会**再次真实移动簇**，需要二次确认；失败段不会被回滚"
                "（它们本来就没搬动）。"
            )
        elif not self._journal_path:
            self._rollback_hint.setText(
                "回滚仅在「本次会话有流水且执行过」时可用 —— 不读历史流水，"
                "避免把不清楚来源的段搬回去。"
            )
        else:
            self._rollback_hint.setText(
                "本次会话已执行过迁移，但没有成功写入任何段，无需回滚。"
            )
