@echo off
REM Starts Brave (or Chrome as fallback) with a remote debugging port and a
REM dedicated profile, so monitor.py can attach to it over CDP.
REM The monitor can launch the browser itself (browser.auto_launch); this is the manual way.
REM The dedicated profile keeps your everyday Brave session untouched.

set PROFILE=%~dp0chrome-profile
set PORT=9222

set BROWSER="C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
if not exist %BROWSER% set BROWSER="C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe"
if not exist %BROWSER% set BROWSER="%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"
if not exist %BROWSER% set BROWSER="C:\Program Files\Google\Chrome\Application\chrome.exe"
if not exist %BROWSER% (
    echo [ERROR] brave.exe / chrome.exe not found. Set browser.browser_path in config.json.
    exit /b 1
)

echo [INFO] Launching %BROWSER% on port %PORT% with profile %PROFILE%
start "" %BROWSER% --remote-debugging-port=%PORT% --user-data-dir="%PROFILE%" --no-first-run --no-default-browser-check
