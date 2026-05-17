Set WshShell = CreateObject("WScript.Shell")
WshShell.Run "taskkill /f /im pythonw.exe", 0
WshShell.Run "taskkill /f /im node.exe", 0
Set WshShell = Nothing
