@echo off
REM ===================================================================
REM  Set / clear the AES-256 PDF encryption passphrase.
REM  Double-click, approve Administrator, and type a passphrase (blank
REM  turns encryption off). Applies to the next print - no restart.
REM ===================================================================
setlocal

net session >nul 2>&1
if %errorlevel% neq 0 goto :elevate

cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" -Action setpassword
echo.
pause
exit /b

:elevate
echo Requesting Administrator privileges...
set "SELF=%~f0"
powershell -NoProfile -Command "Start-Process -FilePath '%SELF:'=''%' -Verb RunAs"
exit /b
