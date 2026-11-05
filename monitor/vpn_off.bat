@echo off
REM Stops the Amnezia WireGuard tunnel service.
REM
REM Stopping a service needs elevation, which the monitor (a Task Scheduler
REM job running as the user) does not have. This script is therefore wrapped in
REM a scheduled task registered with /RL HIGHEST, which the monitor can trigger
REM without being elevated itself. Register it once, from an ADMIN terminal:
REM
REM   schtasks /Create /TN "CitaMonitor-VPNOff" /TR "C:\AI_project\cita-checker\monitor\vpn_off.bat" /SC ONCE /ST 00:00 /RL HIGHEST /F
REM
REM Note: once registered, anything running under this account can stop the VPN
REM without a UAC prompt. Remove it with:  schtasks /Delete /TN "CitaMonitor-VPNOff" /F

net stop "AmneziaWGTunnel$AmneziaVPN"
exit /b 0
