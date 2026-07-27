@echo off
REM ===================================================================
REM  View ONE printer's PDF encryption password (decrypted).
REM  Double-click, approve Administrator and verify the super-admin
REM  password. The password is shown on screen ONLY - never written
REM  to any file or log. Clear the console afterwards.
REM ===================================================================
setlocal

net session >nul 2>&1
if %errorlevel% neq 0 goto :elevate

cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" -Action viewpassword
echo.
pause
exit /b

:elevate
echo Requesting Administrator privileges...
set "SELF=%~f0"
powershell -NoProfile -Command "Start-Process -FilePath '%SELF:'=''%' -Verb RunAs"
exit /b
