"""真机启动校验：用**真实** windows 平台跑一次打包好的 GUI exe。

``build.py`` 里的 ``--smoke`` 走 offscreen 平台，只能证明代码路径不崩。
这一步会真的弹出一个窗口，验证 qwindows 插件、字体、窗口创建在真实平台
上都成立 —— 也就是"双击能用"。

因为会弹窗，所以**不进构建脚本**，作为手工步骤：

    python packaging/verify_gui_launch.py

窗口会在 5 秒后自动关闭。退出码 0 = 主窗口标题匹配到 LBAMover。
"""
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(_ROOT, "dist", "LBAMover", "LBAMover.exe")
proc = subprocess.Popen([EXE, "--demo"], cwd=_ROOT, env={})
time.sleep(5)

if proc.poll() is not None:
    print(f"FAIL 进程提前退出，退出码 {proc.returncode}")
    sys.exit(1)

found = []
user32 = ctypes.WinDLL("user32", use_last_error=True)
EnumWindows = user32.EnumWindows
EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM), wt.LPARAM]
GetWindowTextW = user32.GetWindowTextW
GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
GetWindowThreadProcessId = user32.GetWindowThreadProcessId
GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]

@ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
def cb(hwnd, _l):
    pid = wt.DWORD(0)
    GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value == proc.pid:
        buf = ctypes.create_unicode_buffer(512)
        GetWindowTextW(hwnd, buf, 512)
        if buf.value.strip():
            found.append(buf.value)
    return True

EnumWindows(cb, 0)
proc.terminate()
try:
    proc.wait(timeout=8)
except subprocess.TimeoutExpired:
    proc.kill()

if not found:
    print("FAIL 进程存活但没有可见窗口（可能是无头/插件缺失）")
    sys.exit(1)
main = [t for t in found if "LBAMover" in t]
print("PASS 主窗口：", main[0] if main else found)
sys.exit(0 if main else 1)
