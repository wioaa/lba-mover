"""执行流程回归钉（1.0.0D 修复：「先应用再执行」不执行 / 静默返回 / 取消卡死）。

用户实机反馈「执行模式下点开始迁移却不执行」。根因是待定配置闸门里的 apply
分支只提示"请重新生成计划"就 return，标签承诺与实际行为不符。本文件钉住：

1. 选「先应用再执行」→ 置 `_resume_execute_after_plan` 并**发起重新规划**
   （`_generate_plan` 被调用），不在原地继续；
2. 新计划就绪且意图为真 → **自动接续** `_start_move`（它仍会再走
   DangerConfirmDialog，不跳过人工确认），接续后意图复位且只接续一次；
3. 意图为假 → `_on_plan_ready` 不触发 `_start_move`；
4. 选「按现有计划执行」（proceed）→ 不设置该意图；
5. 规划失败 → 意图被清（不悬挂）；
6. 开头静默 return 补提示（卷/位图未就绪、缺计划两种）。

QA 复核追加（同为本次修复的一部分）：

7. 意图绑到**本次规划的 worker**（`resume_after`），`_generate_plan` 入口即摘下：
   规划因 early-return 没跑起来时，窗口上不留悬挂意图（否则用户下次只是正常点
   「生成计划」就会被意外弹一次危险确认框）；
8. 规划中按 Esc（`PlanWorker` 取消时**不发任何终态信号**）→ `finished` 兜底收尾，
   绝不永久卡在忙碌态。

全部离屏运行，不启动线程、不碰设备句柄。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core.models import (  # noqa: E402
    BitmapSnapshot,
    Extent,
    FileEntry,
    MovePlan,
    MoveSegment,
    PlanItem,
    VolumeInfo,
)

TOTAL = 1_000_000
BPC = 4096


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _make_plan() -> MovePlan:
    """最小可用计划：1 个 item + 1 段，``empty`` 为 False。"""
    entry = FileEntry(
        path="R:\\a.bin", name="a.bin", size=4 * BPC,
        extents=[Extent(vcn=0, lcn=10, clusters=4)], movable=True,
    )
    plan = MovePlan(
        items=[
            PlanItem(
                entry=entry,
                target_lcn=5000,
                clusters=4,
                segments=[MoveSegment(vcn=0, src_lcn=10, dst_lcn=5000, clusters=4)],
            )
        ],
        total_clusters=TOTAL,
    )
    plan.tasks = []
    return plan


def _make_window():
    """构造一个带假卷 / 假位图 / 假计划的 MainWindow（屏蔽真实卷加载）。"""
    _ = _app()
    import app.ui.main_window as mw

    mw.MainWindow._load_volumes = lambda self: None   # 免得真去枚举本机卷
    window = mw.MainWindow()
    window._volume = VolumeInfo(
        letter="R", root="R:\\", label="T", filesystem="NTFS",
        total_bytes=TOTAL * BPC, bytes_per_cluster=BPC, total_clusters=TOTAL,
        mft_zone_start=1000, mft_zone_end=2000, mft_start_lcn=1000, is_ntfs=True,
    )
    window._snapshot = BitmapSnapshot(
        bits=bytes(TOTAL // 8), total_clusters=TOTAL, bytes_per_cluster=BPC
    )
    window._plan = _make_plan()
    window._mode = mw.MODE_EXECUTE
    return window, mw


class _FakeChoiceDialog:
    """替身 ChoiceDialog：``exec()`` 恒 True，``choice`` 由外部指定。"""

    next_choice = "cancel"

    def __init__(self, *_args, **_kwargs) -> None:
        self.choice = _FakeChoiceDialog.next_choice

    def exec(self) -> bool:
        return True


def _patch_choice_dialog(mw, choice: str):
    original = mw.dialogs.ChoiceDialog
    _FakeChoiceDialog.next_choice = choice
    mw.dialogs.ChoiceDialog = _FakeChoiceDialog
    return original


class _FakeSignal:
    """最小信号替身：只记录槽并支持手动 emit（不建线程）。"""

    def __init__(self) -> None:
        self.slots: list = []

    def connect(self, slot) -> None:
        self.slots.append(slot)

    def emit(self, *args) -> None:
        for slot in list(self.slots):
            slot(*args)


class _FakePlanWorker:
    """替身 PlanWorker：记录构造参数 / 连接到的槽，``start()`` 不建线程。"""

    instances: list = []

    def __init__(self, *args, **_kwargs) -> None:
        self.args = args
        self.resume_after = False
        self.completed = _FakeSignal()
        self.failed = _FakeSignal()
        self.finished = _FakeSignal()
        self.started = False
        _FakePlanWorker.instances.append(self)

    def start(self) -> None:
        self.started = True


def _patch_plan_worker(mw):
    """把 ``mw.PlanWorker`` 换成替身，返回原始类供复原。"""
    original = mw.PlanWorker
    _FakePlanWorker.instances = []
    mw.PlanWorker = _FakePlanWorker
    return original


def _select_one_file(window) -> None:
    """往表格塞一条勾选的可移动文件（``_generate_plan`` 的前置条件）。"""
    entry = FileEntry(
        path="R:\\a.bin", name="a.bin", size=4 * BPC,
        extents=[Extent(vcn=0, lcn=10, clusters=4)], movable=True, checked=True,
    )
    window._table.file_model.set_entries([entry])


def _use_tail_strategy(window) -> None:
    """把策略钉成「自动填满末端」（needs_window=False，避开区间前置校验）。"""
    window._target_panel.strategy = lambda: "tail"


class _FakeFeasibility:
    """可行性报告替身（``_generate_plan`` 只读 ``ok`` 与 ``reasons``）。"""

    def __init__(self, ok: bool, reasons=None) -> None:
        self.ok = ok
        self.reasons = list(reasons or [])


class _FakeDangerDialog:
    """计数桩：只统计危险确认框被**实例化**几次，``exec()`` 一律「拒绝」。"""

    count = 0
    result = None                      # != QDialog.Accepted → 不启动搬运线程

    def __init__(self, *_args, **_kwargs) -> None:
        _FakeDangerDialog.count += 1

    def exec(self):
        return _FakeDangerDialog.result


def _patch_danger_dialog(mw):
    """把 ``dialogs.DangerConfirmDialog`` 换成计数桩，返回原始类供复原。"""
    original = mw.dialogs.DangerConfirmDialog
    _FakeDangerDialog.count = 0
    mw.dialogs.DangerConfirmDialog = _FakeDangerDialog
    return original


class _SignalSource(QObject):
    """带**真** Qt 信号的最小发送者。

    用它 emit 才能真正让槽里的 ``self.sender()`` 拿到对象 —— 从而覆盖
    「意图绑在 worker 上」那条代码路径（纯 Python 替身的 emit 不会设置 sender）。
    """

    completed = Signal(object)
    failed = Signal(str)
    finished = Signal()


# --------------------------------------------------------------------------- #
# ① apply 分支：置标志 + 发起重新规划，不原地继续
# --------------------------------------------------------------------------- #
def test_apply_branch_triggers_replan_and_flag() -> None:
    window, mw = _make_window()
    calls: list = []
    window._has_pending = True
    window._generate_plan = lambda: calls.append("plan")

    original = _patch_choice_dialog(mw, "apply")
    try:
        window._start_move()
    finally:
        mw.dialogs.ChoiceDialog = original

    assert window._resume_execute_after_plan is True, "apply 分支必须置接续标志"
    assert calls == ["plan"], f"应只发起重新规划、不原地搬运；实际 {calls}"
    assert window._move_worker is None, "apply 分支不得直接启动搬运线程"


# --------------------------------------------------------------------------- #
# ② 计划就绪 + 标志为真 → 自动接续 _start_move，且只接续一次
# --------------------------------------------------------------------------- #
def test_plan_ready_resumes_execution_once() -> None:
    window, _mw = _make_window()
    window._resume_execute_after_plan = True
    window._feasibility = None                      # 不阻止接续
    calls: list = []
    window._start_move = lambda: calls.append("move")

    window._on_plan_ready(window._plan)

    assert calls == ["move"], f"标志为真时应自动接续执行；实际 {calls}"
    assert window._resume_execute_after_plan is False, "接续后标志必须复位"
    window._on_plan_ready(window._plan)             # 再调一次
    assert calls == ["move"], "标志已复位，不应重复接续"


# --------------------------------------------------------------------------- #
# ③ 标志为假 → 不触发
# --------------------------------------------------------------------------- #
def test_plan_ready_does_not_start_when_flag_clear() -> None:
    window, _mw = _make_window()
    window._resume_execute_after_plan = False
    calls: list = []
    window._start_move = lambda: calls.append("move")
    window._on_plan_ready(window._plan)
    assert calls == []


# --------------------------------------------------------------------------- #
# ④ proceed 分支：不置标志
# --------------------------------------------------------------------------- #
def test_proceed_branch_does_not_set_flag() -> None:
    window, mw = _make_window()
    window._has_pending = True

    original = _patch_choice_dialog(mw, "proceed")
    try:
        window._start_move()
    finally:
        mw.dialogs.ChoiceDialog = original

    assert window._resume_execute_after_plan is False, "proceed 分支不得置接续标志"


# --------------------------------------------------------------------------- #
# ⑤ 规划失败清标志（不悬挂）
# --------------------------------------------------------------------------- #
def test_plan_failure_clears_flag() -> None:
    window, _mw = _make_window()
    window._resume_execute_after_plan = True
    window._on_plan_failed("模拟规划失败")
    assert window._resume_execute_after_plan is False, "规划失败必须清掉接续标志"


# --------------------------------------------------------------------------- #
# ⑥ 开头静默 return 补提示
# --------------------------------------------------------------------------- #
def test_start_move_notifies_when_not_ready() -> None:
    window, _mw = _make_window()
    window._snapshot = None
    window._start_move()
    assert window._notice.text(), "卷/位图未就绪时必须给出提示（不许静默 return）"
    assert "尚未就绪" in window._notice.text()

    window, _mw = _make_window()
    window._plan = None
    window._start_move()
    assert "先生成迁移计划" in window._notice.text()


# --------------------------------------------------------------------------- #
# ⑦ apply 后规划撞 early-return：三条路径都不得留下悬挂意图（QA ①）
# --------------------------------------------------------------------------- #
def _setup_none_selected(window) -> None:
    """(a) 一个文件都没勾选。"""
    return


def _setup_bad_window(window) -> None:
    """(b) 目标区间非法（``end <= start``）。"""
    _select_one_file(window)
    window._target_panel.strategy = lambda: "range"
    window._target_panel.window = lambda: (500, 500)


def _setup_infeasible(window) -> None:
    """(c) 目标区间不可行（可行性报告 ok=False）。"""
    _select_one_file(window)
    window._target_panel.strategy = lambda: "range"
    window._target_panel.window = lambda: (100, 900)
    window._feasibility = _FakeFeasibility(False, ["区间内空闲不足", "挡路者不可驱逐"])


def test_early_return_after_apply_does_not_leave_intent() -> None:
    for setup, fragment in (
        (_setup_none_selected, "请先勾选需要迁移的文件"),
        (_setup_bad_window, "请先设定目标区间"),
        (_setup_infeasible, "当前目标区间不可行"),
    ):
        window, _mw = _make_window()
        setup(window)
        # apply 分支置的意图（模拟「先应用再执行」刚点下去）
        window._resume_execute_after_plan = True
        window._generate_plan()

        assert window._resume_execute_after_plan is False, (
            f"[{fragment}] 规划早退不得留下悬挂意图"
            "（否则下次普通规划会意外弹危险确认框）"
        )
        assert window._plan_worker is None, f"[{fragment}] 早退时不得创建规划线程"
        text = window._notice.text()
        assert fragment in text, f"应提示具体原因，实际 {text!r}"
        assert "自动执行已取消" in text, (
            f"[{fragment}] 早退时必须明说不再自动执行，实际 {text!r}"
        )


# --------------------------------------------------------------------------- #
# ⑧ 意图绑到本次规划的 worker（QA ①），finished 兜底连上（QA ②）
# --------------------------------------------------------------------------- #
def test_intent_is_bound_to_plan_worker() -> None:
    window, mw = _make_window()
    _select_one_file(window)
    _use_tail_strategy(window)
    window._resume_execute_after_plan = True

    original = _patch_plan_worker(mw)
    try:
        window._generate_plan()
        workers = list(_FakePlanWorker.instances)
    finally:
        mw.PlanWorker = original

    assert len(workers) == 1, f"应创建 1 个规划线程，实际 {len(workers)}"
    worker = workers[0]
    assert worker.started is True, "规划线程必须被启动"
    assert worker.resume_after is True, "自动接续意图必须交给本次规划的 worker"
    assert window._resume_execute_after_plan is False, "窗口上的瞬时意图必须立刻摘走"
    assert window._plan_worker is worker
    assert window._on_plan_ready in worker.completed.slots
    assert window._on_plan_failed in worker.failed.slots
    assert window._on_plan_worker_finished in worker.finished.slots, (
        "finished 兜底必须连上：取消时 worker 不发任何终态信号"
    )


# --------------------------------------------------------------------------- #
# ⑨ 意图一次性消费（worker 优先，回落窗口标志）
# --------------------------------------------------------------------------- #
def test_take_resume_intent_consumes_worker_and_window() -> None:
    window, _mw = _make_window()
    worker = _FakePlanWorker()
    worker.resume_after = True

    assert window._take_resume_intent(worker) is True
    assert worker.resume_after is False, "worker 上的意图必须消费即清"
    assert window._take_resume_intent(worker) is False, "同一意图不得被消费两次"

    window._resume_execute_after_plan = True
    assert window._take_resume_intent() is True, "无 worker 时回落到窗口标志（直接调用路径）"
    assert window._resume_execute_after_plan is False


# --------------------------------------------------------------------------- #
# ⑩ 取消兜底：worker 结束但没发终态信号 → 必须退出忙碌态（QA ②）
#     ★ 必须用**真 Qt 信号**驱动：本槽有 `sender() is None → no-op` 守卫
#     （1.0.0E 加的防御），直调会绕过守卫并让断言失真。踩过两次，勿改回直调。
# --------------------------------------------------------------------------- #
def test_plan_cancel_fallback_clears_busy() -> None:
    window, _mw = _make_window()
    source = _SignalSource()               # 替身「规划线程」：有真 finished 信号
    source.resume_after = True
    source.finished.connect(window._on_plan_worker_finished)
    window._plan_worker = source
    window._set_busy(True)

    source.finished.emit()                 # 线程结束且没发 completed/failed

    assert window._plan_worker is None, "取消后必须放掉 worker 槽位"
    assert window._busy is False, "取消后必须退出忙碌态（否则进度条一直转、按钮全禁）"
    assert source.resume_after is False, "被取消的规划不得保留自动执行意图"
    assert window._resume_execute_after_plan is False
    assert "已取消" in window._notice.text(), window._notice.text()


# --------------------------------------------------------------------------- #
# ⑪ 正常收尾后 finished 事件到达 → no-op（不许误清别的任务的忙碌态）
# --------------------------------------------------------------------------- #
def test_plan_finished_is_noop_after_terminal_signal() -> None:
    window, _mw = _make_window()
    source = _SignalSource()
    source.finished.connect(window._on_plan_worker_finished)
    window._plan_worker = None            # completed / failed 已收尾
    window._busy = True                   # 假设别的任务正忙
    source.finished.emit()
    assert window._busy is True, "没有未收尾的规划线程时 finished 必须什么都不做"
    assert window._plan_worker is None


# --------------------------------------------------------------------------- #
# ⑪b 直调兜底槽必须 no-op（1.0.0E 契约钉：本槽只由 QThread.finished 驱动）
#     动机：`sender()` 为 None 时若仍「接管收尾」，会清掉一个**可能还在跑**的
#     规划槽位 → 界面误判空闲、用户可再点一次生成计划，旧线程 completed 回来
#     时槽位已是新值（双重规划线程）。QA 提的两个改动要点之一。
# --------------------------------------------------------------------------- #
def test_plan_finished_direct_call_is_noop() -> None:
    window, _mw = _make_window()
    worker = _FakePlanWorker()
    worker.resume_after = True
    window._plan_worker = worker
    window._set_busy(True)

    window._on_plan_worker_finished()      # 直调：不是 finished 信号驱动

    assert window._plan_worker is worker, "直调不得清掉规划槽位"
    assert window._busy is True, "直调不得退出忙碌态"
    assert worker.resume_after is True, "直调不得消费接续意图"


# --------------------------------------------------------------------------- #
# ⑫ 正常规划（无意图）完成 → 不弹危险确认框、不启动搬运（QA 钉子 2）
# --------------------------------------------------------------------------- #
def _armed_window():
    """执行模式、权限齐备、危险确认框换成计数桩的窗口。"""
    window, mw = _make_window()
    window._is_admin = True
    window._privilege_ok = True
    original = _patch_danger_dialog(mw)
    return window, mw, original


def test_plan_ready_without_intent_does_not_confirm() -> None:
    window, mw, original = _armed_window()
    source = _SignalSource()               # 真信号 → 槽里 sender() 可用
    source.resume_after = False
    source.completed.connect(window._on_plan_ready)
    try:
        source.completed.emit(window._plan)
    finally:
        mw.dialogs.DangerConfirmDialog = original

    assert _FakeDangerDialog.count == 0, "没有接续意图时不得弹任何危险确认框"
    assert window._move_worker is None, "没有接续意图时不得启动搬运线程"


# --------------------------------------------------------------------------- #
# ⑬ 带意图的规划完成 → 接续一次、危险确认框恰好实例化 1 次（QA 钉子 3）
# --------------------------------------------------------------------------- #
def test_plan_ready_with_intent_confirms_exactly_once() -> None:
    window, mw, original = _armed_window()
    source = _SignalSource()
    source.resume_after = True
    source.completed.connect(window._on_plan_ready)
    try:
        source.completed.emit(window._plan)
        first = _FakeDangerDialog.count
        source.completed.emit(window._plan)      # 第二次：意图已被消费
        second = _FakeDangerDialog.count
    finally:
        mw.dialogs.DangerConfirmDialog = original

    assert first == 1, f"接续必须恰好弹 1 次危险确认框（人工闸门不许跳过），实际 {first}"
    assert second == 1, "意图只消费一次：重复的 completed 不得再弹确认框"
    assert window._move_worker is None, "确认框桩返回「拒绝」时不得启动搬运线程"
    assert window._resume_execute_after_plan is False
    assert getattr(source, "resume_after") is False, "worker 上的意图消费即清"


# --------------------------------------------------------------------------- #
# ⑭ 1.0.0F 修复①：计划过期时「生成迁移计划」按钮**不**再被可行性判定卡灰。
#     此前要求 ``_feasibility.ok`` 才算可点；可行性为 None（区间被清空 / 还没
#     评估）时按钮直接灰掉 —— 用户既不能执行也不能重新生成 → 表现「点了没反应」。
# --------------------------------------------------------------------------- #
def test_stale_plan_generate_button_enabled_without_feasibility() -> None:
    window, mw = _make_window()
    _select_one_file(window)
    window._target_panel.set_volume(TOTAL, BPC)   # 默认区间 = 整卷 → window_ok = True
    window._mode = mw.MODE_EXECUTE
    window._plan_stale = True                     # 过期 → 走「生成迁移计划」
    window._feasibility = None                    # 此前为 None 时按钮灰掉（bug）
    window._sync_buttons()
    assert window._action_button.text() == "生成迁移计划"
    assert window._action_button.isEnabled() is True, (
        "可行性为 None 时「生成迁移计划」必须可点（交给 _generate_plan 给原因）"
    )
    calls: list = []
    window._generate_plan = lambda: calls.append("gen")
    window._on_primary_action()
    assert calls == ["gen"], "点击过期态的主按钮应重新发起规划"


# --------------------------------------------------------------------------- #
# ⑮ 1.0.0F 修复①续：可行性为 False 同样不卡灰按钮；点击后 _generate_plan
#     会给出**明确不可行原因**（而非静默无反应）。
# --------------------------------------------------------------------------- #
def test_stale_plan_infeasible_generate_button_explains() -> None:
    window, _mw = _make_window()
    _select_one_file(window)
    window._target_panel.set_volume(TOTAL, BPC)
    window._mode = _mw.MODE_EXECUTE
    window._plan_stale = True
    window._feasibility = _FakeFeasibility(False, ["区间内空闲不足", "挡路者不可驱逐"])
    window._sync_buttons()
    assert window._action_button.text() == "生成迁移计划"
    assert window._action_button.isEnabled() is True, "可行性 False 也不应卡灰按钮"
    window._on_primary_action()                   # 走真实 _generate_plan
    text = window._notice.text()
    assert "不可行" in text, f"应给出明确不可行原因，实际 {text!r}"


# --------------------------------------------------------------------------- #
# ⑯ 1.0.0F 修复②：执行引擎切换改为**直通**——立即提交并发 engineChanged，
#     不再留在「应用配置」暂存里，否则用户切完发现主按钮没反应（实机反馈：
#     流式引擎切完仍是 LBA 的灰按钮「生成迁移计划」）。
# --------------------------------------------------------------------------- #
def test_engine_switch_passthrough_no_pending_stream_button() -> None:
    window, mw = _make_window()
    window._mode = mw.MODE_PREVIEW
    window._target_panel.set_volume(TOTAL, BPC)
    _select_one_file(window)
    assert window._io_panel.engine() == "lba", "默认引擎应为 LBA"
    assert window._has_pending is False
    # 切到「分块流式」——直通，无需点应用
    window._io_panel._engine_switch.set_current(1, emit=True)
    assert window._io_panel.engine() == "stream", "引擎切换必须立即提交"
    assert window._has_pending is False, "引擎直通不应留下待定（不必点应用）"
    window._sync_buttons()
    assert window._action_button.text() == "开始分块流式迁移", (
        "切到流事后主按钮应立即变成流式按钮"
    )
    assert window._action_button.isEnabled() is True


# --------------------------------------------------------------------------- #
# ⑱ D-010 回归钉：分块流式迁移的危险确认框**必须**收到 target_range。
#     1.0.0F 时期 DangerConfirmDialog 的 target_range 改为必填（v2 新增），
#     LBA 迁移 / 回滚调用点都已传，唯独 _start_stream_move 漏传 —— 用户点
#     「开始分块流式迁移」会 TypeError 崩溃。本钉用记录型假对话框捕获构造
#     关键字参，断言 target_range 存在且为占位 (0, 0)，缺参即失败。
# --------------------------------------------------------------------------- #
class _RecordDangerDialog:
    """记录型假对话框：捕获构造关键字参，``exec()`` 一律「拒绝」避免起线程。"""

    last_kwargs: dict = {}

    def __init__(self, *args, **kwargs) -> None:
        _RecordDangerDialog.last_kwargs = dict(kwargs)

    def exec(self):
        return False


def _patch_record_danger_dialog(mw):
    original = mw.dialogs.DangerConfirmDialog
    _RecordDangerDialog.last_kwargs = {}
    mw.dialogs.DangerConfirmDialog = _RecordDangerDialog
    return original


def test_stream_move_dialog_receives_target_range() -> None:
    window, mw = _make_window()
    window._io_panel._engine_switch.set_current(1, emit=True)  # → 分块流式
    _select_one_file(window)

    original = _patch_record_danger_dialog(mw)
    try:
        window._start_stream_move()
    finally:
        mw.dialogs.DangerConfirmDialog = original

    kwargs = _RecordDangerDialog.last_kwargs
    assert kwargs, "分块流式迁移必须弹出危险确认框（构造 DangerConfirmDialog）"
    assert "target_range" in kwargs, (
        "DangerConfirmDialog 必填参数 target_range 缺失 → 真机会 TypeError 崩溃"
    )
    assert kwargs["target_range"] == (0, 0), (
        f"流式无固定目标区间应传占位 (0,0)，实际 {kwargs['target_range']}"
    )


# --------------------------------------------------------------------------- #
# ⑰ 1.0.0F 修复②续：io_panel._emit_pending 发**实算**的待定（不再硬编码 True）。
#     此前硬编码 True 会让引擎直通后的补刷把「配置未应用」徽章假亮。
# --------------------------------------------------------------------------- #
def test_emit_pending_real_state_not_hardcoded() -> None:
    _ = _app()
    from app.ui.io_panel import IoPanel, ENGINE_STREAM

    panel = IoPanel()
    pending: list = []
    panel.pendingChanged.connect(lambda p, c: pending.append((p, c)))
    engine_signals: list = []
    panel.engineChanged.connect(lambda e: engine_signals.append(e))

    # 引擎直通：commit 后控件值 == 已提交值 → 实算待定应为 (False, 0)
    panel._engine_switch.set_current(1, emit=True)
    assert panel.engine() == ENGINE_STREAM
    assert panel.is_pending() is False
    assert pending and pending[-1] == (False, 0), (
        f"引擎直通后应实算为无待定，实际 {pending}"
    )
    assert engine_signals == [ENGINE_STREAM], "应发出 engineChanged"

    # 真正的待定变更：流式缓存模式 内存 → 硬盘（index 0 → 1）
    panel._stream_cache_switch.set_current(1, emit=True)
    assert panel.is_pending() is True, "改了未提交值应进入待定"
    assert pending[-1] == (True, 1), f"应实算为有 1 项待定，实际 {pending}"


# --------------------------------------------------------------------------- #
# D-006 回归钉：流水句柄释放（切换卷 / 回滚完成）
# --------------------------------------------------------------------------- #
def test_release_journal_closes_handle_and_clears_reference() -> None:
    """``_release_journal`` 必须**先关句柄**再丢引用，且幂等、不抛异常。

    背景（D-006）：``MoveJournal`` 内部持有打开的 JSONL 文件句柄，早期实现把
    ``self._journal`` 直接置 ``None``，句柄要泄漏到 GC 回收为止。
    """
    import tempfile

    from pathlib import Path

    from app.core.journal import JournalEntry, MoveJournal

    window, _mw = _make_window()
    with tempfile.TemporaryDirectory() as folder:
        journal = MoveJournal(Path(folder) / "journal-R-test.jsonl")
        journal.append(
            JournalEntry(
                phase="place", path="R:\\a.bin", vcn=0,
                src_lcn=10, dst_lcn=5000, clusters=4,
            )
        )
        handle = journal._handle
        assert handle is not None and not handle.closed, "前置条件：句柄此刻是打开的"

        window._journal = journal
        window._journal_path = str(journal.path)

        window._release_journal()

        assert journal._closed is True, "流水必须已被 close()"
        assert journal._handle is None, "文件句柄必须已释放（不许泄漏到 GC）"
        assert window._journal is None
        assert window._journal_path == ""

        # 幂等：重复调用 / 已关闭的情况下都不许二次抛错
        window._release_journal()
        window._release_journal()

        # close 会强制 fsync —— 关闭前写的内容必须已落盘可读（回滚清单完整性）
        loaded = MoveJournal.load(Path(journal.path))
        assert len(loaded) == 1 and loaded[0].path == "R:\\a.bin"


def test_release_journal_is_reachable_from_both_drop_paths() -> None:
    """两条释放路径都必须调用 ``_release_journal``，不许绕过它直接置空引用。"""
    import inspect

    import app.ui.main_window as mw

    for method in (mw.MainWindow._on_volume_changed, mw.MainWindow._on_rollback_completed):
        source = inspect.getsource(method)
        assert "self._release_journal()" in source, (
            f"{method.__qualname__} 未释放流水句柄（D-006）"
        )
        assert "self._journal = None" not in source, (
            f"{method.__qualname__} 仍在直接把 _journal 置空（绕过 close，句柄泄漏）"
        )


def test_volume_changed_closes_the_open_journal() -> None:
    """功能钉：真的切一次卷，先前打开的流水句柄必须被关掉（不是只丢引用）。"""
    import tempfile

    from pathlib import Path

    from app.core.journal import JournalEntry, MoveJournal
    from app.win32 import storage

    window, _mw = _make_window()
    volumes = [
        VolumeInfo(
            letter="R", root="R:\\", label="T", filesystem="NTFS",
            total_bytes=TOTAL * BPC, bytes_per_cluster=BPC, total_clusters=TOTAL,
            mft_zone_start=1000, mft_zone_end=2000, mft_start_lcn=1000, is_ntfs=True,
        ),
        VolumeInfo(
            letter="S", root="S:\\", label="U", filesystem="NTFS",
            total_bytes=TOTAL * BPC, bytes_per_cluster=BPC, total_clusters=TOTAL,
            mft_zone_start=1000, mft_zone_end=2000, mft_start_lcn=1000, is_ntfs=True,
        ),
    ]
    window._volumes = volumes
    window._volume_combo.clear()
    for volume in volumes:
        window._volume_combo.addItem(volume.display_name)

    original_probe = storage.probe_volume_storage
    storage.probe_volume_storage = lambda letter: (
        storage.MediaProbe(), storage.VolumeDiskMap()
    )
    try:
        with tempfile.TemporaryDirectory() as folder:
            journal = MoveJournal(Path(folder) / "journal-R-before.jsonl")
            journal.append(
                JournalEntry(
                    phase="place", path="R:\\a.bin", vcn=0,
                    src_lcn=10, dst_lcn=5000, clusters=4,
                )
            )
            window._journal = journal
            window._journal_path = str(journal.path)

            window._volume_combo.setCurrentIndex(1)   # 触发 _on_volume_changed

            assert journal._closed is True, "切卷后旧流水句柄必须已关闭（D-006）"
            assert journal._handle is None
            assert window._journal is None
            assert window._journal_path == ""
            assert window._volume is volumes[1]
    finally:
        storage.probe_volume_storage = original_probe


# --------------------------------------------------------------------------- #
# D-012 回归钉：journal 生命周期（迁移完成后必须仍可写 —— 回滚靠它）
#
# 旧 bug：_on_move_completed 把 journal close() 了却保留引用，之后点回滚走
# RollbackWorker(journal=self._journal) ⇒ 第 1 段 FSCTL 已真实执行，紧接着
# journal.record() 撞上 journal.py 的 `if self._closed: raise RuntimeError` ⇒
# 剩余段不执行、整文件报「回滚异常」—— 兜底绳子是断的。
# --------------------------------------------------------------------------- #
class _AllowAllGuard:
    """RollbackWorker / mover 用的最小守卫替身（恒放行）。"""

    def allow(self, lcn: int, clusters: int):
        return True, ""

    def reserve(self, lcn: int, clusters: int) -> None:
        return None


def _window_with_journal(journal):
    """构造「刚跑完一次迁移」的窗口状态（屏蔽位图重载等真实 IO）。"""
    window, mw = _make_window()
    mw.MainWindow._reload_bitmap = lambda self: None   # 免得真的起 BitmapWorker 线程
    window._journal = journal
    window._journal_path = str(journal.path)
    return window


def test_journal_stays_writable_after_move_completed() -> None:
    """D-012 主钉：``_on_move_completed`` 之后流水必须**仍可写**（回滚要写 rollback 行）。"""
    import tempfile

    from pathlib import Path

    from app.core.journal import MoveJournal
    from app.core.models import MoveOutcome

    with tempfile.TemporaryDirectory() as folder:
        journal = MoveJournal(Path(folder) / "journal-R.jsonl")
        journal.record(
            phase="place", path="R:\\a.bin", vcn=0,
            src_lcn=10, dst_lcn=5000, clusters=4, ok=True,
        )
        window = _window_with_journal(journal)

        # ① 迁移完成回调（旧实现在这里就把句柄关了）
        window._on_move_completed(MoveOutcome(moved_clusters=4, moved_files=1))

        # ② 回滚还要往同一条流水里写 —— 旧实现在这一步抛 RuntimeError
        journal.record(
            phase="rollback", path="R:\\a.bin", vcn=0,
            src_lcn=5000, dst_lcn=10, clusters=4, ok=True,
        )

        assert journal.record_count == 2, "place + rollback 两行都必须成功写入"
        phases = [entry.phase for entry in MoveJournal.load(Path(journal.path))]
        assert phases == ["place", "rollback"], phases
        assert journal._closed is False, "迁移完成 ≠ 会话结束：句柄必须保持可用"
        assert window._journal is journal, "引用必须留给回滚用"

        # 会话收尾（顺带证明：修复后句柄确实还开着 —— Windows 上正是
        # "句柄未关 ⇒ 临时目录删不掉"这一行为，反过来印证 inspect 判据）
        journal.close()
        assert journal._closed is True


def test_move_completed_keeps_records_durable() -> None:
    """D-012 耐久性守卫：不 close 也必须把已写记录刷到盘上（``load()`` 能全量读回）。"""
    import tempfile

    from pathlib import Path

    from app.core.journal import MoveJournal
    from app.core.models import MoveOutcome

    with tempfile.TemporaryDirectory() as folder:
        journal = MoveJournal(Path(folder) / "journal-R.jsonl")
        for src_lcn, clusters in ((10, 4), (20, 6)):
            journal.record(
                phase="place", path="R:\\a.bin", vcn=0,
                src_lcn=src_lcn, dst_lcn=5000 + src_lcn, clusters=clusters, ok=True,
            )
        window = _window_with_journal(journal)
        # 探针：盯住 force_fsync 有没有真的被调用（防止后人"省事式修复"——
        # 直接把 close() 删掉而不补刷盘，那样 host 掉电时回滚清单会不完整）
        fsync_calls = {"n": 0}
        original_fsync = journal.force_fsync

        def _spy():
            fsync_calls["n"] += 1
            return original_fsync()

        journal.force_fsync = _spy

        window._on_move_completed(MoveOutcome(moved_clusters=10, moved_files=1))

        assert fsync_calls["n"] >= 1, "完成回调必须显式强制刷盘（durability 不许丢）"
        entries = MoveJournal.load(Path(journal.path))
        assert len(entries) == 2, f"两条流水都必须已落盘可读，实际 {len(entries)}"
        assert all(entry.ok for entry in entries)
        assert sorted(entry.clusters for entry in entries) == [4, 6]
        # 「执行与恢复」面板的回滚量也来自落盘内容 —— 数字必须正确
        assert journal.rollback_clusters() == 10

        journal.close()      # 会话收尾


def test_rollback_writer_can_continue_on_same_journal() -> None:
    """D-012：RollbackWorker 走的**真实写入链路**（mover → journal.record）仍能写。

    用 mover 的 win32 替身（不碰哪个 handle），但流水是**真的** MoveJournal。
    """
    import tempfile

    from pathlib import Path

    from app.core.journal import MoveJournal
    from app.core.models import MoveOutcome, MoveSegment
    from app.win32 import mover

    with tempfile.TemporaryDirectory() as folder:
        journal = MoveJournal(Path(folder) / "journal-R.jsonl")
        journal.record(
            phase="place", path="R:\\a.bin", vcn=0,
            src_lcn=10, dst_lcn=5000, clusters=4, ok=True,
        )
        window = _window_with_journal(journal)
        window._on_move_completed(MoveOutcome(moved_clusters=4, moved_files=1))

        original_move, original_open = mover.move_clusters, mover.open_file_for_move
        mover.move_clusters = lambda *args, **kwargs: None
        mover.open_file_for_move = lambda path: 7
        try:
            outcome = mover.rollback_segments(
                0xBEEF,
                "R:\\a.bin",
                [MoveSegment(vcn=0, src_lcn=5000, dst_lcn=10, clusters=4)],
                guard=_AllowAllGuard(),
                policy=None,
                journal=window._journal,
            )
        finally:
            mover.move_clusters = original_move
            mover.open_file_for_move = original_open

        assert outcome.failures == [], f"回滚应零失败（旧实现会在这里挂 RuntimeError）：{outcome.failures}"
        assert outcome.moved_clusters == 4
        assert outcome.moved_files == 1
        written = [entry.phase for entry in MoveJournal.load(Path(journal.path))]
        assert written == ["place", "rollback"], written

        journal.close()      # 会话收尾（真实会话由 closeEvent / _release_journal 负责）


def test_close_event_still_closes_the_journal() -> None:
    """D-012 交叉守卫：不在完成时关了，**关窗**路径必须补上（不许把 D-006 泄漏引回来）。"""
    import tempfile

    from pathlib import Path

    from PySide6.QtGui import QCloseEvent

    from app.core.journal import MoveJournal
    from app.core.models import MoveOutcome

    with tempfile.TemporaryDirectory() as folder:
        journal = MoveJournal(Path(folder) / "journal-R.jsonl")
        journal.record(
            phase="place", path="R:\\a.bin", vcn=0,
            src_lcn=10, dst_lcn=5000, clusters=4, ok=True,
        )
        window = _window_with_journal(journal)
        window._on_move_completed(MoveOutcome(moved_clusters=4, moved_files=1))
        assert journal._closed is False, "完成回调只是 fsync，不应关闭句柄"

        window.closeEvent(QCloseEvent())

        assert journal._closed is True, "关窗必须补最后一刀（否则 D-006 句柄泄漏复辟）"
        assert journal._handle is None


def test_new_migration_releases_previous_journal() -> None:
    """D-012 审计补漏：开启新一轮迁移前必须先释放上一条流水。

    D-012 之后流水**不在完成时关闭**了（要留给回滚写 rollback 行），于是
    「执行 → 完成 → 再执行一次」会丢掉一个未关闭的句柄 = D-006 泄漏复辟。
    """
    import inspect

    import app.ui.main_window as mw

    source = inspect.getsource(mw.MainWindow._start_move)
    assert "self._release_journal()" in source, (
        "_start_move 未释放上一条流水句柄（D-012 之后会泄漏）"
    )
    assert source.index("self._release_journal()") < source.index("self._journal = journal"), (
        "必须先释放旧流水，再挂上新流水"
    )


if __name__ == "__main__":
    from tests._harness import exit_with

    exit_with(globals())
