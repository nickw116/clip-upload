@echo off
chcp 65001 >nul 2>&1
echo ========================================
echo   Clip Upload - 打包 exe
echo ========================================
echo.

:: 检查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未检测到 Python，请先安装 Python 3.10+
    pause
    exit /b 1
)

echo [1/3] 安装依赖...
pip install pyinstaller Pillow keyboard pystray win10toast pywin32

echo [2/3] 打包 exe...
pyinstaller --noconfirm --onefile --windowed ^
    --hidden-import=keyboard ^
    --hidden-import=pystray ^
    --hidden-import=win10toast ^
    --hidden-import=win32clipboard ^
    --hidden-import=win32con ^
    --name=ClipUpload ^
    clip_upload.py

echo [3/3] 完成!
echo.
echo   exe 文件位置: dist\ClipUpload.exe
echo   可直接拷贝使用，无需安装 Python
echo.
pause
