"""T04 验收：依赖排序与执行引擎（``scheduler`` / ``mover`` / ``scanner``）。

覆盖设计文档 §6 T04 验收 1–3、6、8：Kahn + 电梯序、环断裂（relay）、
``guard.allow() == False`` 时 **FSCTL 调用次数必须为 0**、预热失败不抛异常、
信号契约不变。

注意：本文件**不会**执行任何真实写盘 —— 所有涉及写路径的用例都在
``guard`` 拒绝分支上验证。
"""

from __future__ import annotations

import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QCoreApplication  # noqa: E402

from app.core import scheduler as sc  # noqa: E402
from app.core.guard import SafetyGuard  # noqa: E402
from app.core.models import (  # noqa: E402
    BitmapSnapshot,
    MoveSegment,
    MoveTask,
    VolumeInfo,
)
from app.core.packer import SpaceLedger  # noqa: E402
from app.core.safety import SafetyPolicy  # noqa: E402
from app.win32 import api  # noqa: E402
from app.win32 import mover as mover_mod  # noqa: E402

_APP = QCoreApplication.instance() or QCoreApplication([])


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def task(order: int, src: int, dst: int, clusters: int = 10, phase: str = "place", path: str = "") -> MoveTask:
    return MoveTask(
        order=order,
        phase=phase,
        path=path or f"f{order}",
        segments=[MoveSegment(vcn=0, src_lcn=src, dst_lcn=dst, clusters=clusters)],
    )


def positions(ordered):
    return {t.order: index for index, t in enumerate(ordered)}


# --------------------------------------------------------------------------- #
# 1) order_elevator
# --------------------------------------------------------------------------- #
def test_order_monotonic_without_edges() -> None:
    tasks = [task(i, 0, dst) for i, dst in enumerate((900, 100, 500, 300))]
    edges = sc.build_dependencies(tasks)
    ordered, remaining = sc.order_elevator(tasks, edges, "ascending")
    assert remaining == []
    assert [t.first_dst for t in ordered] == [100, 300, 500, 900]

    ordered_desc, _ = sc.order_elevator(tasks, edges, "descending")
    assert [t.first_dst for t in ordered_desc] == [900, 500, 300, 100]


def test_order_respects_random_dags() -> None:
    """1000 次随机 DAG：执行序必须满足所有依赖边（pos[b] < pos[a]）。"""
    rng = random.Random(20261026)
    checked = 0
    for _ in range(1000):
        count = rng.randint(2, 12)
        tasks = [task(i, 1000 + i * 100, 5000 + i * 100) for i in range(count)]
        edges: dict = {t.order: set() for t in tasks}
        for a in range(count):
            for b in range(count):
                if a != b and rng.random() < 0.15:
                    edges[a].add(b)
        # 只保留无环图（随机边可能成环，这里用 DFS 检测后跳过）
        if sc.cyclic_components(range(count), edges):
            continue
        ordered, remaining = sc.order_elevator(tasks, edges, "ascending")
        assert remaining == [], remaining
        pos = positions(ordered)
        for node, prerequisites in edges.items():
            for prerequisite in prerequisites:
                assert pos[prerequisite] < pos[node], (node, prerequisite)
        assert sc.violates_dependency(ordered, edges) is None
        checked += 1
    assert checked > 300, f"随机 DAG 覆盖不足：{checked}"


def test_order_keeps_single_direction() -> None:
    """就绪集合内保持单向：相邻节点的 dst 方向反转不超过 2 次。"""
    rng = random.Random(7)
    for _ in range(50):
        count = 20
        tasks = [task(i, 0, rng.randint(0, 10_000)) for i in range(count)]
        ordered, _ = sc.order_elevator(tasks, sc.build_dependencies(tasks), "ascending")
        reversals = 0
        previous = None
        for item in ordered:
            value = item.first_dst
            if previous is not None:
                if reversals == 0 and value < previous:
                    reversals += 1
                elif reversals >= 1 and value > previous:
                    reversals += 1
                previous = max(previous, value) if reversals == 0 else min(previous, value)
            else:
                previous = value
        assert reversals <= 2, reversals


def test_order_detects_cycle() -> None:
    a = task(1, 1000, 2000)
    b = task(2, 2000, 1000)
    ordered, remaining = sc.order_elevator([a, b], sc.build_dependencies([a, b]))
    assert ordered == []
    assert sorted(remaining) == [1, 2]


def test_tarjan_scc_cases() -> None:
    # 0↔1 环 + 2 独立
    assert sorted(map(sorted, sc.tarjan_scc([0, 1, 2], {0: {1}, 1: {0}, 2: set()}))) == [
        [0, 1],
        [2],
    ]
    # 自环
    assert sc.tarjan_scc([0], {0: {0}}) == [[0]]
    # 链 0→1→2
    assert sorted(map(sorted, sc.tarjan_scc([0, 1, 2], {0: {1}, 1: {2}, 2: set()}))) == [
        [0],
        [1],
        [2],
    ]


# --------------------------------------------------------------------------- #
# 2) break_cycles
# --------------------------------------------------------------------------- #
def _two_cycle() -> list:
    # a 的目标覆盖 b 的源；b 的目标覆盖 a 的源
    a = task(1, 1000, 1010, clusters=10)
    b = task(2, 1010, 1000, clusters=10)
    return [a, b]


def test_break_two_cycle_inserts_relay() -> None:
    tasks = _two_cycle()
    edges = sc.build_dependencies(tasks)
    assert sc.cyclic_components([t.order for t in tasks], edges)
    ledger = SpaceLedger([(5000, 5100)])
    result = sc.break_cycles(tasks, edges, ledger, _window(), max_rounds=3)
    assert result.errors == []
    assert result.rounds == 1
    assert len(result.tasks) == 3
    phases = sorted(t.phase for t in result.tasks)
    assert phases == ["place", "relay_in", "relay_out"]
    # 中转落点在区间外空闲处
    relay_out = next(t for t in result.tasks if t.phase == "relay_out")
    assert relay_out.first_dst == 5000
    # 最终顺序必须可拓扑（无环）
    assert sc.order_elevator(result.tasks, sc.build_dependencies(result.tasks))[1] == []


def test_break_three_cycle() -> None:
    a = task(1, 1000, 1010, clusters=10)
    b = task(2, 1010, 1020, clusters=10)
    c = task(3, 1020, 1000, clusters=10)
    tasks = [a, b, c]
    edges = sc.build_dependencies(tasks)
    ledger = SpaceLedger([(5000, 5100)])
    result = sc.break_cycles(tasks, edges, ledger, _window(), max_rounds=3)
    assert result.errors == []
    assert result.rounds <= 3
    assert sc.order_elevator(result.tasks, sc.build_dependencies(result.tasks))[1] == []


def test_break_self_loop() -> None:
    """自环：目标压在自己身上。"""
    a = task(1, 1000, 1005, clusters=10)
    edges = {1: {1}}
    ledger = SpaceLedger([(5000, 5100)])
    result = sc.break_cycles([a], edges, ledger, _window(), max_rounds=3)
    assert result.errors == []
    assert any(t.phase == "relay_out" for t in result.tasks)


def test_break_cycles_out_of_rounds() -> None:
    """中转轮数超过上限时必须返回明确原因（不抛未捕获异常）。"""
    tasks = _two_cycle()
    edges = sc.build_dependencies(tasks)
    ledger = SpaceLedger([(5000, 5100)])
    result = sc.break_cycles(tasks, edges, ledger, _window(), max_rounds=0)
    assert result.errors
    assert "依赖环" in result.errors[0] or "中转" in result.errors[0]


def test_break_cycles_no_relay_space() -> None:
    tasks = _two_cycle()
    edges = sc.build_dependencies(tasks)
    result = sc.break_cycles(tasks, edges, SpaceLedger([]), _window(), max_rounds=3)
    assert result.errors
    assert "中转空间" in result.errors[0]


def _window():
    from app.core.models import TargetWindow

    return TargetWindow(1000, 3000)


# --------------------------------------------------------------------------- #
# 3) guard.allow() == False ⇒ FSCTL 调用次数必须为 0
# --------------------------------------------------------------------------- #
def _deny_guard() -> SafetyGuard:
    """构造一个必定拒绝的守卫：策略未初始化。"""
    return SafetyGuard(SafetyPolicy(), None)


def test_execute_task_no_fsctl_when_guard_denies() -> None:
    calls = {"count": 0}
    original = mover_mod.api.device_io_control

    def counting(handle, code, *args, **kwargs):
        if code == api.FSCTL_MOVE_FILE:
            calls["count"] += 1
        return b"", 0

    mover_mod.api.device_io_control = counting
    try:
        outcome = mover_mod.execute_task(
            0,
            task(1, 1000, 2000, clusters=10, path=os.path.abspath(__file__)),
            _deny_guard(),
            SafetyPolicy(),
            None,
        )
    finally:
        mover_mod.api.device_io_control = original

    assert calls["count"] == 0, "守卫拒绝时绝不能调用 FSCTL_MOVE_FILE"
    assert outcome.failures, "必须产出一条 MoveFailure"
    assert outcome.failures[0].winerror != 0 or "校验" in outcome.failures[0].message


def test_execute_segments_refuses_without_validation() -> None:
    """既无 guard 又无 validate_target ⇒ 直接拒绝（不猜、不写）。"""
    outcome = mover_mod.execute_segments(
        0, os.path.abspath(__file__), [MoveSegment(0, 1, 2, 3)]
    )
    assert outcome.failures
    assert "拒绝执行" in outcome.failures[0].message


def test_execute_task_rejects_forbidden_target_without_fsctl() -> None:
    """目标落在禁区：守卫拒绝，FSCTL 计数必须为 0。"""
    calls = {"count": 0}
    original = mover_mod.api.device_io_control

    def counting(handle, code, *args, **kwargs):
        if code == api.FSCTL_MOVE_FILE:
            calls["count"] += 1
        return b"", 0

    policy = SafetyPolicy(
        total_clusters=10_000, bytes_per_cluster=4096, head_reserve_clusters=500
    )
    snapshot = BitmapSnapshot(bits=bytes(10_000 // 8), total_clusters=10_000)
    guard = SafetyGuard(policy, snapshot)
    mover_mod.api.device_io_control = counting
    try:
        outcome = mover_mod.execute_task(
            0,
            task(1, 1000, 100, clusters=10, path=os.path.abspath(__file__)),
            guard,
            policy,
            None,
        )
    finally:
        mover_mod.api.device_io_control = original
    assert calls["count"] == 0
    assert outcome.failures


# --------------------------------------------------------------------------- #
# 6) prewarm_file
# --------------------------------------------------------------------------- #
def test_prewarm_missing_file_returns_zero() -> None:
    assert mover_mod.prewarm_file("C:\\definitely-not-exists.bin", 1 << 20, 1 << 16) == 0


def test_prewarm_cancel_returns_immediately() -> None:
    target = os.path.abspath(__file__)
    assert mover_mod.prewarm_file(target, 1 << 20, 1 << 16, should_cancel=lambda: True) == 0


def test_prewarm_reads_some_bytes() -> None:
    target = os.path.abspath(__file__)
    size = os.path.getsize(target)
    read = mover_mod.prewarm_file(target, size, 4096)
    assert read == size


# --------------------------------------------------------------------------- #
# 8) 信号契约不变
# --------------------------------------------------------------------------- #
def test_move_worker_signal_signatures() -> None:
    """四个信号的**完整签名**必须与 v1 一字不差（否则 UI 连接会静默失效）。"""
    from app.core.models import MovePlan
    from app.core.scanner import MoveWorker

    worker = MoveWorker(VolumeInfo(), BitmapSnapshot(), MovePlan())
    meta = worker.metaObject()
    for signature in (
        "progress(int,int,QString)",
        "file_result(QString,bool,QString)",
        "completed(PyObject)",
        "failed(QString)",
    ):
        assert meta.indexOfSignal(signature) >= 0, signature
    # 构造签名必须保持 v1 的前 4 个位置参数
    import inspect

    parameters = list(inspect.signature(MoveWorker.__init__).parameters)
    assert parameters[:5] == ["self", "info", "snapshot", "plan", "parent"]


def test_index_worker_signal_signatures() -> None:
    from app.core.scanner import IndexWorker

    worker = IndexWorker(VolumeInfo(letter="Z"), None)
    meta = worker.metaObject()
    for signature in ("progress(int,int,QString)", "ready(PyObject)", "failed(QString)"):
        assert meta.indexOfSignal(signature) >= 0, signature


# --------------------------------------------------------------------------- #
# 附带：MoveWorker 卷身份校验（伪造序列号 ⇒ failed 且零写调用）
# --------------------------------------------------------------------------- #
def test_move_worker_rejects_identity_mismatch() -> None:
    from app.core.models import MovePlan
    from app.core.scanner import MoveWorker

    info = VolumeInfo(
        letter="R",
        root="R:\\",
        total_clusters=1,          # 与真实卷不符
        bytes_per_cluster=4096,
        serial=0xDEADBEEF,
        is_ntfs=True,
    )
    plan = MovePlan(tasks=[task(1, 10, 20, clusters=5, path=os.path.abspath(__file__))])
    plan.safety = SafetyPolicy(total_clusters=19_660_799, bytes_per_cluster=4096)
    worker = MoveWorker(info, BitmapSnapshot(bits=bytes(100), total_clusters=800), plan)

    captured = {}
    worker.failed.connect(lambda message: captured.setdefault("failed", message))
    worker.completed.connect(lambda outcome: captured.setdefault("completed", outcome))

    calls = {"count": 0}
    original = mover_mod.api.device_io_control

    def counting(handle, code, *args, **kwargs):
        if code == api.FSCTL_MOVE_FILE:
            calls["count"] += 1
        return b"", 0

    mover_mod.api.device_io_control = counting
    try:
        worker.run()          # 直接调用 run()，不起线程
    finally:
        mover_mod.api.device_io_control = original

    assert "failed" in captured, "身份不一致必须发 failed 信号"
    message = captured["failed"]
    assert ("拒绝执行" in message) or ("变化" in message) or ("校验" in message), (
        f"failed 消息未说明身份校验失败：{message!r}"
    )
    assert calls["count"] == 0, "身份校验失败时不得产生任何写调用"
    assert "completed" not in captured


# --------------------------------------------------------------------------- #
# D-005 回归钉：worker 侧的汇总口径（MoveWorker / RollbackWorker 共用）
# --------------------------------------------------------------------------- #
def test_account_move_result_counts_partial_success() -> None:
    """有任意段成功 ⇒ 计 1 个文件；全失败（0 簇）⇒ 不计。

    ``app.core.scanner.account_move_result`` 是 UI 上「移动 N 个文件」的唯一
    汇总点，两个 worker 都走它，保证口径一致。
    """
    from app.core.models import MoveFailure, MoveOutcome
    from app.core.scanner import account_move_result

    failure = MoveFailure(
        path="R:\\a.bin", src_lcn=5010, dst_lcn=1010,
        clusters=10, winerror=5, message="移动失败（测试）",
    )
    total = MoveOutcome(failures=[])

    # ① 部分成功：20 簇 + 1 条失败 ⇒ 算 1 个文件
    assert account_move_result(total, MoveOutcome(moved_clusters=20, failures=[failure])) is True
    assert total.moved_clusters == 20
    assert total.moved_files == 1, "部分成功必须计入文件数（D-005）"

    # ② 全失败：0 簇 ⇒ 不许算文件（也不许改坏已累计的簇数）
    assert account_move_result(total, MoveOutcome(moved_clusters=0, failures=[failure])) is False
    assert total.moved_clusters == 20
    assert total.moved_files == 1

    # ③ 空结果（零段）同样不许算
    assert account_move_result(total, MoveOutcome()) is False
    assert total.moved_files == 1


def test_both_workers_use_the_shared_accounting_rule() -> None:
    """两个 worker 的执行循环都必须调用 ``account_move_result``（口径不许分叉）。"""
    import inspect

    from app.core.scanner import MoveWorker, RollbackWorker

    for owner in (MoveWorker.run, RollbackWorker.run):
        source = inspect.getsource(owner)
        assert "account_move_result" in source, (
            f"{owner.__qualname__} 未使用统一汇总（口径可能分叉）"
        )
        assert "outcome.moved_files += 1" not in source, (
            f"{owner.__qualname__} 仍在内联自增 moved_files（绕过统一口径）"
        )


if __name__ == "__main__":
    from tests._harness import exit_with

    exit_with(globals())
