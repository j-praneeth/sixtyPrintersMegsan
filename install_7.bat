@echo off
REM ===================================================================
REM  Virtual Cloud Printer - installer for WINDOWS 7 SP1 (x64) clients
REM  Double-click this file. It verifies the Windows 7 prerequisites
REM  (.NET Framework 4.7.2+ and PowerShell 5.1), then opens the same
REM  click-through wizard as install.bat. setup.ps1 detects Windows 7
REM  automatically and uses the Python 3.8 embeddable + WMI printer
REM  management (see SETUP.md, "Windows 7 SP1 clients").
REM
REM  TIP: for offline Windows 7 machines, first run on ANY online PC:
REM     powershell -ExecutionPolicy Bypass -File vendor\fetch-win7-bundle.ps1
REM  then copy the whole folder here - no internet needed during install.
REM ===================================================================
setlocal

REM --- OS guard: this file is for Windows 7/8 (NT major < 10) ---
for /f %%v in ('powershell -NoProfile -Command "[Environment]::OSVersion.Version.Major"') do set OSMAJ=%%v
if %OSMAJ% GEQ 10 (
    echo.
    echo   This machine is running Windows 10/11.
    echo   Please run install_10_11.bat instead.
    echo.
    pause
    exit /b 1
)

REM --- Prereq 1: .NET Framework 4.7.2+ (Release value 461808+) ---
REM     Needed by the super-admin password hash in setup.ps1.
set REL=0
set RELD=0
for /f "tokens=3" %%r in ('reg query "HKLM\SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full" /v Release 2^>nul ^| find "Release"') do set REL=%%r
set /a RELD=%REL% 2>nul
echo   Detected: .NET Framework release ID = %RELD%  ^(need 461808+ for 4.7.2^)
if %RELD% LSS 461808 (
    echo.
    echo   MISSING PREREQUISITE: .NET Framework 4.7.2 or newer ^(detected release ID %RELD%^).
    echo   If %RELD% is 0: no .NET 4.5+ is installed at all, or this reg key is
    echo   missing/blocked - re-check as Administrator if you believe 4.8 is installed.
    echo   Install .NET Framework 4.8 first ^(then WMF 5.1 if PowerShell is old^):
    echo     https://dotnet.microsoft.com/download/dotnet-framework/net48
    echo   Reboot after installing, then run this file again.
    echo.
    pause
    exit /b 1
)

REM --- Prereq 2: Windows PowerShell 5.1 (WMF 5.1) ---
set PSMAJ=0
for /f %%v in ('powershell -NoProfile -Command "$PSVersionTable.PSVersion.Major" 2^>nul') do set PSMAJ=%%v
echo   Detected: PowerShell major version = %PSMAJ%  ^(need 5+^)
if %PSMAJ% LSS 5 (
    echo.
    echo   MISSING PREREQUISITE: Windows PowerShell 5.1 ^(detected version %PSMAJ%^).
    echo   Install WMF 5.1 - Win7AndW2K8R2-KB3191566-x64:
    echo     https://www.microsoft.com/download/details.aspx?id=54616
    echo   ^(.NET 4.8 must already be installed FIRST - see above - then WMF 5.1, then reboot.^)
    echo   Then run this file again.
    echo.
    pause
    exit /b 1
)

echo   Prerequisites OK ^(.NET Release %RELD%, PowerShell %PSMAJ%^).

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
