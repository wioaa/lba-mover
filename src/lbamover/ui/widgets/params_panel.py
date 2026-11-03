"""参数配置。

每个参数旁边都有"为什么调它"。没有解释的可调参数等于给用户一堆开关让
他自己背锅——尤其是"并发"和"队列深度"，调大反而更慢的情况很常见。

一个硬约束：**内存上界实时算给用户看**。它等于
``chunk_blocks × block_size × queue_depth × concurrency``，
改任何一项都会立刻反映。这是"内存可控"唯一诚实的表达方式。
"""

from __future__ import annotations

import os

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...contract.enums import ExecMode
from ...contract.models import MAX_IGNORED_FAILURES, JobParams
from ..format import fmt_bytes
from ..theme import DARK, Palette

__all__ = ["ParamsPanel"]


class ParamsPanel(QWidget):
    paramsChanged = Signal(object)  # JobParams

    def __init__(self, palette: Palette = DARK, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._p = palette
        self._block_size = 4096
        self._suppress = False
        #: 目标卷是否允许缓存搬运。默认允许（镜像 / 内存盘就是这样），
        #: 选中已挂载的 NTFS 卷时由主窗口关掉。
        self._cache_allowed = True

        form = QFormLayout()
        form.setLabelAlignment(form.labelAlignment())
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)
        form.setContentsMargins(0, 0, 0, 0)

        self.chunk = _spin(1, 4096, 256, " 块", "单次连续读写的块数。大 → 吞吐高、单次延迟大")
        self.depth = _spin(1, 32, 4, " 块", "在途 chunk 数。≥2 才能形成读写重叠")
        self.concurrency = _spin(1, 8, 2, " 路", "同时处理的文件数。HDD 建议 1，SSD 可 2~4")
        self.retries = _spin(0, 10, 3, " 次", "单个 chunk 失败后的重试上限")
        self.rate = QDoubleSpinBox()
        self.rate.setRange(0.0, 4000.0)
        self.rate.setSingleStep(10.0)
        self.rate.setDecimals(0)
        self.rate.setSuffix(" MiB/s")
        self.rate.setToolTip("0 表示不限速。限速会明显降低吞吐，但能让机器保持可用")
        self.reserve = _spin(0, 1_000_000, 0, " 块", "卷尾保留块数，为系统预留活动空间")
        self.verify = QCheckBox("回读校验（慢一倍，但能发现坏块）")
        self.verify.setChecked(True)
        # 默认关闭：默认行为必须是"保守收尾"。打开它等于主动放弃熔断保护，
        # 所以要用户自己勾，且旁边把代价写清楚。
        self.ignore_errors = QCheckBox("忽略错误继续运行")
        self.ignore_errors.setChecked(False)
        self.ignore_errors.setToolTip(
            "不因连续失败停下，把整卷能搬的搬完；累计失败到 "
            f"{MAX_IGNORED_FAILURES} 个才会停。\n"
            "代价：设备真的出问题（掉线、只读）时会在废设备上空转更久，\n"
            "失败的条目最后统一列在日志与报告里。"
        )
        self._ignore_hint = QLabel(
            "不因连续失败停下，把整卷能搬的搬完；累计失败到 "
            f"{MAX_IGNORED_FAILURES} 个才会停。"
        )
        self._ignore_hint.setWordWrap(True)
        self._ignore_hint.setStyleSheet(f"color: {palette.text_faint}; font-size: 11px;")
        self._ignore_hint.setVisible(False)

        form.addRow("分块大小", self.chunk)
        form.addRow("队列深度", self.depth)
        form.addRow("并发", self.concurrency)
        form.addRow("重试上限", self.retries)
        form.addRow("限速", self.rate)
        form.addRow("尾部保留", self.reserve)
        form.addRow("", self.verify)
        # 独立占一行：它跟"回读校验"是两个正交的决策，不能挤在同一格，
        # 也不能靠布局间隙找位置（本项目踩过控件重叠、标题被压）。
        form.addRow("", self.ignore_errors)
        # 说明只在勾选时出现，平时不占面板高度（左栏在 900 高的窗口里很紧）
        form.addRow("", self._ignore_hint)
        form.setRowVisible(self._ignore_hint, False)

        # 三个"很少动、动了容易出事"的参数收进高级区，默认折叠。
        # 常驻 7 行会让左栏在 900 高的窗口里放不下，而其中 3 个参数
        # 用户一年也改不了一次。
        #
        # 「半锁定」也放这里：它改变的是与系统其它程序的交互方式，不是
        # 一次调优，属于"知道自己为什么要开才开"的那一类。
        self.partial_lock = QCheckBox("半锁定簇区间（实验）")
        self.partial_lock.setChecked(False)
        self.partial_lock.setToolTip(
            "动之前先给要搬的簇加共享锁：别的程序仍能读，但写不进来，\n"
            "这样「扫描时的布局」到「搬的时候」不会被改（真机上 3/20 的失败\n"
            "就是布局复核不通过）。\n"
            "代价：对正在使用该文件的程序，它的写操作会失败；\n"
            "读取路径（镜像演练）也会在读每一段时加同样的锁。"
        )
        form.addRow("", self.partial_lock)

        # -- 搬运方式：跨盘缓存（独立引擎）---------------------------
        # 目录输入框**常驻**高级区（不随勾选显隐）：勾选后才发现没地方
        # 放缓存，构造会直接抛 ValueError，用户看到的是"参数非法"这种
        # 完全指不到根因的提示。
        self.exec_cached = QCheckBox("跨盘缓存（磁盘中转）")
        self.exec_cached.setToolTip(
            "把数据先顺序写到工作目录，再顺序写回目标。\n"
            "换的是另一套执行引擎，与盘内搬簇不共享代码。\n"
            "必须指向另一块盘：同盘时总 IO 翻倍、顺序性不会变好。\n"
            "只能用于镜像与内存盘 —— 已挂载的 NTFS 卷会被拒绝（裸写会损坏文件系统）"
        )
        self.cache_dir = QLineEdit()
        self.cache_dir.setPlaceholderText("缓存工作目录（应指向另一块盘）")
        self.cache_dir.setMinimumHeight(26)
        self.cache_dir.setToolTip("例如 I:/lbamover-cache")

        # -- 内存缓存（固定块池，第三套引擎）--------------------------
        self.exec_memcached = QCheckBox("内存缓存（内存块池）")
        self.exec_memcached.setToolTip(
            "数据不落盘：读进一个**预分配**的内存块池，攒满一块就回写。\n"
            "收益来自「多个碎读攒成一次大写」，不是「更快」——\n"
            "它和跨盘缓存一样只用于镜像与内存盘（真机仍走搬簇）。\n"
            "内存占用是固定的：块数 × 块大小，运行期不增长。"
        )
        # 两个数字含义不同：块大小决定一次能凑多长，块数决定能提前读多少。
        # 合成一个"总大小"旋钮就调不动这个取舍了。
        self.mem_blocks = _spin(
            1,
            4096,
            32,
            " 块",
            "块池里的块数。它同时是内存上限与读写错位深度："
            "块越多越能提前读，但内存占用线性增长",
        )
        self.mem_kib = _spin(
            16,
            65536,
            1024,
            " KiB",
            "单块大小。块越大，回写时一次写得越长、目标盘的小写越少；"
            "代价是攒满一块要等更久",
        )

        # -- 目标区预占 ----------------------------------------------
        self.preoccupy = QCheckBox("预占目标区")
        self.preoccupy.setToolTip(
            "搬之前先用占位文件把目标区占住，防止执行期间被别的程序写进去。\n"
            "只防执行那一瞬间的抢占，规划阶段的抢占管不了。\n"
            "需要管理员权限；真机尚未验证。"
        )

        self._advanced_rows = (
            self.depth,
            self.retries,
            self.reserve,
            self.partial_lock,
            self.exec_cached,
            self.cache_dir,
            self.exec_memcached,
            self.mem_blocks,
            self.mem_kib,
            self.preoccupy,
        )
        self.advanced_toggle = QPushButton("高级参数 ▸")
        self.advanced_toggle.setObjectName("ghost")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setChecked(False)
        self.advanced_toggle.clicked.connect(self._toggle_advanced)
        # 先插进表单再统一隐藏。反过来的话 setRowVisible 时这几行还没进
        # 表单，是空操作 —— 而 insertRow 插入的行默认可见，界面会多出几行，
        # 压实模式的内容高度直接超出可视区。
        form.insertRow(form.indexOf(self.partial_lock) + 1, "缓存目录", self.cache_dir)
        form.insertRow(form.indexOf(self.cache_dir), "", self.exec_cached)
        form.insertRow(form.indexOf(self.cache_dir) + 1, "", self.exec_memcached)
        form.insertRow(form.indexOf(self.exec_memcached) + 1, "内存块数", self.mem_blocks)
        form.insertRow(form.indexOf(self.mem_blocks) + 1, "块大小", self.mem_kib)
        form.insertRow(form.indexOf(self.mem_kib) + 1, "", self.preoccupy)
        for widget in self._advanced_rows:
            form.setRowVisible(widget, False)

        self.budget = QLabel()
        self.budget.setWordWrap(True)
        self.budget.setStyleSheet(f"color: {palette.text_faint}; font-size: 11px;")
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setVisible(False)
        self.error.setStyleSheet(f"color: {palette.danger}; font-size: 11px;")

        # 缓存方式为什么不可用。**不能只把复选框置灰**：灰掉的控件在用户眼里
        # 是"功能没做"，而不是"这块盘不支持"。
        self.cache_note = QLabel()
        self.cache_note.setWordWrap(True)
        self.cache_note.setVisible(False)
        self.cache_note.setStyleSheet(f"color: {palette.text_faint}; font-size: 11px;")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addLayout(form)
        lay.addWidget(self.advanced_toggle)
        lay.addWidget(self.cache_note)
        lay.addWidget(self.budget)
        lay.addWidget(self.error)

        for widget in (
            self.chunk,
            self.depth,
            self.concurrency,
            self.retries,
            self.reserve,
        ):
            widget.valueChanged.connect(self._emit)
        # 复选框与输入框没有 valueChanged，得按各自信号接 —— 混在一个
        # 循环里会在窗口构造期就抛 AttributeError，整个界面起不来。
        self.exec_cached.toggled.connect(self._on_exec_cached)
        self.exec_memcached.toggled.connect(self._on_exec_memcached)
        self.mem_blocks.valueChanged.connect(self._emit)
        self.mem_kib.valueChanged.connect(self._emit)
        self.cache_dir.textChanged.connect(self._emit)
        self.preoccupy.toggled.connect(self._emit)
        self.rate.valueChanged.connect(self._emit)
        self.verify.toggled.connect(self._emit)
        self.ignore_errors.toggled.connect(self._on_ignore_errors_toggled)
        self.partial_lock.toggled.connect(self._emit)

        self._sync_exec_rows()
        self._refresh_budget()

    # -- 状态 -------------------------------------------------------------

    def set_block_size(self, block_size: int) -> None:
        self._block_size = max(512, block_size)
        self._refresh_budget()

    def current(self) -> JobParams | None:
        try:
            return JobParams(
                block_size=self._block_size,
                chunk_blocks=self.chunk.value(),
                queue_depth=self.depth.value(),
                concurrency=self.concurrency.value(),
                max_retries=self.retries.value(),
                rate_limit_mbps=float(self.rate.value()),
                verify=self.verify.isChecked(),
                reserve_tail_blocks=self.reserve.value(),
                ignore_errors=self.ignore_errors.isChecked(),
                partial_lock=self.partial_lock.isChecked(),
                exec_mode=self.exec_mode(),
                cache_dir=self._cache_dir_text(),
                mem_cache_blocks=self.mem_blocks.value(),
                mem_cache_block_kib=self.mem_kib.value(),
                preoccupy_target=self.preoccupy.isChecked(),
            )
        except ValueError as exc:
            self.error.setText(str(exc))
            self.error.setVisible(True)
            return None

    def exec_mode(self) -> ExecMode:
        """三种搬运方式互斥。**判定只看勾选**，不猜默认值。

        但有一条例外：目标卷不允许缓存时，勾选也一律按 ``NATIVE`` 算。
        配置是跨会话恢复的 —— 上次在镜像上用 memcached，这次选中的是 C:，
        复选框会被禁用，但 ``apply()`` 恢复出来的勾选状态还在。只靠置灰
        挡不住「参数已经带上 memcached 了」这件事。
        """
        if not self._cache_allowed:
            return ExecMode.NATIVE
        if self.exec_memcached.isChecked():
            return ExecMode.MEMCACHED
        if self.exec_cached.isChecked():
            return ExecMode.CACHED
        return ExecMode.NATIVE

    def set_cache_allowed(self, allowed: bool, reason: str = "") -> None:
        """目标卷是否允许缓存搬运（磁盘中转 / 内存块池）。

        已挂载的 NTFS 卷一律不允许：缓存引擎写的是裸块设备，会绕过文件系统
        覆盖 ``$MFT`` 指着的簇区。**在选卷时就禁掉，而不是等按下开始才报错**
        —— 后者用户已经排了半天参数，最后拿到一句关于锁卷的报错。
        """
        self._cache_allowed = allowed
        if not allowed:
            # 先屏蔽信号再取消勾选：否则每个 setChecked 都会发 toggled，
            # _emit 抓到的是一份"两个都没勾"的中间态。
            for box in (self.exec_cached, self.exec_memcached):
                box.blockSignals(True)
                box.setChecked(False)
                box.blockSignals(False)
        self.cache_note.setText(reason)
        self.cache_note.setVisible(bool(reason))
        self._sync_exec_rows()
        if not allowed:
            self._emit()

    def apply(self, params: JobParams) -> None:
        self._suppress = True
        try:
            self._block_size = params.block_size
            self.chunk.setValue(params.chunk_blocks)
            self.depth.setValue(params.queue_depth)
            self.concurrency.setValue(params.concurrency)
            self.retries.setValue(params.max_retries)
            self.rate.setValue(params.rate_limit_mbps)
            self.reserve.setValue(params.reserve_tail_blocks)
            self.verify.setChecked(params.verify)
            self.ignore_errors.setChecked(params.ignore_errors)
            self.partial_lock.setChecked(params.partial_lock)
            self.exec_cached.setChecked(
                self._cache_allowed and params.exec_mode is ExecMode.CACHED
            )
            self.exec_memcached.setChecked(
                self._cache_allowed and params.exec_mode is ExecMode.MEMCACHED
            )
            self.mem_blocks.setValue(params.mem_cache_blocks)
            self.mem_kib.setValue(params.mem_cache_block_kib)
            if params.cache_dir:
                self.cache_dir.setText(params.cache_dir)
            self.preoccupy.setChecked(params.preoccupy_target)
        finally:
            self._suppress = False
        self._sync_ignore_hint()
        # 恢复配置后要重算"哪些输入框有意义"：只 apply 不 sync 的话，
        # 恢复出来的内存缓存参数会是灰的，用户以为存丢了。
        self._sync_exec_rows()
        self._refresh_budget()

    def _cache_dir_text(self) -> str:
        """勾了缓存但没填目录时，给一个临时目录兜底。

        直接让 ``JobParams`` 抛"必须指定 cache_dir"，用户看到的是一句
        与界面控件对不上的报错；而临时目录在同盘上虽然不划算，至少不会
        让整个参数面板变成"非法"。
        """
        text = self.cache_dir.text().strip()
        if text or not self.exec_cached.isChecked():
            return text
        import tempfile

        return os.path.join(tempfile.gettempdir(), "lbamover-cache")

    # -- 三种搬运方式的互斥 ---------------------------------------------

    def _on_exec_cached(self, checked: bool) -> None:
        if checked and self.exec_memcached.isChecked():
            # 程序化取消勾选**必须屏蔽信号**：否则会再发一次 toggled，
            # _emit 在两者都未勾的瞬间造出一份 NATIVE 参数，把刚做的选择
            # 又冲刷回默认值。
            self.exec_memcached.blockSignals(True)
            self.exec_memcached.setChecked(False)
            self.exec_memcached.blockSignals(False)
        self._sync_exec_rows()
        self._emit()

    def _on_exec_memcached(self, checked: bool) -> None:
        if checked and self.exec_cached.isChecked():
            self.exec_cached.blockSignals(True)
            self.exec_cached.setChecked(False)
            self.exec_cached.blockSignals(False)
        self._sync_exec_rows()
        self._emit()

    def _sync_exec_rows(self) -> None:
        """缓存目录只在「跨盘缓存」下有意义；块池参数只在「内存缓存」下有意义。

        不禁用而是提示：用户会先勾再填。把输入框整行隐藏会让"要填什么"
        完全不可见，那正是旧版"勾了缓存才发现没地方放"的成因。

        但**复选框本身**在目标卷不支持时必须禁用：可点却一定失败的选择，
        比灰掉的控件更糟。
        """
        self.exec_cached.setEnabled(self._cache_allowed)
        self.exec_memcached.setEnabled(self._cache_allowed)
        self.cache_dir.setEnabled(self.exec_cached.isChecked())
        self.mem_blocks.setEnabled(self.exec_memcached.isChecked())
        self.mem_kib.setEnabled(self.exec_memcached.isChecked())
        # 预占与半锁定都是 **MOVE_FILE 路径**上的动作：前者用占位文件占住目标
        # 簇，后者给要搬的簇加共享锁。缓存引擎写的是裸块设备，这两件事对它
        # 没有意义 —— 留着能勾就是"看着生效、实际被忽略"。
        cache_on = self.exec_cached.isChecked() or self.exec_memcached.isChecked()
        self.preoccupy.setEnabled(not cache_on)
        self.partial_lock.setEnabled(not cache_on)
        hint = "缓存搬运不使用盘内搬簇，预占与半锁定对它不适用"
        self.preoccupy.setToolTip(
            hint
            if cache_on
            else "搬之前先用占位文件把目标区占住，防止执行期间被别的程序写进去。\n"
            "只防执行那一瞬间的抢占，规划阶段的抢占管不了。\n需要管理员权限；真机尚未验证。"
        )

    def _toggle_advanced(self) -> None:
        shown = self.advanced_toggle.isChecked()
        self.advanced_toggle.setText("高级参数 ▾" if shown else "高级参数 ▸")
        form = self.layout().itemAt(0).layout()
        for widget in self._advanced_rows:
            form.setRowVisible(widget, shown)
        # 隐藏的行不会自动让面板缩小，必须显式让上层重新算尺寸
        form.activate()
        self.updateGeometry()

    def set_locked(self, locked: bool) -> None:
        """任务运行期间锁住结构性参数（热更新只允许限速与校验）。"""
        for widget in (
            self.chunk,
            self.depth,
            self.concurrency,
            self.retries,
            self.reserve,
            self.exec_cached,
            self.cache_dir,
            self.exec_memcached,
            self.mem_blocks,
            self.mem_kib,
            self.preoccupy,
        ):
            widget.setEnabled(not locked)
        # 「忽略错误运行」也是结构性参数：它决定的是循环里的闸门，而闸门在
        # 构造 JobRunner 时就定了，中途改只会让界面与实际行为说法不一。
        self.ignore_errors.setEnabled(not locked)
        # 半锁定同理：锁的时机在 run_op 里，中途切换只会前后不一致。
        self.partial_lock.setEnabled(not locked)
        # 上面统一 setEnabled(True) 会顺手把"这块盘不支持"的复选框放开，
        # 所以最后按当前卷的能力再收一次。
        self._sync_exec_rows()

    # -- 内部 -------------------------------------------------------------

    def _on_ignore_errors_toggled(self, checked: bool) -> None:
        self._sync_ignore_hint(checked)
        self._emit()

    def _sync_ignore_hint(self, show: bool | None = None) -> None:
        """说明文字只在勾选时出现，避免常驻多占一行高度。"""
        if show is None:
            show = self.ignore_errors.isChecked()
        form = self.layout().itemAt(0).layout()
        if form is not None:
            form.setRowVisible(self._ignore_hint, show)
            form.activate()
        self._ignore_hint.setVisible(show)
        self.updateGeometry()

    def _emit(self) -> None:
        if self._suppress:
            return
        self._refresh_budget()
        params = self.current()
        if params is None:
            return
        self.error.setVisible(False)
        self.paramsChanged.emit(params)

    def _refresh_budget(self) -> None:
        chunk_bytes = self.chunk.value() * self._block_size
        budget = chunk_bytes * self.depth.value() * self.concurrency.value()
        # 块池是**额外**的固定占用，不是流水线缓冲的一部分。不单列出来，
        # 用户看到的"内存上界"就比实际少几十上百 MiB。
        pool = 0
        if self.exec_memcached.isChecked():
            pool = self.mem_blocks.value() * self.mem_kib.value() * 1024
        text = (
            f"内存上界 ≈ {fmt_bytes(budget + pool)}"
            f"（{self.chunk.value()} 块 × {fmt_bytes(self._block_size)}"
            f" × 深度 {self.depth.value()} × 并发 {self.concurrency.value()}"
            + (f"，另有内存块池 {fmt_bytes(pool)}" if pool else "")
            + "），与卷大小无关"
        )
        self.budget.setText(text)


def _spin(lo: int, hi: int, val: int, suffix: str, tip: str) -> QSpinBox:
    box = QSpinBox()
    box.setRange(lo, hi)
    box.setValue(val)
    box.setSuffix(suffix)
    box.setToolTip(tip)
    box.setAccelerated(True)
    return box
