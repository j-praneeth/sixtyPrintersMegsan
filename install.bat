@echo off
REM ===================================================================
REM  Virtual Cloud Printer - one-click installer
REM  Double-click this file. It asks for Administrator rights, then
REM  opens a click-through wizard (paste works in every box) that
REM  installs everything and creates your first virtual printer.
REM  Console/scripted install: call setup.ps1 -Action install directly.
REM ===================================================================
setlocal

REM --- Re-launch elevated if we are not already Administrator ---
net session >nul 2>&1
if %errorlevel% neq 0 goto :elevate

cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-gui.ps1" -Mode install
exit /b

:elevate
echo Requesting Administrator privileges...
REM Double any single quotes in our own path so the PowerShell single-quoted
REM string stays valid even for paths like C:\Users\O'Brien\...
set "SELF=%~f0"
powershell -NoProfile -Command "Start-Process -FilePath '%SELF:'=''%' -Verb RunAs"
exit /b
