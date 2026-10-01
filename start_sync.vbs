' 公歧导航同步服务 - 开机静默自启
' 作用：开机后在后台静默运行 sync_server.py（端口8787），无黑窗口
Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "C:\Users\gongq\Doubao\chats\2026-10-01\new-chat-2\我的导航站\_sync"
WshShell.Run "cmd /c python sync_server.py", 0, False
