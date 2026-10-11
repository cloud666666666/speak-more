@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在启动 CapsWriter-Offline 服务端 (qwen_asr)...
echo 首次加载模型约需 10 秒，加载完成后会自动最小化到托盘。
echo.
".venv\Scripts\python.exe" start_server.py
echo.
echo 服务端已退出。
pause
