"""极简测试运行器：让测试既能被 pytest 收集，也能 `python tests/xxx.py` 直接跑。

不引入 pytest 依赖（项目约定只允许 PySide6 一个新依赖）。
"""

from __future__ import annotations

import sys
import traceback
from typing import Any, Dict, List, Tuple


def run_module_tests(namespace: Dict[str, Any], verbose: bool = True) -> Tuple[int, int]:
    """执行 ``namespace`` 里所有 ``test_*`` 可调用对象。

    Returns:
        ``(通过数, 失败数)``。
    """
    cases: List[Tuple[str, Any]] = sorted(
        (name, value)
        for name, value in namespace.items()
        if name.startswith("test_") and callable(value)
    )
    passed = 0
    failed = 0
    for name, func in cases:
        try:
            func()
        except Exception:  # noqa: BLE001 - 测试运行器要打印全部失败
            failed += 1
            print(f"[FAIL] {name}")
            traceback.print_exc()
        else:
            passed += 1
            if verbose:
                print(f"[ ok ] {name}")
    print(f"---- {passed} passed, {failed} failed ----")
    return passed, failed


def main(namespace: Dict[str, Any]) -> int:
    """``__main__`` 入口。"""
    passed, failed = run_module_tests(namespace)
    _ = passed
    return 1 if failed else 0


def exit_with(namespace: Dict[str, Any]) -> None:
    """给测试文件的 ``__main__`` 块用。"""
    sys.exit(main(namespace))
