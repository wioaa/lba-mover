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


if __name__ == "__main__":
    from tests._harness import exit_with

    exit_with(globals())
