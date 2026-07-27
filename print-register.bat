@echo off
REM ===================================================================
REM  Print & Register (batch)
REM  Double-click to pick one or more files, give each its OWN
REM  registration number, and print them to the virtual printer.
REM  No admin needed. (Requires that install.bat has been run once.)
REM ===================================================================
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0print-register.ps1"
