# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：单文件、无控制台的 GUI 应用。

构建（在项目根目录）::

    python -m PyInstaller --clean --noconfirm LBA-Tail-Mover.spec

产物：``dist/LBA-Tail-Mover.exe``（onefile，双击即用，无需 Python 环境）。

关键点
--------
* **hiddenimports**：收罗所有「函数内动态 import」的模块。最要命的是
  ``app.win32.mover``——它是**唯一的写路径**（``FSCTL_MOVE_FILE``），只在
  ``MoveWorker.run()`` / 回滚 Worker 内按需 import（见 ``app/core/scanner.py``
  的 1009 / 1238 行）。PyInstaller 的静态分析看不见这类 import，若不显式声明，
  打包出来的 EXE 一进「执行模式」就会 ``ModuleNotFoundError``。
  同理还有 MFT 双通道的 ``app.core.mft`` / ``app.win32.mft_reader``。
* **console=False**：GUI 应用不弹黑色控制台。若需排查打包问题，可临时
  ``set LBA_BUILD_CONSOLE=1`` 出一个带控制台的版本（Analysis 完全相同，
  打包模块集一致，仅子系统不同）。
* **uac_admin=True**：**默认以管理员启动**。PyInstaller 会生成并嵌入
  ``requireAdministrator`` 清单，双击 EXE 即弹出 UAC 提权（同时保留
  Common-Controls / 高 DPI 感知，不回归无边框界面）。去掉该行即恢复为
  「按启动者权限运行」。
* **excludes**：环境里装了但本程序用不到的重依赖（WebEngine / Pdf），剔除以
  减小体积、加快 onefile 解压启动。
* Qt 插件（platforms / styles / imageformats）由 PySide6 的 PyInstaller hook
  自动收集，无需手工 ``--add-data``；界面图标与 QSS 全部代码自绘，无外部资源。
"""

import os

block_cipher = None

# 仅供排查：置 1 时产出带控制台的 EXE（其余完全一致）
BUILD_CONSOLE = os.environ.get("LBA_BUILD_CONSOLE") == "1"
# 输出名可用环境变量覆盖（排查时避免覆盖正式产物）
BUILD_NAME = os.environ.get("LBA_BUILD_NAME", "LBA-Tail-Mover")
# 默认请求管理员；LBA_NO_ADMIN=1 可出免提权调试版（配合 LBA_BUILD_CONSOLE=1 用于
# 在非交互/无 UAC 的环境里冒烟验证打包完整性，如跑 --selftest）
BUILD_UAC_ADMIN = os.environ.get("LBA_NO_ADMIN") != "1"

#: 只在函数体内动态 import、静态分析抓不到的模块（务必手工声明）。
hiddenimports = [
    "app.win32.mover",          # 唯一写路径，仅 MoveWorker/回滚内按需 import
    "app.core.mft",             # MFT 通道 FILE 记录解析
    "app.win32.mft_reader",     # $MFT 原始读（唯一原始读点，只读）
    "app.core.scheduler",       # build_tasks 等在函数内 import
    "app.core.io_profile",      # merge_segments 等在函数内 import
    "app.core.ranges",          # subtract_runs 等在函数内 import
    "app.core.models",          # 双保险
]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "PySide6_WebEngine",
        "PySide6_WebEngineCore",
        "PySide6_WebEngineWidgets",
        "PySide6_Pdf",
        "PySide6_PdfWidgets",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name=BUILD_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=BUILD_CONSOLE,
    uac_admin=BUILD_UAC_ADMIN,  # 默认以管理员启动：嵌入 requireAdministrator 清单，双击即请求 UAC 提权
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version="tools/version_info.txt",
)
