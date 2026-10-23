#!/usr/bin/env python3
"""汇总运行 lba-mover 全部单元测试，输出 pytest 风格摘要，供 testflow 解析。

背景：lba-mover 的测试是「每个 ``tests/test_*.py`` 自带 ``tests/_harness.py``、
作为独立入口运行、退出码 0/1」的形式，没有 pytest 那样的单一收集入口。
本脚本把它们汇成 testflow 的一个步骤：子进程逐个跑 → 聚合计数 → 失败项转成
``FAILED <文件>::<用例>`` 行 → 末尾打一行 ``N passed, M failed``。这套输出正好
是 testflow 内置 **pytest 解析器**认识的格式，因此 ``tools/testflow.py`` 可以
原样拷贝、不必改一行。

用法::

    python tools/run_all_tests.py            # 跑全部
    python tools/run_all_tests.py -k relay   # 只跑文件名含 relay 的测试文件
    # 经 testflow：python tools/testflow.py -k relay

退出码：0=全过，1=有用例失败，3=一个测试文件都没跑到（配置/环境问题）。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"

# 每个测试文件结尾的汇总行：---- 258 passed, 0 failed ----
_SUMMARY = re.compile(r"----\s*(\d+)\s+passed,\s*(\d+)\s+failed\s*----")
# 失败用例行：[FAIL] test_name
_FAILED = re.compile(r"^\[FAIL\]\s+(\S+)", re.M)


def main(argv: list[str]) -> int:
    files = sorted(TESTS.glob("test_*.py"))
    if len(argv) >= 2 and argv[0] == "-k":
        files = [f for f in files if argv[1] in f.name]
    if not files:
        print("no tests ran")
        return 3

    total_passed = total_failed = 0
    fail_lines: list[str] = []
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        proc = subprocess.run(
            [sys.executable, str(path)],
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        out = proc.stdout.decode("utf-8", "replace")
        summary = None
        for summary in _SUMMARY.finditer(out):
            pass  # 取最后一个汇总行
        if summary is not None:
            total_passed += int(summary.group(1))
            total_failed += int(summary.group(2))
        for name in _FAILED.findall(out):
            fail_lines.append(f"FAILED {rel}::{name} - see file output")
        if summary is None and proc.returncode != 0:
            # 文件在打出汇总行前就崩了（import 错、收集失败等）
            total_failed += 1
            fail_lines.append(
                f"FAILED {rel} - crashed before summary (exit {proc.returncode})"
            )

    for line in fail_lines:
        print(line)
    parts = []
    if total_passed:
        parts.append(f"{total_passed} passed")
    if total_failed:
        parts.append(f"{total_failed} failed")
    print(", ".join(parts) if parts else "0 passed")
    return 1 if total_failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
