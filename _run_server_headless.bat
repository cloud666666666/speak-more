@echo off
rem CapsWriter-Offline 服务端 —— 手动启动用（无窗口，输出重定向到文件）
rem
rem 日常不需要双击这个：客户端会用 core/client/server_lifecycle.py
rem 自动把服务端拉起来（用 CREATE_NO_WINDOW + 输出重定向），
rem 退出客户端时也会自动收掉它。这个 bat 只在你想单独跑服务端
rem 调试时用。
rem
rem 输出重定向是必须的，不是好看：rich 对隐藏控制台做 ANSI 光标控制
rem 会让 asyncio 事件循环死锁（见 core/server/connection/ws_recv.py 的 spinner）。
cd /d "%~dp0"
if not exist "logs" mkdir "logs"
".venv\Scripts\python.exe" start_server.py >> "logs\server_console.log" 2>&1
