' ============================================================
'  CapsWriter-Offline - silent one-click launcher
'
'  只启动客户端，**不再单独启动服务端**。
'
'  服务端现在由客户端自己按需拉起、退出时自动收掉，
'  见 core/client/server_lifecycle.py。
'  这样托盘里只会看到一个图标（服务端的托盘图标已由补丁关闭，
'  控制项并到了客户端菜单的「🔌 重启服务端」）。
'
'  幂等：已经在跑的进程不会被重复启动，可以无脑双击。
'
'  NOTE: the real process is started by _run_client_headless.bat,
'  which redirects stdout/stderr into logs\client_console.log.
'  That redirection is REQUIRED, not cosmetic: with a hidden console,
'  rich's ANSI cursor writes (the "正在接收音频" spinner in
'  core/server/connection/ws_recv.py) can deadlock the asyncio event
'  loop. Writing to a file makes rich see a non-terminal and skip
'  that code path entirely.
' ============================================================

Option Explicit

Dim sh, fso, base, py
Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

base = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = base
py = base & "\.venv\Scripts\python.exe"

If Not fso.FileExists(py) Then
    MsgBox "Python venv not found:" & vbCrLf & py & vbCrLf & vbCrLf & _
           "Run 'uv sync' in " & base & " first.", _
           16, "CapsWriter-Offline"
    WScript.Quit 1
End If

If Not IsRunning("start_client.py") Then
    sh.Run """" & base & "\_run_client_headless.bat""", 0, False
End If

' 客户端会自动拉起服务端（端口 6016 已经在监听的话就直接复用）。
' 控制台输出：logs\client_console.log 和 logs\server_console.log

Function IsRunning(needle)
    Dim wmi, procs, p
    IsRunning = False
    On Error Resume Next
    Set wmi = GetObject("winmgmts:\\.\root\cimv2")
    Set procs = wmi.ExecQuery("SELECT CommandLine FROM Win32_Process WHERE Name='python.exe'")
    For Each p In procs
        If Not IsNull(p.CommandLine) Then
            If InStr(p.CommandLine, needle) > 0 Then
                IsRunning = True
                Exit Function
            End If
        End If
    Next
    On Error GoTo 0
End Function
