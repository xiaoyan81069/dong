Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "C:\Users\15927\.qwenpaw\workspaces\default\exes"
WshShell.Run "C:\Users\15927\AppData\Local\Programs\Python\Python312\pythonw.exe -m dong", 0
Set WshShell = Nothing
