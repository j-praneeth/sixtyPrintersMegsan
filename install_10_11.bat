@echo off
REM ===================================================================
REM  Virtual Cloud Printer - installer for WINDOWS 10 / 11 clients
REM  Double-click this file. It verifies the OS, asks for Administrator
REM  rights, then opens the click-through wizard (same as install.bat).
REM  On Windows 7 use install_7.bat instead.
REM ===================================================================
setlocal

REM --- OS guard: this file is for Windows 10/11 (NT major 10+) ---
for /f %%v in ('powershell -NoProfile -Command "[Environment]::OSVersion.Version.Major"') do set OSMAJ=%%v
if %OSMAJ% LSS 10 (
    echo.
    echo   This machine is running Windows %OSMAJ%.x ^(Windows 7/8^).
    echo   Please run install_7.bat instead.
    echo.
    pause
    exit /b 1
)

REM --- Re-launch elevated if we are not already Administrator ---
net session >nul 2>&1
if %errorlevel% neq 0 goto :elevate

cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-gui.ps1" -Mode install
exit /b

:elevate
echo Requesting Administrator privileges...
set "SELF=%~f0"
powershell -NoProfile -Command "Start-Process -FilePath '%SELF:'=''%' -Verb RunAs"
exit /b
