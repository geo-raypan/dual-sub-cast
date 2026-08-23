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

' Pull the latest code and receiver files before starting, so a fresh
' git push from elsewhere shows up here without a manual "git pull" every
' time. Uses the credentials already cached by git (same ones the Apply
' Style button's automatic push already relies on) -- if that's not set up
' yet, or there's no internet, this silently does nothing and start-up
' continues anyway with whatever is on disk.
On Error Resume Next
shell.Run "cmd /c git -C """ & scriptDir & """ pull", 0, True
shell.Run "cmd /c git -C """ & scriptDir & "\receiver"" pull", 0, True
On Error Goto 0

' python.exe (not pythonw.exe) keeps real stdout/stderr handles, just hidden by
' windowStyle 0 below -- pythonw.exe detaches them entirely, which crashes
' every request because http.server tries to log errors to a null stderr.
' 0 = hidden window, False = don't wait for it to finish
shell.Run "python.exe """ & scriptDir & "\local_server.py"" 8899", 0, False

' Give the server a moment to bind the port before opening the browser
WScript.Sleep 1500

shell.Run "http://localhost:8899/"
