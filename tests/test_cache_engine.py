"""跨盘缓存引擎：读源 → 顺序写缓存 → 顺序读缓存 → 写目标。

这组用例守四件事：

1. **数据必须逐字节一致**。缓存中转比直写多两次搬运，任何一处偏移算错
   都会静默写坏数据 —— 而"长度对、内容错"是最难发现的那种。
2. **阶段一必须先读完全部源数据**。逐块"读一块写一块"在 src 与 dst 是同一
   块盘时会把还没读的源数据覆盖掉（整理场景里两者就是同一个卷）。
3. **缓存文件必须清理**。每个 op 一个 GB 级文件，不删就是磁盘炸弹。
4. **不能作用于已挂载的 NTFS 卷**。裸写绕过文件系统，会覆盖 $MFT 指着的簇区。
"""

from __future__ import annotations

import pytest

from lbamover.contract.enums import ExecMode, Strategy
from lbamover.contract.models import Extent, JobParams, MoveOp, Plan
from lbamover.devices.base import DeviceError
from lbamover.devices.memory import MemoryDevice
from lbamover.engine.cache_engine import CacheConfig, CachedCopyExecutor

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
    """可区分的测试数据：每个块的首字节是它的序号，便于定位错位。"""
    out = bytearray()
    for i in range(blocks):
        out += bytes([(seed + i) % 256]) * BLOCK
    return bytes(out)


@pytest.fixture()
def device():
    dev = MemoryDevice(4096, BLOCK, name="MEM", writable=True)
    yield dev
    dev.close()


class TestDataIntegrity:
    def test_single_extent_roundtrip(self, device, tmp_path):
        data = patterned(20)
        device.write_blocks(100, data)
        ex = CachedCopyExecutor(
            device, device, JobParams(), CacheConfig(work_dir=str(tmp_path))
        )
        res = ex.run_op(op([(100, 20)], dst_start=1000))
        ex.close()

        assert res.ok and res.blocks_done == 20
        assert device.read_blocks(1000, 20) == data

    def test_multi_extent_keeps_vcn_order(self, device, tmp_path):
        """两段源必须按 VCN 顺序拼到目标区 —— 段间不能交换。

        目标起点写成「源地址 + 位移」是这里最容易犯的错：结果长度正确、
        每段内容也正确，只是顺序反了。
        """
        a, b = patterned(8, seed=10), patterned(8, seed=50)
        device.write_blocks(200, a)
        device.write_blocks(400, b)
        ex = CachedCopyExecutor(
            device, device, JobParams(), CacheConfig(work_dir=str(tmp_path))
        )
        res = ex.run_op(op([(200, 8), (400, 8)], dst_start=2000))
        ex.close()

        assert res.ok
        got = device.read_blocks(2000, 16)
        assert got == a + b, "第一段在前、第二段在后"

    def test_source_is_fully_read_before_any_write(self, device, tmp_path):
        """目标区与源区重叠时也不能损坏数据 —— 阶段一先读完才写阶段二。

        整理现场里 src 和 dst 往往是同一块盘，紧凑的目标区很容易压到还没
        读的源数据上。逐块「读一块写一块」会在这里写坏自己的输入。
        """
        data = patterned(40, seed=7)
        device.write_blocks(100, data)
        # 目标 [120,160) 与源 [100,140) 重叠 20 块
        ex = CachedCopyExecutor(
            device, device, JobParams(), CacheConfig(work_dir=str(tmp_path))
        )
        res = ex.run_op(op([(100, 40)], dst_start=120))
        ex.close()

        assert res.ok
        assert device.read_blocks(120, 40) == data, "重叠区间被自己的写入污染了"


class TestCacheLifecycle:
    def test_cache_file_is_removed(self, device, tmp_path):
        device.write_blocks(0, patterned(4))
        ex = CachedCopyExecutor(
            device, device, JobParams(), CacheConfig(work_dir=str(tmp_path))
        )
        ex.run_op(op([(0, 4)], dst_start=100))
        ex.close()
        assert list(tmp_path.iterdir()) == [], "缓存文件必须清理掉"

    def test_keep_cache_leaves_the_file(self, device, tmp_path):
        device.write_blocks(0, patterned(4))
        ex = CachedCopyExecutor(
            device,
            device,
            JobParams(),
            CacheConfig(work_dir=str(tmp_path), keep_cache=True),
        )
        ex.run_op(op([(0, 4)], dst_start=100))
        ex.close()
        assert len(list(tmp_path.iterdir())) == 1

    def test_work_dir_is_created(self, device, tmp_path):
        target = tmp_path / "nested" / "cache"
        CachedCopyExecutor(
            device, device, JobParams(), CacheConfig(work_dir=str(target))
        ).close()
        assert target.is_dir(), "工作目录不存在时应当自己建，而不是让第一个 op 失败"

    def test_empty_work_dir_rejected(self):
        with pytest.raises(ValueError):
            CacheConfig(work_dir="")


class TestSequentiality:
    def test_cached_side_is_fully_sequential(self, device, tmp_path):
        """缓存盘的写和读都必须是 100% 顺序 —— 这是这个引擎唯一的性能卖点。"""
        device.write_blocks(0, patterned(32))
        ex = CachedCopyExecutor(
            device, device, JobParams(), CacheConfig(work_dir=str(tmp_path), chunk_blocks=4)
        )
        ex.run_op(op([(0, 16), (100, 16)], dst_start=2000))
        ex.close()

        stats = ex.stats
        assert stats.cache_writes > 0 and stats.cache_reads > 0
        assert stats.dst_writes > 0
        # 缓存盘两侧都该是满序；目标盘按 VCN 拼接后也是连续的
        assert stats.dst_write_ratio == 1.0

    def test_multi_extent_lowers_target_sequentiality_but_not_cache(self, device, tmp_path):
        """目标侧因源的分段而跳读是**事实**，不能美化。

        这条用例的意义是把这个事实钉在测试里：如果哪天有人把目标侧的顺序率
        也报成 100%，说明指标被算错了（而不是性能变好了）。
        """
        device.write_blocks(0, patterned(8))
        device.write_blocks(500, patterned(8))
        ex = CachedCopyExecutor(
            device, device, JobParams(), CacheConfig(work_dir=str(tmp_path), chunk_blocks=4)
        )
        ex.run_op(op([(0, 8), (500, 8)], dst_start=2000))
        ex.close()

        d = ex.stats.to_dict()
        assert d["cached_mib"] > 0
        # 目标按 VCN 拼接后仍连续；源按文件实际布局读，段间会跳一次
        assert ex.stats.dst_write_ratio == 1.0
        assert ex.stats.src_read_ratio < 1.0, "源盘跳读是事实，不能美化指标"


class TestResume:
    def test_start_blocks_skips_completed_work(self, device, tmp_path):
        """断点续做：前 N 块已经搬完，只写剩下的。"""
        data = patterned(20, seed=3)
        device.write_blocks(100, data)
        device.write_blocks(1000, b"\x00" * (20 * BLOCK))
        ex = CachedCopyExecutor(
            device, device, JobParams(), CacheConfig(work_dir=str(tmp_path))
        )
        res = ex.run_op(op([(100, 20)], dst_start=1000), start_blocks=12)
        ex.close()

        assert res.ok
        assert res.blocks_done == 8, "只应汇报本次真正写出的块数"
        assert device.read_blocks(1012, 8) == data[12 * BLOCK :]
        assert device.read_blocks(1000, 12) == b"\x00" * (12 * BLOCK), (
            "断点之前的部分不该被重写"
        )


class TestFailureModes:
    def test_read_error_is_reported_with_stage(self, device, tmp_path):
        ex = CachedCopyExecutor(
            device, device, JobParams(), CacheConfig(work_dir=str(tmp_path))
        )
        res = ex.run_op(op([(100_000, 8)], dst_start=0))  # 越界
        ex.close()
        assert not res.ok
        assert res.stage, "失败必须带阶段，否则用户不知道该看哪里"

    def test_out_of_space_is_fatal_not_retryable(self, device, tmp_path, monkeypatch):
        """缓存盘满不该重试：同一个错误会立刻再发生一次。"""
        import lbamover.engine.cache_engine as ce

        def boom(*_a, **_k):
            raise OSError(28, "No space left on device")

        device.write_blocks(0, patterned(4))
        ex = CachedCopyExecutor(
            device, device, JobParams(), CacheConfig(work_dir=str(tmp_path))
        )
        monkeypatch.setattr(ce, "open", boom, raising=False)
        res = ex.run_op(op([(0, 4)], dst_start=100))
        ex.close()
        assert not res.ok
        assert res.error_class == "fatal"


class TestEngineGate:
    """缓存引擎的硬边界：已挂载的 NTFS 卷一律拒绝。"""

    def _core(self):
        from lbamover.engine.engine import EngineCore
        from lbamover.contract.models import VolumeInfo, Geometry

        core = EngineCore()
        core.volume = VolumeInfo(
            device_path="\\\\.\\F:",
            mount_point="F:\\",
            label="data",
            filesystem="NTFS",
            kind=__import__("lbamover.contract.enums", fromlist=["VolumeKind"]).VolumeKind.FIXED,
            geometry=Geometry(BLOCK, 100_000),
            free_bytes=0,
            is_system=False,
            is_mounted=True,
            writable=True,
        )
        return core

    def test_mounted_ntfs_is_rejected(self):
        from lbamover.engine.errors import PlanRejected

        core = self._core()
        core.params = JobParams(exec_mode=ExecMode.CACHED, cache_dir="I:/cache")
        with pytest.raises(PlanRejected) as ei:
            core.check_exec_mode(None)
        text = str(ei.value)
        assert "NTFS" in text and "损坏" in text, "拒绝原因必须说清后果"

    def test_native_mode_passes(self):
        core = self._core()
        core.params = JobParams()
        core.check_exec_mode(None)  # 不抛

    def test_cached_mode_without_dir_rejected(self):
        from lbamover.engine.engine import EngineCore
        from lbamover.contract.models import Geometry, VolumeInfo
        from lbamover.contract.enums import VolumeKind
        from lbamover.engine.errors import PlanRejected

        core = EngineCore()
        core.volume = VolumeInfo(
            device_path="demo.img",
            mount_point="DEMO:\\",
            label="demo",
            filesystem="SYNTH",
            kind=VolumeKind.RAMDISK,
            geometry=Geometry(BLOCK, 1000),
            free_bytes=0,
            is_system=False,
            is_mounted=False,
            writable=True,
        )
        # 构造期就拦住了，所以这里只能绕过 __post_init__ 直接造
        params = JobParams.__new__(JobParams)
        object.__setattr__(params, "exec_mode", ExecMode.CACHED)
        object.__setattr__(params, "cache_dir", "  ")
        core.params = params
        with pytest.raises(PlanRejected):
            core.check_exec_mode(None)


class TestEndToEndOnMemoryDevice:
    def test_engine_runs_plan_through_cache(self, tmp_path):
        """整条链路：EngineCore + 缓存引擎 + 内存盘，逐块校验结果。"""
        from lbamover.contract.models import Geometry, VolumeInfo
        from lbamover.contract.enums import VolumeKind
        from lbamover.engine.engine import EngineCore

        dev = MemoryDevice(4096, BLOCK, name="MEM", writable=True)
        payload = patterned(30, seed=11)
        dev.write_blocks(64, payload)

        from lbamover.contract.models import FileFrag
        from lbamover.engine.frag import analyze, free_extents_from_files

        geo = Geometry(BLOCK, 4096)
        frag = FileFrag("f0", "/f0", (Extent(64, 30),))
        core = EngineCore()
        core.report = analyze(
            [frag], free_extents_from_files(geo, [frag]), geo, bucket_count=8
        )
        core.volume = VolumeInfo(
            device_path="mem://x",
            mount_point="MEM:\\",
            label="mem",
            filesystem="SYNTH",
            kind=VolumeKind.RAMDISK,
            geometry=Geometry(BLOCK, 4096),
            free_bytes=4096 * BLOCK,
            is_system=False,
            is_mounted=False,
            writable=True,
        )
        core.params = JobParams(
            exec_mode=ExecMode.CACHED,
            cache_dir=str(tmp_path),
            chunk_blocks=8,
            verify=True,
            checkpoint_every_chunks=0,
        )
        plan = Plan(
            job_id="cache-e2e",
            strategy=Strategy.COALESCE,
            ops=(op([(64, 30)], dst_start=2048),),
        )
        core._state_dir = tmp_path / "state"
        result = core.execute(plan, dev)
        dev.close()

        assert result.moved_ops == 1, f"失败：{result.failures[:1]}"
        assert list(tmp_path.glob("lbamover-cache-*")) == []


class _Ctl:
    """最小控制对象：只需要 ``cancelled`` 这一个属性。"""

    def __init__(self) -> None:
        self.cancelled = False


class TestInterruptedFill:
    """阶段一被打断时，**绝不能**拿缓存文件去写目标。

    ``preallocate`` 会把缓存文件一次扩到最终大小，未写部分是零。若照样跑
    阶段二，目标区就被零覆盖了 —— 那里可能正躺着还没搬走的数据。断点续做
    时更隐蔽：前 ``done`` 块被跳过，正好从零区长开始写。
    """

    def _setup(self, device):
        data = patterned(12, seed=5)
        device.write_blocks(100, data)
        device.write_blocks(1000, b"\x55" * (12 * BLOCK))
        return data

    def _cancel_after_first_read(self, device, ctl):
        real = device.read_blocks
        calls: list[tuple[int, int]] = []

        def wrapper(lba, n):
            data = real(lba, n)
            calls.append((lba, n))
            ctl.cancelled = True
            return data

        device.read_blocks = wrapper
        return real, calls

    def test_cancel_mid_fill_writes_nothing(self, device, tmp_path):
        self._setup(device)
        ctl = _Ctl()
        ex = CachedCopyExecutor(
            device,
            device,
            JobParams(block_size=BLOCK),
            CacheConfig(work_dir=str(tmp_path), chunk_blocks=2),
            control=ctl,
        )
        real, calls = self._cancel_after_first_read(device, ctl)
        res = ex.run_op(op([(100, 12)], dst_start=1000))
        device.read_blocks = real
        ex.close()

        assert not res.ok and res.cancelled
        assert res.blocks_done == 0, "一个字节都没写，就不该报进度"
        assert device.read_blocks(1000, 12) == b"\x55" * (12 * BLOCK), (
            "中断后目标区必须原样不动，不能被缓存文件里的零覆盖"
        )

    def test_cancel_mid_fill_with_resume_offset(self, device, tmp_path):
        """续做偏移 + 中断：最容易写出零的组合。"""
        self._setup(device)
        ctl = _Ctl()
        ex = CachedCopyExecutor(
            device,
            device,
            JobParams(block_size=BLOCK),
            CacheConfig(work_dir=str(tmp_path), chunk_blocks=2),
            control=ctl,
        )
        real, _calls = self._cancel_after_first_read(device, ctl)
        res = ex.run_op(op([(100, 12)], dst_start=1000), start_blocks=4)
        device.read_blocks = real
        ex.close()

        assert not res.ok
        assert res.blocks_done == 0
        assert device.read_blocks(1000, 12) == b"\x55" * (12 * BLOCK), (
            "断点之前的块被跳过之后，剩下的必须整段不写"
        )

    def test_short_read_from_source_is_an_error(self, device, tmp_path):
        """设备短读必须报错，不能把半截数据写进缓存（那会让后面整体错位）。"""
        self._setup(device)
        real = device.read_blocks

        def short(lba, n):
            return real(lba, n)[:-BLOCK]  # 少一块

        device.read_blocks = short
        ex = CachedCopyExecutor(
            device,
            device,
            JobParams(block_size=BLOCK),
            CacheConfig(work_dir=str(tmp_path), chunk_blocks=2),
        )
        res = ex.run_op(op([(100, 12)], dst_start=1000))
        device.read_blocks = real
        ex.close()

        assert not res.ok
        assert "读取不完整" in res.error
        assert device.read_blocks(1000, 12) == b"\x55" * (12 * BLOCK)


class TestVerifyWired:
    """磁盘缓存路径也要真的回读校验（同 2.0.0L 的漏项）。"""

    def test_mismatch_is_detected(self, device, tmp_path):
        class CorruptingDst:
            def write_blocks(self, lba, data):
                bad = bytearray(data)
                bad[0] ^= 0xFF
                return device.write_blocks(lba, bad)

            def read_blocks(self, lba, n):
                return device.read_blocks(lba, n)

        device.write_blocks(100, patterned(4))
        ex = CachedCopyExecutor(
            CorruptingDst(),
            CorruptingDst(),
            JobParams(block_size=BLOCK, verify=True),
            CacheConfig(work_dir=str(tmp_path), chunk_blocks=2),
        )
        res = ex.run_op(op([(100, 4)], dst_start=1000))
        ex.close()
        assert not res.ok
        assert res.error_class == "verify_mismatch"
        assert "校验" in res.error

    def test_switch_off_allows_no_readback(self, device, tmp_path):
        device.write_blocks(100, patterned(4))
        ex = CachedCopyExecutor(
            device,
            device,
            JobParams(block_size=BLOCK, verify=False),
            CacheConfig(work_dir=str(tmp_path), chunk_blocks=2),
        )
        res = ex.run_op(op([(100, 4)], dst_start=1000))
        ex.close()
        assert res.ok
        assert device.read_blocks(1000, 4) == patterned(4)
