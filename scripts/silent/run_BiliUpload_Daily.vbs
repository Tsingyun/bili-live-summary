Option Explicit
' Silent launcher for Windows Task Scheduler.
' Starts python.exe with window style 0 (hidden console) and returns its exit code.
' ASCII-only on purpose: auto_upload_daily.py resolves its config from DEFAULT_CONFIG,
' so this file never needs to carry a non-ASCII path (WSH reads .vbs as ANSI by default).
Dim sh, py, script, rc
Set sh = CreateObject("WScript.Shell")
py     = "C:\Users\Tsing\.workbuddy\binaries\python\envs\bili-archive\Scripts\python.exe"
script = "C:\Users\Tsing\.workbuddy\skills\bili-live-summary\scripts\auto_upload_daily.py"
rc = sh.Run("""" & py & """ -u """ & script & """", 0, True)
If rc < 0 Then rc = 1
WScript.Quit rc
