"""手动选择要整理的文件：表格模型 + 选择对话框。

为什么需要这个模式
------------------
整卷整理是"把能治的都治一遍"，但真实使用里常见的是"就这个游戏目录/这个
虚拟机镜像在拖后腿"。整卷跑一次可能几十分钟，还要动几十万个文件；
手动模式下用户只对选中的文件负责，代价与风险都小得多。

三个设计约束（都是为了不让"手动"变成长时间卡死或选错文件）
------------------------------------------------------------
1. **模型只做索引，不做控件**。卷上可能有上百万个文件，一行一个 QWidget
   必然卡死；QTableView + QAbstractTableModel 是虚拟化的，行数是上百万也
   只画可见的几十行。
2. **筛选与排序在模型里做**，不进 ``QSortFilterProxyModel``：多一层代理
   意味着每个可见行都要过一次 Python 调用，百万行时滚动会明显掉帧。
3. **选择结果按 file_id 存**，不按行号。行号随筛选/排序变化，用它当身份
   就会"选中的文件悄悄换了一个"。
"""

from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ...contract.models import FileFrag
from ..format import fmt_bytes, fmt_count, fmt_percent
from ..theme import DARK, Palette, mono_font

__all__ = ["FileSelectDialog", "FileTableModel", "SCOPE_SORTS"]

#: 排序方式：``(键, 显示文本)``。默认按段数降序 —— 碎得最厉害的先看到。
SCOPE_SORTS = (
    ("runs", "段数（多→少）"),
    ("size", "大小（大→小）"),
    ("frag", "碎片度（高→低）"),
    ("path", "路径（A→Z）"),
)

_COL_CHECK, _COL_PATH, _COL_SIZE, _COL_RUNS, _COL_FRAG = range(5)

#: ``Qt.CheckState.Checked`` 的数值（2）。**不要用 ``int(Qt.CheckState.X)``**：
#: PySide6 的枚举不支持 ``int()``，会抛 TypeError，被 except 吞掉之后
#: ``bool(枚举成员)`` 恒为 True —— 表现就是"勾上了再也取消不掉"。
_CHECKED_VALUE = Qt.CheckState.Checked.value


def _is_checked(value) -> bool:
    """判断勾选状态。QTableView 可能给枚举，也可能给 0/2 这样的整数。"""
    raw = getattr(value, "value", value)
    return raw == _CHECKED_VALUE


class FileTableModel(QAbstractTableModel):
    """把 ``FragReport.files`` 摆成一张可筛选、可勾选的表。"""

    HEADERS = ("", "路径", "大小", "段数", "碎片度")

    def __init__(
        self,
        files: tuple[FileFrag, ...],
        block_size: int,
        parent: QWidget | None = None,
        palette: Palette = DARK,
    ) -> None:
        super().__init__(parent)
        self._files = files
        self._block_size = max(1, block_size)
        self._p = palette
        self._rows: list[int] = []
        self._checked: set[str] = set()
        self._only_fragmented = True
        self._needle = ""
        self._sort = "runs"
        self._checked_blocks = 0
        self.refresh()

    # -- 数据访问 ---------------------------------------------------------

    def frag_at(self, row: int) -> FileFrag | None:
        if 0 <= row < len(self._rows):
            return self._files[self._rows[row]]
        return None

    def visible_count(self) -> int:
        return len(self._rows)

    def total_count(self) -> int:
        return len(self._files)

    def selection_ids(self) -> tuple[str, ...]:
        return tuple(self._checked)

    def selection_count(self) -> int:
        return len(self._checked)

    def selection_blocks(self) -> int:
        return self._checked_blocks

    def selection_bytes(self) -> int:
        return self._checked_blocks * self._block_size

    # -- 筛选 / 排序 ------------------------------------------------------

    def set_filter(
        self,
        *,
        needle: str | None = None,
        only_fragmented: bool | None = None,
        sort: str | None = None,
    ) -> None:
        changed = False
        if needle is not None and needle != self._needle:
            self._needle = needle
            changed = True
        if only_fragmented is not None and only_fragmented != self._only_fragmented:
            self._only_fragmented = only_fragmented
            changed = True
        if sort is not None and sort != self._sort:
            self._sort = sort
            changed = True
        if changed:
            self.refresh()

    def refresh(self) -> None:
        needle = self._needle.casefold().strip()
        only_frag = self._only_fragmented
        rows: list[int] = []
        files = self._files
        for i, f in enumerate(files):
            if only_frag and not f.is_fragmented:
                continue
            if needle and needle not in f.path.casefold():
                continue
            rows.append(i)

        sort = self._sort
        if sort == "path":
            rows.sort(key=lambda i: files[i].path.casefold())
        elif sort == "size":
            rows.sort(key=lambda i: (-files[i].total_blocks, files[i].path.casefold()))
        elif sort == "frag":
            rows.sort(key=lambda i: (-files[i].frag_score, -files[i].total_blocks))
        else:  # runs
            rows.sort(key=lambda i: (-files[i].run_count, -files[i].total_blocks))

        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    # -- 选择 -------------------------------------------------------------

    def set_selection(self, ids) -> None:
        self._checked = {i for i in ids}
        self._checked_blocks = sum(
            f.total_blocks for f in self._files if f.file_id in self._checked
        )
        if self._rows:
            self.dataChanged.emit(
                self.index(0, _COL_CHECK),
                self.index(len(self._rows) - 1, _COL_FRAG),
                [Qt.ItemDataRole.CheckStateRole],
            )

    def _apply(self, frag: FileFrag, on: bool) -> None:
        if on:
            if frag.file_id in self._checked:
                return
            self._checked.add(frag.file_id)
            self._checked_blocks += frag.total_blocks
        else:
            if frag.file_id not in self._checked:
                return
            self._checked.discard(frag.file_id)
            self._checked_blocks -= frag.total_blocks

    def select_all_visible(self) -> None:
        for i in self._rows:
            self._apply(self._files[i], True)
        self._emit_all()

    def clear_selection(self) -> None:
        self._checked.clear()
        self._checked_blocks = 0
        self._emit_all()

    def _emit_all(self) -> None:
        if self._rows:
            self.dataChanged.emit(
                self.index(0, _COL_CHECK),
                self.index(len(self._rows) - 1, _COL_FRAG),
                [Qt.ItemDataRole.CheckStateRole],
            )

    # -- Qt 模型接口 -------------------------------------------------------

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section: int, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation != Qt.Orientation.Horizontal:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section] if 0 <= section < len(self.HEADERS) else None
        if role == Qt.ItemDataRole.TextAlignmentRole and section in (_COL_SIZE, _COL_RUNS, _COL_FRAG):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        frag = self.frag_at(index.row())
        if frag is None:
            return None
        col = index.column()

        if role == Qt.ItemDataRole.CheckStateRole and col == _COL_CHECK:
            return (
                Qt.CheckState.Checked
                if frag.file_id in self._checked
                else Qt.CheckState.Unchecked
            )
        if role == Qt.ItemDataRole.DisplayRole:
            if col == _COL_PATH:
                return frag.path
            if col == _COL_SIZE:
                return fmt_bytes(frag.total_blocks * self._block_size)
            if col == _COL_RUNS:
                return f"{frag.run_count} 段"
            if col == _COL_FRAG:
                return fmt_percent(frag.frag_score)
            return ""
        if role == Qt.ItemDataRole.ToolTipRole:
            return (
                f"{frag.path}\n{frag.run_count} 段 / "
                f"{fmt_bytes(frag.total_blocks * self._block_size)} / "
                f"碎片度 {fmt_percent(frag.frag_score)}"
            )
        if role == Qt.ItemDataRole.TextAlignmentRole and col in (
            _COL_SIZE,
            _COL_RUNS,
            _COL_FRAG,
        ):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ForegroundRole and not frag.is_fragmented:
            # 连续文件本来就不需要整理，压暗即可；颜色只从色板取
            return QColor(self._p.text_faint)
        return None

    def flags(self, index: QModelIndex):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.column() == _COL_CHECK:
            return base | Qt.ItemFlag.ItemIsUserCheckable
        return base

    def setData(self, index: QModelIndex, value, role=Qt.ItemDataRole.EditRole) -> bool:  # noqa: N802
        if role != Qt.ItemDataRole.CheckStateRole or index.column() != _COL_CHECK:
            return False
        frag = self.frag_at(index.row())
        if frag is None:
            return False
        self._apply(frag, _is_checked(value))
        self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
        return True


class FileSelectDialog(QDialog):
    """勾选要整理的文件。结果用 :meth:`selection_ids` 取。"""

    def __init__(
        self,
        files: tuple[FileFrag, ...],
        block_size: int,
        palette: Palette = DARK,
        *,
        preselect: tuple[str, ...] = (),
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._p = palette
        self.setWindowTitle("选择要整理的文件")
        self.setMinimumSize(920, 620)
        self.model = FileTableModel(files, block_size, self, palette)
        self.model.set_selection(preselect)

        # -- 顶部：搜索 + 筛选 + 排序 --
        self.search = QLineEdit()
        self.search.setPlaceholderText("输入路径片段过滤，例如 \\Steam\\ 或 .vmdk")
        self.search.setClearButtonEnabled(True)

        self.kind = QComboBox()
        self.kind.addItem("只看碎片文件", True)
        self.kind.addItem("全部文件", False)

        self.sort = QComboBox()
        for key, label in SCOPE_SORTS:
            self.sort.addItem(label, key)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(180)  # 百万行时逐键重排会明显卡手，等手停下来
        self._debounce.timeout.connect(self._apply_filter)
        self.search.textChanged.connect(lambda _t: self._debounce.start())

        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(self.search, 1)
        top.addWidget(self.kind)
        top.addWidget(self.sort)

        # -- 表格 --
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setAlternatingRowColors(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.setWordWrap(False)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        head.setStretchLastSection(False)
        self.table.setColumnWidth(_COL_CHECK, 34)
        head.setSectionResizeMode(_COL_PATH, QHeaderView.ResizeMode.Stretch)
        for col, width in ((_COL_SIZE, 96), (_COL_RUNS, 76), (_COL_FRAG, 78)):
            self.table.setColumnWidth(col, width)
        self.table.setFont(mono_font(9))
        self.table.doubleClicked.connect(self._toggle_row)

        # -- 底部 --
        self.summary = QLabel()
        self.summary.setObjectName("cardHint")

        self.btn_all = QPushButton("全选筛选结果")
        self.btn_all.setObjectName("ghost")
        self.btn_all.clicked.connect(self._select_all)

        self.btn_clear = QPushButton("清空选择")
        self.btn_clear.setObjectName("ghost")
        self.btn_clear.clicked.connect(self._clear)

        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.clicked.connect(self.reject)

        self.btn_ok = QPushButton("使用这些文件")
        self.btn_ok.setObjectName("primary")
        self.btn_ok.clicked.connect(self.accept)

        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        bottom.addWidget(self.summary, 1)
        bottom.addWidget(self.btn_all)
        bottom.addWidget(self.btn_clear)
        bottom.addWidget(self.btn_cancel)
        bottom.addWidget(self.btn_ok)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)
        lay.addLayout(top)
        lay.addWidget(self.table, 1)
        lay.addLayout(bottom)

        self.model.dataChanged.connect(lambda *_: self._refresh_summary())
        self.kind.currentIndexChanged.connect(lambda _i: self._apply_filter())
        self.sort.currentIndexChanged.connect(lambda _i: self._apply_filter())
        self._refresh_summary()

    # -- 结果 -------------------------------------------------------------

    def selection_ids(self) -> tuple[str, ...]:
        return self.model.selection_ids()

    # -- 交互 -------------------------------------------------------------

    def _apply_filter(self) -> None:
        self.model.set_filter(
            needle=self.search.text(),
            only_fragmented=bool(self.kind.currentData()),
            sort=str(self.sort.currentData()),
        )
        self._refresh_summary()

    def _toggle_row(self, index: QModelIndex) -> None:
        frag = self.model.frag_at(index.row())
        if frag is None:
            return
        on = frag.file_id not in self.model.selection_ids()
        self.model.setData(
            self.model.index(index.row(), _COL_CHECK),
            Qt.CheckState.Checked if on else Qt.CheckState.Unchecked,
            Qt.ItemDataRole.CheckStateRole,
        )

    def _select_all(self) -> None:
        self.model.select_all_visible()
        self._refresh_summary()

    def _clear(self) -> None:
        self.model.clear_selection()
        self._refresh_summary()

    def _refresh_summary(self) -> None:
        count = self.model.selection_count()
        shown = self.model.visible_count()
        total = self.model.total_count()
        if count:
            text = (
                f"已选 {fmt_count(count)} 个 · "
                f"{fmt_bytes(self.model.selection_bytes())} · "
                f"预计搬 {fmt_count(self.model.selection_blocks())} 块"
            )
        else:
            text = "还没有选中文件（勾选左侧方框，或双击整行）"
        text += f"　｜　列表显示 {fmt_count(shown)} / {fmt_count(total)} 个文件"
        self.summary.setText(text)
        self.btn_ok.setEnabled(count > 0)
