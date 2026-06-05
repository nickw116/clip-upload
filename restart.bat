@echo off
chcp 65001 >/dev/null
echo ========================================
echo   Clip Upload 重启工具
echo ========================================
echo.
echo [1] 关闭所有旧版本...
taskkill /f /im ClipUpload.exe >/dev/null 2>&1
taskkill /f /im "ClipUpload (1).exe" >/dev/null 2>&1
taskkill /f /im "ClipUpload (2).exe" >/dev/null 2>&1
taskkill /f /im "ClipUpload (3).exe" >/dev/null 2>&1
taskkill /f /im "ClipUpload (4).exe" >/dev/null 2>&1
del /f /q "%APPDATA%\clip-upload\clip_upload.lock" >/dev/null 2>&1
timeout /t 2 /nobreak >/dev/null

echo [2] 启动新版本...
start "" "%~dp0ClipUpload.exe"

echo.
echo 完成！请检查右下角系统托盘（可能需要点击 ^ 展开隐藏图标）
echo.
pause
