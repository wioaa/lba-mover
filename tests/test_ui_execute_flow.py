"""执行流程回归钉（1.0.0D 修复：「先应用再执行」不执行 / 静默返回）。

用户实机反馈「执行模式下点开始迁移却不执行」。根因是待定配置闸门里的 apply
分支只提示"请重新生成计划"就 return，标签承诺与实际行为不符。本文件钉住：

1. 选「先应用再执行」→ 置 `_resume_execute_after_plan` 并**发起重新规划**
   （`_generate_plan` 被调用），不在原地继续；
2. 新计划就绪且标志为真 → **自动接续** `_start_move`（它仍会再走
   DangerConfirmDialog，不跳过人工确认），接续后标志复位且只接续一次；
3. 标志为假 → `_on_plan_ready` 不触发 `_start_move`；
4. 选「按现有计划执行」（proceed）→ 不设置该标志；
5. 规划失败 → 标志被清（不悬挂）；
6. 开头静默 return 补提示（卷/位图未就绪、缺计划两种）。

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


if __name__ == "__main__":
    from tests._harness import exit_with

    exit_with(globals())
