@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在启动 CapsWriter-Offline 客户端...
echo 需要先启动服务端。启动后窗口会自动最小化到托盘。
echo.
echo 使用方法：按住 CapsLock 键（或鼠标侧键 X2）说话，松开自动上屏。
echo.
".venv\Scripts\python.exe" start_client.py
echo.
echo 客户端已退出。
pause
