"""拆解未提权扫描的单文件成本：打开 / 分配缓冲 / 查询 各占多少。

    python scripts/bench_query.py R:
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lbamover.devices.win32 import AlignedBuffer, Win32Native  # noqa: E402
from lbamover.platform.dirscan import _walk  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mount", nargs="?", default="R:\\")
    ap.add_argument("--limit", type=int, default=1500)
    args = ap.parse_args()
    mount = args.mount if args.mount.endswith("\\") else args.mount + "\\"

    paths = []
    for path, attrs, size in _walk(mount):
        if attrs & 0x10 or attrs & 0x400 or size <= 0:
            continue
        paths.append(path)
        if len(paths) >= args.limit:
            break
    print(f"{len(paths)} 个文件")

    # 1. 纯打开/关闭
    t = time.monotonic()
    opens = 0
    for p in paths:
        try:
            h = Win32Native.open_file(p, writable=False, attributes_only=True)
        except Exception:
            continue
        opens += 1
        Win32Native.close(h)
    open_s = time.monotonic() - t
    print(f"打开+关闭  {open_s:6.2f}s  {open_s / max(1, opens) * 1000:6.3f}ms/个")

    # 2. 复用一块缓冲 vs 每次新建
    t = time.monotonic()
    for _ in range(len(paths)):
        b = AlignedBuffer(64 * 1024)
        b.free()
    alloc_s = time.monotonic() - t
    print(f"缓冲分配   {alloc_s:6.2f}s  {alloc_s / len(paths) * 1000:6.3f}ms/个")

    buf = AlignedBuffer(64 * 1024)
    t = time.monotonic()
    queried = 0
    for p in paths:
        try:
            h = Win32Native.open_file(p, writable=False, attributes_only=True)
        except Exception:
            continue
        try:
            Win32Native.query_retrieval_pointers(h)
            queried += 1
        except Exception:
            pass
        finally:
            Win32Native.close(h)
    buf.free()
    both_s = time.monotonic() - t
    print(f"打开+查询  {both_s:6.2f}s  {both_s / max(1, queried) * 1000:6.3f}ms/个")

    t = time.monotonic()
    for p in paths:
        try:
            h = Win32Native.open_file(p, writable=False, attributes_only=True)
        except Exception:
            continue
        try:
            Win32Native._query_with_buffer(h, buf)
        except Exception:
            pass
        finally:
            Win32Native.close(h)
    buf.free()
    reuse_s = time.monotonic() - t
    print(f"复用缓冲   {reuse_s:6.2f}s  {reuse_s / max(1, queried) * 1000:6.3f}ms/个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
