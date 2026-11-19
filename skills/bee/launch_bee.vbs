Set FSO = CreateObject("Scripting.FileSystemObject")
ScriptDir = FSO.GetParentFolderName(WScript.ScriptFullName)
WidgetPath = FSO.BuildPath(ScriptDir, "scripts\bee_widget.py")
Set WshShell = CreateObject("WScript.Shell")
WshShell.Run "pythonw """ & WidgetPath & """", 0, False
