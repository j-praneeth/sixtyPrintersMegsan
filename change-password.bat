@echo off
REM ===================================================================
REM  Change ONE printer's AES-256 PDF encryption password.
REM  Double-click, approve Administrator, verify the super-admin
REM  password, then type the new password (blank turns encryption off
REM  for that printer). Applies to the next print - no restart.
REM ===================================================================
setlocal

net session >nul 2>&1
if %errorlevel% neq 0 goto :elevate

cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" -Action changepassword
echo.
pause
exit /b

:elevate
echo Requesting Administrator privileges...
set "SELF=%~f0"
powershell -NoProfile -Command "Start-Process -FilePath '%SELF:'=''%' -Verb RunAs"
exit /b
