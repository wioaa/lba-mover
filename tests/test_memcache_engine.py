"""内存缓存引擎：固定块池中转，按块回写。

这组用例守五件事：

1. **数据逐字节一致**。中转介质换了，偏移算法没换 —— 而"长度对、内容错"
   是最难发现的那类错误。
2. **攒块真的减少了目标盘的写次数**。这是这个引擎**唯一**的性能理由：
   源碎成 N 段、目标连续时，流式要写 N 次，块池只写 1 次。守不住这条，
   它就只是个更慢的直写。
3. **池是固定的**。脏块数不超过配置的块数，内存占用可预测 —— 否则用户
   在"内存上界"里看到的数字就是假的。
4. **断点续做只统计真正写下去的块**。多算一块 = 那一块永远没人写 = 数据
   静默缺失。
5. **接口与其它两个引擎一致**（``prepare`` / ``run_op`` / ``close``），
   调度器才能无差别地换着用。
"""

from __future__ import annotations

import pytest

from lbamover.contract.enums import ErrorClass, ExecMode, Strategy
from lbamover.contract.models import Extent, JobParams, MoveOp, Plan, VolumeInfo
from lbamover.contract.enums import VolumeKind
from lbamover.devices.base import DeviceError
from lbamover.devices.memory import MemoryDevice
from lbamover.engine.cache_engine import CachedCopyExecutor, CacheConfig
from lbamover.engine.memcache_engine import (
    MemCacheConfig,
    MemCacheExecutor,
    _runs as mem_runs,
)
from lbamover.engine import cache_engine as cache_mod

BLOCK = 4096


def op(src_spans, dst_start: int, verify: bool = False, op_id: int = 0) -> MoveOp:
    return MoveOp(
        op_id=op_id,
        file_id=f"f{op_id}",
        path=f"/f{op_id}",
        src_extents=tuple(Extent(s, n) for s, n in src_spans),
        dst_start=dst_start,
        verify=verify,
    )


def patterned(blocks: int, seed: int = 1) -> bytes:
    """可区分的测试数据：每块内容不同，能定位到具体偏移的错位。"""
    out = bytearray()
    for i in range(blocks):
        out += bytes([(seed + i) % 256]) * BLOCK
    return bytes(out)


@pytest.fixture()
def device():
    dev = MemoryDevice(8192, BLOCK, name="MEM", writable=True)
    yield dev
    dev.close()


class TestDataIntegrity:
    def test_single_extent_roundtrip(self, device):
        data = patterned(20)
        device.write_blocks(100, data)
        ex = MemCacheExecutor(
            device, device, JobParams(), MemCacheConfig(block_count=4, block_bytes=4 * BLOCK)
        )
        res = ex.run_op(op([(100, 20)], dst_start=1000))
        ex.close()
        assert res.ok and res.blocks_done == 20
        assert device.read_blocks(1000, 20) == data

    def test_multi_extent_keeps_vcn_order(self, device):
        """多段文件的落点必须是 ``dst_start + 已累计块数``，不是"源地址 + 位移"。"""
        a = patterned(8, seed=10)
        b = patterned(12, seed=90)
        device.write_blocks(200, a)
        device.write_blocks(900, b)
        ex = MemCacheExecutor(
            device, device, JobParams(), MemCacheConfig(block_count=2, block_bytes=4 * BLOCK)
        )
        res = ex.run_op(op([(200, 8), (900, 12)], dst_start=3000))
        ex.close()
        assert res.ok
        assert device.read_blocks(3000, 8) == a, "第一段应在目标区开头"
        assert device.read_blocks(3008, 12) == b, "第二段应紧接在第一段之后"

    def test_runs_match_cache_engine(self, device):
        """两套引擎各有一份 ``_runs``（刻意不共享）。这条测试防止它们漂移。"""
        o = op([(100, 3), (200, 5), (300, 7)], dst_start=777)
        assert mem_runs(o) == cache_mod._runs(o)

    def test_block_not_multiple_of_extent_merges_reads(self, device):
        """一个块里装多个源段时，读要按段读、写要按块写。"""
        parts = [(i * 100, 3) for i in range(5)]  # 5 段 × 3 块 = 15 块
        for idx, (lba, n) in enumerate(parts):
            device.write_blocks(lba, patterned(n, seed=idx * 7))
        ex = MemCacheExecutor(
            device, device, JobParams(), MemCacheConfig(block_count=2, block_bytes=4 * BLOCK)
        )
        res = ex.run_op(op(parts, dst_start=4096))
        ex.close()
        assert res.ok and res.blocks_done == 15
        rebuilt = bytearray()
        for idx, (lba, n) in enumerate(parts):
            rebuilt += patterned(n, seed=idx * 7)
        assert device.read_blocks(4096, 15) == bytes(rebuilt)


class TestWriteCoalescing:
    def test_scattered_source_becomes_one_big_write(self, device):
        """碎源 + 连续目标：块池应把多次小读合成一次大写。

        这是它相对"逐块直写"的唯一优势。``chunk_blocks`` 越小，直写路径的
        写次数越多，差距越明显 —— 这里把 chunk 设成 1 块来放大差异。
        """
        spans = [(1000 + i * 10, 1) for i in range(16)]  # 16 个 1 块的小段
        for i, (lba, n) in enumerate(spans):
            device.write_blocks(lba, patterned(n, seed=i))
        params = JobParams(chunk_blocks=1)
        ex = MemCacheExecutor(
            device,
            device,
            params,
            MemCacheConfig(block_count=8, block_bytes=8 * BLOCK),
        )
        before = device.write_calls
        res = ex.run_op(op(spans, dst_start=6000))
        writes = device.write_calls - before
        ex.close()
        assert res.ok
        assert res.blocks_done == 16
        assert writes <= 2, f"16 个碎段应合成 1~2 次写，实际写了 {writes} 次"
        assert ex.stats.coalesced_blocks >= 2, "合并回写要记账"
        assert ex.stats.src_reads == 16, "读仍然是按段读"

    def test_non_contiguous_target_is_not_merged(self, device):
        """目标地址不连续时不允许合并 —— 那会一次写跨过别人的数据。"""
        spans = [(10, 2), (500, 2)]
        for i, (lba, n) in enumerate(spans):
            device.write_blocks(lba, patterned(n, seed=i))
        ex = MemCacheExecutor(
            device, device, JobParams(), MemCacheConfig(block_count=8, block_bytes=2 * BLOCK)
        )
        # 单 op 的目标区总是连续的（dst_start + vcn），所以这里用两个 op 验证
        # 池在两个 op 之间被清空、不会跨 op 合并。
        ex.run_op(op(spans, dst_start=100, op_id=0))
        ex.run_op(op([(20, 2)], dst_start=50, op_id=1))
        ex.close()
        assert ex.stats.dst_writes >= 2


class TestPoolBound:
    def test_dirty_blocks_never_exceed_pool(self, device):
        device.write_blocks(0, patterned(64))
        ex = MemCacheExecutor(
            device, device, JobParams(), MemCacheConfig(block_count=4, block_bytes=4 * BLOCK)
        )
        ex.prepare([])
        assert ex.stats.blocks_allocated == 4
        res = ex.run_op(op([(0, 64)], dst_start=2048))
        ex.close()
        assert res.ok
        assert ex.stats.peak_dirty <= 4, "脏块数不能超过池容量，否则内存上界是假的"
        assert ex.stats.evictions > 0, "64 块的数据过 4 块的池，必须发生淘汰"
        assert device.read_blocks(2048, 64) == patterned(64)

    def test_pool_allocated_once(self, device):
        device.write_blocks(0, patterned(8))
        ex = MemCacheExecutor(
            device, device, JobParams(), MemCacheConfig(block_count=4, block_bytes=4 * BLOCK)
        )
        ex.prepare([])
        ids = {id(s.buf) for s in ex._slots}
        ex.run_op(op([(0, 8)], dst_start=100))
        ex.run_op(op([(0, 8)], dst_start=200))
        assert {id(s.buf) for s in ex._slots} == ids, "块缓冲区必须复用，不能每 op 重新分配"
        ex.close()

    def test_close_is_idempotent(self, device):
        ex = MemCacheExecutor(
            device, device, JobParams(), MemCacheConfig(block_count=2, block_bytes=BLOCK)
        )
        ex.prepare([])
        ex.close()
        ex.close()
        assert ex._slots == []


class TestResume:
    def test_already_done_blocks_are_not_rewritten(self, device):
        data = patterned(16)
        device.write_blocks(0, data)
        ex = MemCacheExecutor(
            device, device, JobParams(), MemCacheConfig(block_count=4, block_bytes=4 * BLOCK)
        )
        before = device.write_calls
        res = ex.run_op(op([(0, 16)], dst_start=1000), start_blocks=8)
        ex.close()
        assert res.ok
        assert res.blocks_done == 8, "续做只写剩下的 8 块"
        assert device.write_calls - before >= 1
        assert device.read_blocks(1008, 8) == data[8 * BLOCK :]

    def test_start_beyond_total_restarts(self, device):
        """断点说"已经搬完"却又被判未完成 —— 矛盾状态下宁可整份重做。"""
        data = patterned(4)
        device.write_blocks(10, data)
        ex = MemCacheExecutor(
            device, device, JobParams(), MemCacheConfig(block_count=2, block_bytes=2 * BLOCK)
        )
        res = ex.run_op(op([(10, 4)], dst_start=500), start_blocks=99)
        ex.close()
        assert res.ok and res.blocks_done == 4
        assert device.read_blocks(500, 4) == data


class _CancelAfter:
    """跑到第 N 次读之后才"被用户按下停止"的控制块。"""

    def __init__(self, reads: int) -> None:
        self.left = reads
        self.cancelled = False

    def tick(self) -> None:
        if self.left <= 0:
            self.cancelled = True
        self.left -= 1


class TestCancel:
    def test_cancel_reports_only_written_blocks(self, device):
        device.write_blocks(0, patterned(64))
        ctl = _CancelAfter(reads=2)
        ex = MemCacheExecutor(
            device,
            device,
            JobParams(),
            MemCacheConfig(block_count=2, block_bytes=2 * BLOCK),
            control=ctl,
        )
        # 用一个小包装把"读一次就 tick 一次"接进来
        original = device.read_blocks

        def counting(lba, count):
            ctl.tick()
            return original(lba, count)

        device.read_blocks = counting  # type: ignore[method-assign]
        try:
            res = ex.run_op(op([(0, 64)], dst_start=1024))
        finally:
            device.read_blocks = original  # type: ignore[method-assign]
            ex.close()
        assert res.cancelled
        assert res.blocks_done > 0
        # 汇报的块数必须真的写到了盘上
        assert device.read_blocks(1024, res.blocks_done) == patterned(64)[: res.blocks_done * BLOCK]


class TestConfig:
    def test_rejects_bad_numbers(self):
        with pytest.raises(ValueError):
            MemCacheConfig(block_count=0)
        with pytest.raises(ValueError):
            MemCacheConfig(block_bytes=0)
        with pytest.raises(ValueError):
            MemCacheConfig(block_bytes=4096, coalesce_limit_bytes=4096 - 1)

    def test_from_params_aligns_to_block_size(self):
        params = JobParams(block_size=4096, mem_cache_blocks=8, mem_cache_block_kib=100)
        cfg = MemCacheConfig.from_params(params)
        assert cfg.block_bytes % 4096 == 0
        assert cfg.block_count == 8
        assert cfg.total_bytes == 8 * cfg.block_bytes

    def test_from_params_never_zero_blocks_per_slot(self):
        """块大小设得比卷块还小时，不能算出「一块装 0 个块」的除零/死循环。"""
        params = JobParams(block_size=65536, mem_cache_blocks=2, mem_cache_block_kib=16)
        cfg = MemCacheConfig.from_params(params)
        assert cfg.block_bytes >= 65536


def _volume(filesystem: str = "NTFS", mounted: bool = True) -> VolumeInfo:
    from lbamover.contract.models import Geometry

    geo = Geometry(BLOCK, 4096)
    return VolumeInfo(
        device_path="\\\\.\\X:",
        mount_point="X:\\",
        label="x",
        filesystem=filesystem,
        kind=VolumeKind.FIXED,
        geometry=geo,
        free_bytes=geo.total_bytes // 2,
        is_mounted=mounted,
        writable=True,
    )


class TestEngineWiring:
    def test_memcached_never_uses_cluster_mover(self):
        """走缓存就必须打开裸块设备；MOVE_FILE 路径刻意不打开它。

        这条不成立的话 ``runner.src`` 是 None，第一个 read 就崩在 NoneType 上。
        """
        from lbamover.engine.engine import EngineCore

        core = EngineCore()
        core.params = JobParams(exec_mode=ExecMode.MEMCACHED)
        core.volume = _volume()
        core.plan = Plan(job_id="j", strategy=Strategy.COALESCE)
        assert core.uses_cluster_mover() is False

    def test_mounted_ntfs_rejected(self):
        from lbamover.engine.engine import EngineCore
        from lbamover.engine.errors import PlanRejected

        core = EngineCore()
        core.params = JobParams(exec_mode=ExecMode.MEMCACHED)
        core.volume = _volume("NTFS", mounted=True)
        with pytest.raises(PlanRejected):
            core.check_exec_mode(None)

    def test_unmounted_image_allowed(self):
        from lbamover.engine.engine import EngineCore

        core = EngineCore()
        core.params = JobParams(exec_mode=ExecMode.MEMCACHED)
        core.volume = _volume("NTFS", mounted=False)
        core.check_exec_mode(None)  # 不抛即通过

    def test_factory_returns_memcache_executor(self, device):
        from lbamover.engine.engine import EngineCore
        from lbamover.engine.scheduler import JobRunner
        from lbamover.engine.control import RunControl

        core = EngineCore()
        core.params = JobParams(exec_mode=ExecMode.MEMCACHED, mem_cache_blocks=3)
        core.volume = _volume("SYNTH", mounted=False)
        runner = JobRunner(device, device, core.params, RunControl())
        factory = core.make_executor_factory(None)
        assert factory is not None
        ex = factory(runner)
        assert isinstance(ex, MemCacheExecutor)
        assert ex.config.block_count == 3


class TestInterfaceParity:
    """三套引擎只共享 ``run_op`` 契约，调度器靠它无差别调用。"""

    def test_same_methods(self, device, tmp_path):
        mem = MemCacheExecutor(
            device, device, JobParams(), MemCacheConfig(block_count=2, block_bytes=BLOCK)
        )
        disk = CachedCopyExecutor(
            device, device, JobParams(), CacheConfig(work_dir=str(tmp_path))
        )
        for name in ("run_op", "close"):
            assert callable(getattr(mem, name)), name
            assert callable(getattr(disk, name)), name
        # prepare 是可选的：调度器用 getattr 探测，没有也能跑
        assert callable(getattr(mem, "prepare", None))
        mem.close()
        disk.close()

    def test_result_shape_matches(self, device, tmp_path):
        device.write_blocks(0, patterned(4))
        mem = MemCacheExecutor(
            device, device, JobParams(), MemCacheConfig(block_count=2, block_bytes=2 * BLOCK)
        )
        disk = CachedCopyExecutor(
            device, device, JobParams(), CacheConfig(work_dir=str(tmp_path))
        )
        a = mem.run_op(op([(0, 4)], dst_start=100))
        b = disk.run_op(op([(0, 4)], dst_start=200))
        mem.close()
        disk.close()
        assert (a.ok, a.blocks_done) == (b.ok, b.blocks_done)
        assert device.read_blocks(100, 4) == device.read_blocks(200, 4)


class TestConfigScale:
    """块大小的合法范围必须与界面一致（1..65536 KiB）。"""

    def test_large_block_does_not_break_the_config(self):
        """单块 8 MiB / 64 MiB 是合法配置，不能因为合并上限写死 4 MiB 就抛错。

        2.0.0L 之前：``MemCacheConfig.from_params`` 不传 ``coalesce_limit_bytes``，
        默认 4 MiB 小于块大小 → 构造期 ``ValueError: coalesce_limit_bytes
        不能小于单块大小``。界面上这个值能一路调上去，报出来却是纯内部约定的话。
        """
        for kib in (4096, 8192, 65536):
            params = JobParams(
                exec_mode=ExecMode.MEMCACHED,
                mem_cache_blocks=2,
                mem_cache_block_kib=kib,
            )
            cfg = MemCacheConfig.from_params(params)
            assert cfg.block_bytes == kib * 1024
            assert cfg.coalesce_limit_bytes >= cfg.block_bytes, (
                "合并上限必须跟着块大小走，否则构造期就会判非法"
            )

    def test_block_size_is_aligned_down_to_whole_blocks(self):
        """非整块倍的 KiB 向下取整，绝不能让块里出现半个卷块。"""
        # 17 KiB = 17408 B，除以 2 KiB 得 8.5 块 —— 必须向下取整到 8 块
        params = JobParams(
            block_size=2048, mem_cache_blocks=2, mem_cache_block_kib=17
        )
        cfg = MemCacheConfig.from_params(params)
        assert cfg.block_bytes == 8 * 2048, f"应当取整到 16384，实得 {cfg.block_bytes}"
        assert cfg.block_bytes % 2048 == 0


class TestPoolIntegrity:
    """失败不能把槽位从池子里弄丢。

    丢一块的后果不在这一次失败，而在之后：失败攒够 ``block_count`` 次，
    ``_acquire_slot`` 会在空的 ``_dirty`` 上 ``popleft`` 直接 IndexError ——
    一个 IO 错误最后表现成"引擎崩了"。开了「忽略错误运行」时这条路走得到。
    """

    def _exec(self, blocks: int = 2, **kw):
        cfg = MemCacheConfig(block_count=blocks, block_bytes=BLOCK)
        return MemCacheExecutor(
            kw.pop("src"), kw.pop("dst"), JobParams(block_size=BLOCK), cfg, **kw
        )

    def test_write_failure_returns_slots(self):
        class FlakyDst:
            fail = True

            def __init__(self, inner):
                self.inner = inner

            def write_blocks(self, lba, data):
                if self.fail:
                    raise DeviceError("模拟写失败", error_class=ErrorClass.RETRYABLE)
                return self.inner.write_blocks(lba, data)

            def read_blocks(self, lba, n):
                return self.inner.read_blocks(lba, n)

        dev = MemoryDevice(64, BLOCK)
        dev.write_blocks(0, patterned(4))
        dst = FlakyDst(dev)
        ex = self._exec(src=dev, dst=dst)
        res = ex.run_op(op([(0, 4)], dst_start=8))
        assert not res.ok
        assert len(ex._free) == 2 and not ex._dirty, (
            f"失败后池子必须完整，实际 free={len(ex._free)} dirty={len(ex._dirty)}"
        )

        dst.fail = False
        ok = ex.run_op(op([(0, 4)], dst_start=8, op_id=2))
        assert ok.ok and ok.blocks_done == 4
        assert dev.read_blocks(8, 4) == patterned(4)

    def test_read_failure_returns_slot(self):
        dev = MemoryDevice(64, BLOCK)

        def boom(lba, n):
            raise DeviceError("模拟读失败", error_class=ErrorClass.RETRYABLE)

        dev.read_blocks = boom
        ex = self._exec(src=dev, dst=dev)
        res = ex.run_op(op([(0, 4)], dst_start=8))
        assert not res.ok
        assert len(ex._free) == 2 and not ex._dirty, "读失败也不能吃掉槽位"

    def test_many_failures_do_not_exhaust_the_pool(self):
        """连续失败远超池容量也不能崩 —— 这是 IndexError 的直接回归。"""
        calls = {"n": 0}

        class AlwaysFail:
            def write_blocks(self, lba, data):
                calls["n"] += 1
                raise DeviceError("总是失败", error_class=ErrorClass.RETRYABLE)

        dev = MemoryDevice(64, BLOCK)
        dev.write_blocks(0, patterned(4))
        ex = self._exec(blocks=1, src=dev, dst=AlwaysFail())
        for _ in range(10):
            res = ex.run_op(op([(0, 4)], dst_start=8))
            assert not res.ok, "应当老老实实报失败"
        assert calls["n"] == 10


class TestWorkerResolution:
    """并发数必须被引擎的串行声明压住。

    2.0.0L 之前这里没有任何约束：``workers = concurrency``（默认 2），
    两个 op 共用同一个块池执行。``_written_blocks`` 是实例级计数，A 的回写
    会被算进 B 的结果 —— 断点记下的块数比真正写下去的多，续做时跳过没写的
    块，**数据静默缺失**。所以这条不是性能取舍，是正确性。
    """

    def _params(self, concurrency=4):
        from types import SimpleNamespace

        return SimpleNamespace(concurrency=concurrency)

    def test_memcache_forces_single_worker(self):
        from types import SimpleNamespace

        from lbamover.engine.scheduler import resolve_workers

        dev = MemoryDevice(64, BLOCK)
        ex = MemCacheExecutor(
            dev, dev, JobParams(block_size=BLOCK),
            MemCacheConfig(block_count=2, block_bytes=BLOCK),
        )
        workers, why = resolve_workers(
            ex, SimpleNamespace(requires_serial=False), self._params(4)
        )
        assert workers == 1, "共享块池的引擎必须串行"
        assert "共享中转缓冲" in why, "降并发要写在日志里，不能悄悄改"

    def test_other_engines_keep_user_concurrency(self):
        from types import SimpleNamespace

        from lbamover.engine.scheduler import resolve_workers

        workers, why = resolve_workers(
            object(), SimpleNamespace(requires_serial=False), self._params(4)
        )
        assert workers == 4 and why == ""

    def test_requires_serial_still_wins(self):
        from types import SimpleNamespace

        from lbamover.engine.scheduler import resolve_workers

        workers, why = resolve_workers(
            object(), SimpleNamespace(requires_serial=True), self._params(4)
        )
        assert workers == 1 and "复用" in why


class TestEngineWiringLargeBlocks:
    def test_large_block_reaches_the_executor(self):
        """大块配置要能一路走到执行器，不能在中途被配置校验拦下。

        2.0.0L 之前 ``start_job`` 会直接失败并报「coalesce_limit_bytes
        不能小于单块大小」—— 用户看到的是"命令执行失败"，完全对不上界面上的
        「块大小 8 MiB」。
        """
        from types import SimpleNamespace

        from lbamover.engine.engine import EngineCore

        core = EngineCore()
        core.params = JobParams(
            block_size=BLOCK,
            exec_mode=ExecMode.MEMCACHED,
            mem_cache_blocks=2,
            mem_cache_block_kib=8192,
        )
        factory = core.make_executor_factory(None)
        assert factory is not None
        dev = MemoryDevice(64, BLOCK)
        runner = SimpleNamespace(
            src=dev,
            dst=dev,
            params=core.params,
            limiter=None,
            chunk_callback=None,
            retry_callback=None,
        )
        ex = factory(runner)
        assert ex.config.block_bytes == 8192 * 1024
        assert ex.serial_only is True


class _CorruptingDst:
    """写下去的数据会被改坏一个字节 —— 模拟"长度对、内容错"。"""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.reads = 0

    def write_blocks(self, lba, data):
        bad = bytearray(data)
        bad[0] ^= 0xFF
        return self.inner.write_blocks(lba, bad)

    def read_blocks(self, lba, n):
        self.reads += 1
        return self.inner.read_blocks(lba, n)


class TestVerifyWired:
    """「校验」这个勾选在缓存引擎里必须真的做事。

    2.0.0L 之前两条缓存路径 **完全没有实现** 回读校验：界面上写着
    「校验=开」，诊断报告也照抄这一项，实际一次回读都没有。用户据此认为
    "坏块会被发现"，而那正是这个选项存在的理由。
    """

    def _run(self, verify: bool):
        dev = MemoryDevice(64, BLOCK)
        dev.write_blocks(0, patterned(4))
        dst = _CorruptingDst(dev)
        ex = MemCacheExecutor(
            dev,
            dst,
            JobParams(block_size=BLOCK, verify=verify),
            MemCacheConfig(block_count=2, block_bytes=BLOCK),
        )
        return ex.run_op(op([(0, 4)], dst_start=8)), dst

    def test_mismatch_is_detected(self):
        res, dst = self._run(verify=True)
        assert not res.ok
        assert res.error_class == "verify_mismatch", (
            f"校验不一致不能被归成可重试，实际 {res.error_class}"
        )
        assert "校验" in res.error
        assert dst.reads >= 1, "开着校验就必须回读，否则这个开关是空的"

    def test_switch_off_really_skips_it(self):
        res, dst = self._run(verify=False)
        assert res.ok
        assert dst.reads == 0, "关掉校验就不该产生回读 IO"
