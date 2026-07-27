@echo off
REM ===================================================================
REM  Virtual Cloud Printer - fix / unjam the print queue
REM  Double-click this file. It asks for Administrator rights, then
REM  restarts the print spooler (clears stuck jobs) and prints the
REM  SYSTEM-side diagnostics (interpreter check + log tail + failed/).
REM ===================================================================
setlocal

REM --- Re-launch elevated if we are not already Administrator ---
net session >nul 2>&1
if %errorlevel% neq 0 goto :elevate

cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" -Action fixqueue
echo.
pause
exit /b

:elevate
echo Requesting Administrator privileges...
set "SELF=%~f0"
powershell -NoProfile -Command "Start-Process -FilePath '%SELF:'=''%' -Verb RunAs"
exit /b
