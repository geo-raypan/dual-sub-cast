' Double-click this file to start the media server (hidden, no console window)
' and open the sender page in Chrome. No terminal typing needed.
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)

Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = scriptDir

' Kill any leftover python.exe from a previous run first -- otherwise two
' server instances end up bound to the same port and fight over requests,
' which shows up as inconsistent behavior between pages/devices.
' NOTE: this kills ALL python.exe processes on this machine, not just ours.
On Error Resume Next
shell.Run "taskkill /F /IM python.exe", 0, True
On Error Goto 0

' python.exe (not pythonw.exe) keeps real stdout/stderr handles, just hidden by
' windowStyle 0 below -- pythonw.exe detaches them entirely, which crashes
' every request because http.server tries to log errors to a null stderr.
' 0 = hidden window, False = don't wait for it to finish
shell.Run "python.exe """ & scriptDir & "\local_server.py"" 8899", 0, False

' Give the server a moment to bind the port before opening the browser
WScript.Sleep 1500

shell.Run "http://localhost:8899/sender.html"
