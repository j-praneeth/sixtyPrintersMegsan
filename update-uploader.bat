@echo off
REM ===================================================================
REM  Virtual Cloud Printer - update ONLY the uploader (upload.py)
REM  Double-click this file. It asks for Administrator rights (needed
REM  because the install folder is locked to SYSTEM/Admins), copies the
REM  latest upload.py into %ProgramData%\VirtualCloudPrinter, and exits.
REM  No spooler restart is needed - each print launches upload.py fresh,
REM  so the very next "Attach & print" uses the new one (with the
REM  "Printed By" e-sign footer).
REM ===================================================================
setlocal

REM --- Re-launch elevated if we are not already Administrator ---
net session >nul 2>&1
if %errorlevel% neq 0 goto :elevate

set "DEST=%ProgramData%\VirtualCloudPrinter"
if not exist "%DEST%\" (
    echo ERROR: %DEST% not found - is the printer installed on this machine?
    echo.
    pause
    exit /b 1
)

echo Copying upload.py to "%DEST%" ...
copy /Y "%~dp0upload.py" "%DEST%\upload.py" >nul
if %errorlevel% neq 0 (
    echo ERROR: copy failed.
    echo.
    pause
    exit /b 1
)
echo Done. The next print will include the "Printed By" e-sign footer.
echo.
pause
exit /b

:elevate
echo Requesting Administrator privileges...
set "SELF=%~f0"
powershell -NoProfile -Command "Start-Process -FilePath '%SELF:'=''%' -Verb RunAs"
exit /b
