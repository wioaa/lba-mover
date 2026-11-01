"""在**真实卷**上核对位图解析：新实现 vs 逐位朴素实现。

    python scripts/verify_bitmap_real.py R:

自由区间直接决定规划器往哪写，所以这一步不能只靠合成数据。朴素实现对
1 亿簇的卷要跑几十秒，只在这支脚本里跑一次。
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lbamover.devices import DeviceSpec, open_device  # noqa: E402
from lbamover.ntfs import MftCatalog  # noqa: E402
from lbamover.ntfs.bitmap import free_runs, free_runs_naive  # noqa: E402
from lbamover.platform.volumes import enumerate_volumes  # noqa: E402

captured: dict[str, bytes] = {}
_orig = MftCatalog._read_bitmap


def _spy(self, records, boot):
    data = _orig(self, records, boot)
    if data is not None:
        captured["bitmap"] = data
    return data


MftCatalog._read_bitmap = _spy


def main() -> int:
    mount = (sys.argv[1] if len(sys.argv) > 1 else "R:").upper()[:2]
    vol = next(v for v in enumerate_volumes() if v.mount_point.upper().startswith(mount))
    dev = open_device(
        DeviceSpec(
            kind="win32",
            path=vol.device_path,
            block_size=vol.geometry.block_size,
            writable=False,
            unbuffered=True,
        )
    )
    cat = MftCatalog(dev, root=vol.mount_point)
    cat.scan()
    boot = cat.boot
    total = boot.cluster_count
    cat.close()

    bm = captured.get("bitmap")
    if bm is None:
        print("这个卷拿不到 $Bitmap（可能未提权），无法核对")
        return 2

    t = time.monotonic()
    fast = free_runs(bm, total)
    fast_s = time.monotonic() - t
    t = time.monotonic()
    slow = free_runs_naive(bm, total)
    slow_s = time.monotonic() - t

    same = [tuple(x) for x in fast] == [tuple(x) for x in slow]
    print(f"卷 {vol.mount_point} 簇数 {total:,} 位图 {len(bm) / 2**20:.2f} MiB")
    print(f"新实现 {fast_s:.3f}s / {len(fast)} 段；朴素 {slow_s:.3f}s / {len(slow)} 段")
    print(f"自由簇数 {sum(n for _, n in fast):,}（{sum(n for _, n in fast) * boot.cluster_size / 2**30:.2f} GiB）")
    print("结果一致" if same else "!!! 结果不一致 !!!")
    if not same:
        for i, (a, b) in enumerate(zip(fast, slow)):
            if a != b:
                print(f"  首个差异 @{i}: 新 {a} vs 朴素 {b}")
                break
    return 0 if same else 1


if __name__ == "__main__":
    raise SystemExit(main())
