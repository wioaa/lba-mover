"""对 MFT 扫描做一次剖析，看时间到底花在哪。

    python scripts/prof_scan.py R:
"""

from __future__ import annotations

import cProfile
import os
import pstats
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lbamover.devices import DeviceSpec, open_device  # noqa: E402
from lbamover.ntfs import MftCatalog  # noqa: E402
from lbamover.platform.volumes import enumerate_volumes  # noqa: E402


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
    prof = cProfile.Profile()
    prof.enable()
    cat.scan()
    prof.disable()
    pstats.Stats(prof).sort_stats("tottime").print_stats(18)
    cat.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
