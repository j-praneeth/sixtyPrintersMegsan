@echo off
REM LIMS Print Hub - stop the service (self-elevates via install-hub-service.bat).
call "%~dp0install-hub-service.bat" stop
