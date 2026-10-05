@echo off
rem SC Log Tracker - starts the Python version in the background, without a console window.
rem The dashboard opens in your browser. To stop it: Settings > Quit SC Log Tracker.
rem Optional: pass a path, e.g.  start.bat "D:\Games\StarCitizen\LIVE"
rem To see the events printed in a console instead, run:  python sc_log_tracker.py
cd /d "%~dp0"
where pyw >nul 2>nul && (start "" pyw -3 sc_log_tracker.py %* & exit /b 0)
where pythonw >nul 2>nul && (start "" pythonw sc_log_tracker.py %* & exit /b 0)
echo SC Log Tracker needs Python 3.9 or newer: https://www.python.org/downloads/
echo Or download SC-Log-Tracker.exe from the releases page, it doesn't need Python:
echo https://github.com/DefaultHahn/sc-log-tracker/releases/latest
pause
