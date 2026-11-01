"""裸测：CreateFileW + DeviceIoControl 在多线程下到底能不能并行。

    python scripts/bench_raw_io.py "C:\\Windows\\System32" 8000 1,4,8,16
"""

from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lbamover.devices.win32 import Win32Native  # noqa: E402
from lbamover.platform.dirscan import _walk  # noqa: E402


def collect(root: str, limit: int) -> list[str]:
    out = []
    for path, attrs, size in _walk(root):
        if attrs & 0x10 or attrs & 0x400 or size <= 0:
            continue
        out.append(path)
        if len(out) >= limit:
            break
    return out


def one(path: str) -> int:
    try:
        h = Win32Native.open_file(path, writable=False, attributes_only=True)
    except Exception:
        return 0
    try:
        return len(Win32Native.query_retrieval_pointers(h))
    except Exception:
        return 0
    finally:
        Win32Native.close(h)


def main() -> int:
    root = sys.argv[1] if len(sys.argv) > 1 else r"C:\Windows\System32"
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 8000
    plan = sys.argv[3] if len(sys.argv) > 3 else "1,4,8,16"
    paths = collect(root, limit)
    print(f"{len(paths)} 个文件")
    for w in [int(x) for x in plan.split(",")]:
        t = time.monotonic()
        if w == 1:
            total = sum(one(p) for p in paths)
        else:
            with ThreadPoolExecutor(max_workers=w) as pool:
                total = sum(pool.map(one, paths))
        dt = time.monotonic() - t
        print(f"线程 {w:2d}  {dt:6.2f}s  {len(paths) / dt:8.0f} 文件/秒  "
              f"{dt / len(paths) * 1e6:6.1f}µs/文件  (共 {total} 段)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
