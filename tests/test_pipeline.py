"""流水线：数据完整性、短读、重试、取消、内存上界。

这里每个用例都对应一类真实事故：

* 多 extent 拼接错位 → 文件搬完内容错乱；
* 短读被当成正常 → 目标区写入截断数据；
* 取消后不排空队列 → 生产线程卡在 put() 上，进程关不掉；
* 队列无界 → 大卷吃满内存。
"""

from __future__ import annotations

import threading
import time

import pytest

from lbamover.contract.enums import ErrorClass
from lbamover.contract.models import Extent, JobParams, MoveOp
from lbamover.devices.base import DeviceError
from lbamover.devices.memory import MemoryDevice
from lbamover.engine.control import RunControl
from lbamover.engine.errors import CancelledError
from lbamover.engine.pipeline import ChunkPipeline
from lbamover.engine.retry import RetryPolicy, classify_exception, run_with_retry


def pair(blocks=256, bs=4096, pattern=1):
    src = MemoryDevice(blocks, bs, name="src")
    dst = MemoryDevice(blocks, bs, name="dst")
    src.fill_pattern(pattern)
    return src, dst


class TestPipelineIntegrity:
    def test_single_extent_copy(self):
        src, dst = pair()
        params = JobParams(chunk_blocks=8, queue_depth=2)
        op = MoveOp(0, "f", "f", (Extent(0, 32),), dst_start=100)
        res = ChunkPipeline(src, dst, params, RunControl()).run_op(op)
        assert res.ok and res.blocks_done == 32
        assert dst.read_blocks(100, 32) == src.read_blocks(0, 32)

    def test_multi_extent_is_packed_contiguously(self):
        """核心不变量：目标区紧密拼接，不跟源地址走。"""
        src, dst = pair()
        params = JobParams(chunk_blocks=4, queue_depth=2)
        op = MoveOp(0, "f", "f", (Extent(10, 8), Extent(60, 8)), dst_start=200)
        res = ChunkPipeline(src, dst, params, RunControl()).run_op(op)
        assert res.ok
        expected = src.read_blocks(10, 8) + src.read_blocks(60, 8)
        assert dst.read_blocks(200, 16) == expected

    def test_partial_op_with_odd_chunk(self):
        src, dst = pair()
        params = JobParams(chunk_blocks=7, queue_depth=3)
        op = MoveOp(0, "f", "f", (Extent(0, 23),), dst_start=100)
        res = ChunkPipeline(src, dst, params, RunControl()).run_op(op)
        assert res.ok and res.blocks_done == 23
        assert dst.read_blocks(100, 23) == src.read_blocks(0, 23)

    def test_resume_skips_completed_blocks(self):
        src, dst = pair()
        params = JobParams(chunk_blocks=8, queue_depth=2, verify=False)
        op = MoveOp(0, "f", "f", (Extent(0, 40),), dst_start=100)
        # 先正常搬一遍，再"假装断点在 16 块处"重跑：结果必须仍然正确
        ChunkPipeline(src, dst, params, RunControl()).run_op(op)
        dst.write_blocks(100, b"\x00" * (8 * 4096 * 2))  # 打脏前 16 块
        res = ChunkPipeline(src, dst, params, RunControl()).run_op(op, start_blocks=16)
        assert res.ok
        assert res.skipped_blocks == 16
        assert res.blocks_done == 24
        # 前 16 块保持"脏"（跳过），后面 24 块正确 —— 这正是断点语义
        assert dst.read_blocks(116, 24) == src.read_blocks(16, 24)

    def test_verify_detects_corruption(self):
        src, dst = pair()
        dst.corrupt_writes = True
        params = JobParams(chunk_blocks=4, max_retries=0, verify=True)
        op = MoveOp(0, "f", "f", (Extent(0, 8),), dst_start=100)
        res = ChunkPipeline(src, dst, params, RunControl()).run_op(op)
        assert not res.ok
        assert "校验" in res.error

    def test_verify_disabled_still_copies(self):
        src, dst = pair()
        params = JobParams(chunk_blocks=4, verify=False)
        op = MoveOp(0, "f", "f", (Extent(0, 8),), dst_start=100)
        assert ChunkPipeline(src, dst, params, RunControl()).run_op(op).ok
        assert dst.read_blocks(100, 8) == src.read_blocks(0, 8)


class TestPipelineFailures:
    def test_short_read_becomes_error_then_retry(self):
        src, dst = pair()
        src.short_read_blocks = 2
        params = JobParams(chunk_blocks=8, max_retries=0, retry_base_ms=1)
        op = MoveOp(0, "f", "f", (Extent(0, 8),), dst_start=100)
        res = ChunkPipeline(src, dst, params, RunControl()).run_op(op)
        assert not res.ok, "短读必须失败，不能静默写入截断数据"
        assert "短读" in res.error

    def test_retry_recovers_transient_read(self):
        src, dst = pair()
        src.fail_next_reads = 2
        params = JobParams(chunk_blocks=8, max_retries=3, retry_base_ms=1)
        op = MoveOp(0, "f", "f", (Extent(0, 16),), dst_start=100)
        res = ChunkPipeline(src, dst, params, RunControl()).run_op(op)
        assert res.ok and res.retries >= 2

    def test_fatal_error_not_retried(self):
        src, dst = pair()
        dst.read_only_from = 100  # 目标区被模拟成写保护
        params = JobParams(chunk_blocks=4, max_retries=5, retry_base_ms=1)
        op = MoveOp(0, "f", "f", (Extent(0, 8),), dst_start=100)
        res = ChunkPipeline(src, dst, params, RunControl()).run_op(op)
        assert not res.ok
        assert res.retries == 0, "不可恢复错误不应重试"

    def test_cancel_before_start(self):
        src, dst = pair()
        ctrl = RunControl()
        ctrl.request_cancel()
        params = JobParams(chunk_blocks=4)
        op = MoveOp(0, "f", "f", (Extent(0, 8),), dst_start=100)
        res = ChunkPipeline(src, dst, params, ctrl).run_op(op)
        assert res.cancelled and not res.ok

    def test_cancel_midflight_does_not_hang(self):
        """取消后必须能立刻返回：生产者线程不能卡在满队列的 put() 上。"""
        src, dst = pair()
        src.latency_s = 0.002
        ctrl = RunControl()
        params = JobParams(chunk_blocks=2, queue_depth=1)
        op = MoveOp(0, "f", "f", (Extent(0, 200),), dst_start=0 + 256 - 200)
        timer = threading.Timer(0.02, ctrl.request_cancel)
        timer.start()
        t0 = time.monotonic()
        res = ChunkPipeline(src, dst, params, ctrl).run_op(op)
        elapsed = time.monotonic() - t0
        timer.cancel()
        assert res.cancelled
        assert elapsed < 3.0, f"取消耗时 {elapsed:.1f}s，疑似线程卡死"
        # 除了 runner 自己，不应残留读线程
        leftover = [t for t in threading.enumerate() if t.name.startswith("lba-read-op")]
        assert not leftover, f"残留线程 {leftover}"


class TestMemoryBound:
    def test_reads_interleave_with_writes(self):
        """真正的流式：第一次写发生时，源端不应该已经全读完。

        如果实现成"先把整个文件读进内存再写"，这里会看到 read_calls 一次
        性等于总 chunk 数 —— 那正是"假流式"，大卷会把内存吃满。
        """
        src, dst = pair(blocks=512)
        params = JobParams(chunk_blocks=4, queue_depth=1, verify=False)
        op = MoveOp(0, "f", "f", (Extent(0, 64),), dst_start=100)
        reads_at_write: list[int] = []
        pipe = ChunkPipeline(
            src, dst, params, RunControl(), on_chunk=lambda sl: reads_at_write.append(src.read_calls)
        )
        res = pipe.run_op(op)
        assert res.ok
        assert len(reads_at_write) == 16
        assert reads_at_write[0] <= 3, f"首次写入前已读 {reads_at_write[0]} 次，说明不是流式"

    def test_large_op_uses_constant_budget(self):
        """1 万个块的搬运不应随规模增加内存（用队列深度参数化验证）。"""
        src, dst = pair(blocks=20000, bs=512)
        params = JobParams(block_size=512, chunk_blocks=64, queue_depth=2, verify=False)
        op = MoveOp(0, "f", "f", (Extent(0, 10000),), dst_start=10000)
        res = ChunkPipeline(src, dst, params, RunControl()).run_op(op)
        assert res.ok and res.blocks_done == 10000
        assert params.buffer_budget_bytes == 64 * 512 * 2 * 2
        # 队列深度 × chunk 大小 与搬运总量无关：这里是 64 KiB vs 5 MB 数据
        assert params.buffer_budget_bytes * 20 < res.bytes_done


class TestRetryPolicy:
    def test_backoff_grows_and_caps(self):
        pol = RetryPolicy(max_retries=5, base_delay=0.1, factor=2.0, max_delay=0.5, jitter=0.0)
        assert pol.delay_for(0) == pytest.approx(0.1)
        assert pol.delay_for(1) == pytest.approx(0.2)
        assert pol.delay_for(4) == pytest.approx(0.5)  # 被上限截断

    def test_jitter_stays_in_band(self):
        pol = RetryPolicy(base_delay=1.0, jitter=0.5)
        for _ in range(50):
            d = pol.delay_for(0)
            assert 0.5 <= d <= 1.5

    def test_max_attempts(self):
        assert RetryPolicy(max_retries=3).max_attempts == 4

    def test_invalid_policy_rejected(self):
        with pytest.raises(ValueError):
            RetryPolicy(max_retries=-1)
        with pytest.raises(ValueError):
            RetryPolicy(jitter=2.0)

    def test_classification(self):
        assert classify_exception(DeviceError("x", error_class=ErrorClass.RETRYABLE)) is ErrorClass.RETRYABLE
        assert classify_exception(DeviceError("x", error_class=ErrorClass.FATAL)) is ErrorClass.FATAL
        assert classify_exception(CancelledError()) is ErrorClass.CANCELLED
        assert classify_exception(ValueError()) is ErrorClass.FATAL

    def test_gives_up_after_max_retries(self):
        calls = {"n": 0}

        def boom():
            calls["n"] += 1
            raise DeviceError("x", error_class=ErrorClass.RETRYABLE)

        with pytest.raises(DeviceError):
            run_with_retry(boom, RetryPolicy(max_retries=2, base_delay=0), sleep=lambda _s: None)
        assert calls["n"] == 3

    def test_cancel_propagates_immediately(self):
        ctrl = RunControl()
        ctrl.request_cancel()

        def boom():
            raise DeviceError("x", error_class=ErrorClass.RETRYABLE)

        with pytest.raises(CancelledError):
            run_with_retry(boom, RetryPolicy(max_retries=5), control=ctrl)


class TestFailureStage:
    """失败必须记下"卡在哪一步"。

    "读不出来"和"写不进去"的处理方式完全不同：前者多半是坏块或介质问题，
    后者多半是目标越界、卷写保护或者空间不够。以前两者都只报一句
    ``DeviceError``，等于把最重要的信息扔了。
    """

    def test_write_failure_records_stage(self):
        src, dst = pair()
        dst.fail_lba_set = set(range(100, 108))
        params = JobParams(chunk_blocks=4, verify=False, max_retries=0)
        op = MoveOp(0, "f", "f", (Extent(0, 8),), dst_start=100)

        res = ChunkPipeline(src, dst, params, RunControl()).run_op(op)

        assert not res.ok
        assert res.stage == "写入数据", f"阶段记成了 {res.stage!r}"
        assert res.error_type, "没有异常类型，贴给 AI 也无从定位"
        assert res.error_class, "没有错误分类，用户不知道该不该重试"

    def test_read_failure_records_stage(self):
        src, dst = pair()
        src.fail_lba_set = {0}
        params = JobParams(chunk_blocks=4, verify=False, max_retries=0)
        op = MoveOp(0, "f", "f", (Extent(0, 8),), dst_start=100)

        res = ChunkPipeline(src, dst, params, RunControl()).run_op(op)

        assert not res.ok
        assert res.stage == "读取数据", f"阶段记成了 {res.stage!r}"
