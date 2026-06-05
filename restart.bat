@echo off
chcp 65001 >/dev/null
echo 正在关闭旧版本...
taskkill /f /im ClipUpload.exe >/dev/null 2>&1
timeout /t 2 /nobreak >/dev/null
echo 启动新版本...
start "" "%~dp0ClipUpload.exe"
echo 完成！
exit
