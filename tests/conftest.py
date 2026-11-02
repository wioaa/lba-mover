"""pytest 全局配置。

两件事：

1. 把 ``src`` 放进 ``sys.path``，让测试不依赖 ``pip install -e``；
2. 在**导入 PySide6 之前**把 Qt 平台设成 offscreen，否则无显示环境会直接
   在 import 阶段崩掉（报 "could not connect to display"）。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.*=false")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

#: 本次测试会话专用的配置目录（见 ``pytest_configure``）
_SETTINGS_DIR = ""


def pytest_configure(config):  # noqa: ARG001
    global _SETTINGS_DIR
    # 断点/流水默认写到仓库内 .lbamover/，测试期间统一挪到系统临时目录
    os.environ.setdefault("LBAMOVER_STATE_DIR", os.path.join(_ROOT, ".tmp", "state"))
    # 用户配置同理：**绝不能碰真实的 %APPDATA%\LBAMover\settings.json**。
    # 界面测试会构造并关闭主窗口，而关窗就会保存配置 —— 不隔离的话，
    # 跑一次测试就把开发机上的设置改掉，接着冒烟会因为恢复了这些设置而失败
    # （"测试污染了被测对象"，最难查的一类）。
    _SETTINGS_DIR = tempfile.mkdtemp(prefix="lbamover-settings-")
    os.environ["LBAMOVER_CONFIG"] = os.path.join(_SETTINGS_DIR, "settings.json")


def pytest_unconfigure(config):  # noqa: ARG001
    if _SETTINGS_DIR:
        shutil.rmtree(_SETTINGS_DIR, ignore_errors=True)


import pytest  # noqa: E402  —— 放在下面是为了让环境变量先就位


@pytest.fixture(autouse=True)
def _clean_settings():
    """每个用例从"没有配置文件"开始。

    不这么做的话，前一个用例关窗时保存的策略会被后一个用例恢复 ——
    测试之间通过磁盘上的配置互相影响，失败顺序还取决于执行次序。
    """
    path = os.environ.get("LBAMOVER_CONFIG")
    if path:
        try:
            os.unlink(path)
        except OSError:
            pass
    yield
