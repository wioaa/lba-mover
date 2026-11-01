"""测并发度对「逐文件查簇映射」的加速比。

    python scripts/bench_workers.py "C:\\Windows\\System32" --workers 1,2,4,8,12
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lbamover.contract.enums import VolumeKind  # noqa: E402
from lbamover.contract.models import Geometry, VolumeInfo  # noqa: E402
from lbamover.platform.dirscan import DirectoryCatalog  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="?", default=r"C:\Windows\System32")
    ap.add_argument("--workers", default="1,2,4,8,12")
    ap.add_argument("--limit", type=int, default=20000)
    ap.add_argument("--null-query", action="store_true",
                    help="用假查询替换系统调用，用来量 Python 侧开销")
    args = ap.parse_args()
    root = args.root if args.root.endswith("\\") else args.root + "\\"
    vol = VolumeInfo(
        device_path="x",
        mount_point=root,
        label="bench",
        filesystem="NTFS",
        kind=VolumeKind.FIXED,
        geometry=Geometry(4096, 1 << 30),
        free_bytes=0,
        writable=False,
    )
    for w in [int(x) for x in args.workers.split(",")]:
        t = time.monotonic()
        kw = {"query": (lambda p: ((0, 1000, 4),))} if args.null_query else {}
        cat = DirectoryCatalog(vol, workers=w, max_files=args.limit, **kw)
        files = cat.list_files()
        dt = time.monotonic() - t
        print(f"workers={w:2d}  {dt:6.2f}s  {len(files):6d} 文件  "
              f"{len(files) / max(dt, 1e-9):8.0f} 文件/秒")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
