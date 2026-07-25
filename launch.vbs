' Запуск приложения полностью без окна консоли/терминала.
' Ярлык на рабочем столе указывает сюда, а не напрямую на python.
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
pythonw = scriptDir & "\.venv\Scripts\pythonw.exe"
mainPy = scriptDir & "\main.py"

Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = scriptDir
' 0 = окно полностью скрыто, False = не ждать завершения
shell.Run """" & pythonw & """ """ & mainPy & """", 0, False
