@echo off
REM ===================================================================
REM  LIMS Print Hub - Windows service (Scheduled Task, LocalSystem,
REM  auto-start at boot, auto-restart on crash). Double-click to install;
REM  it self-elevates. The installer builds the venv if run.bat has not.
REM
REM  Manage it (double-click the wrappers, or pass an action here):
REM     install-hub-service.bat            install / refresh (default)
REM     install-hub-service.bat restart    stop the old process, start fresh
REM     install-hub-service.bat stop       stop (also kills anything on the port)
REM     install-hub-service.bat start      start it
REM     install-hub-service.bat status     show state + health
REM     install-hub-service.bat uninstall  remove the service
REM  (--restart / -restart are accepted too.)
REM ===================================================================
setlocal
set "ACT=%~1"
if "%ACT%"=="" set "ACT=install"
if "%ACT:~0,2%"=="--" set "ACT=%ACT:~2%"
if "%ACT:~0,1%"=="-"  set "ACT=%ACT:~1%"

net session >nul 2>&1
if %errorlevel% neq 0 goto :elevate
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-hub-service.ps1" -Action %ACT%
echo.
pause
exit /b

:elevate
echo Requesting Administrator privileges (%ACT%)...
set "SELF=%~f0"
powershell -NoProfile -Command "Start-Process -FilePath '%SELF:'=''%' -ArgumentList '%ACT%' -Verb RunAs"
exit /b
