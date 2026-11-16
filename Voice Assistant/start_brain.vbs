' Starts Triton's big brain (brain_server.py) in the background, no window. setup_laptop.ps1 puts this in
' the Startup folder so the N95 can always reach it; log: brain_server.log. "Start Big Brain.bat" shows a window.
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = dir
sh.Run """" & dir & "\.venv\Scripts\pythonw.exe"" """ & dir & "\brain_server.py""", 0, False
