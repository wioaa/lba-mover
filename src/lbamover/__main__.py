"""``python -m lbamover`` 入口。无子命令时直接开界面。"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
