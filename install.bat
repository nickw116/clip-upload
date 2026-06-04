@echo off
chcp 65001 >nul 2>&1
echo ========================================
echo   Clip Upload - Windows 安装脚本
echo ========================================
echo.

:: 检查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未检测到 Python，请先安装 Python 3.10+
    echo 下载地址: https://www.python.org/downloads/
    echo 安装时请勾选 "Add Python to PATH"
    pause
    exit /b 1
)

echo [1/3] 安装依赖...
pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo [错误] 依赖安装失败
    pause
    exit /b 1
)

echo [2/3] 创建启动快捷方式...
set SCRIPT_DIR=%~dp0
set SHORTCUT=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\ClipUpload.lnk

powershell -Command "$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut('%SHORTCUT%'); $s.TargetPath = 'pythonw'; $s.Arguments = '\"%SCRIPT_DIR%clip_upload.py\"'; $s.WorkingDirectory = '%SCRIPT_DIR%'; $s.Description = 'Clip Upload - 截图上传工具'; $s.Save()"

echo [3/3] 启动程序...
start "" pythonw "%~dp0clip_upload.py"

echo.
echo ========================================
echo   安装完成！
echo.
echo   使用方法:
echo     1. 微信截图 (Alt+A)
echo     2. 按 Ctrl+Alt+U 上传
echo     3. Ctrl+V 粘贴得到服务器路径
echo.
echo   托盘图标: 右下角系统托盘
echo   配置文件: %APPDATA%\clip-upload\config.json
echo ========================================
pause
