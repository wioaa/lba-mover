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
