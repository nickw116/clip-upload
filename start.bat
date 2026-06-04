@echo off
chcp 65001 >nul 2>&1
python "%~dp0clip_upload.py"
if errorlevel 1 (
    echo.
    echo 运行出错，按任意键退出...
    pause >nul
)
