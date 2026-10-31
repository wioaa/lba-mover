"""pytest 全局配置。

两件事：

1. 把 ``src`` 放进 ``sys.path``，让测试不依赖 ``pip install -e``；
2. 在**导入 PySide6 之前**把 Qt 平台设成 offscreen，否则无显示环境会直接
   在 import 阶段崩掉（报 "could not connect to display"）。
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.*=false")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


def pytest_configure(config):  # noqa: ARG001
    # 断点/流水默认写到仓库内 .lbamover/，测试期间统一挪到系统临时目录
    os.environ.setdefault("LBAMOVER_STATE_DIR", os.path.join(_ROOT, ".tmp", "state"))
