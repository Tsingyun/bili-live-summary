Option Explicit
' Silent launcher for Windows Task Scheduler (manual / on-demand entry).
' Reads workdir/logs/pending_run.json and runs the requested script there.
' ASCII-only on purpose so the VBS never mojibakes in the task log.
Dim sh, py, script, rc
Set sh = CreateObject("WScript.Shell")
py     = "C:\Users\Tsing\.workbuddy\binaries\python\envs\bili-archive\Scripts\python.exe"
script = "C:\Users\Tsing\.workbuddy\skills\bili-live-summary\scripts\run_pending.py"
rc = sh.Run("""" & py & """ -u """ & script & """", 0, True)
If rc < 0 Then rc = 1
WScript.Quit rc
