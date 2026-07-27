@echo off
REM ===================================================================
REM  Seed DEMO printer_data into the hub's local DB (LOCAL-MODE testing).
REM  Double-click. No enroll key needed - this writes straight to
REM  hub\data\hub.db so the Catalog + print dropdowns have demo rows
REM  without Supabase. Once Supabase is configured, the real sync
REM  replaces these rows. Safe to run while the hub is running.
REM ===================================================================
setlocal
cd /d "%~dp0"

REM Prefer the hub's own venv; fall back to the py launcher, then python.
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY="
if not defined PY (
    where py >nul 2>&1 && set "PY=py"
)
if not defined PY set "PY=python"

"%PY%" "%~dp0seed-demo-data.py"
if errorlevel 1 (
    echo.
    echo Could not seed. Make sure Python is available - running hub\run.bat once
    echo builds the .venv this script prefers. If the DB is locked, try again.
)
echo.
pause
