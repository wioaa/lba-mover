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

from lbamover.contract.enums import ExecMode, Strategy
from lbamover.contract.models import Extent, JobParams, MoveOp, Plan, VolumeInfo
from lbamover.contract.enums import VolumeKind
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
