"""端到端：真机 NTFS 结构 → MFT 解析 → 碎片报告 → 规划 → 保护过滤。

这条链路是"能不能用"的分水岭：解析器单独能过，不代表引擎吃进去还能出正确
的计划。这里用合成 NTFS 镜像把整条链路串起来，并验证受保护文件确实被挡在
计划之外。
"""

from __future__ import annotations

import os

import pytest

from dataclasses import replace

from lbamover.contract.enums import Strategy, VolumeKind
from lbamover.contract.models import Extent, Geometry, JobParams, VolumeInfo
from lbamover.contract.ports import ListSink
from lbamover.devices.memory import MemoryDevice
from lbamover.engine.catalog import open_ntfs_catalog
from lbamover.engine.engine import EngineCore
from lbamover.ntfs.catalog import MftCatalog
from lbamover.ntfs.synth import build_ntfs_image


def volume_for(geometry: Geometry) -> VolumeInfo:
    return VolumeInfo(
        device_path="\\\\?\\Volume{ntfs-test}",
        mount_point="T:\\",
        label="ntfs-test",
        filesystem="NTFS",
        kind=VolumeKind.FIXED,
        geometry=geometry,
        free_bytes=geometry.total_bytes // 2,
        is_mounted=True,
        writable=True,
    )


@pytest.fixture()
def ntfs_env(tmp_path):
    dev = MemoryDevice(1024, 4096, writable=True, name="ntfs.img")
    info = build_ntfs_image(dev, total_clusters=1024, record_slots=64)
    vol = volume_for(Geometry(4096, 1024))
    sink = ListSink()
    core = EngineCore(
        sink=sink,
        catalog=MftCatalog(dev),
        state_dir=tmp_path / "state",
    )
    core.params = JobParams(block_size=4096, chunk_blocks=8)
    core.set_target(vol, core.params)
    return core, sink, dev, info, vol


class TestScanFromMft:
    def test_files_and_extents_reach_the_report(self, ntfs_env):
        core, sink, dev, info, vol = ntfs_env
        report = core.scan(vol)

        got = {f.path: f.extents for f in report.files}
        for name, extents in info.layout:
            assert got[name] == extents, f"{name} 在报告里被改写了"

        assert report.file_count == len(info.layout)
        assert report.buckets, "分布图分桶不应为空"
        assert report.free_blocks > 0

    def test_metafiles_never_reach_report(self, ntfs_env):
        core, _sink, _dev, _info, vol = ntfs_env
        report = core.scan(vol)
        assert all(int(f.file_id) >= 16 for f in report.files)

    def test_scan_logs_mft_stats(self, ntfs_env):
        core, sink, _dev, _info, vol = ntfs_env
        core.scan(vol)
        messages = [e.message for e in sink.events if hasattr(e, "message")]
        assert any("文件表解析" in m for m in messages)

    def test_factory_catalog_is_closed_after_scan(self, tmp_path):
        """扫描用的是"临时打开、用完即关"的目录服务，句柄不能泄漏。"""
        holder = {}

        def factory(_vol):
            dev = MemoryDevice(1024, 4096, writable=True)
            build_ntfs_image(dev, total_clusters=1024, record_slots=64)
            cat = MftCatalog(dev)
            holder["cat"] = cat
            holder["dev"] = dev
            return cat

        core = EngineCore(sink=ListSink(), catalog_factory=factory, state_dir=tmp_path)
        core.set_target(volume_for(Geometry(4096, 1024)))
        core.scan()
        assert holder["dev"].closed, "扫描结束后设备句柄必须关闭"

    def test_non_ntfs_volume_rejected_with_reason(self, tmp_path):
        from lbamover.engine.catalog import CatalogError

        core = EngineCore(sink=ListSink(), catalog_factory=open_ntfs_catalog, state_dir=tmp_path)
        vol = replace(volume_for(Geometry(4096, 1024)), filesystem="exFAT")
        core.set_target(vol)
        with pytest.raises(CatalogError) as exc:
            core.scan(vol)
        assert "只支持 NTFS" in str(exc.value)


class TestPlanOnRealLayout:
    def test_protected_file_excluded_from_plan(self, ntfs_env):
        """locked.sys 带系统属性，必须被保护策略挡在计划之外。"""
        core, _sink, _dev, _info, vol = ntfs_env
        core.scan(vol)
        plan = core.make_plan(Strategy.COALESCE)
        planned = {op.path for op in plan.ops}
        assert "locked.sys" not in planned
        assert planned, "其余文件应当能进计划"

    def test_plan_targets_never_overlap_sources(self, ntfs_env):
        core, _sink, _dev, _info, vol = ntfs_env
        core.scan(vol)
        plan = core.make_plan(Strategy.COALESCE)
        for op in plan.ops:
            assert not op.self_overlapping
            for other in plan.ops:
                if other.op_id == op.op_id:
                    continue
                if plan.requires_serial:
                    continue
                for src in other.src_extents:
                    assert not op.dst_extent.overlaps(src)

    def test_blocked_count_is_logged(self, ntfs_env):
        core, sink, _dev, _info, vol = ntfs_env
        core.scan(vol)
        core.make_plan(Strategy.COMPACT)
        messages = [e.message for e in sink.events if hasattr(e, "message")]
        assert any("保护策略拦下" in m for m in messages)

    def test_audit_runs_before_write(self, ntfs_env):
        """执行前审计会拦下"计划里含受保护文件"的情况，且一块都不写。"""
        from lbamover.contract.models import MoveOp, Plan
        from lbamover.engine.errors import PlanRejected

        core, _sink, _dev, _info, vol = ntfs_env
        core.scan(vol)
        bad = Plan(
            job_id="bad",
            strategy=Strategy.COALESCE,
            ops=(
                MoveOp(0, "24", "locked.sys", (Extent(900, 1),), dst_start=950),
            ),
        )
        with pytest.raises(PlanRejected) as exc:
            core.execute(bad, device=None)
        assert any("受保护" in r for r in exc.value.problems)


class TestExecutorSelection:
    def test_ntfs_on_windows_uses_cluster_mover(self, ntfs_env):
        core, _sink, _dev, _info, vol = ntfs_env
        core.scan(vol)
        plan = core.make_plan(Strategy.COALESCE)
        expected = os.name == "nt"
        assert core.uses_cluster_mover(plan) is expected

    def test_synth_volume_uses_block_copy(self, tmp_path):
        from lbamover.engine.catalog import StaticCatalog

        vol = replace(volume_for(Geometry(4096, 256)), filesystem="SYNTH")
        core = EngineCore(sink=ListSink(), catalog=StaticCatalog(), state_dir=tmp_path)
        core.set_target(vol)
        assert core.uses_cluster_mover(None) is False
