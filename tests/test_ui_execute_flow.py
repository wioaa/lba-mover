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
# ⑦ apply 后规划撞 early-return：窗口不留悬挂意图（QA ①）
# --------------------------------------------------------------------------- #
def test_early_return_after_apply_does_not_leave_intent() -> None:
    window, _mw = _make_window()
    # 一个文件都没勾选 → _generate_plan 在建 worker 之前就 early-return
    window._resume_execute_after_plan = True
    window._generate_plan()

    assert window._resume_execute_after_plan is False, (
        "规划早退不得留下悬挂意图（否则下次普通规划会意外弹危险确认框）"
    )
    assert window._plan_worker is None, "早退时不该创建规划线程"
    assert "自动执行已取消" in window._notice.text(), window._notice.text()


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
# --------------------------------------------------------------------------- #
def test_plan_cancel_fallback_clears_busy() -> None:
    window, _mw = _make_window()
    worker = _FakePlanWorker()
    worker.resume_after = True
    window._plan_worker = worker
    window._set_busy(True)

    window._on_plan_worker_finished()      # 模拟：线程结束且没发 completed/failed

    assert window._plan_worker is None, "取消后必须放掉 worker 槽位"
    assert window._busy is False, "取消后必须退出忙碌态（否则进度条一直转、按钮全禁）"
    assert worker.resume_after is False, "被取消的规划不得保留自动执行意图"
    assert window._resume_execute_after_plan is False
    assert "已取消" in window._notice.text(), window._notice.text()


# --------------------------------------------------------------------------- #
# ⑪ 正常收尾后 finished 必须是 no-op（不许误清别的任务的忙碌态）
# --------------------------------------------------------------------------- #
def test_plan_finished_is_noop_after_terminal_signal() -> None:
    window, _mw = _make_window()
    window._plan_worker = None            # completed / failed 已收尾
    window._busy = True                   # 假设别的任务正忙
    window._on_plan_worker_finished()
    assert window._busy is True, "没有未收尾的规划线程时 finished 必须什么都不做"


if __name__ == "__main__":
    from tests._harness import exit_with

    exit_with(globals())
