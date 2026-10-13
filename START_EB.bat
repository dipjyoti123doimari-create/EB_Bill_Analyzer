@echo off
title RDC Electricity Intelligence - DO NOT CLOSE THIS WINDOW
color 0B

echo ============================================================
echo   RDC Electricity Intelligence (RDC-EB)
echo   Website:   http://localhost:2002
echo   Scheduler: Oracle scan every 2 hours (runs inside this server)
echo   DO NOT CLOSE THIS WINDOW - closing it stops the scanning
echo ============================================================
echo.

cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

rem Optional settings (uncomment to change):
rem set EB_HOST=0.0.0.0        (reachable from other PCs on the network - put HTTPS in front of it and set EB_HTTPS=1)
rem set EB_PORT=2002
rem set EB_SCHEDULER=0         (the scheduler is run by "python -m eb_app.worker" instead)

".venv\Scripts\python.exe" watchdog.py
pause
