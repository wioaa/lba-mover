"""T04 验收 · 执行引擎（``app/win32/mover.py`` + ``app/core/journal.py`` 契约）。

**这些用例绝不执行真实 ``FSCTL_MOVE_FILE``**：要么把 ``mover.move_clusters``
换成计数器，要么用 ``guard.allow() == False`` 让流程根本走不到写调用。
"""

from __future__ import annotations

import os
import sys
import tempfile
from typing import List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.journal import MoveJournal  # noqa: E402
from app.core.models import (  # noqa: E402
    FileEntry,
    MovePlan,
    MoveSegment,
    MoveTask,
    PlanItem,
)
from app.win32 import api, mover, retrieval  # noqa: E402


# --------------------------------------------------------------------------- #
# 测试替身
# --------------------------------------------------------------------------- #
class _Guard:
    """最小守卫替身：只实现 ``allow`` / ``reserve``。"""

    def __init__(self, allowed: bool = True, reason: str = "测试拒绝") -> None:
        self.allowed = allowed
        self.reason = reason
        self.reserved: List[Tuple[int, int]] = []

    def allow(self, lcn: int, clusters: int) -> Tuple[bool, str]:
        return (self.allowed, "" if self.allowed else self.reason)

    def reserve(self, lcn: int, clusters: int) -> None:
        self.reserved.append((lcn, clusters))


class _MoverStub:
    """把真正的 FSCTL 调用替换成记录器。"""

    def __init__(self, fail_on: Tuple[int, ...] = ()) -> None:
        self.calls: List[Tuple[int, int, int, int]] = []
        self.fail_on = fail_on
        self._original = mover.move_clusters

    def __enter__(self) -> "_MoverStub":
        def fake(volume_handle, file_handle, vcn, dst_lcn, clusters):
            self.calls.append((vcn, dst_lcn, clusters, file_handle))
            if dst_lcn in self.fail_on:
                raise api.Win32Error(api.ERROR_SHARING_VIOLATION, "移动失败（测试）")
            return None

        mover.move_clusters = fake                      # type: ignore[assignment]
        mover.open_file_for_move = lambda path: 7       # type: ignore[assignment]
        return self

    def __exit__(self, *_exc: object) -> None:
        mover.move_clusters = self._original            # type: ignore[assignment]
        mover.open_file_for_move = _ORIGINAL_OPEN       # type: ignore[assignment]


_ORIGINAL_OPEN = mover.open_file_for_move


def _segments(count: int = 3, clusters: int = 10) -> List[MoveSegment]:
    return [
        MoveSegment(vcn=index * clusters, src_lcn=1000 + index * clusters,
                    dst_lcn=5000 + index * clusters, clusters=clusters)
        for index in range(count)
    ]


# --------------------------------------------------------------------------- #
# 路径未知必须被拦下（验收 8）
# --------------------------------------------------------------------------- #
def test_execute_task_rejects_unknown_path_without_any_write() -> None:
    task = MoveTask(path="(record 33)", path_known=False, segments=_segments(2))
    with _MoverStub() as stub:
        outcome = mover.execute_task(0xBEEF, task, _Guard(True), None, None)
    assert stub.calls == []
    assert len(outcome.failures) == 1
    assert outcome.failures[0].winerror == api.ERROR_INVALID_PARAMETER
    assert "路径未知" in outcome.failures[0].message


def test_execute_task_rejects_placeholder_path_even_if_flag_says_ok() -> None:
    """``path_known`` 被上游写错也拦得住（双保险）。"""
    task = MoveTask(path="(record 7)", path_known=True, segments=_segments(1))
    with _MoverStub() as stub:
        outcome = mover.execute_task(0xBEEF, task, _Guard(True), None, None)
    assert stub.calls == []
    assert outcome.failures


# --------------------------------------------------------------------------- #
# 守卫拒绝 ⇒ 零写调用（验收 1）
# --------------------------------------------------------------------------- #
def test_guard_rejection_never_calls_move() -> None:
    task = MoveTask(path="R:\\ok.bin", segments=_segments(3))
    guard = _Guard(False, "目标区间未通过安全校验（测试）")
    with _MoverStub() as stub:
        outcome = mover.execute_task(0xBEEF, task, guard, None, None)
    assert stub.calls == []
    assert len(outcome.failures) == 3
    for failure in outcome.failures:
        assert failure.winerror == api.ERROR_INVALID_PARAMETER
        assert "安全校验" in failure.message


def test_missing_guard_and_validator_refuses_to_execute() -> None:
    """既没 guard 也没 validate_target ⇒ 拒绝执行（没有校验就没有移动）。"""
    with _MoverStub() as stub:
        outcome = mover.execute_segments(0xBEEF, "R:\\x.bin", _segments(2), None, None, None)
    assert stub.calls == []
    assert outcome.failures and "拒绝执行" in outcome.failures[0].message


# --------------------------------------------------------------------------- #
# 进度契约（验收 2）
# --------------------------------------------------------------------------- #
def test_progress_is_monotonic_and_ends_at_total() -> None:
    segments = _segments(4)
    total = sum(segment.clusters for segment in segments)
    seen: List[Tuple[int, int]] = []
    with _MoverStub(fail_on=(5010,)) as stub:      # 中间一段故意失败
        outcome = mover.execute_segments(
            0xBEEF,
            "R:\\a.bin",
            segments,
            None,
            None,
            lambda done, total_, index: seen.append((done, total_)),
            guard=_Guard(True),
        )
    assert stub.calls, "至少应尝试过移动"
    assert [done for done, _ in seen] == sorted(done for done, _ in seen)
    assert seen[-1][0] == total == seen[-1][1]
    assert outcome.failures and outcome.moved_clusters < total


# --------------------------------------------------------------------------- #
# D-005 回归钉：moved_files 的口径必须是「有任意段真的搬成功」
# --------------------------------------------------------------------------- #
def test_partial_success_counts_file_and_keeps_failure() -> None:
    """部分段失败：``moved_files`` 必须计 1（早期实现给 0，UI 漏算）。

    同时必须保持既有语义：失败段照旧进 ``failures``、``moved_clusters`` 只算
    真正成功的簇。
    """
    segments = _segments(3, 10)                    # dst = 5000 / 5010 / 5020
    with _MoverStub(fail_on=(5010,)) as stub:
        outcome = mover.execute_segments(
            0xBEEF, "R:\\partial.bin", segments, None, None, None, guard=_Guard(True)
        )
    assert [dst for _vcn, dst, _c, _h in stub.calls] == [5000, 5010, 5020]
    assert outcome.moved_clusters == 20
    assert outcome.moved_files == 1, "部分成功的文件必须计入 moved_files（D-005）"
    assert len(outcome.failures) == 1
    assert outcome.failures[0].dst_lcn == 5010
    assert outcome.cancelled is False


def test_partial_then_cancel_counts_file() -> None:
    """先成功一段、随后被取消：已搬成的那段算数（``moved_files == 1``）。

    早期实现里 ``cancelled`` 会把 ``moved_files`` 压成 0，于是 UI 出现
    「移动了 10 簇 / 0 个文件」这种自相矛盾的文案。
    """
    decisions = {"checked": 0}

    def should_cancel() -> bool:
        decisions["checked"] += 1
        return decisions["checked"] > 1            # 第 2 段开始前取消

    with _MoverStub() as stub:
        outcome = mover.execute_segments(
            0xBEEF,
            "R:\\cancel.bin",
            _segments(3, 10),
            None,
            should_cancel,
            None,
            guard=_Guard(True),
        )
    assert len(stub.calls) == 1, "取消点之后的段不许再执行"
    assert outcome.cancelled is True
    assert outcome.moved_clusters == 10
    assert outcome.moved_files == 1, "取消前已成功的部分必须计入文件数（D-005）"
    assert outcome.failures == []


def test_nothing_moved_never_counts_file() -> None:
    """反向钉：一段都没搬成功 ⇒ ``moved_files`` 必须仍是 0（不许虚报成功）。"""
    with _MoverStub(fail_on=(5000, 5010, 5020)) as stub:
        outcome = mover.execute_segments(
            0xBEEF, "R:\\allfail.bin", _segments(3, 10), None, None, None,
            guard=_Guard(True),
        )
    assert len(stub.calls) == 3
    assert outcome.moved_clusters == 0
    assert outcome.moved_files == 0
    assert len(outcome.failures) == 3


def test_guard_rejected_file_never_counts_file() -> None:
    """反向钉：守卫全拒 ⇒ 零簇搬动 ⇒ ``moved_files == 0``。"""
    with _MoverStub() as stub:
        outcome = mover.execute_segments(
            0xBEEF, "R:\\blocked.bin", _segments(2, 10), None, None, None,
            guard=_Guard(False, "目标区间未通过安全校验（测试）"),
        )
    assert stub.calls == []
    assert outcome.moved_clusters == 0
    assert outcome.moved_files == 0


def test_plan_total_move_clusters_counts_evictions_and_relay() -> None:
    place = PlanItem(entry=FileEntry(path="R:\\a"), segments=_segments(1, 100))
    evict = PlanItem(entry=FileEntry(path="R:\\b"), role="evict", segments=_segments(1, 50))
    plan = MovePlan(
        items=[place],
        evictions=[evict],
        tasks=[
            MoveTask(phase="evict", path="R:\\b", segments=_segments(1, 50)),
            MoveTask(phase="place", path="R:\\a", segments=[
                MoveSegment(vcn=0, src_lcn=1000, dst_lcn=5000, clusters=40),
                MoveSegment(vcn=40, src_lcn=2000, dst_lcn=6000, clusters=40),
            ]),
            MoveTask(phase="relay_out", path="R:\\a", segments=[
                MoveSegment(vcn=80, src_lcn=3000, dst_lcn=7000, clusters=10)
            ]),
            MoveTask(phase="relay_in", path="R:\\a", segments=[
                MoveSegment(vcn=80, src_lcn=7000, dst_lcn=8000, clusters=10)
            ]),
        ],
    )
    # 以**实际执行量**汇总（中转两趟都算），与进度条分母同源
    assert plan.total_move_clusters == 50 + 80 + 10 + 10
    # v1 兼容：没有 tasks 时退化为 items + evictions
    legacy = MovePlan(items=[place], evictions=[evict])
    assert legacy.total_move_clusters == 100 + 50


# --------------------------------------------------------------------------- #
# 流水（验收 5）
# --------------------------------------------------------------------------- #
def test_execute_task_writes_journal_and_counts_clusters() -> None:
    temp_dir = tempfile.mkdtemp(prefix="lba_journal_")
    path = os.path.join(temp_dir, "j.jsonl")
    try:
        with MoveJournal(path) as journal:
            with _MoverStub() as stub:
                outcome = mover.execute_task(
                    0xBEEF,
                    MoveTask(path="R:\\a.bin", segments=_segments(3)),
                    _Guard(True),
                    None,
                    journal,
                )
            assert len(stub.calls) == 3
            assert journal.moved_clusters == 30
        assert outcome.moved_clusters == 30
        assert outcome.moved_files == 1
        entries = MoveJournal.load(__import__("pathlib").Path(path))
        assert len(entries) == 3
        assert all(entry.result == "ok" for entry in entries)

        loaded = MoveJournal.load(__import__("pathlib").Path(path))
        assert len(loaded) == 3
        pairs = MoveJournal(__import__("pathlib").Path(path)).rollback_segments()
        assert all(pair[0] == "R:\\a.bin" for pair in pairs)
        segments_back = [segment for _path, segment in pairs]
        # 逆序 + src↔dst 互换
        assert [segment.vcn for segment in segments_back] == [20, 10, 0]
        assert [segment.src_lcn for segment in segments_back] == [5020, 5010, 5000]
        assert [segment.dst_lcn for segment in segments_back] == [1020, 1010, 1000]
    finally:
        import shutil

        shutil.rmtree(temp_dir, ignore_errors=True)


def test_rollback_segments_executes_flipped_directions() -> None:
    segments = [
        MoveSegment(vcn=0, src_lcn=5020, dst_lcn=1020, clusters=10),
        MoveSegment(vcn=10, src_lcn=5010, dst_lcn=1010, clusters=10),
    ]
    with _MoverStub() as stub:
        outcome = mover.rollback_segments(0xBEEF, "R:\\a.bin", segments, _Guard(True), None, None)
    assert [(vcn, dst) for vcn, dst, _c, _h in stub.calls] == [(0, 1020), (10, 1010)]
    assert outcome.moved_clusters == 20


# --------------------------------------------------------------------------- #
# 预热（验收 4）
# --------------------------------------------------------------------------- #
def test_prewarm_missing_file_returns_zero() -> None:
    missing = os.path.join(tempfile.gettempdir(), "lba_no_such_file_9999.bin")
    assert mover.prewarm_file(missing, 1 << 20, 1 << 16) == 0


def test_prewarm_respects_cancel() -> None:
    temp_dir = tempfile.mkdtemp(prefix="lba_prewarm_")
    try:
        path = os.path.join(temp_dir, "big.bin")
        with open(path, "wb") as handle:
            handle.write(b"0" * (256 * 1024))
        assert mover.prewarm_file(path, 1 << 20, 1 << 16, should_cancel=lambda: True) == 0
        assert mover.prewarm_file(path, 1 << 20, 1 << 16) == 256 * 1024
    finally:
        import shutil

        shutil.rmtree(temp_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 扫描阶段的压缩 / EFS 前置识别（验收 7）
# --------------------------------------------------------------------------- #
def test_get_basic_info_flags_encrypted_and_compressed() -> None:
    original_attrs = api.get_file_attributes
    original_open = api.open_handle
    original_close = api.close_handle
    original_extents = retrieval.get_extents
    try:
        retrieval.get_extents = lambda handle, buffer_size=None: []   # type: ignore[assignment]
        api.open_handle = lambda *a, **k: 1                            # type: ignore[assignment]
        api.close_handle = lambda handle: None                         # type: ignore[assignment]

        api.get_file_attributes = lambda path: api.FILE_ATTRIBUTE_ENCRYPTED  # type: ignore[assignment]
        extents, attributes, reason = retrieval.get_basic_info("R:\\secret.docx")
        assert extents == [] and "EFS" in reason
        assert attributes & api.FILE_ATTRIBUTE_ENCRYPTED

        api.get_file_attributes = lambda path: api.FILE_ATTRIBUTE_COMPRESSED  # type: ignore[assignment]
        extents, attributes, reason = retrieval.get_basic_info("R:\\small.txt")
        assert extents == [] and "压缩" in reason

        api.get_file_attributes = lambda path: 0                      # type: ignore[assignment]
        retrieval.get_extents = lambda handle, buffer_size=None: [     # type: ignore[assignment]
            __import__("app.core.models", fromlist=["Extent"]).Extent(0, 100, 4)
        ]
        extents, attributes, reason = retrieval.get_basic_info("R:\\normal.bin")
        assert reason == "" and len(extents) == 1
    finally:
        api.get_file_attributes = original_attrs        # type: ignore[assignment]
        api.open_handle = original_open                 # type: ignore[assignment]
        api.close_handle = original_close               # type: ignore[assignment]
        retrieval.get_extents = original_extents        # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# 信号契约（验收 6）
# --------------------------------------------------------------------------- #
def test_move_worker_signal_signatures_unchanged() -> None:
    from PySide6.QtCore import QCoreApplication

    from app.core import scanner

    app = QCoreApplication.instance() or QCoreApplication([])
    assert app is not None

    worker = scanner.MoveWorker(
        __import__("app.core.models", fromlist=["VolumeInfo"]).VolumeInfo(),
        __import__("app.core.models", fromlist=["BitmapSnapshot"]).BitmapSnapshot(),
        MovePlan(),
    )
    meta = worker.metaObject()
    assert meta.indexOfSignal("progress(int,int,QString)") >= 0
    assert meta.indexOfSignal("file_result(QString,bool,QString)") >= 0
    assert meta.indexOfSignal("completed(QVariant)") >= 0 or meta.indexOfSignal("completed(PyObject)") >= 0
    assert meta.indexOfSignal("failed(QString)") >= 0


if __name__ == "__main__":
    from tests._harness import exit_with

    exit_with(globals())
