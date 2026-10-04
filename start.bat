@echo off
rem SC Log Tracker - start with Python.
rem Optional: pass a path, e.g.  start.bat "D:\Games\StarCitizen\LIVE"
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 sc_log_tracker.py %*
) else (
    python sc_log_tracker.py %*
)
pause
