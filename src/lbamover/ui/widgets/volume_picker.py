"""目标卷选择。

信息优先级按"用户做决定需要什么"排：盘符 → 容量占用 → 文件系统 → 风险。
系统卷单独标红，因为它是唯一一个"点错了可能开不了机"的选项。

没有可整理卷时**不留空白列表**，而是给出原因与下一步——空列表是 UI 里
最让人困惑的状态。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ...contract.models import VolumeInfo
from ..format import fmt_bytes, fmt_percent
from ..theme import DARK, Palette

__all__ = ["VolumePicker"]


class _UsageBar(QFrame):
    """1px 描边的细进度条。用自绘而不是 QProgressBar，避免 QSS 高度限制。"""

    def __init__(self, palette: Palette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._p = palette
        self._ratio = 0.0
        self.setFixedHeight(5)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_ratio(self, ratio: float, danger: bool = False) -> None:
        self._ratio = max(0.0, min(1.0, ratio))
        self._danger = danger
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor, QPainter

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(self._p.track))
        p.drawRoundedRect(r, 2.5, 2.5)
        if self._ratio > 0:
            fill = QRectF(r)
            fill.setWidth(max(3.0, r.width() * self._ratio))
            p.setBrush(QColor(self._p.danger if getattr(self, "_danger", False) else self._p.accent))
            p.drawRoundedRect(fill, 2.5, 2.5)


class _VolumeRow(QWidget):
    def __init__(self, vol: VolumeInfo, palette: Palette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.volume = vol
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)

        head = QHBoxLayout()
        head.setSpacing(8)
        badge = QLabel(vol.letter.replace("\\", ""))
        badge.setFixedSize(30, 30)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        color = palette.danger if vol.is_system else palette.accent
        badge.setStyleSheet(
            f"background: {color}22; color: {color}; border: 1px solid {color}55;"
            "border-radius: 8px; font-weight: 700; font-size: 12px;"
        )
        head.addWidget(badge)

        title_box = QVBoxLayout()
        title_box.setSpacing(1)
        title = QLabel(vol.label or f"{vol.letter} 本地磁盘")
        title.setStyleSheet(f"color: {palette.text}; font-weight: 600; font-size: 13px;")
        sub_text = f"{vol.filesystem or '未知文件系统'}"
        if vol.is_system:
            sub_text += " · 系统卷"
        if not vol.writable:
            sub_text += " · 只读"
        sub = QLabel(sub_text)
        sub.setStyleSheet(f"color: {palette.text_faint}; font-size: 11px;")
        title_box.addWidget(title)
        title_box.addWidget(sub)
        head.addLayout(title_box, 1)
        root.addLayout(head)

        used = vol.geometry.total_bytes - vol.free_bytes
        bar = _UsageBar(palette)
        bar.set_ratio(used / vol.geometry.total_bytes if vol.geometry.total_bytes else 0)
        root.addWidget(bar)

        foot = QLabel(
            f"可用 {fmt_bytes(vol.free_bytes)} / 共 {fmt_bytes(vol.geometry.total_bytes)}"
            f" · 已用 {fmt_percent(used / vol.geometry.total_bytes if vol.geometry.total_bytes else 0, 0)}"
        )
        foot.setStyleSheet(f"color: {palette.text_dim}; font-size: 11px;")
        root.addWidget(foot)


class VolumePicker(QWidget):
    """卷列表。选中即发出 ``volumeSelected``。"""

    volumeSelected = Signal(object)
    refreshRequested = Signal()

    def __init__(self, palette: Palette = DARK, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._p = palette
        self._list = QListWidget()
        self._list.setSpacing(0)
        self._list.setUniformItemSizes(False)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.currentItemChanged.connect(self._on_current)

        self._empty = QLabel("正在枚举本机卷…")
        self._empty.setWordWrap(True)
        self._empty.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._empty.setStyleSheet(f"color: {palette.text_faint}; font-size: 12px; padding: 10px 2px;")

        head = QHBoxLayout()
        head.setSpacing(6)
        head.addWidget(self._empty, 1)
        self._refresh = QPushButton("刷新")
        self._refresh.setObjectName("ghost")
        self._refresh.clicked.connect(self.refreshRequested.emit)
        head.addWidget(self._refresh)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        lay.addLayout(head)
        lay.addWidget(self._list, 1)

    # -- 数据 -------------------------------------------------------------

    def set_volumes(self, volumes: list[VolumeInfo], preselect: int | None = None) -> None:
        self._list.blockSignals(True)
        self._list.clear()
        if not volumes:
            self._empty.setText(
                "没有找到可整理的卷。\n\n"
                "常见原因：\n"
                "· 只有网络映射盘（无法按 LBA 访问）\n"
                "· 光驱/未插入介质的读卡器\n"
                "· 非 Windows 平台"
            )
            self._empty.show()
            self._list.hide()
        else:
            self._empty.hide()
            self._list.show()
            for vol in volumes:
                item = QListWidgetItem()
                row = _VolumeRow(vol, self._p)
                item.setSizeHint(row.sizeHint())
                item.setData(Qt.ItemDataRole.UserRole, vol)
                self._list.addItem(item)
                self._list.setItemWidget(item, row)
            idx = 0 if preselect is None else max(0, min(len(volumes) - 1, preselect))
            self._list.setCurrentRow(idx)
        self._list.blockSignals(False)
        if volumes:
            self.volumeSelected.emit(self.current())

    def current(self) -> VolumeInfo | None:
        item = self._list.currentItem()
        if item is None:
            return None
        return item.data(Qt.ItemDataRole.UserRole)

    def count(self) -> int:
        return self._list.count()

    def select_letter(self, letter: str) -> bool:
        for i in range(self._list.count()):
            vol = self._list.item(i).data(Qt.ItemDataRole.UserRole)
            if vol is not None and vol.letter.upper().startswith(letter.upper()):
                self._list.setCurrentRow(i)
                return True
        return False

    def _on_current(self, cur, prev) -> None:
        if cur is None:
            return
        vol = cur.data(Qt.ItemDataRole.UserRole)
        if vol is not None:
            self.volumeSelected.emit(vol)
