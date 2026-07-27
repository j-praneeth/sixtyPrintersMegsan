@echo off
REM ===================================================================
REM  Clear DEMO printer_data from the hub's local DB (undo seed-demo-data).
REM  Double-click to remove only the demo rows (REG-2026-000x).
REM  To wipe the ENTIRE printer_data table instead:
REM      clear-demo-data.bat all
REM  No enroll key needed - writes straight to hub\data\hub.db.
REM ===================================================================
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY="
if not defined PY (
    where py >nul 2>&1 && set "PY=py"
)
if not defined PY set "PY=python"

"%PY%" "%~dp0clear-demo-data.py" %1
if errorlevel 1 (
    echo.
    echo Could not clear. Make sure Python is available ^(run hub\run.bat once to
    echo build the .venv^). If the DB is locked, try again.
)
echo.
pause
