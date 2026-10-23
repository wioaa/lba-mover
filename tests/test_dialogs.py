"""B-1 回归钉：``ChoiceDialog.choice`` 必须存**代号**而不是按钮文字。

缺陷复述（QA `docs/QA-REPORT-1.0.0D-commits.md` B-1）：早期实现把 ``choices``
当成 ``(文字, 角色)`` 二元组，``choice`` 被存成按钮文字 —— 调用方按代号比对的
两个动作分支（"apply" / "proceed"）**永远不可达**，执行确认框的
「按现有计划执行」逃生门实际不可用。本文件钉死：`choices` 为
``(代号, 文字, 角色)`` 三元组，``_pick(i)`` 后 ``choice`` 必须等于第 i 项的代号。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.ui.dialogs import ChoiceDialog  # noqa: E402


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


_CHOICES = [
    ("apply", "先应用再执行", "primary"),
    ("proceed", "按现有计划执行", "danger"),
    ("cancel", "取消", "secondary"),
]


def test_choice_stores_code_not_text() -> None:
    """点击第 i 个按钮 → ``choice`` 必须是第 i 项的**代号**。"""
    _ = _app()
    dialog = ChoiceDialog("t", "m", choices=_CHOICES, kind="warn")
    dialog._pick(0)
    assert dialog.choice == "apply", f"应存代号 'apply'，实际 {dialog.choice!r}"
    dialog2 = ChoiceDialog("t", "m", choices=_CHOICES, kind="warn")
    dialog2._pick(1)
    assert dialog2.choice == "proceed", f"应存代号 'proceed'，实际 {dialog2.choice!r}"
    dialog3 = ChoiceDialog("t", "m", choices=_CHOICES, kind="warn")
    dialog3._pick(2)
    assert dialog3.choice == "cancel"


def test_choice_none_when_rejected() -> None:
    """未点按钮（Esc / 关闭）→ ``choice`` 保持 ``None``。"""
    _ = _app()
    dialog = ChoiceDialog("t", "m", choices=_CHOICES, kind="warn")
    assert dialog.choice is None


def test_choice_codes_match_choices() -> None:
    """``_codes`` 与 ``choices`` 的代号列逐一对应（渲染用文字，判定用代号）。"""
    _ = _app()
    from PySide6.QtWidgets import QPushButton

    dialog = ChoiceDialog("t", "m", choices=_CHOICES, kind="warn")
    assert dialog._codes == ["apply", "proceed", "cancel"]
    # 渲染层：按钮文字必须来自第二列（不是代号）
    texts = [button.text() for button in dialog.findChildren(QPushButton)]
    assert texts == ["先应用再执行", "按现有计划执行", "取消"]


if __name__ == "__main__":
    from tests._harness import exit_with

    exit_with(globals())
