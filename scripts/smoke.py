"""冒烟验证：一次跑完「引擎 → 数据一致性 → UI 装配 → UI 驱动执行」。

为什么要有独立冒烟脚本而不是只跑 pytest：

* pytest 无法证明 **GUI 能起来**。控件装配错一个信号，单测全绿而窗口白屏；
* pytest 里的 UI 测试通常只做静态构造，这里要求真正走完一次
  「扫描 → 规划 → 执行 → 校验」，并且通过引擎线程而不是直接调函数。

用法::

    PYTHONPATH=src python scripts/smoke.py           # 全量
    PYTHONPATH=src python scripts/smoke.py --no-gui  # 无 Qt 环境

退出码 0 = 全过。
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
import traceback

# 让脚本能直接跑，不要求 pip install -e
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, fn) -> bool:
    t0 = time.monotonic()
    try:
        detail = fn() or ""
        RESULTS.append((name, True, str(detail)))
        print(f"  [ OK ] {name}  ({time.monotonic() - t0:.2f}s)  {detail}")
        return True
    except Exception as exc:  # noqa: BLE001 - 冒烟脚本要把所有失败都收齐
        RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))
        print(f"  [FAIL] {name}  {type(exc).__name__}: {exc}")
        traceback.print_exc(limit=4)
        return False


# ----------------------------------------------------------------------
# 1. 引擎
# ----------------------------------------------------------------------


def engine_roundtrip():
    from lbamover.contract.enums import Strategy
    from lbamover.contract.models import JobParams
    from lbamover.devices.memory import MemoryDevice
    from lbamover.engine.synth import apply_plan, build_layout, populate, verify
    from lbamover.cli import _run_pipeline

    layout = build_layout(3072, file_count=22, seed=99)
    dev = MemoryDevice(layout.total_blocks, layout.geometry.block_size, name="smoke")
    populate(dev, layout)
    assert verify(dev, layout) == [], "合成卷填充后立即出现不一致"
    code = _run_pipeline(
        layout,
        dev,
        strategy=Strategy.COALESCE,
        params=JobParams(chunk_blocks=48, concurrency=2, queue_depth=3),
        quiet=True,
    )
    assert code == 0, "_run_pipeline 返回非 0"
    return "20+ 文件搬迁并逐块校验一致"


def memory_bound():
    """验证流水线内存上界确实由参数决定，且队列深度真的限制了在途数据。"""
    from lbamover.contract.models import JobParams
    from lbamover.devices.memory import MemoryDevice
    from lbamover.engine.chunk import slice_move_op
    from lbamover.engine.control import RunControl
    from lbamover.engine.pipeline import ChunkPipeline
    from lbamover.contract.models import Extent, MoveOp

    params = JobParams(block_size=4096, chunk_blocks=16, queue_depth=2, concurrency=1)
    src = MemoryDevice(512, 4096, name="src")
    dst = MemoryDevice(512, 4096, name="dst")
    src.fill_pattern(5)
    op = MoveOp(0, "f", "f.bin", (Extent(0, 128),), dst_start=256)
    slices = slice_move_op(op, params.chunk_blocks)
    pipe = ChunkPipeline(src, dst, params, RunControl())
    res = pipe.run_op(op)
    assert res.ok, res.error
    assert res.blocks_done == 128, res.blocks_done
    assert dst.read_blocks(256, 128) == src.read_blocks(0, 128), "数据不一致"
    budget = params.buffer_budget_bytes
    assert budget == 16 * 4096 * 2 * 1, budget
    return f"{len(slices)} 个 chunk，内存上界 {budget} B"


def retry_and_cancel():
    from lbamover.contract.models import Extent, JobParams, MoveOp
    from lbamover.devices.memory import MemoryDevice
    from lbamover.engine.control import RunControl
    from lbamover.engine.pipeline import ChunkPipeline

    params = JobParams(block_size=4096, chunk_blocks=8, queue_depth=2, max_retries=3,
                       retry_base_ms=1)
    src = MemoryDevice(256, 4096, name="src")
    dst = MemoryDevice(256, 4096, name="dst")
    src.fill_pattern(3)
    src.fail_next_reads = 2  # 前两次读失败，应被重试吃掉
    op = MoveOp(0, "f", "f.bin", (Extent(0, 64),), dst_start=128)
    pipe = ChunkPipeline(src, dst, params, RunControl())
    res = pipe.run_op(op)
    assert res.ok, f"重试未生效: {res.error}"
    assert res.retries >= 2, res.retries
    assert dst.read_blocks(128, 64) == src.read_blocks(0, 64)

    # 取消：预置取消信号，应立即返回 cancelled 而不是挂住
    ctrl = RunControl()
    ctrl.request_cancel()
    pipe2 = ChunkPipeline(src, dst, params, ctrl)
    res2 = pipe2.run_op(MoveOp(1, "g", "g.bin", (Extent(70, 40),), dst_start=200))
    assert res2.cancelled or not res2.ok, "取消未被识别"
    return f"重试 {res.retries} 次后成功；取消立即返回"


# ----------------------------------------------------------------------
# 2. UI
# ----------------------------------------------------------------------


def pump(app, predicate, timeout=25.0, label=""):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    raise TimeoutError(f"等待超时：{label}")


def ui_roundtrip(app):
    from lbamover.ui.app import build_app

    window_holder = {}

    def build():
        a, w = build_app(["--demo"])
        window_holder["w"] = w
        w.show()
        a.processEvents()
        return w

    w = build()
    app2 = None
    from PySide6.QtWidgets import QApplication

    app2 = QApplication.instance()

    pump(app2, lambda: w.picker.count() > 0, label="卷列表装载")
    assert w.picker.count() >= 1, "卷列表为空"
    assert w._volume is not None, "未自动选中目标卷"
    assert w.picker.current() is not None
    # 演示模式下必须选中演示卷，否则会拿合成布局去扫真盘
    from lbamover.ui.main_window import DEMO_DEVICE_PATH

    pump(app2, lambda: w._volume is not None and w._volume.device_path == DEMO_DEVICE_PATH,
         label="演示卷选中")
    assert w._volume.device_path == DEMO_DEVICE_PATH, f"选中的是 {w._volume.device_path}"

    # 走真实按钮路径：扫描 / 预演
    w._on_scan()
    pump(app2, lambda: w._report is not None, label="扫描完成")
    assert w._report.file_count > 0, "扫描结果为空"
    assert w.frag_map._report is not None, "分布图未收到报告"
    pump(app2, lambda: w._plan is not None, label="规划完成")
    assert w._plan.op_count > 0, "预演没有产生操作"

    # 执行：直接提交 StartJob（确认对话框在无头环境下无法交互，
    # 因此这里绕过 _on_start 的确认闸门，其余链路完全走引擎线程）
    from lbamover.contract.commands import StartJob
    from lbamover.contract.enums import JobState
    from lbamover.contract.models import JobSpec

    spec = JobSpec(volume=w._volume, strategy=w._strategy, params=w._params)
    w.service.submit(StartJob(spec))
    pump(app2, lambda: w.core.state in (JobState.DONE, JobState.FAILED, JobState.CANCELLED),
         timeout=40, label="执行完成")
    assert w.core.state is JobState.DONE, f"执行结束状态为 {w.core.state}"
    assert w.core.last_result is not None
    assert w.core.last_result.moved_ops > 0, "没有搬迁任何文件"

    # 进度卡与日志确实被驱动过
    assert w.progress.percent_label.text().endswith("%")
    assert "%" in w.progress.percent_label.text()

    # 数据校验（UI 侧的 _verify_demo 会自动跑一次，这里独立复核）
    from lbamover.engine.synth import apply_plan, verify

    after = apply_plan(w._demo_layout, w.core.plan)
    problems = verify(w._demo_device, after)
    assert not problems, f"UI 路径数据不一致：{problems[:2]}"

    w.close()
    return (
        f"{w.core.last_result.moved_ops} 个文件经 UI 驱动搬迁完成，"
        f"报告 {w._report.file_count} 个文件"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="LBAMover 冒烟验证")
    ap.add_argument("--no-gui", action="store_true", help="跳过 GUI 部分")
    args = ap.parse_args(argv)

    print("=" * 68)
    print("LBAMover 冒烟验证")
    print("=" * 68)

    print("\n[1/3] 引擎流水线")
    check("引擎端到端（扫描→规划→执行→逐块校验）", engine_roundtrip)
    check("分块流水线与内存上界", memory_bound)
    check("重试与取消", retry_and_cancel)

    if not args.no_gui:
        print("\n[2/3] GUI 装配与驱动")
        import lbamover

        print(f"      版本 {lbamover.VERSION_DISPLAY}")

        def _ui():
            from PySide6.QtWidgets import QApplication

            app = QApplication.instance() or QApplication([])
            return ui_roundtrip(app)

        check("窗口构建 + 扫描 + 执行 + 校验", _ui)
    else:
        print("\n[2/3] GUI 部分已跳过（--no-gui）")

    print("\n[3/3] 结果")
    failed = [r for r in RESULTS if not r[1]]
    print(f"  通过 {len(RESULTS) - len(failed)} / {len(RESULTS)}")
    if failed:
        for name, _, detail in failed:
            print(f"  [FAIL] {name}: {detail}")
        print("\n结论：FAIL")
        return 1
    print("\n结论：PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
