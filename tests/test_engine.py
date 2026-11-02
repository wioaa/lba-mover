"""引擎编排：状态机、事件、规划-执行-校验闭环、断点续做、目录绑定。

这里用真实的内存设备跑完整链路，不 mock。原因：引擎的价值全在"这几块
拼起来还能对"，把其中任何一块换成假件，测的就只是接线图。
"""

from __future__ import annotations

import time

import pytest

from lbamover.contract.commands import CancelJob, StartJob, UpdateTarget
from lbamover.contract.enums import ErrorClass, EventKind, JobState, LogLevel, Strategy, VolumeKind
from lbamover.contract.models import Extent, FileFrag, Geometry, JobParams, Plan, VolumeInfo
from lbamover.contract.ports import ListSink
from lbamover.devices.memory import MemoryDevice
from lbamover.engine.catalog import CatalogError, StaticCatalog, UnsupportedCatalog
from lbamover.engine.checkpoint import Checkpoint, CheckpointStore
from lbamover.engine.engine import EngineCore, EngineService
from lbamover.engine.errors import PlanRejected
from lbamover.engine.synth import apply_plan, build_layout, populate, verify


def make_volume(
    geometry: Geometry, *, writable: bool = True, mount="T:\\", device_path="memory://test"
) -> VolumeInfo:
    return VolumeInfo(
        device_path=device_path,
        mount_point=mount,
        label="test",
        filesystem="SYNTH",
        kind=VolumeKind.RAMDISK,
        geometry=geometry,
        free_bytes=geometry.total_bytes // 2,
        is_mounted=False,
        writable=writable,
    )


@pytest.fixture()
def scenario(tmp_path):
    layout = build_layout(1024, file_count=16, seed=42)
    dev = MemoryDevice(layout.total_blocks, layout.geometry.block_size, name="t")
    populate(dev, layout)
    vol = make_volume(layout.geometry)
    sink = ListSink()
    core = EngineCore(
        sink=sink,
        catalog=StaticCatalog(layout.files, layout.free, volume_key=vol.device_path),
        state_dir=tmp_path / "state",
    )
    core.params = JobParams(chunk_blocks=16, concurrency=2, queue_depth=2)
    core.set_target(vol, core.params)
    return core, sink, dev, layout, vol


class TestScan:
    def test_produces_report_and_event(self, scenario):
        core, sink, _dev, layout, vol = scenario
        report = core.scan(vol)
        assert report.file_count == len(layout.files)
        assert report.buckets
        kinds = [e.kind for e in sink.events]
        assert EventKind.FRAG_MAP in kinds
        assert any(e.kind is EventKind.STATE and e.state is JobState.SCANNING for e in sink.events)
        assert core.state is JobState.IDLE

    def test_free_extents_derived_when_catalog_omits_them(self, scenario):
        core, _sink, _dev, layout, _vol = scenario
        core.catalog = StaticCatalog(layout.files, (), volume_key="memory://test")
        report = core.scan()
        assert report.free_blocks > 0

    def test_catalog_volume_mismatch_is_rejected(self, scenario):
        core, _sink, _dev, layout, vol = scenario
        other = make_volume(layout.geometry, mount="Q:\\", device_path="memory://other")
        with pytest.raises(CatalogError):
            core.scan(other)
        assert core.state is JobState.IDLE

    def test_unsupported_catalog_explains_itself(self, scenario):
        core, _sink, _dev, _layout, vol = scenario
        core.catalog = UnsupportedCatalog()
        with pytest.raises(CatalogError) as exc:
            core.scan(vol)
        assert "未实现" in str(exc.value)

    def test_scan_without_target(self, scenario):
        core, _sink, _dev, _layout, _vol = scenario
        core.volume = None
        with pytest.raises(ValueError):
            core.scan()


class TestPlanAndExecute:
    def test_end_to_end_data_integrity(self, scenario):
        core, _sink, dev, layout, vol = scenario
        core.scan(vol)
        plan = core.make_plan(Strategy.COALESCE)
        assert plan.ops and plan.feasible
        res = core.execute(plan, dev)
        assert res.moved_ops == plan.op_count
        assert res.failed_ops == 0
        after = apply_plan(layout, plan)
        assert verify(dev, after) == [], "搬迁后数据不一致"
        assert core.state is JobState.DONE

    def test_analyze_only_has_no_ops(self, scenario):
        core, _sink, _dev, _layout, vol = scenario
        core.scan(vol)
        plan = core.make_plan(Strategy.ANALYZE_ONLY)
        assert plan.ops == ()
        res = core.execute(plan)
        assert res.moved_ops == 0

    def test_execute_without_plan_raises(self, scenario):
        core, _sink, _dev, _layout, _vol = scenario
        with pytest.raises(ValueError):
            core.execute(None)

    def test_invalid_plan_rejected_before_any_write(self, scenario):
        core, _sink, dev, _layout, vol = scenario
        core.scan(vol)
        bad = Plan(
            job_id="bad",
            strategy=Strategy.COALESCE,
            ops=(),
            feasible=False,
            reasons=("人为构造的不可行计划",),
        )
        with pytest.raises(PlanRejected):
            core.execute(bad, dev)

    def test_self_overlapping_plan_rejected(self, scenario):
        from lbamover.contract.models import MoveOp

        core, _sink, dev, _layout, vol = scenario
        core.scan(vol)
        bad = Plan(
            job_id="bad",
            strategy=Strategy.COALESCE,
            ops=(MoveOp(0, "x", "x", (Extent(0, 10),), dst_start=5),),
        )
        with pytest.raises(PlanRejected) as exc:
            core.execute(bad, dev)
        assert any("自我覆盖" in p for p in exc.value.problems)

    def test_checkpoint_cleared_after_full_success(self, scenario, tmp_path):
        core, _sink, dev, _layout, vol = scenario
        core.scan(vol)
        plan = core.make_plan(Strategy.COALESCE)
        core.execute(plan, dev)
        store = CheckpointStore(tmp_path / "state" / f"{plan.job_id}.checkpoint.json")
        assert not store.exists

    def test_resume_state_reported(self, scenario, tmp_path):
        core, sink, dev, _layout, vol = scenario
        core.scan(vol)
        plan = core.make_plan(Strategy.COALESCE)
        store = CheckpointStore(tmp_path / "state" / f"{plan.job_id}.checkpoint.json")
        store.save(
            Checkpoint(
                job_id=plan.job_id,
                plan_hash=plan.plan_hash,
                completed_ops=tuple(op.op_id for op in plan.ops[:1]),
            )
        )
        res = core.execute(plan, dev)
        assert res.skipped_ops >= 1
        assert any("断点" in e.message for e in sink.events if e.kind is EventKind.LOG)

    def test_plan_hash_mismatch_refuses_resume(self, scenario, tmp_path):
        core, sink, dev, _layout, vol = scenario
        core.scan(vol)
        plan = core.make_plan(Strategy.COALESCE)
        store = CheckpointStore(tmp_path / "state" / f"{plan.job_id}.checkpoint.json")
        store.save(Checkpoint(job_id=plan.job_id, plan_hash="stale-hash", completed_ops=(0,)))
        core.execute(plan, dev)
        assert any("不匹配" in e.message for e in sink.events if e.kind is EventKind.LOG)

    def test_state_sequence_is_legal(self, scenario):
        core, sink, dev, _layout, vol = scenario
        core.scan(vol)
        plan = core.make_plan(Strategy.COALESCE)
        core.execute(plan, dev)
        states = [e.state for e in sink.events if e.kind is EventKind.STATE]
        assert states[0] is JobState.IDLE
        assert JobState.RUNNING in states
        assert states[-1] is JobState.DONE

    def test_progress_events_carry_rate(self, scenario):
        core, sink, dev, _layout, vol = scenario
        core.scan(vol)
        plan = core.make_plan(Strategy.COALESCE)
        core.execute(plan, dev)
        progress = [e for e in sink.events if e.kind is EventKind.PROGRESS]
        assert progress, "没有任何进度事件"
        assert progress[-1].progress.total_blocks == plan.total_blocks

    def test_job_done_event_summarises(self, scenario):
        core, sink, dev, _layout, vol = scenario
        core.scan(vol)
        plan = core.make_plan(Strategy.COALESCE)
        core.execute(plan, dev)
        done = [e for e in sink.events if e.kind is EventKind.JOB_DONE][-1]
        assert done.moved_ops == plan.op_count
        assert done.bytes_moved > 0
        assert done.state is JobState.DONE

    def test_journal_file_written(self, scenario, tmp_path):
        from lbamover.engine.checkpoint import JsonlJournal

        core, _sink, dev, _layout, vol = scenario
        core.scan(vol)
        plan = core.make_plan(Strategy.COALESCE)
        core.execute(plan, dev)
        recs = JsonlJournal(tmp_path / "state" / f"{plan.job_id}.jsonl").records()
        assert len(recs) == plan.op_count
        assert all("op_id" in r for r in recs)


class TestStrategies:
    @pytest.mark.parametrize("strategy", [Strategy.COALESCE, Strategy.COMPACT, Strategy.RELOCATE_TAIL])
    def test_each_write_strategy_keeps_data_intact(self, strategy, tmp_path):
        layout = build_layout(1200, file_count=18, seed=7)
        dev = MemoryDevice(layout.total_blocks, layout.geometry.block_size)
        populate(dev, layout)
        vol = make_volume(layout.geometry)
        core = EngineCore(
            sink=ListSink(),
            catalog=StaticCatalog(layout.files, layout.free, volume_key=vol.device_path),
            state_dir=tmp_path / "s",
        )
        core.params = JobParams(chunk_blocks=16)
        core.set_target(vol, core.params)
        core.scan(vol)
        plan = core.make_plan(strategy)
        if not plan.ops:
            pytest.skip("该布局下该策略无可执行操作")
        res = core.execute(plan, dev)
        assert res.failed_ops == 0
        assert verify(dev, apply_plan(layout, plan)) == []


class TestEngineService:
    def _service(self, tmp_path, layout, dev, vol):
        sink = ListSink()
        core = EngineCore(
            sink=sink,
            catalog=StaticCatalog(layout.files, layout.free, volume_key=vol.device_path),
            state_dir=tmp_path / "s",
        )
        core.params = JobParams(chunk_blocks=16)
        svc = EngineService(core, device_provider=lambda v: dev)
        svc.start()
        return svc, core, sink

    def test_update_target_scans_and_plans(self, tmp_path):
        layout = build_layout(512, file_count=12, seed=3)
        dev = MemoryDevice(layout.total_blocks, layout.geometry.block_size)
        populate(dev, layout)
        vol = make_volume(layout.geometry)
        svc, core, sink = self._service(tmp_path, layout, dev, vol)
        try:
            svc.submit(UpdateTarget(vol, core.params, strategy=Strategy.COALESCE))
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and core.plan is None:
                time.sleep(0.01)
            assert core.report is not None
            assert core.plan is not None and core.plan.ops
        finally:
            svc.stop()

    def test_pause_and_resume_are_immediate(self, tmp_path):
        layout = build_layout(256, file_count=6, seed=1)
        dev = MemoryDevice(layout.total_blocks, layout.geometry.block_size)
        populate(dev, layout)
        vol = make_volume(layout.geometry)
        svc, core, _sink = self._service(tmp_path, layout, dev, vol)
        try:
            svc.submit(UpdateTarget(vol, core.params))
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and core.report is None:
                time.sleep(0.01)
            from lbamover.contract.commands import PauseJob, ResumeJob

            svc.submit(PauseJob())
            assert core.control.paused
            assert core.state is JobState.PAUSED
            svc.submit(ResumeJob())
            assert not core.control.paused
            assert core.state is JobState.RUNNING
        finally:
            svc.stop()

    def test_cancel_sets_control(self, tmp_path):
        """空闲时按停止：只置取消标志，**不**把状态机推进 STOPPING。

        STOPPING 的语义是"正在等当前步骤收尾"。空闲时没有任何步骤在跑，
        推进去就会永远停在"正在停止…"，而按钮又是灰的 —— 界面看起来死了。
        （按钮本身在空闲时也是禁用的，这条路只有 API/自动化能走到。）
        """
        layout = build_layout(256, file_count=6, seed=1)
        dev = MemoryDevice(layout.total_blocks, layout.geometry.block_size)
        populate(dev, layout)
        vol = make_volume(layout.geometry)
        svc, core, _sink = self._service(tmp_path, layout, dev, vol)
        try:
            svc.submit(CancelJob())
            assert core.control.cancelled
            assert core.state is JobState.IDLE
        finally:
            svc.stop()

    def test_cancel_during_planning_enters_stopping(self, tmp_path):
        """规划中按停止：进入 STOPPING，并由规划循环自己收尾成 CANCELLED。"""
        layout = build_layout(256, file_count=6, seed=1)
        dev = MemoryDevice(layout.total_blocks, layout.geometry.block_size)
        populate(dev, layout)
        vol = make_volume(layout.geometry)
        svc, core, _sink = self._service(tmp_path, layout, dev, vol)
        try:
            core._set_state(JobState.PLANNING, "测试")
            svc.submit(CancelJob())
            assert core.state is JobState.STOPPING
        finally:
            svc.stop()

    def test_stop_shuts_thread_down(self, tmp_path):
        layout = build_layout(64, file_count=2, seed=1)
        dev = MemoryDevice(layout.total_blocks, layout.geometry.block_size)
        vol = make_volume(layout.geometry)
        svc, _core, _sink = self._service(tmp_path, layout, dev, vol)
        svc.stop(timeout=3)
        assert svc._thread is None


class TestScanProgress:
    """扫描进度：没有进度条时，大卷上界面看起来就是卡死。

    扫描没有"总量"可算（要遍历完才知道有多少文件），所以上报的是**已看条目数**
    与当前路径，UI 用不确定进度条 + 条目数展示，而不是编一个百分比出来。
    """

    def test_scan_emits_progress_events_with_counts(self, tmp_path, monkeypatch):
        import lbamover.engine.engine as engine_mod
        from lbamover.contract.enums import EventKind

        # 关掉节流：真实场景是 0.2s 一次，测试里三次回调发生在同一毫秒内
        monkeypatch.setattr(engine_mod, "SCAN_PROGRESS_MIN_INTERVAL", 0.0)
        from lbamover.contract.models import Extent
        from lbamover.ntfs.catalog import CatalogStats

        layout = build_layout(512, file_count=6, seed=7)
        vol = make_volume(layout.geometry)

        class SlowCatalog:
            """模拟未提权的目录遍历：会多次回调进度。"""

            restricted = False

            def __init__(self) -> None:
                self.stats = CatalogStats(kind="遍历", files=1)

            def list_files(self, volume):
                for i in (1, 50, 120):
                    self.on_progress(i, f"T:/f{i}.bin")
                return layout.files[:1]

            def free_extents(self, volume):
                return (Extent(400, 100),)

            def close(self):
                pass

        cat = SlowCatalog()

        def factory(volume, on_progress=None):
            cat.on_progress = on_progress or (lambda n, p: None)
            return cat

        sink = ListSink()
        core = EngineCore(sink=sink, catalog=UnsupportedCatalog(), catalog_factory=factory)
        core.set_target(vol, JobParams())
        core.scan(vol)

        events = [e for e in sink.events if getattr(e, "kind", None) is EventKind.PROGRESS]
        counts = [e.progress.scanned_items for e in events]
        assert counts and max(counts) >= 120, f"没有上报扫描条目数：{counts}"
        assert any("f120" in e.progress.current_path for e in events), "没有上报当前路径"


class TestPartialOutcome:
    """结局必须三档：全成功 / 部分完成 / 全失败。

    原来只有两档，于是"19 个成功 1 个失败"落进 ``DONE``，界面显示「已完成」。
    用户看到绿色的"已完成"就走了，而没搬走的那些文件还躺在碎片里 —— 状态
    在撒谎，比报错更糟：报错至少会让人去查。
    """

    def _prepare(self, core, vol, dev, *, fail_all: bool = False):
        from dataclasses import replace

        core.scan(vol)
        core.params = replace(core.params, verify=False, max_retries=0)
        plan = core.make_plan(Strategy.COALESCE)
        assert plan.op_count >= 2, "这个合成卷至少要产生两个 op 才谈得上部分失败"
        for op in plan.ops if fail_all else plan.ops[:1]:
            dev.fail_lba_set.update(range(op.dst_start, op.dst_start + op.blocks))
        return plan

    def test_some_failed_reports_partial(self, scenario):
        core, sink, dev, _layout, vol = scenario
        plan = self._prepare(core, vol, dev)
        res = core.execute(plan, dev, resume=False)

        assert res.failed_ops == 1
        assert res.moved_ops == plan.op_count - 1, "一个文件失败后其余被跳过了"
        assert core.state is JobState.PARTIAL, (
            f"有失败却报成了 {core.state.value} —— 界面会显示「已完成」"
        )
        done = [e for e in sink.events if e.kind is EventKind.JOB_DONE][-1]
        assert done.state is JobState.PARTIAL
        assert "1 个失败" in done.message, f"事件里没说失败数：{done.message}"
        assert done.failed_ops == 1

    def test_all_failed_reports_failed(self, scenario):
        core, _sink, dev, _layout, vol = scenario
        plan = self._prepare(core, vol, dev, fail_all=True)
        res = core.execute(plan, dev, resume=False)
        assert res.moved_ops == 0
        assert core.state is JobState.FAILED

    def test_partial_keeps_checkpoint_for_resume(self, tmp_path):
        """有失败时必须留着断点：否则"再点一次开始"会把已搬的又搬一遍。"""
        layout = build_layout(1024, file_count=16, seed=42)
        dev = MemoryDevice(layout.total_blocks, layout.geometry.block_size, name="t")
        populate(dev, layout)
        vol = make_volume(layout.geometry)
        core = EngineCore(
            sink=ListSink(),
            catalog=StaticCatalog(layout.files, layout.free, volume_key=vol.device_path),
            state_dir=tmp_path / "state",
        )
        plan = self._prepare(core, vol, dev)
        core.execute(plan, dev, resume=False)
        assert core.state is JobState.PARTIAL
        store = CheckpointStore(tmp_path / "state" / f"{plan.job_id}.checkpoint.json")
        assert store.exists, "部分完成却把断点清了，续做就没了依据"


class TestErrorSummary:
    """失败原因要归并后再报。

    几十个文件逐行刷屏等于没有信息：用户真正要知道的是"是不是同一个原因"——
    同一个原因往往意味着关掉一个占用程序就能重跑，各不相同的则说明是散点问题。
    """

    def test_groups_same_reason(self):
        from lbamover.engine.engine import _summarize_errors

        rows = _summarize_errors(
            [
                r"C:\a.pst: DeviceError: 文件被独占打开",
                r"C:\b.pst: DeviceError: 文件被独占打开",
                r"C:\c.tmp: DeviceError: 拒绝访问",
            ]
        )
        assert rows[0] == ("DeviceError: 文件被独占打开", 2)
        assert ("DeviceError: 拒绝访问", 1) in rows

    def test_keeps_top_n_and_folds_rest(self):
        from lbamover.engine.engine import _summarize_errors

        rows = _summarize_errors([f"/f{i}: 原因{i % 6}" for i in range(12)], limit=3)
        assert len(rows) == 4
        assert rows[-1][0].startswith("其它")
        assert sum(count for _k, count in rows) == 12, "归并后不能丢条数"

    def test_empty_stays_empty(self):
        from lbamover.engine.engine import _summarize_errors

        assert _summarize_errors([]) == []
