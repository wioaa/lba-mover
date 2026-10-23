"""主窗口：无边框圆角窗口 + 全部布局编排与业务联动（v2 / T05）。

图层结构::

    MainWindow (frameless, translucent)
      └ RootFrame                  —— 手绘阴影 / 圆角 / 边缘缩放
          ├ TitleBar
          ├ 工具条卡片              —— 卷选择 + 阈值 + 扫描按钮
          │    └ ScanControlPanel   —— 通道分段控件 + 统计条 + 回退告警
          ├ QSplitter
          │    ├ 左列: LBA 布局卡片（可拖选目标区间） + 大文件列表卡片
          │    └ 右列(滚动): 迁移摘要 / 目标区间+可行性+挡路者+保留区
          │                 / IO 加速 / 执行与恢复 / 模式与操作
          ├ 底部卡片: 进度条 + 折叠日志
          └ 状态栏

**写操作仍然只有一条路径**：``MoveWorker`` / ``RollbackWorker`` 内部按需导入
``app.win32.mover``，本文件不出现任何写调用点。
"""

from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import QPoint, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QGuiApplication,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPen,
    QShortcut,
)
from PySide6.QtWidgets import (
    QHBoxLayout,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from app.core import densify as densify_mod
from app.core import ranges as ranges_mod
from app.core.io_profile import IoProfile
from app.core.journal import MoveJournal, default_journal_path
from app.core.models import (
    MB,
    BitmapSnapshot,
    FeasibilityReport,
    FileEntry,
    MoveOutcome,
    MovePlan,
    TargetWindow,
    VolumeInfo,
    human_bytes,
)
from app.core.safety import build_policy, default_head_mb, default_tail_mb
from app.core.scanner import (
    BitmapWorker,
    BlockStreamWorker,
    FeasibilityWorker,
    IndexWorker,
    MftScanWorker,
    MoveWorker,
    PlanWorker,
    RollbackWorker,
    ScanWorker,
    describe_failures,
    scan_config_for,
)
from app.ui import cards, dialogs, theme
from app.ui.disk_map import DiskMapWidget
from app.ui.file_table import FileTableView
from app.ui.io_panel import ENGINE_STREAM, IoPanel, PlanSummaryPanel
from app.ui.log_panel import LogPanel
from app.ui.result_panel import RecoveryPanel
from app.ui.scan_panel import CHANNEL_HANDLE, CHANNEL_MFT, ScanControlPanel
from app.ui.target_panel import TargetPanel
from app.ui.title_bar import TitleBar
from app.win32 import bitmap as bitmap_mod
from app.win32 import privilege, storage, volume as volume_mod

THRESHOLD_PRESETS = [
    ("64 MB", 64 * MB),
    ("256 MB", 256 * MB),
    ("1 GB", 1024 * MB),
    ("4 GB", 4 * 1024 * MB),
]

MODE_PREVIEW = 0
MODE_EXECUTE = 1

#: 目标区间拖选后重算可行性的防抖间隔（毫秒）
FEASIBILITY_DEBOUNCE_MS = 140

#: 挡路者清单在磁盘布局图上的最大叠加区间数（超出只画前 N 个，避免卡顿）
MAX_BLOCKER_OVERLAYS = 200


# --------------------------------------------------------------------------- #
# 无边框根容器
# --------------------------------------------------------------------------- #
class RootFrame(QWidget):
    """手绘阴影 + 圆角卡片 + 边缘拖拽缩放。"""

    MARGIN = 14
    RADIUS = 14

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._flush = False
        self.setMouseTracking(True)

    def set_flush(self, flush: bool) -> None:
        """最大化时取消外边距与圆角。"""
        self._flush = flush
        self.update()

    def _card_rect(self) -> QRectF:
        margin = 0.0 if self._flush else float(self.MARGIN)
        return QRectF(self.rect()).adjusted(margin, margin, -margin, -margin)

    def paintEvent(self, event) -> None:  # noqa: D102
        palette = theme.palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        card = self._card_rect()
        radius = 0.0 if self._flush else float(self.RADIUS)

        if not self._flush:
            shadow = QColor(palette["shadow"])
            layers = 12
            for index in range(layers, 0, -1):
                alpha = int(2 + 24 * (index / layers) ** 2)
                shadow.setAlpha(alpha)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(shadow)
                grow = index * 0.75
                painter.drawRoundedRect(
                    card.adjusted(-grow, -grow + 3, grow, grow + 3),
                    radius + grow,
                    radius + grow,
                )

        painter.setBrush(QColor(palette["bg"]))
        painter.setPen(QPen(QColor(palette["border"]), 1.0))
        painter.drawRoundedRect(card, radius, radius)
        painter.end()

    # ---------------- 边缘缩放 ---------------- #
    def _edges_at(self, position: QPoint) -> Qt.Edge:
        edges = Qt.Edge(0)
        if self._flush:
            return edges
        margin = self.MARGIN + 2
        if position.x() <= margin:
            edges |= Qt.Edge.LeftEdge
        if position.x() >= self.width() - margin:
            edges |= Qt.Edge.RightEdge
        if position.y() <= margin:
            edges |= Qt.Edge.TopEdge
        if position.y() >= self.height() - margin:
            edges |= Qt.Edge.BottomEdge
        return edges

    @staticmethod
    def _cursor_for(edges: Qt.Edge) -> Qt.CursorShape:
        left = bool(edges & Qt.Edge.LeftEdge)
        right = bool(edges & Qt.Edge.RightEdge)
        top = bool(edges & Qt.Edge.TopEdge)
        bottom = bool(edges & Qt.Edge.BottomEdge)
        if (left and top) or (right and bottom):
            return Qt.CursorShape.SizeFDiagCursor
        if (right and top) or (left and bottom):
            return Qt.CursorShape.SizeBDiagCursor
        if left or right:
            return Qt.CursorShape.SizeHorCursor
        if top or bottom:
            return Qt.CursorShape.SizeVerCursor
        return Qt.CursorShape.ArrowCursor

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: D102
        self.setCursor(self._cursor_for(self._edges_at(event.position().toPoint())))
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: D102
        if event.button() == Qt.MouseButton.LeftButton:
            edges = self._edges_at(event.position().toPoint())
            if edges:
                handle = self.window().windowHandle()
                if handle is not None and handle.startSystemResize(edges):
                    event.accept()
                    return
        super().mousePressEvent(event)


# --------------------------------------------------------------------------- #
# 主窗口
# --------------------------------------------------------------------------- #
class MainWindow(QMainWindow):
    """LBA Tail Mover 主窗口（v2）。"""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("LBA Tail Mover")
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowMinMaxButtonsHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMinimumSize(1280, 950)
        self.resize(1420, 980)

        # ---------------- 运行时状态 ---------------- #
        self._volumes: List[VolumeInfo] = []
        self._volume: Optional[VolumeInfo] = None
        self._snapshot: Optional[BitmapSnapshot] = None
        self._plan: Optional[MovePlan] = None
        self._plan_stale = True
        self._mode = MODE_PREVIEW
        self._is_admin = False
        self._privilege_ok = False
        self._busy = False
        self._maximized = False
        self._normal_geometry = None

        self._probe = storage.MediaProbe()
        self._disk_map_info = storage.VolumeDiskMap()
        self._ownership = None                # OwnershipIndex
        self._index_payload = None
        self._feasibility: Optional[FeasibilityReport] = None
        self._has_pending = False            # 1.0.0D：是否存在未应用的配置修改
        # 1.0.0D：可行性后台计算的状态
        self._feasibility_worker: Optional[FeasibilityWorker] = None
        self._feasibility_seq = 0
        self._feasibility_signature = None
        self._feasibility_rerun = False

        self._journal: Optional[MoveJournal] = None
        self._journal_path = ""

        self._bitmap_worker: Optional[BitmapWorker] = None
        self._scan_worker = None
        self._index_worker: Optional[IndexWorker] = None
        self._plan_worker: Optional[PlanWorker] = None
        self._move_worker: Optional[MoveWorker] = None
        self._rollback_worker: Optional[RollbackWorker] = None

        self._feasibility_timer = QTimer(self)
        self._feasibility_timer.setSingleShot(True)
        self._feasibility_timer.setInterval(FEASIBILITY_DEBOUNCE_MS)
        self._feasibility_timer.timeout.connect(self._compute_feasibility_now)

        # ---- 1.0.0D 性能：扫描结果批量插入（64 条或 100ms）---- #
        self._scan_buffer: List[FileEntry] = []
        self._scan_flush_timer = QTimer(self)
        self._scan_flush_timer.setSingleShot(True)
        self._scan_flush_timer.setInterval(100)
        self._scan_flush_timer.timeout.connect(self._flush_scan_buffer)
        # ---- 1.0.0D 性能：按钮状态记忆（避免高频 QSS unpolish+polish）---- #
        self._button_state: Optional[tuple] = None

        theme.theme().prepare_fonts()

        self._build_ui()
        self._apply_theme()
        self._install_shortcuts()
        self._probe_privilege()
        QTimer.singleShot(0, self._load_volumes)

    # ================================================================== #
    # 界面构建
    # ================================================================== #
    def _build_ui(self) -> None:
        self._root = RootFrame(self)
        self.setCentralWidget(self._root)

        outer = QVBoxLayout(self._root)
        outer.setContentsMargins(
            RootFrame.MARGIN, RootFrame.MARGIN, RootFrame.MARGIN, RootFrame.MARGIN
        )
        outer.setSpacing(0)

        self._title_bar = TitleBar(self._root)
        self._title_bar.minimizeRequested.connect(self.showMinimized)
        self._title_bar.maximizeRequested.connect(self._toggle_maximize)
        self._title_bar.closeRequested.connect(self.close)
        self._title_bar.themeToggleRequested.connect(self._toggle_theme)
        outer.addWidget(self._title_bar)

        content = QWidget(self._root)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(18, 14, 18, 12)
        content_layout.setSpacing(12)
        outer.addWidget(content, 1)

        content_layout.addWidget(self._build_toolbar())

        splitter = QSplitter(Qt.Orientation.Horizontal, content)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(12)
        splitter.addWidget(self._build_left_column(splitter))
        splitter.addWidget(self._build_right_panel(splitter))
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([900, 430])
        content_layout.addWidget(splitter, 1)

        content_layout.addWidget(self._build_bottom(content))
        content_layout.addWidget(self._build_status_bar(content))

    # ---------------- 工具条 + 扫描控制区 ---------------- #
    def _build_toolbar(self) -> QWidget:
        card = cards.Card(self._root, padding=14, spacing=8)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)

        row.addWidget(cards.IconLabel("drive", 18, "accent"), 0,
                      Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(cards.label("卷", "dim"), 0, Qt.AlignmentFlag.AlignVCenter)

        self._volume_combo = cards.SoftComboBox(card)
        self._volume_combo.setMinimumWidth(320)
        self._volume_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._volume_combo.currentIndexChanged.connect(self._on_volume_changed)
        row.addWidget(self._volume_combo, 3)

        row.addWidget(cards.Divider(vertical=True), 0,
                      Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(cards.label("大文件阈值", "dim"), 0,
                      Qt.AlignmentFlag.AlignVCenter)

        self._threshold_combo = cards.SoftComboBox(card)
        for text, _ in THRESHOLD_PRESETS:
            self._threshold_combo.addItem(text)
        self._threshold_combo.setCurrentIndex(1)
        self._threshold_combo.setFixedWidth(116)
        self._threshold_combo.currentIndexChanged.connect(self._on_threshold_changed)
        row.addWidget(self._threshold_combo, 0)

        self._reload_button = cards.IconButton("refresh", 38, 16, "重新读取卷位图")
        self._reload_button.clicked.connect(self._reload_bitmap)
        row.addWidget(self._reload_button, 0)

        row.addStretch(1)

        self._scan_button = QPushButton("扫描", card)
        self._scan_button.setProperty("role", "primary")
        self._scan_button.setMinimumHeight(40)
        self._scan_button.setMinimumWidth(124)
        self._scan_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._scan_button.clicked.connect(self._start_scan)
        row.addWidget(self._scan_button, 0)

        card.body().addLayout(row)
        card.body().addWidget(cards.Divider())

        self._scan_panel = ScanControlPanel(card)
        self._scan_panel.channelChanged.connect(self._on_channel_changed)
        card.body().addWidget(self._scan_panel)
        self._toolbar_hint = cards.label(
            "扫描只读取文件布局，不写入任何数据；执行迁移前还需二次确认。",
            "caption",
            card,
        )
        card.body().addWidget(self._toolbar_hint)
        return card

    # ---------------- 左列 ---------------- #
    def _build_left_column(self, parent: QWidget) -> QWidget:
        wrapper = QWidget(parent)
        column = QVBoxLayout(wrapper)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(12)

        # --- LBA 布局卡片 ---
        map_card = cards.Card(wrapper, padding=16, spacing=8)
        map_header = QHBoxLayout()
        map_header.setContentsMargins(0, 0, 0, 0)
        map_header.setSpacing(8)
        map_header.addWidget(cards.IconLabel("layers", 18, "accent"), 0,
                             Qt.AlignmentFlag.AlignVCenter)
        map_header.addWidget(cards.label("卷 LBA 布局", "h2"), 0,
                             Qt.AlignmentFlag.AlignVCenter)
        self._map_hint = cards.Badge("在图上拖选可设定目标区间", "neutral", map_card)
        map_header.addWidget(self._map_hint, 0, Qt.AlignmentFlag.AlignVCenter)
        map_header.addStretch(1)
        map_header.addWidget(cards.label("左 → 右：LCN 递增", "caption"), 0,
                             Qt.AlignmentFlag.AlignVCenter)
        map_card.body().addLayout(map_header)

        self._disk_map = DiskMapWidget(map_card)
        self._disk_map.setMinimumHeight(140)
        self._disk_map.windowSelected.connect(self._on_window_selected)
        self._disk_map.lcnHovered.connect(self._on_lcn_hovered)
        map_card.body().addWidget(self._disk_map, 1)
        column.addWidget(map_card, 4)

        # --- 文件列表卡片 ---
        table_card = cards.Card(wrapper, padding=16, spacing=8)
        table_header = QHBoxLayout()
        table_header.setContentsMargins(0, 0, 0, 0)
        table_header.setSpacing(8)
        table_header.addWidget(cards.IconLabel("folder", 18, "accent"), 0,
                               Qt.AlignmentFlag.AlignVCenter)
        table_header.addWidget(cards.label("大文件", "h2"), 0,
                               Qt.AlignmentFlag.AlignVCenter)
        self._count_badge = cards.Badge("0 个", "neutral", table_card)
        table_header.addWidget(self._count_badge, 0, Qt.AlignmentFlag.AlignVCenter)
        table_header.addStretch(1)

        self._select_all_button = QPushButton("全选", table_card)
        self._select_all_button.setProperty("role", "ghost")
        self._select_all_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._select_all_button.clicked.connect(self._select_all)
        table_header.addWidget(self._select_all_button, 0)

        self._invert_button = QPushButton("反选", table_card)
        self._invert_button.setProperty("role", "ghost")
        self._invert_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._invert_button.clicked.connect(self._invert_selection)
        table_header.addWidget(self._invert_button, 0)
        table_card.body().addLayout(table_header)

        self._table = FileTableView(table_card)
        self._table.setMinimumHeight(108)
        self._table.toggled.connect(self._on_selection_changed)
        table_card.body().addWidget(self._table, 1)
        column.addWidget(table_card, 5)
        return wrapper

    # ---------------- 右列 ---------------- #
    def _build_right_panel(self, parent: QWidget) -> QWidget:
        wrapper = QWidget(parent)
        wrapper.setMinimumWidth(392)
        wrapper.setMaximumWidth(468)
        holder = QVBoxLayout(wrapper)
        holder.setContentsMargins(0, 0, 0, 0)
        holder.setSpacing(0)

        scroll = QScrollArea(wrapper)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.viewport().setAutoFillBackground(False)
        holder.addWidget(scroll)

        inner = QWidget(scroll)
        inner.setAutoFillBackground(False)
        column = QVBoxLayout(inner)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(12)

        # --- 迁移摘要 ---
        summary_card = cards.Card(inner, padding=14, spacing=8)
        self._summary = PlanSummaryPanel(summary_card)
        summary_card.body().addWidget(self._summary)
        column.addWidget(summary_card, 0)

        # --- 目标区间 + 可行性 + 挡路者 + 保留区 ---
        target_card = cards.Card(inner, padding=14, spacing=8)
        # 1.0.0D：卡片头部的「应用配置 / 放弃」—— 两个面板共享同一暂存周期
        pending_row = QWidget(target_card)
        pending_layout = QHBoxLayout(pending_row)
        pending_layout.setContentsMargins(0, 0, 0, 0)
        pending_layout.setSpacing(6)
        pending_layout.addWidget(
            cards.label("待定修改", "dim", pending_row), 0, Qt.AlignmentFlag.AlignVCenter
        )
        pending_layout.addStretch(1)
        self._discard_button = QPushButton("放弃", pending_row)
        self._discard_button.setProperty("role", "ghost")
        self._discard_button.setFixedHeight(26)
        self._discard_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._discard_button.clicked.connect(self._discard_pending)
        pending_layout.addWidget(self._discard_button)
        self._apply_button = QPushButton("应用配置", pending_row)
        self._apply_button.setProperty("role", "primary")
        self._apply_button.setFixedHeight(26)
        self._apply_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._apply_button.clicked.connect(self._apply_pending)
        pending_layout.addWidget(self._apply_button)
        pending_row.setVisible(False)
        self._pending_row = pending_row
        target_card.body().addWidget(pending_row)

        self._target_panel = TargetPanel(target_card)
        self._target_panel.windowChanged.connect(self._on_target_changed)
        self._target_panel.strategyChanged.connect(self._on_target_changed)
        self._target_panel.excludeChanged.connect(self._on_exclude_changed)
        self._target_panel.reserveChanged.connect(self._on_reserve_changed)
        self._target_panel.pendingChanged.connect(self._on_pending_changed)
        target_card.body().addWidget(self._target_panel)
        column.addWidget(target_card, 0)

        # --- IO 加速 ---
        io_card = cards.Card(inner, padding=14, spacing=8)
        self._io_panel = IoPanel(io_card)
        self._io_panel.profileChanged.connect(self._on_profile_changed)
        self._io_panel.engineChanged.connect(lambda _engine: self._sync_buttons())
        self._io_panel.pendingChanged.connect(self._on_pending_changed)
        io_card.body().addWidget(self._io_panel)
        column.addWidget(io_card, 0)
        self._stream_worker = None

        # --- 执行与恢复 ---
        result_card = cards.Card(inner, padding=14, spacing=8)
        self._recovery = RecoveryPanel(result_card)
        self._recovery.rollbackRequested.connect(self._start_rollback)
        result_card.body().addWidget(self._recovery)
        column.addWidget(result_card, 0)

        # --- 模式与操作 ---
        # 1.0.0D：常驻待定徽章（在模式卡上方，滚动也看得见）
        self._pending_badge = cards.Badge("● 配置未应用", "warning", inner)
        self._pending_badge.setVisible(False)
        column.addWidget(self._pending_badge, 0)

        action = cards.Card(inner, padding=14, spacing=8)
        action.body().addWidget(
            cards.SectionHeader("模式与操作", "shield", "", action)
        )

        self._mode_switch = cards.SegmentedControl(["预览模式", "执行模式"], 0, action)
        self._mode_switch.changed.connect(self._on_mode_changed)
        action.body().addWidget(self._mode_switch)

        self._mode_hint = cards.label(
            "预览模式：只做扫描、规划与布局对比，全程不写盘。", "caption", action, True
        )
        action.body().addWidget(self._mode_hint)

        self._notice = cards.label("", "warning", action, True)
        self._notice.setVisible(False)
        action.body().addWidget(self._notice)

        self._action_button = QPushButton("生成迁移计划", action)
        self._action_button.setProperty("role", "primary")
        self._action_button.setMinimumHeight(44)
        self._action_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._action_button.clicked.connect(self._on_primary_action)
        action.body().addWidget(self._action_button)

        self._cancel_button = QPushButton("取消当前操作", action)
        self._cancel_button.setProperty("role", "secondary")
        self._cancel_button.setMinimumHeight(36)
        self._cancel_button.setVisible(False)
        self._cancel_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cancel_button.clicked.connect(self._cancel_current)
        action.body().addWidget(self._cancel_button)

        column.addWidget(action, 0)
        column.addStretch(1)
        scroll.setWidget(inner)
        return wrapper

    # ---------------- 右栏：LBA 迁移（分块流式已并入 IoPanel 执行引擎）---------------- #
    def _start_stream_move(self) -> None:
        """分块流式引擎：同卷「前端 → 卷尾」，免规划，按 IoPanel 配置执行。"""
        entries = self._table.file_model.checked_entries()
        if not entries:
            self._set_notice("请先勾选要搬迁的文件", "warning")
            return
        mode = self._io_panel.stream_cache_mode()
        cache_dir = self._io_panel.stream_cache_dir() or None
        # 1.0.0D 方案 D：disk 模式暂存目录可为空 —— 默认暂存在源文件所在卷根
        # （.lba-tmp-scratch，卷根不可写回落 %TEMP%），由 BlockStreamWorker 解析。
        if mode == "disk" and not cache_dir:
            self._log.info("硬盘缓存未指定暂存目录：默认使用源文件所在卷根的 .lba-tmp-scratch")

        info = self._volume
        bpc = (info.bytes_per_cluster if info else 4096) or 4096
        dialog = dialogs.DangerConfirmDialog(
            volume_name="分块流式迁移（前端 → 卷尾，同卷内）",
            files=list(entries),
            total_clusters=sum(e.allocated_clusters for e in entries),
            bytes_per_cluster=bpc,
            parent=self,
        )
        if not dialog.exec():
            return

        self._set_notice("", "warning")
        worker = BlockStreamWorker(
            entries,
            mode=mode,
            cache_dir=cache_dir,
            buffer_blocks=self._io_panel.stream_buffer_blocks(),
            bytes_per_cluster=bpc,
            parent=self,
        )
        worker.progress.connect(self._on_stream_progress)
        worker.file_result.connect(self._on_stream_file_result)
        worker.completed.connect(self._on_stream_completed)
        worker.failed.connect(self._on_stream_failed)
        self._stream_worker = worker
        self._set_busy(True)
        self._progress.set_indeterminate(True)
        self._progress.set_state(0, 1, "分块流式迁移中", "")
        self._log.info(
            f"开始分块流式迁移：{len(entries)} 个文件（前端 → 卷尾，同卷内；"
            f"{mode} 缓存，缓冲 {self._io_panel.stream_buffer_blocks()}× 块）"
        )
        worker.start()

    def _on_stream_progress(self, done: int, total: int, name: str) -> None:
        self._progress.set_state(done, max(1, total), f"搬迁 {name}", f"{done}/{total}")

    def _on_stream_file_result(self, path: str, ok: bool, message: str) -> None:
        if ok:
            self._log.success(f"✓ {path} — {message}")
        else:
            self._log.error(f"✗ {path} — {message}")

    def _on_stream_completed(self, summary: object) -> None:
        self._set_busy(False)
        self._progress.set_indeterminate(False)
        self._progress.reset("分块流式迁移完成")
        self._stream_worker = None
        data = summary if isinstance(summary, dict) else {}
        moved, failed = data.get("moved", 0), data.get("failed", 0)
        self._log.success(
            f"分块流式迁移完成：搬走 {moved} 个（{data.get('clusters', 0):,} 簇）/ "
            f"失败 {failed} 个（源已移回收站，可恢复）"
        )
        self._set_notice(
            f"分块流式迁移完成：搬走 {moved} 个，失败 {failed} 个"
            f"（源已移回收站，可恢复）",
            "warning",
        )
        self._sync_buttons()

    def _on_stream_failed(self, message: str) -> None:
        self._set_busy(False)
        self._progress.set_indeterminate(False)
        self._progress.reset("分块流式迁移失败")
        self._stream_worker = None
        self._log.error(message)
        self._set_notice(message, "danger")
        self._sync_buttons()

    # ---------------- 底部 ---------------- #
    def _build_bottom(self, parent: QWidget) -> QWidget:
        card = cards.Card(parent, padding=14, spacing=8)
        card.setFixedHeight(164)
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self._progress = cards.ProgressBarWidget(card)
        card.body().addWidget(self._progress)

        self._log = LogPanel(card)
        self._log.collapsedChanged.connect(self._on_log_collapsed)
        card.body().addWidget(self._log, 1)
        self._bottom_card = card
        return card

    def _build_status_bar(self, parent: QWidget) -> QWidget:
        bar = QWidget(parent)
        bar.setFixedHeight(24)
        row = QHBoxLayout(bar)
        row.setContentsMargins(6, 0, 6, 0)
        row.setSpacing(10)

        self._admin_badge = cards.Badge("只读模式", "warning", bar)
        row.addWidget(self._admin_badge, 0, Qt.AlignmentFlag.AlignVCenter)

        self._media_badge = cards.Badge("介质：—", "neutral", bar)
        row.addWidget(self._media_badge, 0, Qt.AlignmentFlag.AlignVCenter)

        self._status_labels: List[cards.QLabel] = []
        for index in range(3):
            caption = cards.label("—", "caption", bar)
            row.addWidget(caption, 0, Qt.AlignmentFlag.AlignVCenter)
            self._status_labels.append(caption)
            if index < 2:
                row.addWidget(cards.Divider(vertical=True), 0,
                              Qt.AlignmentFlag.AlignVCenter)

        row.addStretch(1)
        self._hint_label = cards.label(
            "Ctrl+R 扫描 · Ctrl+E 执行 · Esc 取消", "caption", bar
        )
        row.addWidget(self._hint_label, 0, Qt.AlignmentFlag.AlignVCenter)
        return bar

    # ================================================================== #
    # 主题
    # ================================================================== #
    def _apply_theme(self, *_args) -> None:
        manager = theme.theme()
        manager.set_mode(manager.mode, self._root)
        self._title_bar.sync_theme()
        self._log.refresh_theme()
        self._root.update()
        for widget in self.findChildren(QWidget):
            widget.update()

    def _toggle_theme(self) -> None:
        mode = theme.theme().toggle(self._root)
        self._title_bar.sync_theme()
        self._log.refresh_theme()
        for widget in self.findChildren(QWidget):
            widget.update()
        self._log.info(f"已切换到{'深色' if mode == 'dark' else '浅色'}主题")

    # ================================================================== #
    # 快捷键
    # ================================================================== #
    def _install_shortcuts(self) -> None:
        QShortcut(QKeySequence("Ctrl+R"), self, activated=self._start_scan)
        QShortcut(QKeySequence("Ctrl+E"), self, activated=self._on_primary_action)
        QShortcut(QKeySequence("Ctrl+L"), self, activated=self._log.toggle_collapsed)
        # 1.0.0D：Ctrl+Enter = 应用待定配置（干净时无效）
        QShortcut(QKeySequence("Ctrl+Return"), self, activated=self._apply_pending)

    def keyPressEvent(self, event) -> None:  # noqa: D102
        if event.key() == Qt.Key.Key_Escape:
            if self._busy:
                self._cancel_current()
            event.accept()
            return
        super().keyPressEvent(event)

    def _toggle_maximize(self) -> None:
        if self._maximized:
            if self._normal_geometry is not None:
                self.setGeometry(self._normal_geometry)
            self._maximized = False
            self._root.set_flush(False)
        else:
            self._normal_geometry = self.geometry()
            screen = self.screen() or QGuiApplication.primaryScreen()
            if screen is not None:
                self.setGeometry(screen.availableGeometry())
            self._maximized = True
            self._root.set_flush(True)
        self._title_bar.sync_maximized(self._maximized)

    # ================================================================== #
    # 权限
    # ================================================================== #
    def _probe_privilege(self) -> None:
        self._is_admin = privilege.is_admin()
        self._privilege_ok = (
            privilege.enable_manage_volume_privilege() if self._is_admin else False
        )
        if self._is_admin and self._privilege_ok:
            self._admin_badge.set_state("管理员 · 已提权", "success")
        elif self._is_admin:
            self._admin_badge.set_state("管理员 · 提权失败", "danger")
        else:
            self._admin_badge.set_state("只读模式", "warning")
        self._log.log(
            "INFO" if self._privilege_ok else "WARN", privilege.privilege_report()
        )
        self._refresh_notice()

    # ================================================================== #
    # 卷加载
    # ================================================================== #
    def _load_volumes(self) -> None:
        self._volumes = []
        self._volume_combo.blockSignals(True)
        self._volume_combo.clear()
        try:
            self._volumes = volume_mod.list_volumes()
        except Exception as exc:  # noqa: BLE001 - 枚举失败要能在界面上看到
            self._log.error(f"枚举卷失败：{type(exc).__name__}: {exc}")
            self._volumes = []

        for info in self._volumes:
            self._volume_combo.addItem(info.display_name)
        self._volume_combo.blockSignals(False)

        if not self._volumes:
            self._log.error("未找到可用的本地固定卷")
            self._table.set_empty_state("没有可用卷", "未检测到本地固定磁盘", "warning")
            self._sync_buttons()
            return

        self._log.success(f"已枚举 {len(self._volumes)} 个本地固定卷")
        self._volume_combo.setCurrentIndex(0)
        self._on_volume_changed(0)

    def _current_volume(self) -> Optional[VolumeInfo]:
        index = self._volume_combo.currentIndex()
        if 0 <= index < len(self._volumes):
            return self._volumes[index]
        return None

    def _on_volume_changed(self, _index: int) -> None:
        info = self._current_volume()
        if info is None:
            return
        self._volume = info
        self._plan = None
        self._plan_stale = True
        self._snapshot = None
        self._ownership = None
        self._index_payload = None
        self._feasibility = None

        self._table.file_model.clear()
        self._table.set_empty_state(
            "尚未扫描", "点击右上角「扫描」分析该卷上的大文件", "folder"
        )
        self._disk_map.clear()
        self._disk_map.set_selectable(False)
        self._disk_map.set_mft_zone(info.mft_zone_start, info.mft_zone_end)
        self._scan_panel.clear_stats()
        self._summary.clear()
        self._summary.set_bytes_per_cluster(info.bytes_per_cluster)
        self._target_panel.set_volume(info.total_clusters, info.bytes_per_cluster)
        self._target_panel.set_reserve_defaults(
            default_head_mb(info.bytes_per_cluster),
            default_tail_mb(info.bytes_per_cluster),
        )
        self._target_panel.set_feasibility(None)
        self._target_panel.set_blockers([])
        self._recovery.clear()
        self._recovery.set_cluster_size(info.bytes_per_cluster)
        self._recovery.set_journal("", 0)
        self._journal = None
        self._journal_path = ""

        # 介质与磁盘映射（两者都不抛异常）
        self._probe, self._disk_map_info = storage.probe_volume_storage(info.letter)
        self._io_panel.set_volume(
            self._probe, info.bytes_per_cluster, self._window_clusters()
        )
        self._target_panel.set_partition_note(self._disk_map_info.first_offset)
        self._update_media_badge()
        self._log.info(f"介质：{self._probe.describe()}")

        self._update_status_labels()
        self._update_summary()
        self._log.info(
            f"选择卷 {info.letter}: ({info.filesystem or '未知'}) "
            f"总容量 {human_bytes(info.total_bytes)}，"
            f"簇大小 {info.cluster_text}，总簇数 {info.total_clusters:,}"
        )
        if info.volume_error:
            self._log.warn(f"无法读取卷结构信息：{info.volume_error}")
            self._set_notice(
                "无法读取卷结构（需要管理员权限）。当前只能查看基础容量信息，"
                "请以管理员身份重新启动后再扫描与规划。",
                "danger",
            )
        elif not info.is_ntfs:
            self._log.warn("该卷不是 NTFS，位图与 retrieval pointers 可能不可用")
        self._refresh_target_visual()
        self._reload_bitmap()

    def _update_media_badge(self) -> None:
        media = self._probe.media
        text = {"hdd": "介质：HDD", "ssd": "介质：SSD"}.get(
            media, "介质：未知（保守）"
        )
        kind = {"hdd": "success", "ssd": "accent"}.get(media, "warning")
        self._media_badge.set_state(text, kind)
        self._media_badge.setToolTip(self._probe.describe())

    def _update_status_labels(self) -> None:
        info = self._volume
        if info is None:
            for item in self._status_labels:
                item.setText("—")
            return
        clusters_text = f"{info.total_clusters:,}" if info.total_clusters else "—"
        texts = [
            f"簇大小 {info.cluster_text}",
            f"总簇数 {clusters_text}",
            f"可用 {human_bytes(info.free_bytes)}",
        ]
        for item, text in zip(self._status_labels, texts):
            item.setText(text)

    # ================================================================== #
    # 位图
    # ================================================================== #
    def _reload_bitmap(self) -> None:
        info = self._volume
        if info is None:
            return
        if info.total_clusters <= 0:
            self._progress.set_indeterminate(False)
            self._progress.reset("卷布局不可用")
            self._disk_map.clear()
            self._disk_map.set_selectable(False)
            self._sync_buttons()
            return
        if self._bitmap_worker is not None and self._bitmap_worker.isRunning():
            return

        self._log.info(f"正在读取 {info.letter}: 的卷位图（{info.total_clusters:,} 簇）")
        self._progress.set_indeterminate(True)
        self._progress.set_state(0, 1, "正在读取卷位图", "")

        worker = BitmapWorker(info, self)
        worker.progress.connect(self._on_bitmap_progress)
        worker.ready.connect(self._on_bitmap_ready)
        worker.failed.connect(self._on_bitmap_failed)
        self._bitmap_worker = worker
        worker.start()

    def _on_bitmap_progress(self, done: int, total: int) -> None:
        self._progress.set_state(
            done, max(1, total), "正在读取卷位图", f"{done:,} / {total:,} 簇"
        )

    def _on_bitmap_ready(self, snapshot: BitmapSnapshot) -> None:
        if not isinstance(snapshot, BitmapSnapshot):
            return
        self._snapshot = snapshot
        self._progress.set_indeterminate(False)
        info = self._volume
        if info is not None:
            snapshot.bytes_per_cluster = info.bytes_per_cluster
            self._disk_map.set_volume(
                info.total_clusters, info.bytes_per_cluster, snapshot.bits
            )
            self._disk_map.set_mft_zone(info.mft_zone_start, info.mft_zone_end)
            self._disk_map.set_selectable(True)
        self._refresh_target_visual()
        self._progress.set_state(
            1, 1, "卷布局已就绪", f"位图 {snapshot.byte_length:,} 字节"
        )
        self._plan_stale = True
        self._log.success("卷位图读取完成")
        self._sync_buttons()

    def _on_bitmap_failed(self, message: str) -> None:
        self._progress.set_indeterminate(False)
        self._progress.reset("卷位图读取失败")
        self._log.error(message)
        self._set_notice(message, "danger")
        self._table.set_empty_state("布局读取失败", message, "warning")
        self._sync_buttons()

    # ================================================================== #
    # 扫描
    # ================================================================== #
    def _threshold_bytes(self) -> int:
        index = self._threshold_combo.currentIndex()
        if 0 <= index < len(THRESHOLD_PRESETS):
            return THRESHOLD_PRESETS[index][1]
        return 256 * MB

    def _on_threshold_changed(self, _index: int) -> None:
        if self._table.file_model.rowCount() > 0:
            self._set_notice("阈值已改变，重新扫描后生效", "warning")

    def _on_channel_changed(self, channel: str) -> None:
        self._plan_stale = True
        if channel == CHANNEL_MFT:
            self._scan_panel.clear_fallback()
            self._log.info("扫描通道切换为：MFT 快速扫描")
        else:
            self._scan_panel.set_manual_handle_note()
            self._log.warn("扫描通道切换为：句柄扫描（挡路者归属信息将不可用）")
        self._sync_buttons()

    def _start_scan(self) -> None:
        info = self._volume
        if info is None or self._busy:
            return
        if self._scan_worker is not None and self._scan_worker.isRunning():
            return

        threshold = self._threshold_bytes()
        channel = self._scan_panel.channel()
        self._log.info(
            f"开始扫描 {info.letter}:，{self._scan_panel.describe_channel()}，"
            f"阈值 {human_bytes(threshold)}（仅读取布局）"
        )
        self._table.file_model.clear()
        self._scan_buffer.clear()
        self._scan_flush_timer.stop()
        self._table.set_scanning_state("正在扫描卷上的大文件…")
        self._scan_panel.clear_stats()
        self._progress.set_indeterminate(True)
        self._progress.set_state(0, 1, "正在扫描", "")
        self._set_busy(True)
        self._ownership = None
        self._index_payload = None
        self._target_panel.set_blockers([])

        config = scan_config_for(info, threshold)
        if channel == CHANNEL_MFT:
            worker = MftScanWorker(info, config, self, sample_size=32)
            worker.stats.connect(self._on_mft_stats)
            worker.fallback.connect(self._on_mft_fallback)
            worker.failed.connect(self._on_mft_failed)
        else:
            worker = ScanWorker(config, self, info=info, mode="handle")
            worker.failed.connect(self._on_scan_failed)
        worker.progress.connect(self._on_scan_progress)
        worker.found.connect(self._on_scan_found)
        worker.completed.connect(self._on_scan_completed)
        self._scan_worker = worker
        worker.start()

    def _on_mft_stats(self, stats: object) -> None:
        matched = self._table.file_model.rowCount()
        self._scan_panel.set_stats(stats, matched)

    def _on_mft_fallback(self, code: str) -> None:
        """MFT 通道可恢复地失败 → 显式告警 + 自动重启句柄通道（D5 留痕）。"""
        self._scan_panel.set_fallback(code)
        self._log.warn(f"MFT 通道已回退：{code}")
        self._set_notice(
            "MFT 通道不可用，已自动回退句柄通道（挡路者自动驱逐将不可用）",
            "warning",
        )
        self._scan_worker = None
        info = self._volume
        if info is None:
            self._set_busy(False)
            return
        config = scan_config_for(info, self._threshold_bytes())
        worker = ScanWorker(config, self, info=info, mode="handle")
        worker.progress.connect(self._on_scan_progress)
        worker.found.connect(self._on_scan_found)
        worker.completed.connect(self._on_scan_completed)
        worker.failed.connect(self._on_scan_failed)
        self._scan_worker = worker
        worker.start()

    def _on_mft_failed(self, message: str) -> None:
        """MFT 通道真失败（不可恢复）：不反复重试。"""
        self._on_scan_failed(message)

    def _on_scan_progress(self, scanned: int, matched: int, current: str) -> None:
        self._progress.set_state(
            scanned, max(1, scanned + 1), f"已处理 {scanned:,} 项", current
        )

    #: 扫描结果批量插入的缓冲条数（配合 100ms 定时器）
    SCAN_FLUSH_COUNT = 64

    def _on_scan_found(self, entry: FileEntry) -> None:
        if not isinstance(entry, FileEntry):
            return
        # 1.0.0D 性能：逐条 append 会让模型发 N 次 rowsInserted 且每次触发视图
        # 重绘；改为缓冲 64 条或 100ms，一次批量插入。
        self._scan_buffer.append(entry)
        if len(self._scan_buffer) >= self.SCAN_FLUSH_COUNT:
            self._flush_scan_buffer()
        else:
            self._scan_flush_timer.start()

    def _flush_scan_buffer(self) -> None:
        """把扫描缓冲里的条目一次性插进表格模型。"""
        self._scan_flush_timer.stop()
        if not self._scan_buffer:
            return
        items, self._scan_buffer = self._scan_buffer, []
        self._table.file_model.append_entries(items)
        self._count_badge.set_state(
            f"{self._table.file_model.rowCount()} 个", "accent"
        )

    def _on_scan_completed(self, entries: object) -> None:
        self._flush_scan_buffer()
        items: List[FileEntry] = list(entries) if isinstance(entries, list) else []
        self._table.file_model.set_entries(items)
        self._progress.set_indeterminate(False)
        self._scan_worker = None

        if not items:
            self._table.set_empty_state(
                "没有符合条件的文件",
                f"当前阈值 {human_bytes(self._threshold_bytes())}，"
                "可以尝试调低阈值后重新扫描",
                "folder",
            )
            self._progress.reset("扫描完成，未发现大文件")
            self._log.warn("未发现超过阈值的大文件")
        else:
            self._table.set_ready_state()
            movable = sum(1 for item in items if item.movable)
            total = sum(item.size for item in items)
            self._progress.set_state(
                1, 1, "扫描完成", f"{len(items)} 个文件 / {human_bytes(total)}"
            )
            self._log.success(
                f"扫描完成：命中 {len(items)} 个文件（可移动 {movable} 个），"
                f"合计 {human_bytes(total)}"
            )
            self._table.file_model.set_checked_rows(True)
            self._annotate_front_half(items)

        if self._scan_panel.channel() == CHANNEL_HANDLE:
            # 句柄通道没有 MFT 健康度，统计条只报能报的
            self._scan_panel.set_handle_stats(len(items), 0, 0.0)

        self._count_badge.set_state(f"{len(items)} 个", "accent")
        self._on_selection_changed()
        self._build_index()

    def _annotate_front_half(self, items: List[FileEntry]) -> None:
        """卷前半大文件扫描（1.0.0C）：把占前部的大文件标注为「前半·候选」。只读。"""
        info = self._volume
        if info is None or not items:
            return
        try:
            report = densify_mod.front_half_report(items, info.total_clusters)
        except Exception as exc:  # noqa: BLE001
            self._log.warn(f"前半大文件扫描失败：{exc}")
            return
        if not report.files:
            return
        for candidate in report.files:
            self._table.file_model.update_status(candidate.entry.path, "前半·候选")
        self._log.info(
            f"卷前半大文件扫描：{len(report.files)} 个候选，占前半 "
            f"{report.front_occupied:,} 簇（可移动 {report.movable_front:,} 簇），"
            f"分界 LCN {report.boundary_lcn:,} —— 已标注「前半·候选」，"
            "可切到「自动填满末端」策略搬运以腾出前部连续空间"
        )

    def _on_scan_failed(self, message: str) -> None:
        self._flush_scan_buffer()
        self._progress.set_indeterminate(False)
        self._progress.reset("扫描失败")
        self._table.set_empty_state("扫描失败", message, "warning")
        self._log.error(message)
        self._set_notice(message, "danger")
        self._set_busy(False)
        self._scan_worker = None
        self._sync_buttons()

    # ================================================================== #
    # 归属索引（挡路者归属 / 可行性必需）
    # ================================================================== #
    def _build_index(self) -> None:
        info = self._volume
        if info is None:
            return
        if self._index_worker is not None and self._index_worker.isRunning():
            return
        channel = "mft" if self._scan_panel.channel() == CHANNEL_MFT else "handle"
        self._log.info(f"正在建立全卷归属索引（通道 {channel}）…")
        self._progress.set_indeterminate(True)
        self._progress.set_state(0, 1, "正在建立归属索引", "")

        worker = IndexWorker(info, None, self, channel=channel, sample_size=32)
        worker.progress.connect(self._on_index_progress)
        worker.ready.connect(self._on_index_ready)
        worker.failed.connect(self._on_index_failed)
        self._index_worker = worker
        worker.start()

    def _on_index_progress(self, done: int, total: int, current: str) -> None:
        self._progress.set_state(
            done, max(1, total), "正在建立归属索引", current
        )

    def _on_index_ready(self, payload: object) -> None:
        self._index_worker = None
        self._progress.set_indeterminate(False)
        self._set_busy(False)
        self._index_payload = payload
        self._ownership = getattr(payload, "index", None)
        channel = getattr(payload, "channel", "handle")
        files = getattr(payload, "total_files", 0)
        extents = getattr(payload, "total_extents", 0)
        elapsed = getattr(payload, "elapsed", 0.0)
        if channel == "handle":
            self._scan_panel.set_handle_stats(files, extents, elapsed)
            self._log.warn(
                f"归属索引使用句柄通道（{files:,} 个文件 / {extents:,} 个 extent，"
                f"{elapsed:.1f} s）：挡路者自动驱逐不可用"
            )
            self._target_panel.set_blockers_unavailable(
                "归属信息不可用：句柄通道只能逐文件读 retrieval pointers，"
                "无法把「某个簇」反查到所属文件，因此无法判断谁挡路。"
                "如需自动驱逐，请改用 MFT 快速扫描并以管理员身份运行。"
            )
        else:
            self._log.success(
                f"归属索引就绪（MFT 通道，{files:,} 个文件 / {extents:,} 个 extent，"
                f"{elapsed:.1f} s）"
            )
        for note in getattr(payload, "notes", []) or []:
            self._log.info(note)
        self._plan_stale = True
        self._schedule_feasibility()
        self._sync_buttons()

    def _on_index_failed(self, message: str) -> None:
        self._index_worker = None
        self._progress.set_indeterminate(False)
        self._set_busy(False)
        self._ownership = None
        self._log.warn(f"归属索引建立失败：{message}")
        self._target_panel.set_blockers_unavailable(
            f"归属信息不可用：{message}。挡路者自动驱逐不可用，"
            "仍可手工调整目标区间或取消勾选文件。"
        )
        self._schedule_feasibility()
        self._sync_buttons()

    # ================================================================== #
    # 选择 / 摘要
    # ================================================================== #
    def _select_all(self) -> None:
        self._table.file_model.set_checked_rows(True)
        self._on_selection_changed()

    def _invert_selection(self) -> None:
        self._table.file_model.invert_checks()
        self._on_selection_changed()

    def _on_selection_changed(self) -> None:
        # 1.0.0D 性能：`checked_entries()` 只算一次向下传递
        # （原先在本方法 / _update_summary / _refresh_disk_preview 里各算一遍）
        selected = self._table.file_model.checked_entries()
        self._count_badge.set_state(
            f"{self._table.file_model.rowCount()} 个 · 选 {len(selected)}",
            "accent" if selected else "neutral",
        )
        self._plan_stale = True
        self._update_summary(selected)
        self._refresh_disk_preview(selected)
        self._schedule_feasibility()
        self._sync_buttons()

    def _update_summary(self, selected: Optional[List[FileEntry]] = None) -> None:
        if selected is None:
            selected = self._table.file_model.checked_entries()
        total_bytes = sum(e.size for e in selected)
        self._summary.set_selection(len(selected), total_bytes)
        if self._plan is not None and not self._plan_stale:
            self._summary.set_stats(self._plan.stats, self._plan.note)
        else:
            self._summary.clear()
            self._summary.set_selection(len(selected), total_bytes)

    def _window_clusters(self) -> int:
        start, end = self._target_panel.window() if hasattr(self, "_target_panel") else (0, 0)
        return max(0, end - start)

    # ================================================================== #
    # 布局图联动
    # ================================================================== #
    def _refresh_disk_preview(
        self, selected: Optional[List[FileEntry]] = None
    ) -> None:
        if selected is None:
            selected = self._table.file_model.checked_entries()
        sources = []
        file_ranges = []
        for entry in selected:
            for extent in entry.allocated_extents:
                sources.append((extent.lcn, extent.lcn_end))
                file_ranges.append((extent.lcn, extent.lcn_end, entry.name))
        targets = (
            self._plan.target_ranges
            if (self._plan is not None and not self._plan_stale)
            else []
        )
        self._disk_map.set_plan_ranges(sources, targets, file_ranges)

    def _refresh_target_visual(self) -> None:
        """把目标区间 / 保留区 / 挡路者推给布局图。"""
        start, end = self._target_panel.window()
        self._disk_map.set_target_window(start, end)
        info = self._volume
        if info is not None:
            tail_start = max(
                0, info.total_clusters - self._target_panel.reserve_tail_clusters()
            )
            self._disk_map.set_reserve_zones(
                self._target_panel.reserve_head_clusters(), tail_start
            )
        self._io_panel.set_window_clusters(max(0, end - start))

    def _on_window_selected(self, start: int, end: int) -> None:
        """布局图拖选 → 目标区间面板（直通路径：立即提交生效，不走暂存）。"""
        if self._busy:
            return
        self._target_panel.set_window_committed(start, end)
        self._refresh_target_visual()
        self._on_target_changed()

    def _on_lcn_hovered(self, lcn: int) -> None:
        info = self._volume
        if info is None or info.bytes_per_cluster <= 0:
            return
        self._map_hint.set_state(
            f"LCN {lcn:,} · 偏移 {human_bytes(lcn * info.bytes_per_cluster)}",
            "neutral",
        )

    # ================================================================== #
    # 目标区间 / 可行性
    # ================================================================== #
    def _on_target_changed(self) -> None:
        self._plan_stale = True
        self._refresh_target_visual()
        self._schedule_feasibility()
        self._sync_buttons()

    def _on_reserve_changed(self, _head: int, _tail: int) -> None:
        self._plan_stale = True
        self._refresh_target_visual()
        self._schedule_feasibility()
        self._sync_buttons()

    def _on_exclude_changed(self, _excluded: object) -> None:
        """排除挡路者后**立即**重算（要求 4：排除致不可行要马上回显）。"""
        self._plan_stale = True
        self._compute_feasibility_now()
        self._sync_buttons()

    def _on_profile_changed(self) -> None:
        self._plan_stale = True
        self._log.info(f"IO 策略：{self._io_panel.profile().summary}")
        self._sync_buttons()

    # ================================================================== #
    # 配置暂存（1.0.0D）：应用 / 放弃
    # ================================================================== #
    def _on_pending_changed(self, dirty: bool, count: int) -> None:
        """两个面板共享同一暂存周期：任一 dirty 就整体进入待定态。"""
        has_pending = bool(dirty) or self._target_panel.is_pending() or self._io_panel.is_pending()
        self._has_pending = has_pending
        self._pending_row.setVisible(has_pending)
        self._pending_badge.setVisible(has_pending)
        if has_pending:
            total = self._target_panel.pending_count() + self._io_panel.pending_count()
            self._apply_button.setText(f"应用配置（{max(1, total)} 项变更）")
            self._summary.set_pending_note("配置已修改，当前计划基于应用前的参数")
        else:
            self._apply_button.setText("应用配置")
            self._summary.set_pending_note("")
        self._sync_buttons()

    def _apply_pending(self) -> None:
        """提交两个面板的待定修改（一次性发出各自的既有语义信号）。"""
        if not self._has_pending:
            return
        self._log.info("应用待定配置：目标区间 / IO 设置已提交生效")
        self._target_panel.commit_pending()
        self._io_panel.commit_pending()
        # pendingChanged(False, 0) 已在面板内发出 → _on_pending_changed 收尾

    def _discard_pending(self) -> None:
        """把控件恢复到已提交状态（不发语义信号，计划保持有效）。"""
        if not self._has_pending:
            return
        self._log.info("放弃待定配置：控件已恢复为应用前的值")
        self._target_panel.discard_pending()
        self._io_panel.discard_pending()

    def _schedule_feasibility(self) -> None:
        self._feasibility_timer.start()

    def _policy(self):
        info = self._volume
        if info is None:
            return None
        head_mb, tail_mb = self._target_panel.reserve_mb()
        return build_policy(info, head_mb=head_mb, tail_mb=tail_mb)

    def _compute_feasibility_now(self) -> None:
        """把可行性评估**投递到后台线程**（1.0.0D：UI 线程只打包参数与回显）。

        * 参数打包成不可变输入（位图 ``bytes`` 快照 + 区间 + 选中清单）；
        * 结果按 ``seq`` 代次回显：返回时参数已变则丢弃（最新参数由重投覆盖）；
        * 若上一个 worker 还在跑，置「待重算」标记，它结束后立即按最新参数重跑；
        * 防抖（140 ms）仍由 ``_feasibility_timer`` 承担。
        """
        info = self._volume
        snapshot = self._snapshot
        if info is None or snapshot is None or not snapshot.bits:
            self._clear_feasibility()
            return
        start, end = self._target_panel.window()
        if end <= start:
            self._clear_feasibility()
            return
        policy = self._policy()
        if policy is None:
            return

        selected = self._table.file_model.checked_entries()
        excluded = tuple(sorted(self._target_panel.excluded_paths()))
        signature = (
            id(snapshot),
            start,
            end,
            excluded,
            tuple((entry.path, entry.allocated_clusters) for entry in selected),
        )
        if signature == self._feasibility_signature and self._feasibility is not None:
            return                      # 参数没变，不白算

        if self._feasibility_worker is not None and self._feasibility_worker.isRunning():
            # 上一轮还在跑：标记重算，它结束后按最新参数重投
            self._feasibility_rerun = True
            self._feasibility_worker.request_cancel()
            return

        self._feasibility_signature = signature
        self._feasibility_seq += 1
        worker = FeasibilityWorker(
            self._feasibility_seq,
            snapshot.bits,
            (start, end),
            ranges_mod.subtract_runs([(start, end)], policy.forbidden_runs()),
            selected,
            self._ownership,
            excluded,
            info.bytes_per_cluster or 4096,
            parent=self,
        )
        worker.completed.connect(self._on_feasibility_ready)
        worker.failed.connect(self._on_feasibility_failed)
        worker.finished.connect(self._on_feasibility_finished)
        self._feasibility_worker = worker
        worker.start()

    def _clear_feasibility(self) -> None:
        self._feasibility = None
        self._target_panel.set_feasibility(None)
        self._target_panel.set_blockers([])
        self._disk_map.set_blocker_ranges([])

    def _on_feasibility_ready(self, seq: int, report: object) -> None:
        if seq != self._feasibility_seq:
            return                      # 过期结果：参数已变，丢弃
        self._feasibility = report
        self._target_panel.set_feasibility(report)
        self._target_panel.set_blockers(report.blockers)
        self._disk_map.set_blocker_ranges(
            [
                (extent.lcn, extent.lcn_end)
                for blocker in report.blockers
                for extent in blocker.extents_in_window
            ][:MAX_BLOCKER_OVERLAYS]
        )
        self._sync_buttons()

    def _on_feasibility_failed(self, seq: int, message: str) -> None:
        if seq != self._feasibility_seq:
            return
        self._log.error(message)
        self._clear_feasibility()
        self._sync_buttons()

    def _on_feasibility_finished(self) -> None:
        worker = self._feasibility_worker
        if worker is not None and worker.isFinished():
            self._feasibility_worker = None
        if self._feasibility_rerun:
            self._feasibility_rerun = False
            QTimer.singleShot(0, self._compute_feasibility_now)

    # ================================================================== #
    # 模式 / 按钮状态
    # ================================================================== #
    def _on_mode_changed(self, index: int) -> None:
        self._mode = MODE_EXECUTE if index == MODE_EXECUTE else MODE_PREVIEW
        if self._mode == MODE_EXECUTE:
            self._mode_hint.setText(
                "执行模式：确认后将调用 FSCTL_MOVE_FILE 真实移动磁盘簇。"
            )
            self._title_bar.set_mode_badge("执行模式", "danger")
        else:
            self._mode_hint.setText(
                "预览模式：只做扫描、规划与布局对比，全程不写盘。"
            )
            self._title_bar.set_mode_badge("预览模式", "accent")
        self._refresh_notice()
        self._sync_buttons()

    def _set_notice(self, text: str, kind: str = "warning") -> None:
        if not text:
            self._notice.setVisible(False)
            return
        self._notice.setText(text)
        self._notice.setVisible(True)
        cards.set_role(self._notice, kind)

    def _refresh_notice(self) -> None:
        if self._mode == MODE_EXECUTE and not self._is_admin:
            self._set_notice(
                "当前不是管理员：可以扫描与规划，但无法执行真实迁移。"
                "请右键 run.bat 选择「以管理员身份运行」后重试。",
                "danger",
            )
        elif self._mode == MODE_EXECUTE and not self._privilege_ok:
            self._set_notice(
                "SE_MANAGE_VOLUME_NAME 提权失败，FSCTL_MOVE_FILE 会被拒绝。",
                "danger",
            )
        else:
            self._notice.setVisible(False)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._cancel_button.setVisible(busy)
        self._volume_combo.setEnabled(not busy)
        self._threshold_combo.setEnabled(not busy)
        self._reload_button.setEnabled(not busy)
        self._scan_panel.set_busy(busy)
        self._target_panel.set_busy(busy)
        self._io_panel.set_busy(busy)
        # 1.0.0D：应用 / 放弃按钮随忙碌状态一并管理
        self._apply_button.setEnabled(not busy and self._has_pending)
        self._discard_button.setEnabled(not busy and self._has_pending)
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        has_volume = self._volume is not None
        selected = self._table.file_model.checked_count()
        plan_ready = self._plan is not None and not self._plan_stale and bool(
            self._plan.tasks or self._plan.movable_items
        )
        feasible = self._feasibility is not None and self._feasibility.ok
        window_ok = self._target_panel.has_window()

        self._scan_button.setEnabled(has_volume and not self._busy)
        self._scan_button.setText(
            "扫描中…" if self._busy and self._scan_worker else "扫描"
        )
        self._select_all_button.setEnabled(not self._busy and has_volume)
        self._invert_button.setEnabled(not self._busy and has_volume)

        if self._io_panel.engine() == ENGINE_STREAM:
            # 分块流式引擎：免规划，选中文件即可执行（用户态，不要求卷特权）
            self._apply_action_button(
                ("开始分块流式迁移", "danger"),
                selected > 0 and self._snapshot is not None and not self._busy,
            )
            return

        if self._mode == MODE_PREVIEW:
            desired = ("生成迁移计划", "primary")
            enabled = (
                selected > 0
                and self._snapshot is not None
                and window_ok
                and not self._busy
            )
        else:
            if plan_ready:
                desired = ("开始迁移", "danger")
                enabled = self._is_admin and self._privilege_ok and not self._busy
            else:
                desired = ("生成迁移计划", "primary")
                enabled = (
                    selected > 0
                    and self._snapshot is not None
                    and window_ok
                    and not self._busy
                    and feasible
                )
        self._apply_action_button(desired, enabled)

    def _apply_action_button(self, desired: tuple, enabled: bool) -> None:
        """只在 (文案, 角色, 可用态) 真变化时触碰主按钮。

        ``cards.set_role`` 内部是 unpolish + polish（QSS 重解析），高频调用
        会白白烧 CPU；勾选框变化 / 悬停重算都会走到 ``_sync_buttons``，
        这里记忆上次状态，三者全等时直接跳过。
        """
        state = (desired[0], desired[1], enabled, self._has_pending)
        if state == self._button_state:
            return
        self._button_state = state
        self._action_button.setText(desired[0])
        cards.set_role(self._action_button, desired[1])
        self._action_button.setEnabled(enabled)

    # ================================================================== #
    # 主操作
    # ================================================================== #
    def _on_primary_action(self) -> None:
        if self._busy:
            return
        if self._io_panel.engine() == ENGINE_STREAM:
            # 分块流式引擎：不走规划流程，直接按选中文件执行
            self._start_stream_move()
            return
        if self._mode == MODE_EXECUTE and self._plan is not None and not self._plan_stale:
            self._start_move()
            return
        self._generate_plan()

    def _generate_plan(self) -> None:
        info = self._volume
        snapshot = self._snapshot
        selected = self._table.file_model.checked_entries()
        if info is None or snapshot is None:
            self._set_notice("请先等待卷位图加载完成", "warning")
            return
        if not selected:
            self._set_notice("请先勾选需要迁移的文件", "warning")
            return
        strategy = self._target_panel.strategy()
        start, end = self._target_panel.window()
        needs_window = strategy in ("range", "compact", "zone")
        if needs_window:
            if end <= start:
                self._set_notice("请先设定目标区间（起点必须小于终点）", "warning")
                return
            if self._feasibility is not None and not self._feasibility.ok:
                self._set_notice(
                    "当前目标区间不可行：" + "；".join(self._feasibility.reasons[:2]),
                    "danger",
                )
                return

        policy = self._policy()
        profile = self._io_panel.profile()
        if needs_window:
            self._log.info(
                f"开始规划（策略 {strategy}）：{len(selected)} 个文件，目标 LCN "
                f"{start:,} – {max(start, end - 1):,}，IO {profile.summary}"
            )
        else:
            self._log.info(
                f"开始规划（策略 {strategy}）：{len(selected)} 个文件，卷尾保留区 "
                f"{self._target_panel.reserve_tail_clusters():,} 簇，IO {profile.summary}"
            )
        self._progress.set_indeterminate(True)
        self._progress.set_state(0, 1, "正在计算迁移计划", "")
        self._set_busy(True)

        worker = PlanWorker(
            info,
            snapshot,
            selected,
            self._target_panel.reserve_tail_clusters(),
            self,
            window=TargetWindow(start, end) if needs_window else None,
            safety=policy,
            io_profile=profile,
            ownership=self._ownership,
            excluded_paths=tuple(sorted(self._target_panel.excluded_paths())),
            mode=strategy,
        )
        worker.completed.connect(self._on_plan_ready)
        worker.failed.connect(self._on_plan_failed)
        self._plan_worker = worker
        worker.start()

    def _on_plan_ready(self, plan: object) -> None:
        self._progress.set_indeterminate(False)
        self._set_busy(False)
        self._plan_worker = None
        if not isinstance(plan, MovePlan):
            self._set_notice("规划返回了异常结果", "danger")
            return

        self._plan = plan
        self._plan_stale = False
        self._progress.set_state(1, 1, "计划已生成", plan.note)

        for item in plan.all_items:
            self._table.file_model.update_status(item.entry.path, "待迁移")
        for failure in plan.failures:
            self._table.file_model.update_status(failure.entry.path, "不可安排")

        if plan.feasibility is not None:
            self._target_panel.set_feasibility(plan.feasibility)
            self._target_panel.set_blockers(plan.feasibility.blockers)
            self._feasibility = plan.feasibility

        self._refresh_disk_preview()
        self._refresh_target_visual()
        self._update_summary()
        self._sync_buttons()

        if plan.empty:
            self._log.warn(f"计划为空：{plan.note}")
            self._set_notice(plan.note or "没有可迁移的文件", "warning")
        else:
            stats = plan.stats
            self._log.success(
                f"计划生成：装入 {stats.place_files} 个 / {stats.place_clusters:,} 簇，"
                f"驱逐 {stats.evict_files} 个 / {stats.evict_clusters:,} 簇，"
                f"中转 {stats.relay_clusters:,} 簇，"
                f"合计需搬运 {plan.total_move_clusters:,} 簇（{len(plan.tasks)} 个任务）"
            )
            self._set_notice("", "warning")
        if plan.failures:
            self._log.warn(
                f"{len(plan.failures)} 个文件无法安排：{describe_failures(plan.failures)}"
            )

    def _on_plan_failed(self, message: str) -> None:
        self._progress.set_indeterminate(False)
        self._progress.reset("规划失败")
        self._set_busy(False)
        self._plan_worker = None
        self._log.error(message)
        self._set_notice(message, "danger")
        self._sync_buttons()

    # ================================================================== #
    # 真实移动
    # ================================================================== #
    def _start_move(self) -> None:
        info = self._volume
        snapshot = self._snapshot
        plan = self._plan
        if info is None or snapshot is None or plan is None:
            return
        # ---- 1.0.0D：存在未应用配置时，先让用户明确选择 ----
        # 「先应用再执行」= 提交配置 → 计划失效 → 重新生成（不许把不一致的
        # 参数静默带进写盘流程）；「按现有计划执行」= 明确认知后继续。
        if self._has_pending:
            dialog = dialogs.ChoiceDialog(
                "配置未应用",
                "目标区间 / IO 配置有未应用的修改。当前计划基于修改前的参数。",
                choices=[
                    ("先应用再执行", "primary"),
                    ("按现有计划执行", "danger"),
                    ("取消", "secondary"),
                ],
                kind="warn",
                parent=self,
            )
            if not dialog.exec():
                return
            if dialog.choice == "apply":
                self._apply_pending()
                # commit 会发出 windowChanged / profileChanged → 计划失效
                self._set_notice(
                    "配置已应用：当前计划已失效，请重新生成迁移计划后再执行。",
                    "warning",
                )
                self._log.warn("执行前应用了待定配置，计划已失效，需重新生成")
                return
            if dialog.choice != "proceed":
                return
            self._log.warn("用户选择「按现有计划执行」：忽略未应用的配置修改")
        if not self._is_admin or not self._privilege_ok:
            self._set_notice(
                "缺少管理员权限或 SE_MANAGE_VOLUME_NAME 特权，无法执行真实迁移。",
                "danger",
            )
            self._log.error("执行被拒绝：权限不足")
            return
        if plan.empty:
            self._set_notice("当前计划没有可迁移的文件", "warning")
            return

        files = [item.entry for item in plan.all_items if item.entry.path]
        root = (info.root or f"{info.letter}:\\").rstrip("\\").lower() + "\\"
        foreign = [
            item.entry
            for item in plan.all_items
            if item.entry.path and not item.entry.path.lower().startswith(root)
        ]
        if foreign:
            self._log.error(
                f"{len(foreign)} 个文件不在当前卷 {info.letter}: 上，已阻止执行："
                + "、".join(item.path for item in foreign[:3])
            )
            self._set_notice(
                "源文件与目标卷不一致，已阻止执行（只允许同卷内移动）", "danger"
            )
            return

        start, end = self._target_panel.window()
        blockers = 0
        evict_clusters = 0
        if plan.feasibility is not None:
            blockers = len([b for b in plan.feasibility.blockers if b.movable])
            evict_clusters = plan.feasibility.evictable_clusters

        dialog = dialogs.DangerConfirmDialog(
            volume_name=f"{info.letter}: ({info.filesystem or '未知'})",
            files=files,
            total_clusters=plan.total_move_clusters,
            bytes_per_cluster=info.bytes_per_cluster,
            target_range=(start, end),
            parent=self,
            blocker_count=blockers,
            evict_clusters=evict_clusters,
            planned_tasks=len(plan.tasks),
        )
        if dialog.exec() != dialogs.QDialog.DialogCode.Accepted:
            self._log.warn("用户取消了真实迁移")
            return

        on_error = dialog.on_error()
        journal = MoveJournal(default_journal_path(info.letter))
        self._journal = journal
        self._journal_path = str(journal.path)
        self._log.info(f"流水文件：{self._journal_path}")

        profile = plan.io_profile if isinstance(plan.io_profile, IoProfile) else self._io_panel.profile()
        self._log.warn(
            f"开始真实迁移：{len(plan.tasks)} 个任务 / "
            f"{plan.total_move_clusters:,} 簇，on_error={on_error}"
        )
        self._set_busy(True)
        self._progress.set_indeterminate(True)
        self._progress.set_state(
            0, max(1, plan.total_move_clusters), "正在移动簇", ""
        )

        worker = MoveWorker(
            info,
            snapshot,
            plan,
            self,
            safety=plan.safety if plan.safety is not None else self._policy(),
            io_profile=profile,
            journal=journal,
            on_error=on_error,
        )
        worker.progress.connect(self._on_move_progress)
        worker.file_result.connect(self._on_file_result)
        worker.completed.connect(self._on_move_completed)
        worker.failed.connect(self._on_move_failed)
        self._move_worker = worker
        worker.start()

    def _on_move_progress(self, done: int, total: int, current: str) -> None:
        self._progress.set_state(done, max(1, total), "正在移动簇", current)

    def _on_file_result(self, path: str, success: bool, message: str) -> None:
        if success:
            self._table.file_model.update_status(path, "已迁移")
            self._log.success(f"{path} —— {message}")
        else:
            self._table.file_model.update_status(path, "不可安排")
            self._log.error(f"{path} —— {message}")

    def _on_move_completed(self, outcome: object) -> None:
        self._progress.set_indeterminate(False)
        self._set_busy(False)
        self._move_worker = None
        if not isinstance(outcome, MoveOutcome):
            self._log.error("移动线程返回了异常结果")
            return

        planned = len(self._plan.tasks) if self._plan is not None else 0
        self._recovery.set_outcome(outcome, planned)
        if self._journal is not None:
            self._journal.close()
            rollback_clusters = self._journal.rollback_clusters()
            self._recovery.set_journal(self._journal_path, rollback_clusters)
        self._recovery.mark_executed()

        if outcome.cancelled:
            self._log.warn(
                f"迁移被取消：已移动 {outcome.moved_clusters:,} 簇 / "
                f"{outcome.moved_files} 个文件"
            )
        else:
            self._log.success(
                f"迁移结束：移动 {outcome.moved_clusters:,} 簇 / "
                f"{outcome.moved_files} 个文件"
            )
        for failure in outcome.failures:
            self._log.error(
                f"失败：{failure.path} 源 LCN {failure.src_lcn} → 目标 LCN "
                f"{failure.dst_lcn}，{failure.clusters} 簇，"
                f"错误码 {failure.winerror}（{failure.message}）"
            )

        self._progress.set_state(
            outcome.moved_clusters,
            max(1, self._plan.total_move_clusters if self._plan else 1),
            "迁移完成" if not outcome.cancelled else "迁移已取消",
            f"成功 {outcome.moved_files} 个文件，失败 {len(outcome.failures)} 段",
        )
        if outcome.failures:
            self._set_notice(
                f"{len(outcome.failures)} 段移动失败，详情见「执行与恢复」与操作日志",
                "danger",
            )
        self._plan_stale = True
        self._reload_bitmap()
        self._sync_buttons()

    def _on_move_failed(self, message: str) -> None:
        self._progress.set_indeterminate(False)
        self._progress.reset("迁移失败")
        self._set_busy(False)
        self._move_worker = None
        self._log.error(message)
        self._set_notice(message, "danger")
        self._sync_buttons()

    # ================================================================== #
    # 手动回滚
    # ================================================================== #
    def _start_rollback(self) -> None:
        info = self._volume
        if info is None:
            return
        if self._journal is None or not self._recovery.has_journal():
            self._set_notice("本次会话没有可回滚的流水", "warning")
            return
        segments = self._journal.rollback_segments()
        if not segments:
            self._set_notice("流水里没有成功写入的段，无需回滚", "warning")
            return

        clusters = sum(segment.clusters for _path, segment in segments)
        dialog = dialogs.DangerConfirmDialog(
            volume_name=f"{info.letter}: ({info.filesystem or '未知'})",
            files=[],
            total_clusters=clusters,
            bytes_per_cluster=info.bytes_per_cluster,
            target_range=(0, 0),
            parent=self,
            planned_tasks=len(segments),
        )
        if dialog.exec() != dialogs.QDialog.DialogCode.Accepted:
            self._log.warn("用户取消了回滚")
            return

        self._log.warn(
            f"开始回滚：{len(segments)} 段 / {clusters:,} 簇（逆序、src↔dst 互换）"
        )
        self._set_busy(True)
        self._recovery.set_rollback_busy(True, "正在回滚…")
        self._progress.set_indeterminate(True)
        self._progress.set_state(0, max(1, clusters), "正在回滚", "")

        worker = RollbackWorker(
            info, segments, self, policy=self._policy(), journal=self._journal
        )
        worker.progress.connect(self._on_rollback_progress)
        worker.file_result.connect(self._on_rollback_file_result)
        worker.completed.connect(self._on_rollback_completed)
        worker.failed.connect(self._on_rollback_failed)
        self._rollback_worker = worker
        worker.start()

    def _on_rollback_progress(self, done: int, total: int, current: str) -> None:
        self._progress.set_state(done, max(1, total), "正在回滚", current)

    def _on_rollback_file_result(self, path: str, success: bool, message: str) -> None:
        if success:
            self._table.file_model.update_status(path, "已回滚")
            self._log.success(f"{path} —— {message}")
        else:
            self._log.error(f"{path} —— {message}")

    def _on_rollback_completed(self, outcome: object) -> None:
        self._progress.set_indeterminate(False)
        self._set_busy(False)
        self._rollback_worker = None
        self._recovery.set_rollback_busy(False)
        if not isinstance(outcome, MoveOutcome):
            self._log.error("回滚线程返回了异常结果")
            return

        self._log.success(
            f"回滚结束：{outcome.moved_clusters:,} 簇 / {outcome.moved_files} 个文件"
        )
        for failure in outcome.failures:
            self._log.error(
                f"回滚失败：{failure.path} 错误码 {failure.winerror}"
                f"（{failure.message}）"
            )
        self._progress.set_state(
            1, 1, "回滚完成", f"成功 {outcome.moved_files} 个，失败 {len(outcome.failures)} 段"
        )
        # 回滚后流水已消费完毕：不允许再次回滚（否则会来回搬）
        self._recovery.disable_rollback(
            "已回滚过一次。再次回滚会把数据搬回目标位置，不是撤销操作，故已禁用。"
        )
        self._journal = None
        self._journal_path = ""
        self._plan_stale = True
        self._reload_bitmap()
        self._sync_buttons()

    def _on_rollback_failed(self, message: str) -> None:
        self._progress.set_indeterminate(False)
        self._progress.reset("回滚失败")
        self._set_busy(False)
        self._rollback_worker = None
        self._recovery.set_rollback_busy(False)
        self._log.error(message)
        self._set_notice(message, "danger")
        self._sync_buttons()

    # ================================================================== #
    # 取消 / 日志
    # ================================================================== #
    def _cancel_current(self) -> None:
        cancelled = False
        for worker in (
            self._move_worker,
            self._rollback_worker,
            self._scan_worker,
            self._index_worker,
            self._plan_worker,
        ):
            if worker is not None and worker.isRunning():
                worker.request_cancel()
                cancelled = True
        if self._bitmap_worker is not None and self._bitmap_worker.isRunning():
            self._bitmap_worker.request_cancel()
            cancelled = True
        if cancelled:
            self._log.warn("已请求取消当前操作，将在安全点退出")
        else:
            self._log.info("当前没有正在执行的操作")

    def _on_log_collapsed(self, collapsed: bool) -> None:
        self._hint_label.setText(
            "Ctrl+R 扫描 · Ctrl+E 执行 · Esc 取消 · Ctrl+L 日志"
        )
        if hasattr(self, "_bottom_card"):
            self._bottom_card.setFixedHeight(100 if collapsed else 164)

    # ================================================================== #
    # 关闭
    # ================================================================== #
    def closeEvent(self, event) -> None:  # noqa: D102
        for worker in (
            self._move_worker,
            self._rollback_worker,
            self._scan_worker,
            self._index_worker,
            self._plan_worker,
            self._bitmap_worker,
            self._feasibility_worker,
        ):
            if worker is not None and worker.isRunning():
                worker.request_cancel()
                worker.wait(2000)
        if self._journal is not None:
            self._journal.close()
        super().closeEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: D102
        super().resizeEvent(event)
        self._root.update()
