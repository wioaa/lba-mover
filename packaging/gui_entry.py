"""PyInstaller 入口：图形界面。

单独一个入口文件（而不是直接指向 ``lbamover/cli.py``）是为了让 spec 里
的 ``Analysis`` 有一个明确的、不随包结构变化而变的目标。
"""

from __future__ import annotations

import multiprocessing
import os
import sys

if __name__ == "__main__":
    # 冻结后必须调用 freeze_support：否则一旦将来引入 multiprocessing，
    # 子进程会重新执行整个 GUI 启动流程，表现为"双击弹出十几个窗口"。
    multiprocessing.freeze_support()

    # 让 `import lbamover` 在源码树里也能工作（开发时直接跑这个入口）
    _src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    if os.path.isdir(_src) and _src not in sys.path:
        sys.path.insert(0, _src)

    from lbamover.cli import main

    args = sys.argv[1:]
    # 无控制台的窗口程序无法把 stdout 交给调用方，但**退出码**可以。
    # 于是给打包验证留一个 `--smoke`：建完整窗口 → 跑一次搬迁 → 用退出码
    # 汇报结果。没有它就只能"双击看看有没有崩"，验不出数据是否正确。
    if args[:1] == ["--smoke"]:
        raise SystemExit(main(["smoke", *args[1:]]))

    raise SystemExit(main(["gui", *args]))
