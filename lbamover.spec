# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

产物是**两个 exe 共用一个运行库目录**（onedir）：

    dist/LBAMover/
    ├── LBAMover.exe        图形界面（无控制台窗口）
    ├── LBAMover-cli.exe    命令行（有控制台，selftest / volumes / smoke）
    └── _internal/          Python 运行时 + Qt 插件（只有一份）

为什么不用 onefile：onefile 每次启动都要把 ~80MB 解压到临时目录，冷启动
3~5 秒，杀软还会对自解压行为额外扫描。磁盘工具经常被双击后立刻要看到
界面，这个代价不合适。onedir 冷启动 0.6 秒左右，缺点只是"一个文件夹"。
"""

import os
import sys

ROOT = os.path.abspath(os.path.dirname(SPEC))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

from lbamover import REVISION, VERSION  # noqa: E402

ICON = os.path.join(ROOT, "packaging", "lbamover.ico")
VERSION_FILE = os.path.join(ROOT, "packaging", "version_info.txt")

# 显式排除用不到的 Qt 模块。PySide6 的 hook 会把整个 Qt 目录拉进来，
# 不排除的话产物 300MB 起步；这些模块本项目一行都没用到。
EXCLUDES = [
    "tkinter",
    "unittest",
    "pydoc_data",
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DRender",
    "PySide6.QtBluetooth",
    "PySide6.QtCharts",
    "PySide6.QtConcurrent",
    "PySide6.QtDataVisualization",
    "PySide6.QtDesigner",
    "PySide6.QtHelp",
    "PySide6.QtLocation",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtNetworkAuth",
    "PySide6.QtNfc",
    "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtPositioning",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQuickControls2",
    "PySide6.QtQuickWidgets",
    "PySide6.QtRemoteObjects",
    "PySide6.QtScxml",
    "PySide6.QtSensors",
    "PySide6.QtSerialBus",
    "PySide6.QtSerialPort",
    "PySide6.QtSpatialAudio",
    "PySide6.QtSql",
    "PySide6.QtStateMachine",
    "PySide6.QtSvg",
    "PySide6.QtSvgWidgets",
    "PySide6.QtTest",
    "PySide6.QtTextToSpeech",
    "PySide6.QtUiTools",
    "PySide6.QtWebChannel",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebSockets",
    "pytest",
]

# 函数内延迟导入的模块，静态分析可能跟丢；显式列出来更保险
HIDDEN = [
    "lbamover.devices.win32",
    "lbamover.platform.privilege",
    "lbamover.platform.volumes",
    "lbamover.ui.main_window",
    "lbamover.ui.widgets",
]


def analyze(entry: str) -> Analysis:  # noqa: F821 - SPEC 内置名
    return Analysis(
        [os.path.join(ROOT, "packaging", entry)],
        pathex=[SRC],
        binaries=[],
        datas=[],
        hiddenimports=HIDDEN,
        hookspath=[],
        hooksconfig={},
        runtime_hooks=[],
        excludes=EXCLUDES,
        noarchive=False,
        optimize=0,
    )


a_gui = analyze("gui_entry.py")
a_cli = analyze("cli_entry.py")

pyz_gui = PYZ(a_gui.pure)  # noqa: F821
pyz_cli = PYZ(a_cli.pure)  # noqa: F821

exe_gui = EXE(
    pyz_gui,
    a_gui.scripts,
    [],
    exclude_binaries=True,
    name="LBAMover",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=ICON,
    version=VERSION_FILE,
)

exe_cli = EXE(
    pyz_cli,
    a_cli.scripts,
    [],
    exclude_binaries=True,
    name="LBAMover-cli",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    icon=ICON,
    version=VERSION_FILE,
)

coll = COLLECT(  # noqa: F821
    exe_gui,
    a_gui.binaries,
    a_gui.datas,
    exe_cli,
    a_cli.binaries,
    a_cli.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="LBAMover",
)
