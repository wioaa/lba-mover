@echo off
setlocal
cd /d "%~dp0"

echo ==========================================
echo   LBA Tail Mover - 大文件末端 LBA 迁移
echo ==========================================
echo.

where python >nul 2>nul
if errorlevel 1 goto no_python

net session >nul 2>&1
if errorlevel 1 goto not_admin

echo [权限] 管理员身份已就绪，可以执行真实迁移。
echo.
python main.py
goto end

:not_admin
echo [提示] 当前不是管理员，程序将以「只读模式」启动：
echo        可以扫描、规划、对比布局，但无法执行真实迁移。
echo        如需执行迁移，请关闭本窗口，右键 run.bat
echo        选择「以管理员身份运行」。
echo.
python main.py
goto end

:no_python
echo [错误] 未检测到 python 命令。
echo        请安装 Python 3.11+ 并加入 PATH，或修改本文件指向正确的解释器。
pause
exit /b 1

:end
if errorlevel 1 pause
endlocal
