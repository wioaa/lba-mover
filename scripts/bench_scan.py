"""扫描路径的计时台：分别给 MFT 与目录遍历两条路计时。

    python scripts/bench_scan.py R:

不改产物、不写盘，只打印各阶段耗时，用来判断"慢"到底慢在哪一层。
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lbamover.platform.volumes import enumerate_volumes  # noqa: E402

if not hasattr(os, "add_dll_directory") and os.name == "nt":
    os.add_dll_directory  # pragma: no cover


def _pick(mount: str):
    for v in enumerate_volumes():
        if v.mount_point.upper().startswith(mount.upper()[:2]):
            return v
    raise SystemExit(f"找不到卷 {mount}")


def bench_walk(volume) -> None:
    from lbamover.platform.dirscan import DirectoryCatalog

    t0 = time.monotonic()
    walk_only = 0
    count = 0
    from lbamover.platform.dirscan import _walk

    for _ in _walk(volume.mount_point):
        count += 1
    walk_only = time.monotonic() - t0
    print(f"  遍历阶段     {walk_only:7.2f}s  {count} 项")

    t1 = time.monotonic()
    cat = DirectoryCatalog(volume)
    files = cat.list_files(volume)
    total = time.monotonic() - t1
    print(f"  含簇查询     {total:7.2f}s  {len(files)} 个文件")
    if len(files):
        print(f"  每文件均摊   {(total - walk_only) / max(1, len(files)) * 1000:7.3f}ms")


def bench_mft(volume) -> None:
    from lbamover.devices import DeviceSpec, open_device
    from lbamover.ntfs import MftCatalog

    dev = open_device(
        DeviceSpec(
            kind="win32",
            path=volume.device_path,
            block_size=volume.geometry.block_size,
            writable=False,
            unbuffered=True,
        )
    )
    cat = MftCatalog(dev, root=volume.mount_point)

    t = time.monotonic()
    boot = cat.boot
    print(f"  引导扇区     {time.monotonic() - t:7.2f}s  簇 {boot.cluster_size} 记录 {boot.record_size}")

    t = time.monotonic()
    runs = cat._mft_runs()
    print(f"  $MFT 位置    {time.monotonic() - t:7.2f}s  {len(runs)} 段")

    t = time.monotonic()
    records, bad, entries = cat._read_records(runs)
    read_s = time.monotonic() - t
    print(f"  读+解析记录  {read_s:7.2f}s  {len(records)} 条在用 / {bad} 坏")

    t = time.monotonic()
    files, skipped, _ = cat._build_files(records, entries, boot)
    print(f"  组装路径     {time.monotonic() - t:7.2f}s  {len(files)} 可用 / {skipped} 跳过")

    t = time.monotonic()
    free, source = cat._free_extents(records, files, boot)
    print(f"  自由空间     {time.monotonic() - t:7.2f}s  {len(free)} 段（{source}）")
    cat.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mount", nargs="?", default="")
    ap.add_argument("--skip-walk", action="store_true")
    args = ap.parse_args()
    volume = _pick(args.mount) if args.mount else enumerate_volumes()[0]
    print(f"卷 {volume.mount_point} {volume.filesystem} "
          f"{volume.geometry.total_bytes / 2**30:.1f} GiB · block {volume.geometry.block_size}")

    print("[MFT 快路径]")
    try:
        bench_mft(volume)
    except Exception as exc:  # 未提权是常态，不是失败
        print(f"  不可用：{type(exc).__name__}: {exc}")

    if not args.skip_walk:
        print("[目录遍历慢路径]")
        bench_walk(volume)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
