' Silent launcher for watchdog.ps1.
'
' The scheduled task calls this instead of powershell directly, so nothing
' flashes on screen every 10 minutes. The "0" is the window style (hidden)
' and "False" means do not wait for it to finish.
'
' The path is derived from this file's own location - never hard-coded,
' so the whole local/ folder stays movable (see CLAUDE.md).
Option Explicit
Dim fso, here, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
cmd = "powershell -NoProfile -ExecutionPolicy Bypass -File """ & here & "\watchdog.ps1"""
CreateObject("WScript.Shell").Run cmd, 0, False
