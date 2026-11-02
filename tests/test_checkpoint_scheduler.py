"""断点、调度器、进度计量。

断点这块最容易"看起来对但实际没用"：文件写进去了、能读出来、hash 也对，
但只要不是原子写，掉电就会留下半截 JSON，下次加载直接失败 —— 断点等于没有。
所以这里大量断言集中在"损坏时会怎样"。
"""

from __future__ import annotations

import json
import os
import time

import pytest

from lbamover.contract.models import Extent, FileFrag, Geometry, JobParams, MoveOp, Plan
from lbamover.contract.enums import Strategy
from lbamover.devices.memory import MemoryDevice
from lbamover.engine.checkpoint import (
    Checkpoint,
    CheckpointStore,
    JsonlJournal,
    default_state_dir,
    merge_op_progress,
)
from lbamover.engine.control import RunControl
from lbamover.engine.progress import ProgressTracker, RateMeter
from lbamover.engine.scheduler import JobRunner, RateLimiter


class TestCheckpointStore:
    def test_save_load_roundtrip(self, tmp_path):
        store = CheckpointStore(tmp_path / "cp.json")
        cp = Checkpoint(job_id="j", plan_hash="h", completed_ops=(1, 2), op_blocks=((1, 10),))
        store.save(cp)
        got = store.load()
        assert got is not None
        assert got.completed_ops == (1, 2)
        assert got.blocks_of(1) == 10
        assert got.is_op_done(2) and not got.is_op_done(3)

    def test_no_temp_file_left_behind(self, tmp_path):
        store = CheckpointStore(tmp_path / "cp.json")
        store.save(Checkpoint(job_id="j", plan_hash="h"))
        leftovers = [p for p in tmp_path.iterdir() if p.name != "cp.json"]
        assert leftovers == [], f"残留临时文件 {leftovers}"

    def test_corrupt_file_is_quarantined(self, tmp_path):
        store = CheckpointStore(tmp_path / "cp.json")
        (tmp_path / "cp.json").write_text("{ not json", encoding="utf-8")
        assert store.load() is None
        # 坏文件被改名留证，而不是被静默覆盖
        assert (tmp_path / "cp.json.bad").exists()

    def test_missing_file_returns_none(self, tmp_path):
        assert CheckpointStore(tmp_path / "nope.json").load() is None

    def test_clear_is_idempotent(self, tmp_path):
        store = CheckpointStore(tmp_path / "cp.json")
        store.save(Checkpoint(job_id="j", plan_hash="h"))
        store.clear()
        store.clear()
        assert not store.exists

    def test_rejects_non_object_payload(self, tmp_path):
        store = CheckpointStore(tmp_path / "cp.json")
        (tmp_path / "cp.json").write_text("[1,2,3]", encoding="utf-8")
        assert store.load() is None


class TestJsonlJournal:
    def test_append_and_read(self, tmp_path):
        with JsonlJournal(tmp_path / "j.jsonl") as j:
            j.append({"op": 0, "ok": True})
            j.append({"op": 1, "ok": False})
        recs = JsonlJournal(tmp_path / "j.jsonl").records()
        assert [r["op"] for r in recs] == [0, 1]
        assert all("ts" in r for r in recs)

    def test_partial_tail_line_is_skipped(self, tmp_path):
        path = tmp_path / "j.jsonl"
        with JsonlJournal(path) as j:
            j.append({"op": 0})
        # 模拟断电留下的半行
        with open(path, "a", encoding="utf-8") as fh:
            fh.write('{"op": 1, "ok": tr')
        recs = JsonlJournal(path).records()
        assert len(recs) == 1, "半行应被跳过，但不能带塌前面的记录"


def test_merge_op_progress_keeps_max():
    table = merge_op_progress(((1, 10), (2, 4)), 2, 9)
    assert dict(table) == {1: 10, 2: 9}
    assert dict(merge_op_progress(table, 2, 3)) == {1: 10, 2: 9}


class TestRateLimiter:
    def test_zero_rate_is_noop(self):
        limiter = RateLimiter(0)
        t0 = time.monotonic()
        for _ in range(100):
            limiter.acquire(1 << 20)
        assert time.monotonic() - t0 < 0.2

    def test_throttles(self):
        limiter = RateLimiter(bytes_per_sec=2 * 1024 * 1024, burst_seconds=0.0)
        t0 = time.monotonic()
        limiter.acquire(1024 * 1024)
        limiter.acquire(1024 * 1024)
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.25, f"限速未生效，仅耗时 {elapsed:.2f}s"

    def test_update_rate(self):
        limiter = RateLimiter(0)
        limiter.update_rate(1024 * 1024)
        assert limiter.rate == 1024 * 1024


class TestProgressTracker:
    def test_ratio_and_rate(self):
        tr = ProgressTracker(total_blocks=100, block_size=4096, total_ops=2)
        tr.advance(50)
        snap = tr.snapshot()
        assert snap.ratio == 0.5
        assert snap.bytes_done == 50 * 4096
        assert snap.total_ops == 2

    def test_eta_none_when_rate_too_low(self):
        tr = ProgressTracker(total_blocks=1000, block_size=4096)
        tr.advance(1)
        assert tr.snapshot().eta_s is None

    def test_eta_positive_with_rate(self):
        tr = ProgressTracker(total_blocks=100_000, block_size=4096)
        now = time.monotonic()
        tr.advance(50_000, now=now - 1.0)
        tr.advance(1, now=now)
        snap = tr.snapshot(now=now)
        assert snap.rate_bps > 0
        assert snap.eta_s is not None and snap.eta_s > 0

    def test_counters(self):
        tr = ProgressTracker(total_blocks=10, block_size=4096, total_ops=3)
        tr.complete_op()
        tr.skip_op()
        tr.note_retry()
        tr.note_failure()
        snap = tr.snapshot()
        assert (snap.done_ops, snap.skipped_ops, snap.retried_chunks, snap.failed_chunks) == (1, 1, 1, 1)


class TestRateMeter:
    def test_windowed_rate(self):
        m = RateMeter(window_s=1.0)
        now = time.monotonic()
        for i in range(10):
            m.add(1024, now=now - 0.9 + i * 0.1)
        rate = m.rate(now=now)
        assert rate > 0
        m.clear()
        assert m.rate(now=now) == 0.0

    def test_rejects_bad_window(self):
        with pytest.raises(ValueError):
            RateMeter(window_s=0)


# ----------------------------------------------------------------------
# 调度器
# ----------------------------------------------------------------------


def _plan(ops: int = 3, blocks: int = 8, chunk: int = 4) -> Plan:
    return Plan(
        job_id="j-test",
        strategy=Strategy.COALESCE,
        ops=tuple(
            MoveOp(i, f"f{i}", f"f{i}.bin", (Extent(i * 20, blocks),), dst_start=300 + i * 20)
            for i in range(ops)
        ),
    )


class TestJobRunner:
    def _dev(self, blocks=1024):
        d = MemoryDevice(blocks, 4096, name="dev")
        d.fill_pattern(9)
        return d

    def test_moves_all_ops_and_verifies(self, tmp_path):
        dev = self._dev()
        plan = _plan()
        before = {i: dev.read_blocks(i * 20, 8) for i in range(3)}
        store = CheckpointStore(tmp_path / "cp.json")
        runner = JobRunner(
            dev, dev, JobParams(chunk_blocks=4), RunControl(), store=store
        )
        res = runner.run(plan)
        assert res.moved_ops == 3 and res.failed_ops == 0
        for i in range(3):
            assert dev.read_blocks(300 + i * 20, 8) == before[i], f"op{i} 数据错"
        # runner 只负责"如实记录"，清理断点是 EngineCore 在全部成功后做的
        cp = store.load()
        assert cp is not None
        assert set(cp.completed_ops) == {0, 1, 2}
        assert cp.plan_hash == plan.plan_hash

    def test_checkpoint_written_on_cancel(self, tmp_path):
        dev = self._dev()
        ctrl = RunControl()
        store = CheckpointStore(tmp_path / "cp.json")
        runner = JobRunner(dev, dev, JobParams(chunk_blocks=2), ctrl, store=store)
        ctrl.request_cancel()
        res = runner.run(_plan())
        assert res.cancelled
        assert store.exists, "取消后必须留下断点以便续做"

    def test_resume_skips_completed_ops(self, tmp_path):
        dev = self._dev()
        plan = _plan()
        store = CheckpointStore(tmp_path / "cp.json")
        cp = Checkpoint(job_id=plan.job_id, plan_hash=plan.plan_hash, completed_ops=(0, 1))
        store.save(cp)
        runner = JobRunner(dev, dev, JobParams(chunk_blocks=4), RunControl(), store=store)
        res = runner.run(plan, resume=cp)
        assert res.skipped_ops == 2
        assert res.moved_ops == 1
        assert res.blocks_done == 8 * 3  # 已完成的 2 个也计入进度

    def test_requires_serial_forces_single_worker(self, tmp_path):
        dev = self._dev()
        plan = _plan(ops=2)
        plan = Plan(
            job_id=plan.job_id,
            strategy=plan.strategy,
            ops=plan.ops,
            requires_serial=True,
        )
        runner = JobRunner(
            dev, dev, JobParams(chunk_blocks=4, concurrency=4), RunControl(),
            store=CheckpointStore(tmp_path / "cp.json"),
            on_log=lambda m: None,
        )
        res = runner.run(plan)
        assert res.moved_ops == 2

    def test_concurrency_runs_parallel(self, tmp_path):
        """并发 >1 时同时有多个 op 在跑（用延迟放大可观测差异）。"""
        dev = self._dev()
        dev.latency_s = 0.004
        plan = _plan(ops=4, blocks=8, chunk=4)
        t0 = time.monotonic()
        runner = JobRunner(
            dev, dev, JobParams(chunk_blocks=4, concurrency=4), RunControl(),
            store=CheckpointStore(tmp_path / "a.json"),
        )
        res = runner.run(plan)
        parallel = time.monotonic() - t0
        assert res.moved_ops == 4

        t0 = time.monotonic()
        runner2 = JobRunner(
            dev, dev, JobParams(chunk_blocks=4, concurrency=1), RunControl(),
            store=CheckpointStore(tmp_path / "b.json"),
        )
        runner2.run(_plan(ops=4, blocks=8, chunk=4))
        serial = time.monotonic() - t0
        # 并发应当更快；不做严格倍率断言以免 flaky，只要求不更慢
        assert parallel <= serial * 1.35, f"并发 {parallel:.2f}s 未优于串行 {serial:.2f}s"

    def test_halt_on_error_stops_remaining(self, tmp_path):
        dev = self._dev()
        plan = _plan(ops=3)
        dev.read_only_from = 0  # 目标写保护从 0 开始 → 全部失败
        runner = JobRunner(
            dev, dev, JobParams(chunk_blocks=4, max_retries=0), RunControl(),
            store=CheckpointStore(tmp_path / "c.json"),
            halt_on_error=True,
        )
        res = runner.run(plan)
        assert res.failed_ops >= 1
        assert res.cancelled

    def test_journal_records_each_op(self, tmp_path):
        dev = self._dev()
        journal = JsonlJournal(tmp_path / "j.jsonl")
        runner = JobRunner(
            dev, dev, JobParams(chunk_blocks=4), RunControl(),
            store=CheckpointStore(tmp_path / "cp.json"), journal=journal,
        )
        runner.run(_plan(ops=2))
        journal.close()
        recs = JsonlJournal(tmp_path / "j.jsonl").records()
        assert len(recs) == 2
        assert {r["op_id"] for r in recs} == {0, 1}


def test_default_state_dir_respects_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LBAMOVER_STATE_DIR", str(tmp_path / "custom"))
    assert str(default_state_dir()).endswith("custom")


class TestFailureIsolation:
    """一个文件失败，绝不能带走其它文件。

    2026-11 的真实故障：某个文件被独占打开而失败，界面直接切到「已完成」，
    剩下的一大批文件根本没搬。查下来是两件事叠在一起：

    * 执行层其实**没有**停（这条用例把这一点钉住，防止以后"顺手加个
      fail-fast"把它改坏）；
    * 结局判定把"19 个成功 1 个失败"报成 ``DONE``，于是"没搬完"看起来
      和"搬完了"一模一样。

    所以这里同时锁两件事：失败隔离，以及**连续**失败才熔断。
    """

    def _plan(self, ops: int = 4) -> Plan:
        return Plan(
            job_id="t",
            strategy=Strategy.COMPACT,
            ops=tuple(
                MoveOp(
                    op_id=i,
                    file_id=str(i),
                    path=f"/f{i}",
                    src_extents=(Extent(800 + i * 32, 32),),
                    dst_start=100 + i * 200,
                    verify=False,
                )
                for i in range(ops)
            ),
        )

    def test_one_bad_target_does_not_stop_the_rest(self, tmp_path):
        dev = MemoryDevice(2048, 4096)
        plan = self._plan(4)
        # 只让第 2 个 op 的落点坏掉
        bad = plan.ops[1]
        dev.fail_lba_set = set(range(bad.dst_start, bad.dst_start + 32))

        runner = JobRunner(
            dev, dev, JobParams(chunk_blocks=8, max_retries=0, concurrency=1), RunControl(),
            store=CheckpointStore(tmp_path / "cp.json"),
        )
        res = runner.run(plan)
        assert res.failed_ops == 1
        assert res.moved_ops == 3, "一个文件失败后，其余文件被跳过了"
        assert not res.cancelled
        assert res.aborted == ""

    def test_consecutive_failures_trip_the_breaker(self, tmp_path):
        from lbamover.engine.scheduler import MAX_CONSECUTIVE_FAILURES

        dev = MemoryDevice(4096, 4096)
        total = MAX_CONSECUTIVE_FAILURES + 10
        plan = self._plan(total)
        # 全部落点都坏 → 连续失败
        for op in plan.ops:
            dev.fail_lba_set.update(range(op.dst_start, op.dst_start + 32))

        logs: list[str] = []
        runner = JobRunner(
            dev, dev, JobParams(chunk_blocks=8, max_retries=0, concurrency=1), RunControl(),
            store=CheckpointStore(tmp_path / "cp.json"), on_log=logs.append,
        )
        res = runner.run(plan)
        assert res.failed_ops == MAX_CONSECUTIVE_FAILURES, (
            f"熔断没有在 {MAX_CONSECUTIVE_FAILURES} 个连续失败处生效：{res.failed_ops}"
        )
        assert res.aborted, "熔断必须留下原因，否则用户只看到「任务结束了」"
        assert any("系统性故障" in m for m in logs)

    def test_isolated_failure_does_not_trip_breaker(self, tmp_path):
        """失败之间夹着成功就不该熔断 —— 那是"个别文件有问题"，不是系统故障。"""
        from lbamover.engine.scheduler import MAX_CONSECUTIVE_FAILURES

        dev = MemoryDevice(8192, 4096)
        # 每个成功 op 后面跟一个失败的：连续失败数永远是 1
        ops = []
        for i in range(MAX_CONSECUTIVE_FAILURES + 6):
            ops.append(
                MoveOp(op_id=i, file_id=str(i), path=f"/f{i}",
                       src_extents=(Extent(6000 + i * 16, 8),),
                       dst_start=100 + i * 32, verify=False)
            )
        plan = Plan(job_id="t", strategy=Strategy.COMPACT, ops=tuple(ops))
        for i in range(1, len(ops), 2):  # 奇数号坏掉
            dev.fail_lba_set.update(range(ops[i].dst_start, ops[i].dst_start + 8))

        runner = JobRunner(
            dev, dev, JobParams(chunk_blocks=8, max_retries=0, concurrency=1), RunControl(),
            store=CheckpointStore(tmp_path / "cp.json"),
        )
        res = runner.run(plan)
        assert res.aborted == "", "零星失败不该触发熔断"
        assert res.failed_ops == (MAX_CONSECUTIVE_FAILURES + 6) // 2


class _ScriptedExecutor:
    """按 op_id 返回预设结果，用来复现真机上出现过的失败序列。

    为什么不用 MemoryDevice 的 fail_lba_set：那种方式只能造"写盘失败"，而
    真机事故是**打不开文件**（winerror=32）与**布局不符**交错出现，两者在
    熔断上的分量完全不同 —— 用一个能精确编排阶段的执行器才能测这件事。
    """

    def __init__(self, script: dict[int, tuple[str, str, str, int]], cluster_size: int = 4096):
        self.script = script
        self.cluster_size = cluster_size

    def run_op(self, op, start_blocks: int = 0):
        from lbamover.engine.pipeline import OpResult

        spec = self.script.get(op.op_id)
        if spec is None:
            return OpResult(op.op_id, op.blocks, op.blocks * self.cluster_size, ok=True)
        stage, error_type, error_class, winerror = spec
        return OpResult(
            op.op_id, 0, 0, ok=False,
            error=f"{stage}失败", stage=stage, error_type=error_type,
            error_class=error_class, winerror=winerror,
        )

    def close(self) -> None:
        pass


class TestBreakerAgainstRealWorldReport:
    r"""2026-11-01 的真机报告（F: 卷 / relocate_tail / 整卷）。

    那次 20 个失败里 17 个是 ``Steam\logs`` 下的 winerror=32、3 个是"布局
    复核不通过"，连续 20 个触发熔断，**整卷一个文件都没搬成**。根因是规划按
    物理 LCN 排序，同目录小文件挨着排，于是"同一批文件被同一个程序占着"会
    在队列里连成一片；而熔断把它们统统当成系统性故障。
    """

    def _plan(self, count: int) -> Plan:
        ops = tuple(
            MoveOp(
                op_id=i, file_id=str(i), path=f"/f{i}",
                src_extents=(Extent(6000 + i * 16, 8),),
                dst_start=100 + i * 32, verify=False,
            )
            for i in range(count)
        )
        return Plan(job_id="t", strategy=Strategy.RELOCATE_TAIL, ops=ops)

    def _runner(self, tmp_path, script, count: int):
        dev = MemoryDevice(8192, 4096)
        runner = JobRunner(
            dev, dev, JobParams(chunk_blocks=8, max_retries=0, concurrency=1), RunControl(),
            store=CheckpointStore(tmp_path / "cp.json"),
            executor_factory=lambda _r: _ScriptedExecutor(script),
        )
        return runner, self._plan(count)

    def test_open_file_burst_does_not_trip_breaker(self, tmp_path):
        """真机那 20 个失败的真实序列：不该熔断，后面的文件要继续搬。"""
        fail_prefix = 20
        tail_ok = 30
        script: dict[int, tuple[str, str, str, int]] = {}
        # op0/op1 布局不符，op2-4 被占用，op5 布局不符，op6-19 被占用
        script[0] = ("复核布局", "LayoutMismatch", "fatal", 0)
        script[1] = ("复核布局", "LayoutMismatch", "fatal", 0)
        for i in (2, 3, 4):
            script[i] = ("打开文件", "DeviceError", "retryable", 32)
        script[5] = ("复核布局", "LayoutMismatch", "fatal", 0)
        for i in range(6, fail_prefix):
            script[i] = ("打开文件", "DeviceError", "retryable", 32)

        runner, plan = self._runner(tmp_path, script, fail_prefix + tail_ok)
        res = runner.run(plan)

        assert res.aborted == "", (
            f"一整批文件被同一个程序占着不是系统性故障，不该熔断：{res.aborted}"
        )
        assert res.failed_ops == fail_prefix
        assert res.moved_ops == tail_ok, "熔断之外的失败不该让后面的文件停下"

    def test_mass_unopenable_eventually_trips(self, tmp_path):
        """整卷都拿不到句柄时才熔断，而不是 20 个就停。"""
        from lbamover.engine.scheduler import MAX_CONSECUTIVE_UNOPENABLE

        script = {i: ("打开文件", "DeviceError", "retryable", 32) for i in range(MAX_CONSECUTIVE_UNOPENABLE + 10)}
        runner, plan = self._runner(tmp_path, script, MAX_CONSECUTIVE_UNOPENABLE + 10)
        res = runner.run(plan)

        assert res.aborted, "整卷打不开必须留下原因"
        assert "打不开" in res.aborted
        assert res.failed_ops == MAX_CONSECUTIVE_UNOPENABLE

    def test_system_stage_failure_trips_at_twenty(self, tmp_path):
        """搬簇阶段连续失败仍然是系统级故障，20 个就停。"""
        from lbamover.engine.scheduler import MAX_CONSECUTIVE_FAILURES

        script = {
            i: ("搬簇", "DeviceError", "retryable", 1117)
            for i in range(MAX_CONSECUTIVE_FAILURES + 6)
        }
        runner, plan = self._runner(tmp_path, script, MAX_CONSECUTIVE_FAILURES + 6)
        res = runner.run(plan)

        assert res.aborted
        assert "系统性故障" in res.aborted
        assert "搬簇" in res.aborted, "熔断原因必须点出是哪个阶段，否则用户无从判断"
        assert res.failed_ops == MAX_CONSECUTIVE_FAILURES

    def test_alternating_kinds_reset_each_counter(self, tmp_path):
        """两种失败交替出现时两个计数互相清零 —— 谁都不该单独堆到阈值。"""
        from lbamover.engine.scheduler import MAX_CONSECUTIVE_FAILURES

        count = MAX_CONSECUTIVE_FAILURES * 2
        script = {}
        for i in range(count):
            script[i] = (
                ("打开文件", "DeviceError", "retryable", 32)
                if i % 2 == 0
                else ("搬簇", "DeviceError", "retryable", 1117)
            )
        runner, plan = self._runner(tmp_path, script, count)
        res = runner.run(plan)

        assert res.aborted == "", "交替出现的两类失败不该被当成同类连续失败"
        assert res.failed_ops == count


class TestIgnoreErrorsRunToCompletion:
    """「忽略错误运行」：一次跑完，失败的留到最后一起看。

    用户原话是"增加一个忽略错误运行选项"。要解决的问题很具体：整卷整理时
    大半文件被占用/正在写入，默认的两条连续熔断（20 / 200）会提前收尾，用户
    想要的却是"能搬的都搬完，失败的最后逐条列出来"。

    这里锁三件事：开关打开时**不**熔断、开关打开时仍有累计上限、以及开关
    关掉时默认行为一字不变（防回归）。用 ``executor_factory`` 注入
    ``_ScriptedExecutor`` 而不是 MemoryDevice 的 ``fail_lba_set``，因为后者
    只能造"写盘失败"，造不出"打不开文件"这条真机上最常见的连续失败序列。
    """

    def _plan(self, count: int) -> Plan:
        ops = tuple(
            MoveOp(
                op_id=i, file_id=str(i), path=f"/f{i}",
                src_extents=(Extent(6000 + i * 16, 8),),
                dst_start=100 + i * 32, verify=False,
            )
            for i in range(count)
        )
        return Plan(job_id="t", strategy=Strategy.RELOCATE_TAIL, ops=ops)

    def _run(self, tmp_path, script, count: int, params: JobParams) -> tuple:
        dev = MemoryDevice(8192, 4096)
        logs: list[str] = []
        runner = JobRunner(
            dev, dev, params, RunControl(),
            store=CheckpointStore(tmp_path / "cp.json"),
            on_log=logs.append,
            executor_factory=lambda _r: _ScriptedExecutor(script),
        )
        return runner.run(self._plan(count)), logs

    def test_continues_past_breaker_and_finishes_volume(self, tmp_path):
        """用例 A：300 个「打开文件」连续失败之后，剩下 20 个文件照样要搬。

        300 已经越过默认的 200 那道闸门，所以这条用例一过就说明"跳过熔断"
        是真的跳过了，而不是阈值恰好没撞上。
        """
        from lbamover.engine.scheduler import MAX_CONSECUTIVE_UNOPENABLE

        fail_prefix = MAX_CONSECUTIVE_UNOPENABLE + 100  # 300
        tail_ok = 20
        assert fail_prefix > MAX_CONSECUTIVE_UNOPENABLE
        script = {
            i: ("打开文件", "DeviceError", "retryable", 32) for i in range(fail_prefix)
        }
        res, _logs = self._run(
            tmp_path,
            script,
            fail_prefix + tail_ok,
            JobParams(chunk_blocks=8, max_retries=0, concurrency=1, ignore_errors=True),
        )

        assert res.aborted == "", f"打开开关后不该再熔断：{res.aborted}"
        assert res.moved_ops == tail_ok, (
            f"熔断之外的 {tail_ok} 个文件必须搬完，实际只有 {res.moved_ops} 个"
        )
        assert res.failed_ops == fail_prefix
        assert not res.cancelled

    def test_total_failure_ceiling_still_stops(self, tmp_path):
        """用例 B：忽略错误不等于无限容忍，累计失败到上限必须停并说明原因。"""
        from lbamover.contract.models import MAX_IGNORED_FAILURES

        total = MAX_IGNORED_FAILURES + 50
        script = {i: ("打开文件", "DeviceError", "retryable", 32) for i in range(total)}
        res, logs = self._run(
            tmp_path,
            script,
            total,
            JobParams(chunk_blocks=8, max_retries=0, concurrency=1, ignore_errors=True),
        )

        assert res.aborted, "累计失败到上限必须留下原因，否则用户只看到「任务结束了」"
        assert "忽略错误运行" in res.aborted, (
            f"中止原因必须点出是用户开了这个开关，否则像系统故障：{res.aborted}"
        )
        assert res.failed_ops == MAX_IGNORED_FAILURES
        assert any("忽略错误运行" in m for m in logs), "中止原因也要进日志"

    def test_default_behaviour_unaffected(self, tmp_path):
        """用例 C：开关关掉时，300 个「打开文件」失败仍然在 200 个处熔断。"""
        from lbamover.engine.scheduler import MAX_CONSECUTIVE_UNOPENABLE

        fail_prefix = MAX_CONSECUTIVE_UNOPENABLE + 100  # 300
        script = {
            i: ("打开文件", "DeviceError", "retryable", 32) for i in range(fail_prefix)
        }
        res, _logs = self._run(
            tmp_path,
            script,
            fail_prefix + 20,
            JobParams(chunk_blocks=8, max_retries=0, concurrency=1, ignore_errors=False),
        )

        assert res.aborted, "默认（保守收尾）行为被改坏了：该熔断却没熔断"
        assert "打不开" in res.aborted
        assert res.failed_ops == MAX_CONSECUTIVE_UNOPENABLE, (
            f"默认阈值必须仍然是 {MAX_CONSECUTIVE_UNOPENABLE}：{res.failed_ops}"
        )
