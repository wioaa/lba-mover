"""扫描 / 归属索引「取消」语义回归钉（1.0.0E · QA P2）。

**背景（修复前的事实错误）**：取消走的是**成功**分支 —— ``MftScanOutcome.ok`` 的语义
是「数据可用」而不是「没被取消」，所以取消后 ``ok`` 仍为 ``True``，
``MftScanWorker.run`` 进的是 ``completed``（不是 ``_emit_fallback``）。
于是 UI 收到 ``completed`` 就把「用户取消」显示成「扫描完成」，并交出一份
**不完整**的文件列表，零提示；``outcome.notes`` 里的「扫描已取消（结果不完整）」
从不进入任何信号，用户永远看不到。句柄通道 ``ScanWorker`` 的取消路径同样只是
``completed.emit(部分结果)``，``IndexWorker`` 取消则仍会报「归属索引就绪」。

本文件钉住修复后的口径（**取消 ≠ 失败，也 ≠ 完成**）：

1. ``MftScanOutcome.cancelled`` 默认 ``False``；置位后 ``ok`` 仍为 ``True``
   （不把取消当失败，已解析数据仍可用）；
2. ``MftScanWorker`` 取消：``completed`` 发一次、``fallback``/``failed`` 都不发，
   worker 暴露 ``cancelled``，进度文案带「已取消」；
3. ``ScanWorker.cancelled`` 反映 ``request_cancel()``；
4. UI ``_on_scan_completed`` 取消时**不**报「扫描完成」、不自动勾选、不建归属索引、
   且退出忙碌态；正常路径行为不变（对照）；
5. UI ``_on_index_ready`` 取消时**不**报「就绪」、不把部分索引当归属依据。

全部离屏运行，不启动线程、不碰设备句柄。涉及 ``self.sender()`` 的槽一律用
**真 Qt 信号**驱动 —— 纯 Python 替身的 ``emit()`` 不设 sender，直调更是
``sender() is None``，会让断言失真（这个坑本项目已经踩过三次）。
"""

from __future__ import annotations

import ast
import inspect
import os
import sys
import textwrap
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core import mft as mft_mod  # noqa: E402
from app.core import scanner as scanner_mod  # noqa: E402
from app.core.models import (  # noqa: E402
    BitmapSnapshot,
    Extent,
    FileEntry,
    MovePlan,
    VolumeInfo,
)
from app.win32 import mft_reader  # noqa: E402

TOTAL = 1_000_000
BPC = 4096


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _fake_volume() -> VolumeInfo:
    return VolumeInfo(
        letter="R", root="R:\\", label="T", filesystem="NTFS",
        total_bytes=TOTAL * BPC, bytes_per_cluster=BPC, total_clusters=TOTAL,
        mft_zone_start=1000, mft_zone_end=2000, mft_start_lcn=1000, is_ntfs=True,
    )


def _fake_entry() -> FileEntry:
    return FileEntry(
        path="R:\\a.bin", name="a.bin", size=4 * BPC,
        extents=[Extent(vcn=0, lcn=10, clusters=4)], movable=True,
    )


def _messages(window) -> str:
    """把主窗口日志面板里的全部记录拼成一串，便于断言文案。"""
    return "\n".join(record[2] for record in window._log.messages())


class _ScanSource(QObject):
    """带真 Qt 信号的替身 worker：只有真信号才能让槽里的 ``sender()`` 生效。"""

    completed = Signal(object)
    ready = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.cancelled = False


def _make_window():
    """构造一个带假卷 / 假位图 / 假计划的 MainWindow（屏蔽真实卷加载）。"""
    _ = _app()
    import app.ui.main_window as mw

    mw.MainWindow._load_volumes = lambda self: None   # 免得真去枚举本机卷
    window = mw.MainWindow()
    window._volume = _fake_volume()
    window._snapshot = BitmapSnapshot(
        bits=bytes(TOTAL // 8), total_clusters=TOTAL, bytes_per_cluster=BPC
    )
    window._plan = MovePlan(items=[], total_clusters=TOTAL)
    return window, mw


# --------------------------------------------------------------------------- #
# ① 取消不是失败：ok 语义保持「数据可用」
# --------------------------------------------------------------------------- #
def test_mft_outcome_cancel_is_not_failure() -> None:
    outcome = mft_reader.MftScanOutcome()
    assert outcome.cancelled is False, "默认不得是取消态"
    assert outcome.ok is True

    outcome.cancelled = True
    assert outcome.ok is True, "取消不是失败：ok「数据可用」语义不变"

    outcome.fallback_code = mft_mod.FALLBACK_NO_PRIVILEGE
    assert outcome.ok is False, "只有回退码才让通道「不可用」"


# --------------------------------------------------------------------------- #
# ①c 调用点钉子：`scan_volume_mft` 的**每一处**取消分支都必须真的接线
#     动机（QA 提的第 5 次同类漏洞，上移一层）：①b 钉的是函数本身，如果调用点
#     被删掉（`break` 留着），函数钉子仍全绿而取消语义又丢了。事件型分支的
#     "有没有接线"只能靠调用点钉子 —— 用 AST 精确锚定，不用设备、成本极低。
#
#     钉「接线 + 顺序」，不钉「分支数量」（QA 6 组变异审计后重写，`3d2aa27` 那版
#     有两个假绿 + 一个假红）：
#       * 用**直接子语句**而不是 `ast.walk` —— 嵌套结构一律不算接线，于是
#         `if False:` / `while False:` 包住的调用（行为上不可达）会被判红；
#       * 比较**下标顺序** —— `mark_cancelled` 排在 `break` 之后（死代码）判红；
#       * 不要求分支唯一 —— 将来在采样 / 路径重建阶段再加一个**正确**的取消分支
#         不该红，要防的是「没接线」而不是「只许一个分支」；
#       * 别名重写（`probe = should_cancel`）会红 —— 这是**有意的**：AST 认名字，
#         跟随不了别名；失败方向是「响」而非「哑」，符合我们的取舍（按误判方向的
#         危害选择失败方向）。**改结构时必须同步改这枚钉子，别把它删掉。**
# --------------------------------------------------------------------------- #
def _cancel_branches(tree: ast.AST) -> list:
    """所有「test 里出现 `should_cancel`」的 ``if`` 节点（不限制数量）。"""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and any(
            isinstance(sub, ast.Name) and sub.id == "should_cancel"
            for sub in ast.walk(node.test)
        )
    ]


def _first_index(body: list, predicate) -> int | None:
    """返回 ``body`` 里第一个满足 ``predicate`` 的**直接子语句**下标。"""
    return next((index for index, stmt in enumerate(body) if predicate(stmt)), None)


def _is_mark_cancelled_call(stmt: ast.AST) -> bool:
    """该**直接子语句**是不是一次 ``mark_cancelled(...)`` 调用。"""
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Call)
        and getattr(stmt.value.func, "id", "") == "mark_cancelled"
    )


def test_scan_volume_mft_cancel_branch_calls_mark_cancelled() -> None:
    source = textwrap.dedent(inspect.getsource(mft_reader.scan_volume_mft))
    tree = ast.parse(source)

    branches = _cancel_branches(tree)
    assert branches, (
        "`scan_volume_mft` 里找不到任何以 `should_cancel` 为守卫的取消分支"
        "——取消能力被整体删掉，或守卫改名/被抽走（AST 认名字，跟不了别名，"
        "见本钉子 docstring）"
    )

    for node in branches:
        call_at = _first_index(node.body, _is_mark_cancelled_call)
        break_at = _first_index(node.body, lambda stmt: isinstance(stmt, ast.Break))
        line = getattr(node, "lineno", "?")
        assert call_at is not None, (
            f"取消分支（第 {line} 行起）没有**直接**调用 `mark_cancelled(...)`："
            "只 break 不置位会让 UI 把用户取消当成扫描完成；"
            "注意包在 `if False:` / 嵌套块里的调用不算接线（行为上不可达）"
        )
        assert break_at is not None, (
            f"取消分支（第 {line} 行起）没有 `break` —— 取消后不会真的中断解析"
        )
        assert call_at < break_at, (
            f"取消分支（第 {line} 行起）里 `mark_cancelled(...)` 排在 `break` 之后，"
            "是死代码（永远执行不到）"
        )


# --------------------------------------------------------------------------- #
# ①b mft_reader 的取消留痕（`mark_cancelled` 无设备依赖，可离线钉住）
#     动机：`MftScanWorker` 那两个钉子用替身桩掉了 `scan_volume_mft`，覆盖不到
#     「取消分支到底有没有置 cancelled」这一行 —— 变异回「只 break 不置位」时
#     钉子仍全绿（我实测过）。抽出纯函数后这条就能被钉死。
# --------------------------------------------------------------------------- #
def test_mft_reader_marks_cancel_on_outcome() -> None:
    outcome = mft_reader.MftScanOutcome()
    mft_reader.mark_cancelled(outcome)

    assert outcome.cancelled is True, "取消分支必须置 cancelled（否则上层报「完成」）"
    assert outcome.notes, "取消必须在 notes 留痕（「结果不完整」）"
    assert "已取消" in outcome.notes[-1], outcome.notes
    assert outcome.ok is True, "取消不是失败：不得把通道判为不可用"
    assert outcome.fallback_code == "", "取消不得污染 fallback_code（否则会被当成回退）"


# --------------------------------------------------------------------------- #
# ② MftScanWorker 取消：completed 一次、无 fallback/failed、文案带「已取消」
# --------------------------------------------------------------------------- #
def test_mft_scan_worker_cancel_reports_completed_with_flag() -> None:
    entry = _fake_entry()
    original_channel = scanner_mod.build_scan_channel
    original_scan = mft_reader.scan_volume_mft
    holder: dict = {}

    def _fake_scan(*_args, **_kwargs):
        outcome = mft_reader.MftScanOutcome(entries=[entry])
        mft_reader.mark_cancelled(outcome)          # 走真实留痕逻辑
        holder["outcome"] = outcome
        return outcome

    scanner_mod.build_scan_channel = lambda *_a, **_kw: ("mft", "")
    mft_reader.scan_volume_mft = _fake_scan

    completed: list = []
    fallback: list = []
    failed: list = []
    progress: list = []
    found: list = []
    worker = scanner_mod.MftScanWorker(
        _fake_volume(),
        scanner_mod.ScanConfig(root="R:\\", min_size=1, max_matches=10),
        sample_size=32,
    )
    worker.completed.connect(completed.append)
    worker.fallback.connect(fallback.append)
    worker.failed.connect(failed.append)
    worker.found.connect(found.append)
    worker.progress.connect(lambda _d, _t, text: progress.append(text))
    try:
        worker.run()                       # 直接跑 run：不建线程
    finally:
        scanner_mod.build_scan_channel = original_channel
        mft_reader.scan_volume_mft = original_scan

    outcome = holder["outcome"]
    assert len(completed) == 1, f"取消仍应发一次 completed（部分结果），实际 {completed}"
    assert fallback == [], "取消不是回退：不得发 fallback（修复前被误认为会发）"
    assert failed == [], "取消不是失败：不得发 failed"
    assert worker.cancelled is True, "worker 必须向 UI 暴露取消态"
    assert completed[0] == [entry], "已解析的部分结果要如数交给上层"
    assert len(found) == 1, "found 推送的部分结果不得丢失"

    # ---- QA 8 条判据：1（文案互斥）/ 2（N 对齐）/ 3（不污染 fallback_code）----
    text = " ".join(progress)
    assert "已取消" in text, f"进度文案必须明说已取消，实际 {progress}"
    assert "完成" not in text, f"取消文案不得出现「完成」，实际 {progress}"
    assert f"已解析的 {len(completed[0]):,} 项" in text, (
        f"提示里的 N 必须等于 completed 携带的条目数，实际 {progress}"
    )
    # ---- 判据 3/4/6：不被当成回退、不降级通道、不写 fatal_error ----
    assert outcome.fallback_code == "", (
        f"取消不得污染 fallback_code（否则日志把用户取消记成通道失效），"
        f"实际 {outcome.fallback_code!r}"
    )
    assert outcome.channel == "mft", "取消不是降级：channel 不得切成 handle"
    assert outcome.stats.fatal_error == "", "fatal_error 只用于真实失败"


# --------------------------------------------------------------------------- #
# ③ ScanWorker（句柄通道）暴露取消态
# --------------------------------------------------------------------------- #
def test_scan_worker_exposes_cancelled() -> None:
    worker = scanner_mod.ScanWorker(
        scanner_mod.ScanConfig(root="R:\\", min_size=1, max_matches=10),
        None,
        info=_fake_volume(),
        mode="handle",
    )
    assert worker.cancelled is False
    worker.request_cancel()
    assert worker.cancelled is True, "句柄通道取消后必须能被上层识别"


def test_index_worker_exposes_cancelled() -> None:
    worker = scanner_mod.IndexWorker(
        _fake_volume(), scanner_mod.ScanConfig(root="R:\\", min_size=1, max_matches=10)
    )
    assert worker.cancelled is False
    worker.request_cancel()
    assert worker.cancelled is True, "索引取消后必须能被上层识别"


# --------------------------------------------------------------------------- #
# ④ UI：扫描取消 → 不报「完成」、不勾选、不建索引、退出忙碌态
# --------------------------------------------------------------------------- #
def test_ui_scan_completed_reports_cancelled() -> None:
    window, _mw = _make_window()
    built: list = []
    window._build_index = lambda: built.append("index")
    window._set_busy(True)

    source = _ScanSource()
    source.cancelled = True
    source.completed.connect(window._on_scan_completed)
    source.completed.emit([_fake_entry()])

    joined = _messages(window)
    assert "扫描完成" not in joined, f"取消不得被报成完成，实际日志：{joined!r}"
    assert "已取消" in joined, f"取消必须留痕，实际日志：{joined!r}"
    assert built == [], "取消后不得拿部分数据建全卷归属索引"
    assert window._busy is False, "取消后必须退出忙碌态（否则按钮全禁）"
    assert "已取消" in window._notice.text(), (
        f"取消必须有可见提示（不许静默），实际 {window._notice.text()!r}"
    )
    assert window._table.file_model.rowCount() == 1, "已解析到的条目仍应留在表里"
    assert window._table.file_model.checked_count() == 0, (
        "取消时的部分结果不得被当成完整全集自动勾选"
    )


def test_ui_scan_completed_normal_path_unchanged() -> None:
    window, _mw = _make_window()
    built: list = []
    window._build_index = lambda: built.append("index")
    window._set_busy(True)

    source = _ScanSource()                 # cancelled 保持 False
    source.completed.connect(window._on_scan_completed)
    source.completed.emit([_fake_entry()])

    joined = _messages(window)
    assert "扫描完成" in joined, f"正常路径口径不得改变，实际日志：{joined!r}"
    assert built == ["index"], "正常完成仍应建归属索引"
    assert window._table.file_model.checked_count() == 1, "正常完成仍应自动全选"


# --------------------------------------------------------------------------- #
# ⑤ UI：归属索引取消 → 不报「就绪」、不把部分索引当归属依据
# --------------------------------------------------------------------------- #
def _fake_index_payload(notes=None):
    return types.SimpleNamespace(
        entries=[],
        index=object(),
        channel="mft",
        total_files=10,
        total_extents=20,
        elapsed=0.5,
        notes=list(notes or []),
    )


def test_ui_index_ready_reports_cancelled() -> None:
    window, _mw = _make_window()
    window._set_busy(True)
    payload = _fake_index_payload(notes=["已取消"])

    source = _ScanSource()
    source.cancelled = True
    source.ready.connect(window._on_index_ready)
    source.ready.emit(payload)

    joined = _messages(window)
    assert "归属索引就绪" not in joined, f"取消不得被报成就绪，实际日志：{joined!r}"
    assert "归属索引已取消" in joined, f"取消必须留痕，实际日志：{joined!r}"
    assert window._ownership is None, "部分索引不得当作归属依据（挡路者归属会缺项）"
    assert window._busy is False, "取消后必须退出忙碌态"
    assert window._index_worker is None


def test_ui_index_ready_normal_path_unchanged() -> None:
    window, _mw = _make_window()
    payload = _fake_index_payload()

    source = _ScanSource()                 # cancelled 保持 False
    source.ready.connect(window._on_index_ready)
    source.ready.emit(payload)

    joined = _messages(window)
    assert "归属索引就绪" in joined, f"正常路径口径不得改变，实际日志：{joined!r}"
    assert window._ownership is payload.index, "正常完成应设置归属索引"


# --------------------------------------------------------------------------- #
# ⑥ 直调兜底（第 4 次 `sender()` 陷阱的专用钉子）
#     动机：`sender()` 只在信号派发期间有值；测试直调 / 换信号源时它是 None。
#     若只读 sender，取消会被静默当成「完成 / 就绪」，而直调式测试还会假绿
#     —— 所以两处都读 `_xxx_worker` 兜底。这两枚钉子专防第 4 次复发。
# --------------------------------------------------------------------------- #
def test_ui_scan_completed_direct_call_uses_worker_fallback() -> None:
    window, _mw = _make_window()
    built: list = []
    window._build_index = lambda: built.append("index")
    window._set_busy(True)
    worker = _ScanSource()
    worker.cancelled = True
    window._scan_worker = worker               # 兜底来源（直调时 sender() 为 None）

    window._on_scan_completed([_fake_entry()])  # 直调，不是信号驱动

    joined = _messages(window)
    assert "扫描完成" not in joined, f"直调也不得把取消报成完成，实际：{joined!r}"
    assert "已取消" in joined, f"直调也必须报取消，实际：{joined!r}"
    assert "已取消" in window._notice.text(), window._notice.text()
    assert built == [], "取消路径不得建索引（直调同样）"
    assert window._busy is False


def test_ui_index_ready_direct_call_uses_worker_fallback() -> None:
    window, _mw = _make_window()
    window._set_busy(True)
    worker = _ScanSource()
    worker.cancelled = True
    window._index_worker = worker              # 兜底来源（且必须在置 None 之前读）

    window._on_index_ready(_fake_index_payload())   # 直调

    joined = _messages(window)
    assert "归属索引就绪" not in joined, f"直调也不得把取消索引报成就绪，实际：{joined!r}"
    assert "归属索引已取消" in joined, f"直调也必须报取消，实际：{joined!r}"
    assert window._ownership is None, "取消索引（直调）也不得写入归属表"
    assert window._busy is False


if __name__ == "__main__":
    from tests._harness import exit_with

    exit_with(globals())
