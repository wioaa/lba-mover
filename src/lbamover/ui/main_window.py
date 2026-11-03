"""主窗口：信息架构的落地。

布局按「决策顺序」而不是「技术分层」组织：

    目标卷 → 策略 → 参数 → 预演 → 执行 → 观察（分布图 / 进度 / 日志）

左栏只放"做决定需要的输入"，中央放"结果与状态"，底部固定放"动作"。
用户从上到下走一遍就能完成任务，不需要在标签页之间来回找。

响应式：窗口变窄时左栏自动收起，中央内容靠流式布局换行，而不是把控件
压到文字被截断。
"""

from __future__ import annotations

import sys
import traceback
from dataclasses import replace

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .. import VERSION_DISPLAY
from ..contract.commands import (
    CancelJob,
    PauseJob,
    Replan,
    ResumeJob,
    SetParams,
    StartJob,
    UpdateTarget,
)
from ..contract.enums import Intensity, JobState, LogLevel, Strategy, VolumeKind
from ..contract.models import FragReport, JobParams, JobSpec, Plan, Progress, VolumeInfo
from ..devices.base import DeviceError
from ..devices.memory import MemoryDevice
from ..engine.catalog import StaticCatalog, UnsupportedCatalog, open_ntfs_catalog as _open_ntfs_catalog
from ..engine.engine import EngineCore, EngineService
from ..engine.synth import apply_plan, build_layout, populate, verify
from ..platform.volumes import enumerate_volumes
from ..settings import AppSettings, SettingsStore
from .bridge import EngineBridge
from .format import fmt_bytes, fmt_count, fmt_percent
from .theme import DARK, LIGHT, Palette, apply_theme
from .widgets.cards import Badge, Card, MetricTile, hint_label
from .widgets.control_bar import ControlBar
from .widgets.flow import FlowContainer
from .widgets.frag_map import FragMap
from .widgets.log_view import LogView
from .widgets.params_panel import ParamsPanel
from .widgets.file_select import FileSelectDialog
from .widgets.progress_card import ProgressCard
from .widgets.rate_chart import RateChart
from .widgets.scope_panel import ScopePanel
from .widgets.strategy_panel import INTENSITY_HELP, STRATEGY_HELP, StrategyPanel
from .widgets.volume_picker import VolumePicker

__all__ = ["MainWindow"]


def confirmed(ret) -> bool:
    """``QMessageBox.question()`` / ``QDialog.exec()`` 的返回值判「是」。

    **不能写 ``ret is QMessageBox.StandardButton.Yes``。** PySide6（实测 6.12）
    的静态方法 ``question()`` 返回的是**普通 int**（``16384``），不是
    ``StandardButton`` 枚举成员，身份比较恒为 False —— 表现就是"点了『是』，
    界面什么也没发生"，而且不报错、不进日志。

    这一处写错曾同时造成两个"功能坏了"的假象：提权对话框永远被当成取消
    （点了提权没反应），写盘确认永远被当成取消（点开始整理没反应）。
    比数值，不比身份。
    """
    try:
        return QMessageBox.StandardButton(ret) == QMessageBox.StandardButton.Yes
    except (TypeError, ValueError):  # 非枚举/非整数的意外返回值：一律当"否"
        return False


DEMO_DEVICE_PATH = "memory://lbamover-demo"
DEMO_BLOCK_BUDGET = 8192  # 32 MiB @ 4 KiB，足够排出十几段碎片

#: 配置保存的防抖间隔（毫秒）。
#:
#: 拖一次并发数的滑块会连发十几个 ``valueChanged``，每次都落盘既慢又毫无
#: 意义。800ms 足够短 —— 进程被强杀也只丢最近一次改动；也足够长 ——
#: 一次拖动只在松手后写一次。关窗时另有强制落盘（见 ``closeEvent``）。
SAVE_DEBOUNCE_MS = 800


class MainWindow(QMainWindow):
    def __init__(self, palette: Palette = DARK, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._p = palette
        self.setWindowTitle(f"LBAMover {VERSION_DISPLAY} — LBA 块级流式整理")
        self.setMinimumSize(840, 600)
        # 1000 是"所有卡片都不必滚动"的最小高度：940 时左栏装上「整理范围」
        # 卡之后差 53px（多出滚动条），实测 1000 归零。1080p 屏（工作区约
        # 1040）放得下；更矮的屏按设计出滚动条。
        self.resize(1360, 1000)

        # -- 状态 --
        self._volumes: list[VolumeInfo] = []
        self._volume: VolumeInfo | None = None
        self._strategy = Strategy.COALESCE
        self._params = JobParams()
        self._report: FragReport | None = None
        self._plan: Plan | None = None
        self._left_user_hidden = False
        self._demo_device: MemoryDevice | None = None
        self._demo_layout = None
        self._demo_volume: VolumeInfo | None = None
        # 手动选择：``_selection`` 存 file_id，不存行号 —— 行号随筛选/排序变化
        self._manual_scope = False
        self._selection: tuple[str, ...] = ()
        self._sel_blocks = 0
        self._sel_stale = 0
        self._id_index: dict[str, object] | None = None

        # -- 配置持久化 --
        # 保存时机有三处，缺一不可：
        #   1. 改动后 800ms 防抖（_schedule_save）—— 崩了也只丢最后一次改动；
        #   2. 关窗时强制落盘（closeEvent）—— 正常退出一定写成功；
        #   3. 不做"只在退出时写"—— 那样崩一次就回到默认值，正是要修的问题。
        self._store = SettingsStore()
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(SAVE_DEBOUNCE_MS)
        self._save_timer.timeout.connect(self._save_settings)
        #: 恢复配置期间不触发保存：否则恢复动作本身会被当成"用户改了设置"
        self._restoring = False
        #: 保存失败的说明只提示一次，避免每 800ms 刷一行
        self._save_error = ""
        #: 上次的目标卷 + 待恢复的"按卷换算"参数（reclaim/pack 需要块大小）
        self._pending_volume_path = ""
        self._restore_spans = False

        # -- 引擎 --
        self.bridge = EngineBridge(self)
        # 真机卷走 NTFS 文件表解析（每次扫描临时打开、用完即关）；
        # 演示卷在 load_demo 里改成 StaticCatalog。
        self.core = EngineCore(
            sink=self.bridge,
            catalog=UnsupportedCatalog(),
            catalog_factory=_open_ntfs_catalog,
        )
        self.service = EngineService(self.core, device_provider=self._provide_device)

        self._build_ui()
        self._wire()
        self._restore_settings()
        self.service.start()
        QTimer.singleShot(0, self._load_volumes)
        QTimer.singleShot(120, self._probe_capabilities)

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(18, 14, 18, 14)
        outer.setSpacing(12)

        outer.addWidget(self._build_header())

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(10)
        self.left_panel = self._build_left()
        self.splitter.addWidget(self.left_panel)
        self.splitter.addWidget(self._build_center())
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([320, 940])
        outer.addWidget(self.splitter, 1)

        self.control = ControlBar(self._p)
        outer.addWidget(self.control)

    def _build_header(self) -> QWidget:
        bar = QWidget()
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(2, 0, 2, 0)
        lay.setSpacing(10)

        brand = QLabel("LBAMover")
        brand.setObjectName("brand")
        ver = QLabel(f"v{VERSION_DISPLAY}")
        ver.setObjectName("badge")
        self.badge_engine = QLabel("引擎：未探测")
        self.badge_engine.setObjectName("badge")
        self.badge_perm = Badge("权限：未知")
        self.badge_perm.clicked.connect(self._elevate)

        lay.addWidget(brand)
        lay.addWidget(ver)
        lay.addWidget(self.badge_engine)
        lay.addWidget(self.badge_perm)
        lay.addStretch(1)

        self.btn_left = QPushButton("☰  目标与参数")
        self.btn_left.setObjectName("ghost")
        self.btn_left.setCheckable(True)
        self.btn_left.setChecked(True)
        self.btn_left.clicked.connect(self._toggle_left)

        self.btn_demo = QPushButton("演示模式")
        self.btn_demo.setObjectName("ghost")
        self.btn_demo.setToolTip("用一个内存合成卷跑完整流程（不接触真实磁盘）")
        self.btn_demo.clicked.connect(self.load_demo)

        lay.addWidget(self.btn_left)
        lay.addWidget(self.btn_demo)
        return bar

    def _build_left(self) -> QWidget:
        holder = QWidget()
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(0, 0, 6, 0)
        lay.setSpacing(10)

        card_vol = Card("目标卷", "本机卷")
        self.picker = VolumePicker(self._p)
        # 卷多时列表自己滚，别把下面的参数卡挤到视口外
        self.picker.setMaximumHeight(182)
        card_vol.add(self.picker, 1)
        card_vol.setMinimumHeight(206)

        card_scope = Card("整理范围", "整卷 / 手动")
        self.scope_panel = ScopePanel(self._p)
        card_scope.add(self.scope_panel)

        card_strategy = Card("整理策略", "你要解决哪种碎？")
        self.strategy_panel = StrategyPanel(self._p)
        card_strategy.add(self.strategy_panel)

        card_params = Card("运行参数", "内存上界实时可见")
        self.params_panel = ParamsPanel(self._p)
        card_params.add(self.params_panel)

        risk = hint_label("⚠ 裸写已挂载卷会破坏文件系统")
        risk.setStyleSheet(f"color: {self._p.warn}; font-size: 11px;")

        lay.addWidget(card_vol, 1)
        lay.addWidget(card_scope)
        lay.addWidget(card_strategy)
        lay.addWidget(card_params)
        lay.addWidget(risk)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(inner)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMinimumWidth(300)
        scroll.setMaximumWidth(420)

        wrap = QVBoxLayout(holder)
        wrap.setContentsMargins(0, 0, 0, 0)
        wrap.addWidget(scroll)
        return holder

    def _build_center(self) -> QWidget:
        vsplit = QSplitter(Qt.Orientation.Vertical)
        vsplit.setChildrenCollapsible(False)
        vsplit.setHandleWidth(10)

        top = QWidget()
        top_lay = QVBoxLayout(top)
        top_lay.setContentsMargins(0, 0, 0, 0)
        top_lay.setSpacing(12)

        # 碎片分布
        card_map = Card("碎片分布", "")
        self.frag_map = FragMap(self._p)
        card_map.add(self.frag_map, 1)

        self.metrics = FlowContainer(spacing=22)
        self.m_file_frag = MetricTile("文件碎片度")
        self.m_free_frag = MetricTile("自由空间碎片度")
        self.m_frag_files = MetricTile("碎片文件")
        self.m_largest_free = MetricTile("最大连续空闲")
        self.m_files = MetricTile("已扫描文件")
        for tile in (
            self.m_file_frag,
            self.m_free_frag,
            self.m_frag_files,
            self.m_largest_free,
            self.m_files,
        ):
            self.metrics.add(tile)
        card_map.add(self.metrics)

        self.worst_label = QLabel("—")
        self.worst_label.setWordWrap(True)
        self.worst_label.setTextFormat(Qt.TextFormat.RichText)
        self.worst_label.setStyleSheet(f"color: {self._p.text_dim}; font-size: 12px;")
        card_map.add(self.worst_label)

        # 进度
        card_prog = Card("任务进度", "")
        self._progress_card = card_prog
        self.progress = ProgressCard(self._p)
        card_prog.add(self.progress)
        self.rate_chart = RateChart(self._p)
        card_prog.add(self.rate_chart)
        # 预演摘要放标题右侧：它是"这次要动多少"的一句话，不值得占一整行
        self.preview = card_prog.hint_label
        self.preview.setText("尚未预演")

        top_lay.addWidget(card_map)
        top_lay.addWidget(card_prog)
        top_lay.addStretch(1)

        # 中央区可滚动：窗口再矮也只是出现滚动条，而不是把卡片压到互相重叠
        top_scroll = QScrollArea()
        top_scroll.setWidgetResizable(True)
        top_scroll.setFrameShape(QFrame.Shape.NoFrame)
        top_scroll.setWidget(top)
        top_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._center_scroll = top_scroll

        self.log_view = LogView(self._p)
        log_card = Card("", "")
        log_card.add(self.log_view, 1)
        log_card.setMinimumHeight(140)

        vsplit.addWidget(top_scroll)
        vsplit.addWidget(log_card)
        vsplit.setSizes([700, 150])
        return vsplit

    # ------------------------------------------------------------------
    # 信号
    # ------------------------------------------------------------------

    def _wire(self) -> None:
        self.picker.volumeSelected.connect(self._on_volume_selected)
        self.picker.refreshRequested.connect(self._load_volumes)
        self.strategy_panel.strategyChanged.connect(self._on_strategy)
        self.strategy_panel.intensityChanged.connect(self._on_intensity)
        self.strategy_panel.extremeMinChanged.connect(self._on_extreme_min)
        self.strategy_panel.reclaimChanged.connect(self._on_reclaim_changed)
        self.strategy_panel.packChanged.connect(self._on_pack_changed)
        self.scope_panel.scopeChanged.connect(self._on_scope_changed)
        self.scope_panel.pickRequested.connect(self._open_file_picker)
        self.scope_panel.clearRequested.connect(self._clear_selection)
        self.params_panel.paramsChanged.connect(self._on_params)

        self.control.scanRequested.connect(self._on_scan)
        self.control.startRequested.connect(self._on_start)
        self.control.pauseRequested.connect(lambda: self.service.submit(PauseJob()))
        self.control.resumeRequested.connect(lambda: self.service.submit(ResumeJob()))
        self.control.stopRequested.connect(self._on_stop)

        self.bridge.stateChanged.connect(self._on_state)
        self.bridge.progressChanged.connect(self._on_progress)
        self.bridge.logRecord.connect(self.log_view.append)
        self.bridge.fragReportReady.connect(self._on_report)
        self.bridge.planReady.connect(self._on_plan)
        self.bridge.jobFinished.connect(self._on_job_finished)
        self.log_view.diagnoseRequested.connect(self._copy_diagnostic)

        # 配置持久化的触发点：任何"用户改了一个设置"的信号都走同一条防抖。
        # 逐个信号连（而不是重写控件），是因为控件的 setChecked/setValue 也
        # 会发同样的信号 —— 那样恢复配置的过程会反过来触发保存。
        for signal in (
            self.strategy_panel.strategyChanged,
            self.strategy_panel.intensityChanged,
            self.strategy_panel.extremeMinChanged,
            self.strategy_panel.reclaimChanged,
            self.strategy_panel.packChanged,
            self.params_panel.paramsChanged,
            self.scope_panel.scopeChanged,
            self.picker.volumeSelected,
        ):
            signal.connect(self._schedule_save)

    # ------------------------------------------------------------------
    # 目标与参数
    # ------------------------------------------------------------------

    def _load_volumes(self) -> None:
        try:
            self._volumes = enumerate_volumes()
        except OSError as exc:
            self._volumes = []
            self.log_view.append(LogLevel.ERROR, f"枚举卷失败：{exc}")
        extra = [self._demo_volume] if self._demo_volume is not None else []
        all_volumes = self._volumes + extra
        # 重建列表时必须保住当前选择：启动时的延迟加载曾把用户刚选的
        # 演示卷换回 C:，然后对着真盘跑演示数据，属于最危险的一类错。
        #
        # 以 ``self._volume``（引擎真正的目标）为准，而不是只看 picker 的
        # 选中项：程序化切换目标（例如从命令行/自动化驱动）不会经过点击，
        # picker 的高亮还停在旧项上，用它做依据就会把目标换回第一块盘 ——
        # 界面显示的盘和实际要动的盘不一致，是宁可崩也不能出现的情况。
        anchor = self._volume or self.picker.current()
        preselect = None
        if anchor is None and self._pending_volume_path:
            # 启动时优先回到上次的卷。它可能已经不在了（拔了移动硬盘），
            # 那就退回第一块盘并说一声，而不是静默换目标 —— 静默换目标
            # 意味着用户以为在整理 D:，实际动的是 C:。
            for i, v in enumerate(all_volumes):
                if v.device_path == self._pending_volume_path:
                    preselect = i
                    break
            if preselect is None:
                self.log_view.append(
                    LogLevel.WARNING,
                    f"上次的目标卷 {self._pending_volume_path} 不在本机，已改为第一个可用卷",
                )
        if anchor is not None:
            for i, v in enumerate(all_volumes):
                if v.device_path == anchor.device_path:
                    preselect = i
                    break
        if preselect is None and anchor is not None:
            self.log_view.append(
                LogLevel.WARNING, f"目标卷 {anchor.mount_point} 已不可用，请重新选择"
            )
        self.picker.set_volumes(all_volumes, preselect=preselect)
        self.log_view.append(
            LogLevel.INFO,
            f"发现 {len(self._volumes)} 个可整理卷" if self._volumes else "未发现可整理的本地卷",
            "网络映射盘与光驱不参与 LBA 级整理",
        )

    def _on_volume_selected(self, vol: VolumeInfo | None) -> None:
        self._volume = vol
        self.control.set_target_available(vol is not None)
        if vol is None:
            self.params_panel.set_cache_allowed(True)
            return
        self.params_panel.set_block_size(vol.geometry.block_size)
        # 已挂载的 NTFS 卷不能用缓存引擎（裸写会覆盖 $MFT 指着的簇区）。
        # 在**选卷时**就把这两个选项关掉，而不是等按下「开始整理」才抛
        # "必须先锁定卷" —— 那句话既没说明原因，也没说明该改成什么。
        self.params_panel.set_cache_allowed(
            not (vol.filesystem.upper() == "NTFS" and vol.is_mounted),
            "这块盘是已挂载的 NTFS 卷：缓存与内存缓存都要裸写块设备，"
            "会绕过文件系统损坏卷，因此不可用。整理它请用默认的盘内搬簇"
            "（FSCTL_MOVE_FILE）。缓存方式只适用于卷镜像与内存盘。",
        )
        # 「可用空间整理」的大小上限与单位换算都跟着卷走，换卷必须重新校准
        self.strategy_panel.reclaim.set_geometry(
            vol.geometry.block_size, vol.geometry.block_count
        )
        self.strategy_panel.pack.set_geometry(
            vol.geometry.block_size, vol.geometry.block_count
        )
        if self._restore_spans:
            # 恢复配置里那两个"以块为单位"的界面值。必须在 set_geometry 之后：
            # 它们会被卷容量夹紧，夹紧本身也要反映到 _params 里（走 _merge_reclaim）。
            self._restoring = True
            try:
                self.strategy_panel.reclaim.set_values(
                    self._params.reclaim_blocks, self._params.reclaim_at
                )
                self.strategy_panel.pack.set_blocks(self._params.pack_span_blocks)
            finally:
                self._restoring = False
                self._restore_spans = False
        params = self.params_panel.current()
        if params is not None:
            self._params = self._merge_reclaim(params)
        # 演示卷用内存目录；真实卷每次扫描时临时打开 MFT
        self.core.catalog_factory = None if vol.device_path == DEMO_DEVICE_PATH else _open_ntfs_catalog
        self.core.set_target(vol, self._params)
        # 选择是按卷的：换了盘，"上次选的 12 个文件"就不再有意义
        self._selection = ()
        self._id_index = None
        self.scope_panel.set_available(False)
        self._refresh_scope_summary()
        self.frag_map.set_report(None)
        self.progress.reset()
        self.rate_chart.clear()
        self.preview.setText("尚未预演")
        for tile in (self.m_file_frag, self.m_free_frag, self.m_frag_files, self.m_largest_free, self.m_files):
            tile.set_value("—")
        self.worst_label.setText("—")
        self.log_view.append(
            LogLevel.INFO,
            f"目标：{vol.mount_point} · {vol.filesystem or '未知'} · "
            f"{fmt_bytes(vol.geometry.total_bytes)}，可用 {fmt_bytes(vol.free_bytes)}",
            f"块大小 {vol.geometry.block_size} B，共 {fmt_count(vol.geometry.block_count)} 块",
        )

    def _on_strategy(self, strategy: Strategy) -> None:
        self._strategy = strategy
        if strategy is Strategy.RECLAIM:
            # 切到这一档时必须立刻有一个有效的大小：0 在引擎里等于"没设置"，
            # 会直接判不可行，用户会以为功能坏了。
            panel = self.strategy_panel.reclaim
            self._params = replace(
                self._params, reclaim_blocks=panel.blocks(), reclaim_at=panel.side()
            )
        elif strategy is Strategy.PACK_TAIL:
            # 压实的 0 是"整卷"，语义合法，但仍要同步一次面板值 —— 面板可能
            # 还停在上一个卷的范围上。
            self._params = replace(
                self._params, pack_span_blocks=self.strategy_panel.pack.blocks()
            )
        self.log_view.append(LogLevel.INFO, f"策略：{STRATEGY_HELP.get(strategy, strategy.value)}")

    # -- 整理范围（手动选择文件） -----------------------------------------

    def _scope_file_ids(self) -> tuple[str, ...]:
        """当前生效的 file_id 列表。空元组 = 整卷 —— 这条语义来自引擎，别改。"""
        return self._selection if self._manual_scope else ()

    def _on_scope_changed(self, manual: bool) -> None:
        self._manual_scope = manual
        self.log_view.append(
            LogLevel.INFO,
            "范围：手动选择文件（只处理勾选的文件）"
            if manual
            else "范围：整卷（按策略自动挑选文件）",
        )
        self._refresh_scope_summary()
        # 切回整卷时立刻把预览恢复成整卷口径，否则界面上还挂着上一次
        # "只整理 12 个文件"的预演数字，与实际要跑的东西不符。
        if self._report is not None:
            self.service.submit(Replan(self._strategy, self._scope_file_ids()))

    def _ensure_id_index(self) -> dict[str, object]:
        """``file_id → FileFrag``。只建一次，换卷/重扫时失效。

        百万文件建一遍约 0.2~0.4s，所以**不主动建**：只有真的用手动模式
        算统计时才建。整卷模式下这段成本不该被所有人付。
        """
        if self._id_index is None:
            files = self._report.files if self._report is not None else ()
            self._id_index = {f.file_id: f for f in files}
        return self._id_index

    def _refresh_scope_summary(self) -> None:
        self._sel_blocks = 0
        self._sel_stale = 0
        if self._selection and self._report is not None:
            index = self._ensure_id_index()
            blocks = 0
            stale = 0
            for fid in self._selection:
                frag = index.get(fid)
                if frag is None:
                    stale += 1
                else:
                    blocks += frag.total_blocks
            self._sel_blocks = blocks
            self._sel_stale = stale
        block_size = self._volume.geometry.block_size if self._volume is not None else 4096
        self.scope_panel.set_selection(
            len(self._selection), self._sel_blocks, block_size, self._sel_stale
        )
        # 手动模式下一个文件都没选时不许开始：否则 file_ids 为空会被引擎
        # 理解成"整卷"，用户以为只动 12 个文件，实际会动几万个。
        scope_ok = not (self._manual_scope and not self._selection)
        self.control.set_scope_ok(scope_ok)

    def _open_file_picker(self) -> None:
        if self._report is None:
            self.scope_panel.set_available(False)
            self.log_view.append(LogLevel.WARNING, "先扫描一次，再从中挑选文件")
            return
        vol = self._volume
        block_size = vol.geometry.block_size if vol is not None else 4096
        dlg = FileSelectDialog(
            self._report.files,
            block_size,
            self._p,
            preselect=self._selection,
            parent=self,
        )
        if dlg.exec() != int(QDialog.DialogCode.Accepted):
            return
        self._selection = dlg.selection_ids()
        self._refresh_scope_summary()
        if not self._selection:
            self.log_view.append(LogLevel.INFO, "已清空文件选择")
            return
        self.log_view.append(
            LogLevel.INFO,
            f"已选 {len(self._selection)} 个文件，"
            f"共 {fmt_count(self._sel_blocks)} 块 / "
            f"{fmt_bytes(self._sel_blocks * block_size)}",
        )
        self.service.submit(Replan(self._strategy, self._scope_file_ids()))

    def _clear_selection(self) -> None:
        if not self._selection:
            return
        self._selection = ()
        self._refresh_scope_summary()
        self.log_view.append(LogLevel.INFO, "已清空文件选择")
        if self._report is not None:
            self.service.submit(Replan(self._strategy, self._scope_file_ids()))

    def _merge_reclaim(self, params: JobParams) -> JobParams:
        """把「可用空间整理」参数与「强度档位」并进 params。

        这些都由策略面板提供，而参数面板不知道自己不该覆盖它们：用户改一次
        并发数，``ParamsPanel`` 会造一份全新的 ``JobParams``，reclaim 字段
        回到默认的 0 —— 于是"腾 100 GiB"静默变成"没设置"。

        强度更隐蔽：它默认是 STANDARD，丢掉之后"极端档"会静默变成"标准档"，
        跑起来才发现要搬的文件数完全不是刚才预演的那个数。
        """
        return replace(
            params,
            reclaim_blocks=self._params.reclaim_blocks,
            reclaim_at=self._params.reclaim_at,
            reclaim_avoid_protected=self._params.reclaim_avoid_protected,
            intensity=self._params.intensity,
            extreme_min_mib=self._params.extreme_min_mib,
            pack_span_blocks=self._params.pack_span_blocks,
        )

    def _on_intensity(self, intensity: Intensity) -> None:
        self._params = replace(self._params, intensity=intensity)
        self.log_view.append(
            LogLevel.INFO,
            f"强度：{intensity.label} —— {INTENSITY_HELP.get(intensity, '')}",
        )
        self._replan_if_ready()

    def _on_extreme_min(self) -> None:
        mib = self.strategy_panel.extreme_min_mib()
        self._params = replace(self._params, extreme_min_mib=mib)
        self.log_view.append(LogLevel.INFO, f"极端档大文件下限：{mib} MiB")
        self._replan_if_ready()

    def _replan_if_ready(self) -> None:
        """改了"选哪些文件"的参数就重算预演。

        **不要**发 ``UpdateTarget`` —— 那会完整重扫一遍卷，用户只是想看看
        换个档位要搬多少东西，不该为此等一次扫描。
        """
        if self._report is None:
            return
        if self._manual_scope and not self._selection:
            return
        self.service.submit(Replan(self._strategy, self._scope_file_ids()))

    def _on_reclaim_changed(self) -> None:
        panel = self.strategy_panel.reclaim
        self._params = replace(
            self._params, reclaim_blocks=panel.blocks(), reclaim_at=panel.side()
        )
        size = fmt_bytes(self._params.reclaim_bytes)
        self.log_view.append(
            LogLevel.INFO, f"可用空间整理：腾出 {size}，位于{panel.side().label}"
        )
        if self._strategy is Strategy.RECLAIM and self._report is not None:
            self.service.submit(Replan(self._strategy, self._scope_file_ids()))

    def _on_pack_changed(self) -> None:
        blocks = self.strategy_panel.pack.blocks()
        self._params = replace(self._params, pack_span_blocks=blocks)
        scope = "整卷" if blocks == 0 else fmt_bytes(blocks * self._params.block_size)
        self.log_view.append(LogLevel.INFO, f"压实卷尾：范围 {scope}")
        if self._strategy is Strategy.PACK_TAIL:
            self._replan_if_ready()

    def _on_params(self, params: JobParams) -> None:
        self._params = self._merge_reclaim(params)
        if self.core.state in (JobState.RUNNING, JobState.PAUSED):
            # 运行期只热更新限速与校验；结构性参数要下次规划才生效
            self.service.submit(SetParams(params))

    # ------------------------------------------------------------------
    # 动作
    # ------------------------------------------------------------------

    def _on_scan(self) -> None:
        if self._volume is None:
            return
        self.log_view.clear_banner()
        self.rate_chart.clear()
        self.progress.reset()
        # 手动模式下还没选文件时**不做预演**：file_ids 为空等于整卷，
        # 预演出来的"要搬 6.5 GiB"会和用户接下来只整理几个文件的意图不符。
        preview = None if (self._manual_scope and not self._selection) else self._strategy
        self.service.submit(
            UpdateTarget(
                self._volume,
                self._params,
                strategy=preview,
                file_ids=self._scope_file_ids(),
            )
        )

    def _on_start(self) -> None:
        if self._volume is None:
            return
        if self._strategy is Strategy.ANALYZE_ONLY:
            QMessageBox.information(
                self,
                "当前是只分析模式",
                "「只分析」不会写入任何数据。请选择「合并碎片」等策略后再开始。",
            )
            return
        if self._manual_scope and not self._selection:
            QMessageBox.information(
                self,
                "还没有选中文件",
                "当前是「手动选择」范围，但没有勾选任何文件。\n"
                "请点左栏「选择文件…」勾选要整理的文件，或把范围切回「整卷」。",
            )
            return
        if not self._confirm_write(self._volume):
            return
        self.log_view.clear_banner()
        self.rate_chart.clear()
        spec = JobSpec(
            volume=self._volume,
            strategy=self._strategy,
            params=self._params,
            file_ids=self._scope_file_ids(),
        )
        self.service.submit(StartJob(spec))

    def _confirm_write(self, vol: VolumeInfo) -> bool:
        """写盘前的显式确认。系统卷要求手输盘符，不提供"回车即通过"的路径。"""
        if vol.is_system:
            text, ok = QInputDialog.getText(
                self,
                "确认操作系统卷",
                f"你正在对**系统卷** {vol.mount_point} 执行块级整理。\n"
                "非正常中断可能导致系统无法启动。\n\n"
                f"如确认继续，请输入盘符「{vol.letter.rstrip(chr(92))}」：",
            )
            return bool(ok) and text.strip().upper().startswith(vol.letter.rstrip("\\").upper())
        ret = QMessageBox.question(
            self,
            "确认执行整理",
            f"目标：{vol.mount_point}（{vol.filesystem}）\n"
            f"策略：{STRATEGY_HELP.get(self._strategy, self._strategy.value)}\n"
            f"强度：{self._params.intensity.label} —— "
            f"{INTENSITY_HELP.get(self._params.intensity, '')}\n\n"
            "执行期间请勿断电、勿强制关机。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return confirmed(ret)

    def _on_stop(self) -> None:
        # 两个阶段的代价完全不同，不能共用一句"当前 chunk 会写完再退出"：
        # 规划阶段根本没有 chunk，而用户真正要知道的是"会不会写坏东西"。
        if self.core.state in (JobState.SCANNING, JobState.PLANNING):
            text = (
                "当前正在扫描 / 规划，还没有写入任何数据。\n\n"
                "停止会丢弃这次结果，回到可以重新扫描的状态。确定停止？"
            )
        else:
            text = "停止后当前 chunk 会写完再退出，断点会保留以便续做。确定停止？"
        ret = QMessageBox.question(
            self,
            "停止任务",
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirmed(ret):
            self.service.submit(CancelJob(keep_checkpoint=True))

    # ------------------------------------------------------------------
    # 引擎事件
    # ------------------------------------------------------------------

    def _on_state(self, state: JobState) -> None:
        self.control.sync_state(state)
        self.progress.set_state(state)
        self.params_panel.set_locked(state in (JobState.RUNNING, JobState.PAUSED, JobState.STOPPING))
        if state is JobState.RUNNING:
            # 跑起来之后"进度"才是第一眼要看的东西，把它滚进视口。
            # 延后一拍：状态事件到达时布局还没按新内容算完，此时
            # ensureWidgetVisible 会判定"不用滚"，于是什么都没发生。
            # 立刻滚一次（多数情况已够），再补一次校正：状态事件到达时
            # 布局还没按新内容算完，只滚一次会停在"差几像素"的位置。
            self._reveal_progress()
            QTimer.singleShot(120, self._reveal_progress)

    def _reveal_progress(self) -> None:
        scroll = getattr(self, "_center_scroll", None)
        if scroll is None:
            return
        card = getattr(self, "_progress_card", self.progress)
        scroll.ensureWidgetVisible(card, 0, 12)
        bar = scroll.verticalScrollBar()
        # 进度卡是最后一屏内容，直接滚到底最可靠
        if card.y() + card.height() > scroll.viewport().height():
            bar.setValue(bar.maximum())

    def _on_progress(self, pr: Progress) -> None:
        self.progress.update_progress(pr)
        # 扫描阶段没有"搬运速率"这回事，往曲线里推 0 只会得到一条贴底的
        # 假折线，看起来像"卡住了"。
        if not pr.scanned_items:
            self.rate_chart.push(pr.rate_bps)

    def _on_report(self, report: FragReport) -> None:
        self._report = report
        # 报告换了，file_id 索引必须重建；老选择里可能有的文件已经没了
        self._id_index = None
        if self._selection:
            valid = {f.file_id for f in report.files}
            kept = tuple(i for i in self._selection if i in valid)
            dropped = len(self._selection) - len(kept)
            self._selection = kept
            if dropped:
                self.log_view.append(
                    LogLevel.WARNING,
                    f"有 {dropped} 个已选文件不在本次扫描结果里，已从选择中移除",
                )
        self.scope_panel.set_available(True)
        self._refresh_scope_summary()
        # 受限扫描（未提权）只允许看：把"开始整理"锁掉，并说清原因
        self.control.set_restricted(report.restricted)
        self.frag_map.set_report(report)
        self.m_file_frag.set_value(fmt_percent(report.file_frag_score))
        self.m_free_frag.set_value(fmt_percent(report.free_frag_score))
        self.m_frag_files.set_value(
            f"{len(report.fragmented_files)} / {report.file_count}"
        )
        self.m_largest_free.set_value(fmt_bytes(report.largest_free_run * report.geometry.block_size))
        self.m_files.set_value(fmt_count(report.file_count))

        color = self._p.ok
        if report.free_frag_score > 0.5:
            color = self._p.danger
        elif report.free_frag_score > 0.2:
            color = self._p.warn
        self.m_free_frag.set_accent(color)

        worst = report.worst(3)
        if worst:
            rows = "".join(
                f"<div>· <span style='color:{self._p.text}'>{_esc(f.path)}</span> "
                f"<span style='color:{self._p.text_faint}'>— {f.run_count} 段 / "
                f"{fmt_bytes(f.total_blocks * report.geometry.block_size)} / "
                f"碎片度 {fmt_percent(f.frag_score, 0)}</span></div>"
                for f in worst
            )
            self.worst_label.setText(f"<div style='margin-bottom:4px'>最值得整理：</div>{rows}")
        else:
            self.worst_label.setText("没有发现碎片文件。")

    def _on_plan(self, plan: Plan) -> None:
        self._plan = plan
        if plan.ops:
            self.preview.setText(
                f"预演：{plan.op_count} 个文件 · {fmt_count(plan.total_blocks)} 块 · "
                f"{fmt_bytes(plan.total_blocks * (self._volume.geometry.block_size if self._volume else 4096))}"
                + ("（需串行执行）" if plan.requires_serial else "")
            )
        elif plan.reasons:
            self.preview.setText("预演：无法执行 —— " + plan.reasons[0])
        else:
            self.preview.setText("预演：没有可执行的操作")

    def _copy_diagnostic(self) -> None:
        """把当前状态打成一段可粘贴的报告放进剪贴板。

        这是这个工具唯一的"求助出口"。设计目标是：用户不需要理解任何内部
        概念，点一下、粘出去，对方就能看到版本、卷、参数、卡在哪一步、
        系统错误码是什么。
        """
        from ..diagnostics import build_report

        result = getattr(self.core, "last_result", None)
        scope = (
            f"手动选择 {len(self._selection)} 个文件"
            if self._manual_scope and self._selection
            else "整卷"
        )
        text = build_report(
            volume=self._volume,
            strategy=self._strategy.value,
            scope=scope,
            params=self._params,
            moved_ops=getattr(result, "moved_ops", 0),
            failed_ops=getattr(result, "failed_ops", 0),
            skipped_ops=getattr(result, "skipped_ops", 0),
            elapsed_s=getattr(result, "elapsed_s", 0.0),
            aborted=getattr(result, "aborted", ""),
            failures=getattr(result, "failures", ()) or (),
            log_tail=self.log_view.plain_tail(40),
        )
        QApplication.clipboard().setText(text)
        self.log_view.append(
            LogLevel.INFO,
            f"诊断报告已复制到剪贴板（{len(text)} 字符），可直接粘贴给 AI 或贴进 issue",
        )

    def _on_job_finished(self, event) -> None:
        self.log_view.append(
            LogLevel.INFO if event.failed_ops == 0 else LogLevel.WARNING,
            f"任务结束：搬运 {event.moved_ops} 个文件，失败 {event.failed_ops} 个，"
            f"跳过 {event.skipped_ops} 个，共 {fmt_bytes(event.bytes_moved)}，"
            f"耗时 {event.elapsed_s:.1f} 秒",
            event.message,
        )
        if event.failed_ops:
            # 有失败时把"下一步该做什么"直接说出来：只写"失败 N 个"，用户
            # 除了截图整屏日志没有别的办法求助。
            self.log_view.append(
                LogLevel.WARNING,
                f"{event.failed_ops} 个文件没能搬走。点日志右上角「复制诊断」，"
                "把生成的内容整段贴出来即可定位原因",
            )
        if self._demo_layout is not None and self._demo_device is not None and event.moved_ops > 0:
            self._verify_demo()

    # ------------------------------------------------------------------
    # 演示模式
    # ------------------------------------------------------------------

    def load_demo(self) -> None:
        """载入合成卷：完整跑通扫描 → 规划 → 执行 → 校验，不接触真实磁盘。"""
        layout = build_layout(DEMO_BLOCK_BUDGET, file_count=26, seed=20261101)
        device = MemoryDevice(
            layout.total_blocks, layout.geometry.block_size, name="DEMO", writable=True
        )
        populate(device, layout)
        problems = verify(device, layout)
        if problems:
            self.log_view.append(LogLevel.ERROR, f"合成卷自检失败：{problems[0]}")
            return

        self._demo_layout = layout
        self._demo_device = device
        self._demo_volume = VolumeInfo(
            device_path=DEMO_DEVICE_PATH,
            mount_point="DEMO:\\",
            label="合成卷（演示）",
            filesystem="SYNTH",
            kind=VolumeKind.RAMDISK,
            geometry=layout.geometry,
            free_bytes=sum(e.length for e in layout.free) * layout.geometry.block_size,
            is_system=False,
            is_mounted=False,
            writable=True,
            note="内存合成卷，用于演示与冒烟验证",
        )
        self.core.catalog = StaticCatalog(layout.files, layout.free, volume_key=DEMO_DEVICE_PATH)
        self.core.catalog_factory = None  # 演示卷不走 MFT
        self.log_view.append(
            LogLevel.INFO,
            f"演示卷已就绪：{fmt_bytes(layout.geometry.total_bytes)}，"
            f"{layout.file_count if hasattr(layout, 'file_count') else len(layout.files)} 个文件，"
            f"{len([f for f in layout.files if f.is_fragmented])} 个碎片文件",
            "数据已按块写入内存并完成一次自检",
        )
        self._load_volumes()
        self.picker.select_letter("DEMO")

    def _provide_device(self, volume: VolumeInfo):
        if volume.device_path == DEMO_DEVICE_PATH and self._demo_device is not None:
            return self._demo_device
        from ..devices import DeviceSpec, open_device

        return open_device(
            DeviceSpec(
                kind="win32",
                path=volume.device_path,
                block_size=volume.geometry.block_size,
                writable=True,
                lock_volume=not volume.is_mounted,
            )
        )

    def _verify_demo(self) -> None:
        """执行后逐块校验，并把新的布局喂回目录服务，让再次扫描反映真实状态。"""
        plan = self.core.plan
        if plan is None or self._demo_layout is None or self._demo_device is None:
            return
        after = apply_plan(self._demo_layout, plan)
        problems = verify(self._demo_device, after)
        if problems:
            self.log_view.append(
                LogLevel.ERROR,
                f"数据校验失败（{len(problems)} 处不一致）",
                problems[0],
            )
        else:
            self.log_view.append(
                LogLevel.INFO,
                "数据校验通过：搬迁后所有块逐块比对一致",
                f"校验了 {len(after.files)} 个文件",
            )
        # 目录服务切到新布局，下次扫描才能看到"碎片度下降了"
        self._demo_layout = after
        self.core.catalog = StaticCatalog(after.files, after.free, volume_key=DEMO_DEVICE_PATH)
        QTimer.singleShot(
            400,
            lambda: self.service.submit(
                UpdateTarget(self._demo_volume, self._params, strategy=None)
            )
            if self._demo_volume is not None
            else None,
        )

    # ------------------------------------------------------------------
    # 配置持久化
    # ------------------------------------------------------------------

    def _restore_settings(self) -> None:
        """启动时恢复上次的配置。**任何异常都不能挡住启动。**

        配置是便利功能：读不了、坏了、字段非法，都只在日志里说一声，界面
        照常起来。反过来（启动失败）等于用户改坏一个数字就打不开程序。
        """
        loaded = self._store.load()
        st = loaded.settings
        self._restoring = True
        try:
            self._params = st.params
            self._strategy = st.strategy
            self.params_panel.apply(st.params)
            self.strategy_panel.set_strategy(st.strategy)
            self.strategy_panel.set_intensity(st.params.intensity)
            self.strategy_panel.set_extreme_min_mib(st.params.extreme_min_mib)
            # 可用空间整理 / 压实范围用的是"块数"，换算要块大小 —— 现在没有
            # 卷，留到第一次 _on_volume_selected 里补。不补的话，恢复出来的
            # 参数会跟着首个卷的几何被重新按默认值算一遍（静默丢用户设置）。
            self._restore_spans = True
            self._pending_volume_path = st.volume_path
            if not st.left_panel_visible:
                self.left_panel.setVisible(False)
                self.btn_left.setChecked(False)
                self._left_user_hidden = True
            if st.window_width >= self.minimumWidth() and st.window_height >= self.minimumHeight():
                self.resize(st.window_width, st.window_height)
        finally:
            self._restoring = False

        if loaded.recovered:
            self.log_view.append(
                LogLevel.WARNING,
                "上次的配置没能读出来，已按默认值启动（原文件已备份，见日志明细）",
            )
        for note in loaded.notes:
            self.log_view.append(LogLevel.WARNING, f"配置：{note}")
        if loaded.missing:
            self.log_view.append(
                LogLevel.INFO,
                f"首次启动，配置将保存到 {self._store.path}",
            )

    def _schedule_save(self, *_args) -> None:
        """改动后延迟落盘。``*_args`` 是信号带过来的值（策略、参数对象…），不用。"""
        if self._restoring:
            return
        self._save_timer.start()

    def _settings_snapshot(self) -> AppSettings:
        return AppSettings(
            strategy=self._strategy,
            params=self._params,
            volume_path=self._volume.device_path if self._volume is not None else "",
            # isVisible() 依赖祖先窗口是否已显示，关窗路径上不可靠；
            # isHidden() 只反映本控件的显隐意图。
            left_panel_visible=not self.left_panel.isHidden(),
            window_width=self.width(),
            window_height=self.height(),
        )

    def _save_settings(self) -> None:
        """立刻落盘。失败只提示一次 —— 每 800ms 刷一行同样的错误没人看得下去。"""
        self._save_timer.stop()
        error = self._store.save(self._settings_snapshot())
        if not error:
            if self._save_error:
                self._save_error = ""
                self.log_view.append(LogLevel.INFO, "配置已恢复保存")
            return
        if error == self._save_error:
            return
        self._save_error = error
        self.log_view.append(
            LogLevel.WARNING,
            f"配置保存失败：{error}",
            "设置只是便利功能，不影响扫描与整理；下次改配置会再试一次",
        )

    # ------------------------------------------------------------------
    # 杂项
    # ------------------------------------------------------------------

    def _toggle_left(self) -> None:
        visible = not self.left_panel.isVisible()
        self.left_panel.setVisible(visible)
        self.btn_left.setChecked(visible)
        self._left_user_hidden = not visible
        self._schedule_save()

    def _probe_capabilities(self) -> None:
        try:
            from ..devices.win32 import capabilities

            caps = capabilities()
        except Exception:  # noqa: BLE001 - 探测失败不该影响启动
            self.badge_engine.setText("引擎：探测失败")
            return
        if caps.raw_write:
            self.badge_engine.setText("引擎：真机可写")
            self.badge_engine.setStyleSheet(f"color: {self._p.ok}; border-color: {self._p.ok}66;")
        elif caps.raw_read:
            self.badge_engine.setText("引擎：真机只读")
        else:
            self.badge_engine.setText("引擎：受限")
        if caps.is_admin:
            self.badge_perm.setText("权限：管理员")
            self.badge_perm.set_clickable(False)
            self.badge_perm.setToolTip("已具备读取原始卷与搬簇所需的权限")
        else:
            # 状态 + 出口。只写"普通用户"等于把修复办法留给用户自己猜，
            # 而他猜的结果通常是"这软件不能用在真盘上"。
            self.badge_perm.setText("权限：普通用户 · 点此提权")
            self.badge_perm.set_clickable(True)
            self.badge_perm.setToolTip(
                "读取/整理真实卷需要管理员权限与 SeManageVolumePrivilege。\n"
                "点击后用「以管理员身份运行」重启本程序（会弹 UAC）。\n"
                "演示模式不受影响。"
            )
        if caps.reason:
            self.badge_engine.setToolTip(caps.reason)

    def _elevate(self) -> None:
        """以管理员身份重启。**只在用户点击时执行**，不在启动流程里自动弹 UAC。"""
        if self.core.state in (JobState.RUNNING, JobState.PAUSED):
            self.log_view.append(LogLevel.WARNING, "任务运行中，先停止再提权重启")
            return
        ret = QMessageBox.question(
            self,
            "以管理员身份重启",
            "将关闭当前窗口并以管理员身份重新启动 LBAMover。\n"
            "Windows 会弹出 UAC 授权框。继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if not confirmed(ret):
            return
        from ..platform.privilege import relaunch_as_admin

        ok, why = relaunch_as_admin(list(sys.argv[1:]))
        if not ok:
            self.log_view.append(
                LogLevel.ERROR,
                f"提权启动未成功：{why}。也可以手动关闭程序，右键选择「以管理员身份运行」。",
            )
            return
        self.log_view.append(LogLevel.INFO, "已发起提权重启，确认 UAC 后本窗口会自动关闭")
        self.close()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        narrow = self.width() < 1040
        if narrow and self.left_panel.isVisible() and not self._left_user_hidden:
            self.left_panel.setVisible(False)
            self.btn_left.setChecked(False)
        elif not narrow and not self.left_panel.isVisible() and not self._left_user_hidden:
            self.left_panel.setVisible(True)
            self.btn_left.setChecked(True)

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.core.state in (JobState.RUNNING, JobState.PAUSED):
            ret = QMessageBox.question(
                self,
                "任务进行中",
                "任务仍在运行。关闭窗口会请求停止并保存断点。确定关闭？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if not confirmed(ret):
                event.ignore()
                return
        # 先落配置再停引擎：``service.stop`` 最长等 5 秒，而"这次的设置"
        # 是关窗后唯一还需要活下来的东西 —— 不能排在可能卡住的步骤后面。
        self._save_settings()
        try:
            self.service.stop(timeout=5.0)
        except Exception:  # noqa: BLE001
            self.log_view.append(LogLevel.ERROR, "引擎关闭异常", traceback.format_exc(limit=2))
        if self._demo_device is not None:
            self._demo_device.close()
        super().closeEvent(event)


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
