@echo off
rem CapsWriter-Offline / Speak More client - headless launcher.
rem
rem stdout/stderr are redirected to a file so the process never touches a
rem console. rich would otherwise drive ANSI cursor control on a hidden
rem console window and can deadlock the asyncio event loop.
rem
rem NOTE: the console window must still EXIST (just stay hidden) -- the tray
rem icon needs a valid console HWND (see core/ui/tray.py: enable_min_to_tray).
rem So we cannot use pythonw.exe or CREATE_NO_WINDOW here.
rem
rem 日常不要双击这个文件：请用「启动 Speak More.vbs」。双击会弹出黑窗口。
rem 想看运行日志就右键托盘图标 -> 👁️ 显示/隐藏。

title Speak More

cd /d "%~dp0"
if not exist "logs" mkdir "logs"
".venv\Scripts\python.exe" start_client.py >> "logs\client_console.log" 2>&1
