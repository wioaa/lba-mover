"""PyInstaller 入口：命令行。

与 GUI 入口唯一的差别是打包成**控制台**程序（``console=True``），
这样 ``LBAMover-cli.exe volumes`` 的输出能正常出现在终端里。
"""

from __future__ import annotations

import multiprocessing
import os
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()

    _src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    if os.path.isdir(_src) and _src not in sys.path:
        sys.path.insert(0, _src)

    from lbamover.cli import main

    raise SystemExit(main(sys.argv[1:]))
