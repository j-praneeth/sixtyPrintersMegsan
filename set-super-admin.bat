@echo off
REM ===================================================================
REM  Rotate the super-admin password (guards change/view-password).
REM  Double-click, approve Administrator, verify the CURRENT super-
REM  admin password, then type the new one twice. Only a PBKDF2 hash
REM  is stored - rotate the factory default right after install.
REM ===================================================================
setlocal

net session >nul 2>&1
if %errorlevel% neq 0 goto :elevate

cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" -Action setsuperadmin
echo.
pause
exit /b

:elevate
echo Requesting Administrator privileges...
set "SELF=%~f0"
powershell -NoProfile -Command "Start-Process -FilePath '%SELF:'=''%' -Verb RunAs"
exit /b
