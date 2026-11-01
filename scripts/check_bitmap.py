"""位图 → 自由区间的对拍与计时。

    python scripts/check_bitmap.py
"""

from __future__ import annotations

import os
import random
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lbamover.ntfs.bitmap import free_runs, free_runs_naive  # noqa: E402


def correct() -> int:
    random.seed(7)
    bad = 0
    sizes = [1, 2, 7, 8, 9, 63, 64, 65, 1000, 4095, 4096, 4097, 65535, 65536, 65537]
    for trial in range(240):
        total = sizes[trial % len(sizes)]
        nb = (total + 7) // 8
        mode = trial % 4
        if mode == 0:
            bm = bytes(random.getrandbits(8) for _ in range(nb))
        elif mode == 1:
            bm = b"\xff" * nb
        elif mode == 2:
            bm = b"\x00" * nb
        else:
            bm = bytes(random.choice((0x00, 0xFF, 0x0F, 0xF0, 0x55)) for _ in range(nb))
        got, want = free_runs(bm, total), free_runs_naive(bm, total)
        if got != want:
            bad += 1
            if bad <= 3:
                print(f"MISMATCH total={total} mode={mode}\n  got ={got[:6]}\n  want={want[:6]}")
    for total, short in ((100, 3), (1000, 0), (65536, 10), (9, 1)):
        got = free_runs(b"\x00" * short, total)
        want = free_runs_naive(b"\x00" * short, total)
        if got != want:
            bad += 1
            print(f"SHORT MISMATCH total={total} short={short} got={got} want={want}")
    # 跨窗口边界：窗口是 1 MiB 字节 = 8388608 个簇，让空闲段横跨那条线
    total = 1 << 24
    bm = bytearray(b"\xff" * ((total + 7) // 8))
    edge = (1 << 20) * 8
    for c in range(edge - 6, edge + 8):
        bm[c >> 3] &= ~(1 << (c & 7)) & 0xFF
    got = free_runs(bytes(bm), total)
    if got != [(edge - 6, 14)]:
        bad += 1
        print(f"CROSS-WINDOW MISMATCH {got}")
    print(f"对拍：{'通过' if bad == 0 else f'{bad} 处不一致'}")
    return 0 if bad == 0 else 1


def timed() -> int:
    # 1 TiB / 4 KiB 簇 = 2.6 亿个簇，位图 31 MiB；真实卷上段数在几千量级
    clusters = 249 * 1024 * 1024 + 12345
    bm = bytearray(b"\xff" * ((clusters + 7) // 8))
    step = len(bm) // 3000
    for i in range(0, len(bm), max(1, step)):
        span = max(1, step // 3)
        bm[i : i + span] = b"\x00" * len(bm[i : i + span])
    t = time.monotonic()
    runs = free_runs(bytes(bm), clusters)
    print(f"1 TiB 位图（{len(bm) / 2**20:.0f} MiB，{len(runs)} 段）：{time.monotonic() - t:.3f}s")

    t = time.monotonic()
    n = 0
    for c in range(20_000_000):
        n += (bm[c >> 3] >> (c & 7)) & 1
    print(f"旧式逐簇循环 2000 万次对照：{time.monotonic() - t:.3f}s")
    return 0


if __name__ == "__main__":
    rc = correct()
    timed()
    raise SystemExit(rc)
