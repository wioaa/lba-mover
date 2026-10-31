"""命令行入口。

存在的意义不是"给终端用户用"，而是：

* **可自动化验证**。``selftest`` 在没有 GUI 的环境里跑完整条流水线并逐块
  校验，CI 与冒烟脚本都调它；
* **离线作业**。``run`` 能对一份卷镜像执行整理，演练真机操作；
* **排障**。``volumes`` 直接打印引擎看到的卷与能力，不用开窗口猜。

退出码：0 成功，1 业务失败（数据不一致/计划不可行），2 参数或环境错误。
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time

from . import VERSION_DISPLAY
from .contract.enums import Strategy, VolumeKind
from .contract.models import JobParams, JobSpec, VolumeInfo
from .contract.models import Geometry
from .contract.ports import ListSink
from .devices.image import ImageDevice
from .devices.memory import MemoryDevice
from .engine.catalog import JsonCatalog, StaticCatalog
from .engine.engine import EngineCore
from .engine.synth import apply_plan, build_layout, populate, verify

__all__ = ["main"]


def _cmd_volumes(args: argparse.Namespace) -> int:
    from .platform.volumes import enumerate_volumes

    vols = enumerate_volumes()
    if not vols:
        print("未发现可整理的本地卷（网络盘/光驱不参与 LBA 级整理）")
        return 1
    print(f"{'挂载点':<6} {'文件系统':<8} {'容量':>12} {'可用':>12} {'块大小':>8} {'系统卷':>6}")
    for v in vols:
        print(
            f"{v.mount_point:<6} {v.filesystem or '-':<8} "
            f"{v.geometry.total_bytes / 2**30:>10.1f}G {v.free_bytes / 2**30:>10.1f}G "
            f"{v.geometry.block_size:>8} {'是' if v.is_system else '否':>6}"
        )
    return 0


def _run_pipeline(
    layout,
    device,
    *,
    strategy: Strategy,
    params: JobParams,
    quiet: bool = False,
) -> int:
    """共用的端到端流程：扫描 → 规划 → 执行 → 逐块校验。"""
    volume = VolumeInfo(
        device_path="memory://selftest",
        mount_point="T:\\",
        label="selftest",
        filesystem="SYNTH",
        kind=VolumeKind.RAMDISK,
        geometry=layout.geometry,
        free_bytes=sum(e.length for e in layout.free) * layout.geometry.block_size,
        is_mounted=False,
        writable=True,
    )
    sink = ListSink()
    core = EngineCore(
        sink=sink,
        catalog=StaticCatalog(layout.files, layout.free),
        state_dir=tempfile.mkdtemp(prefix="lbamover-"),
    )
    core.params = params
    core.set_target(volume, params)

    t0 = time.monotonic()
    report = core.scan(volume)
    if not quiet:
        print(
            f"[scan] 文件 {report.file_count}，碎片文件 {len(report.fragmented_files)}，"
            f"文件碎片度 {report.file_frag_score:.1%}，自由空间碎片度 {report.free_frag_score:.1%}，"
            f"最大连续空闲 {report.largest_free_run} 块"
        )

    plan = core.make_plan(strategy)
    if not quiet:
        print(
            f"[plan] ops={plan.op_count} blocks={plan.total_blocks} "
            f"feasible={plan.feasible} serial={plan.requires_serial}"
        )
        for reason in plan.reasons:
            print(f"       ! {reason}")
    if not plan.ops:
        print("[FAIL] 计划为空")
        return 1

    result = core.execute(plan, device)
    elapsed = time.monotonic() - t0
    if not quiet:
        print(
            f"[exec] moved={result.moved_ops} failed={result.failed_ops} "
            f"skipped={result.skipped_ops} bytes={result.bytes_moved} "
            f"elapsed={elapsed:.2f}s"
        )

    after = apply_plan(layout, plan)
    problems = verify(device, after)
    if problems:
        print(f"[FAIL] 数据校验不一致 {len(problems)} 处")
        for p in problems[:5]:
            print(f"       {p}")
        return 1
    if result.failed_ops:
        print(f"[FAIL] 有 {result.failed_ops} 个 op 失败")
        return 1
    print(
        f"[PASS] 搬迁 {result.moved_ops} 个文件 / {result.blocks_done} 块，"
        f"逐块校验一致（{len(after.files)} 个文件）"
    )
    return 0


def _cmd_selftest(args: argparse.Namespace) -> int:
    layout = build_layout(
        args.blocks,
        file_count=args.files,
        block_size=args.block_size,
        seed=args.seed,
    )
    device = MemoryDevice(layout.total_blocks, layout.geometry.block_size, name="selftest")
    populate(device, layout)
    pre = verify(device, layout)
    if pre:
        print(f"[FAIL] 合成卷自检失败：{pre[0]}")
        return 1
    params = JobParams(
        block_size=args.block_size,
        chunk_blocks=args.chunk,
        concurrency=args.concurrency,
        queue_depth=args.depth,
        max_retries=args.retries,
        verify=not args.no_verify,
    )
    return _run_pipeline(
        layout,
        device,
        strategy=Strategy(args.strategy),
        params=params,
        quiet=args.quiet,
    )


def _cmd_run(args: argparse.Namespace) -> int:
    """对一份卷镜像执行整理（离线演练）。"""
    if args.block_size <= 0:
        print("[FAIL] block_size 必须为正", file=sys.stderr)
        return 2
    device = ImageDevice(args.image, block_size=args.block_size, writable=True)
    catalog = JsonCatalog(args.layout)
    from .engine.frag import analyze, free_extents_from_files

    files = tuple(catalog.list_files(None))  # type: ignore[arg-type]
    free = tuple(catalog.free_extents(None)) or free_extents_from_files(
        device.geometry, files
    )
    report = analyze(files, free, device.geometry)
    layout = type(
        "L",
        (),
        {
            "geometry": device.geometry,
            "files": report.files,
            "free": report.free_extents,
            "seed": 0,
        },
    )()
    params = JobParams(block_size=args.block_size, chunk_blocks=args.chunk)
    return _run_pipeline(
        layout, device, strategy=Strategy(args.strategy), params=params, quiet=False
    )


def _cmd_ntfs(args: argparse.Namespace) -> int:
    """解析一份 NTFS 卷镜像，打印文件表读到的内容。

    这是**上真机之前的验收手段**：先用 dd / Win32DiskImager 把分区导成镜像，
    在这里确认"文件 → 簇区间"读得对，再去动真卷。没有这一步，解析器写错了
    也只能在真机上才发现，而真机上的代价是数据。
    """
    from .devices.image import ImageDevice
    from .ntfs.catalog import MftCatalog, NtfsScanError
    from .ntfs.synth import build_ntfs_image

    path = args.image
    if args.make_demo:
        ImageDevice.create(path, block_count=1024, block_size=4096).close()
        dev = ImageDevice(path, block_size=4096, writable=True)
        info = build_ntfs_image(dev, total_clusters=1024, record_slots=64)
        dev.close()
        print(f"已生成合成 NTFS 镜像：{path}（{info.cluster_count} 簇 × {info.cluster_size} B）")

    dev = ImageDevice(path, block_size=args.block_size, writable=False)
    try:
        cat = MftCatalog.open(dev)
        stats = cat.scan()
        boot = cat.boot
        print(
            f"卷：sector={boot.bytes_per_sector} cluster={boot.cluster_size} "
            f"records={boot.record_size} mft_lcn={boot.mft_lcn} "
            f"clusters={boot.cluster_count}"
        )
        print(stats.summary(boot.cluster_size))
        for note in stats.notes[:8]:
            print(f"  · {note}")

        files = cat.list_files(None)
        top = sorted(files, key=lambda f: f.run_count, reverse=True)[: args.top]
        print(f"\n最碎的文件（前 {len(top)} 个）：")
        for f in top:
            print(
                f"  {f.run_count:>3} 段  {f.total_blocks * boot.cluster_size / 1048576:>8.1f} MiB  {f.path}"
            )
        free = cat.free_extents(None)
        print(f"\n自由区间 {len(free)} 段，共 {sum(e.length for e in free)} 块")
        return 0
    except NtfsScanError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1
    finally:
        dev.close()


def _cmd_smoke(args: argparse.Namespace) -> int:
    """无头冒烟：建窗口 → 载合成卷 → 扫描 → 规划 → 执行 → 逐块校验。

    存在的理由是**打包产物需要被验证**。源码树里 pytest 全绿，不代表
    PyInstaller 打出来的 exe 能起得来（缺 Qt 插件、缺 fonts、缺 hook 都会
    在运行时才炸）。这个子命令可以用退出码判定，适合放进构建脚本。
    """
    import os
    import time

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from .contract.commands import StartJob
    from .contract.enums import JobState
    from .contract.models import JobSpec
    from .engine.synth import apply_plan, verify

    try:
        from .ui.app import build_app
        from .ui.main_window import DEMO_DEVICE_PATH
    except ImportError as exc:
        print(f"[FAIL] 无法加载 GUI：{exc}", file=sys.stderr)
        return 2

    def pump(predicate, timeout: float, label: str) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            app.processEvents()
            if predicate():
                return True
            time.sleep(0.01)
        print(f"[FAIL] 等待超时：{label}", file=sys.stderr)
        return False

    app, window = build_app(["--demo"])
    window.show()
    try:
        if not pump(
            lambda: window._volume is not None
            and window._volume.device_path == DEMO_DEVICE_PATH,
            20.0,
            "演示卷装载",
        ):
            return 1
        window._on_scan()
        if not pump(lambda: window._plan is not None, 20.0, "扫描 + 规划"):
            return 1
        plan = window._plan
        if not plan.ops:
            print("[FAIL] 预演没有产生任何操作", file=sys.stderr)
            return 1

        spec = JobSpec(volume=window._volume, strategy=window._strategy, params=window._params)
        window.service.submit(StartJob(spec))
        if not pump(
            lambda: window.core.state
            in (JobState.DONE, JobState.FAILED, JobState.CANCELLED),
            60.0,
            "执行完成",
        ):
            return 1
        if window.core.state is not JobState.DONE:
            print(f"[FAIL] 结束状态 {window.core.state.value}", file=sys.stderr)
            return 1
        result = window.core.last_result
        if result is None or result.moved_ops == 0:
            print("[FAIL] 没有搬迁任何文件", file=sys.stderr)
            return 1

        after = apply_plan(window._demo_layout, plan)
        problems = verify(window._demo_device, after)
        if problems:
            print(f"[FAIL] 数据校验不一致 {len(problems)} 处", file=sys.stderr)
            for p in problems[:5]:
                print(f"       {p}", file=sys.stderr)
            return 1

        print(
            f"[PASS] GUI 冒烟：窗口可构建，搬迁 {result.moved_ops} 个文件 / "
            f"{result.blocks_done} 块，逐块校验一致（{len(after.files)} 个文件）"
        )
        return 0
    finally:
        window.close()
        if app is not None:
            app.quit()


def _cmd_gui(args: argparse.Namespace) -> int:
    try:
        from .ui.app import build_app
    except ImportError as exc:
        print(f"[FAIL] 无法加载 GUI：{exc}", file=sys.stderr)
        print("       未安装 PySide6 时可用 `lbamover selftest` 验证引擎。", file=sys.stderr)
        return 2
    extra = ["--demo"] if args.demo else []
    app, window = build_app(extra)
    window.show()
    return app.exec()


def _best_driver(device) -> None:  # pragma: no cover - 占位，保持签名可读
    return None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lbamover",
        description=f"LBAMover {VERSION_DISPLAY} — LBA 块级流式整理引擎",
    )
    p.add_argument("--version", action="version", version=f"LBAMover {VERSION_DISPLAY}")
    sub = p.add_subparsers(dest="cmd")

    g = sub.add_parser("gui", help="启动图形界面")
    g.add_argument("--demo", action="store_true", help="启动即载入合成卷")
    g.set_defaults(func=_cmd_gui)

    v = sub.add_parser("volumes", help="列出本机可整理的卷")
    v.set_defaults(func=_cmd_volumes)

    k = sub.add_parser("smoke", help="无头冒烟：建窗口、跑一次完整搬迁并校验")
    k.set_defaults(func=_cmd_smoke)

    n = sub.add_parser("ntfs", help="解析 NTFS 卷镜像，打印文件表（上真机前的验收）")
    n.add_argument("--image", required=True, help="卷镜像文件路径")
    n.add_argument("--block-size", type=int, default=4096, help="镜像的块大小（=簇大小）")
    n.add_argument("--top", type=int, default=10, help="列出最碎的前 N 个文件")
    n.add_argument("--make-demo", action="store_true", help="先生成一份合成 NTFS 镜像再解析")
    n.set_defaults(func=_cmd_ntfs)

    s = sub.add_parser("selftest", help="在内存合成卷上跑完整流水线并逐块校验")
    s.add_argument("--blocks", type=int, default=4096)
    s.add_argument("--block-size", type=int, default=4096)
    s.add_argument("--files", type=int, default=24)
    s.add_argument("--seed", type=int, default=20261101)
    s.add_argument("--chunk", type=int, default=64, help="每次读写的块数")
    s.add_argument("--depth", type=int, default=4, help="流水线队列深度")
    s.add_argument("--concurrency", type=int, default=2)
    s.add_argument("--retries", type=int, default=3)
    s.add_argument("--strategy", default=Strategy.COALESCE.value,
                   choices=[s.value for s in Strategy if s.writes_blocks])
    s.add_argument("--no-verify", action="store_true", help="关闭回读校验（更快）")
    s.add_argument("--quiet", action="store_true")
    s.set_defaults(func=_cmd_selftest)

    r = sub.add_parser("run", help="对卷镜像执行整理（离线演练）")
    r.add_argument("--image", required=True, help="镜像文件路径")
    r.add_argument("--layout", required=True, help="布局 JSON（files/free）")
    r.add_argument("--block-size", type=int, default=4096)
    r.add_argument("--chunk", type=int, default=64)
    r.add_argument("--strategy", default=Strategy.COALESCE.value,
                   choices=[s.value for s in Strategy if s.writes_blocks])
    r.set_defaults(func=_cmd_run)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        # 没给子命令时直接开界面，符合双击启动的直觉
        args = parser.parse_args(["gui", *(argv if argv is not None else sys.argv[1:])])
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\n[中断] 已停止", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"[FAIL] 参数错误：{exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"[FAIL] 设备/文件错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
