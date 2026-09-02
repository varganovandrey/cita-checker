@echo off
REM Entry point for the Windows Task Scheduler task "CitaMonitor".
REM
REM cd first: monitor.py resolves config.json, .env, logs and state relative to
REM its own folder, but python must find the sibling modules flow/notify/vpn.
cd /d "%~dp0"

REM pythonw, not python: a console-attached daemon dies with STATUS_CONTROL_C_EXIT
REM the moment its window is closed, which is how the monitor was lost mid-day.
REM Without a console there is no window to close and no Ctrl+C to receive.
start "" /b "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe" monitor.py
exit /b 0
