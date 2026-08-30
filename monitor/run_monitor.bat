@echo off
REM Entry point for the Windows Task Scheduler task "CitaMonitor".
REM Task Scheduler does not set a working directory reliably, so cd here first:
REM monitor.py resolves config.json, .env, logs and state relative to its own folder,
REM but python itself must find the sibling modules flow.py / notify.py.

cd /d "%~dp0"
python monitor.py >> "logs\scheduler.out" 2>&1
