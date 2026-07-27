@echo off
REM ===================================================================
REM  Virtual Cloud Printer - add another printer
REM  Double-click to open the click-through wizard and create an
REM  additional virtual printer (hub enrollment or its own URL).
REM  (Requires that install.bat has already been run once.)
REM  Console/scripted use: call setup.ps1 -Action add directly.
REM ===================================================================
setlocal

net session >nul 2>&1
if %errorlevel% neq 0 goto :elevate

cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-gui.ps1" -Mode add
exit /b

:elevate
echo Requesting Administrator privileges...
set "SELF=%~f0"
powershell -NoProfile -Command "Start-Process -FilePath '%SELF:'=''%' -Verb RunAs"
exit /b
